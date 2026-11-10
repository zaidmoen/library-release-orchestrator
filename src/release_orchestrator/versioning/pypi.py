"""PEP 503 package index adapter (pypiserver, devpi, Artifactory/Nexus PyPI repos) — ADR-0012.

Artifacts are **reproducible wheels**: built from ``git archive <release commit>`` with
``SOURCE_DATE_EPOCH`` = commit time, so the same commit always yields the same sha256 and
"same version, same digest" is a meaningful idempotency test.

Publication protocol (never overwrites, never assumes):

1. Read the index. Version present with the same sha256 → ``already_published``; with a
   different sha256 → ``ImmutableVersionError`` (CONFLICT).
2. Upload via the legacy upload API (``:action=file_upload``) with credentials from
   environment variables named in config.
3. Whatever the upload response (200, 409, 5xx, timeout, connection lost), the outcome
   is decided by **re-reading the index**: present with our digest → success;
   different digest → conflict; absent → retry (bounded) or fail.
4. 401/403 → ``RegistryAuthError``, never retried.
"""

from __future__ import annotations

import hashlib
import html.parser
import io
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from release_orchestrator.errors import (
    BlockedError,
    ConfigError,
    ImmutableVersionError,
    IntegrationError,
)
from release_orchestrator.fsutil import temp_dir
from release_orchestrator.gitutil.repo import Git
from release_orchestrator.versioning.registry import PublishedArtifact
from release_orchestrator.versioning.semver import SemVer

# (method, url, headers, body) -> (status, body bytes). Raises OSError/URLError when no
# response arrived (the request may or may not have been applied).
HttpTransport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class RegistryAuthError(IntegrationError):
    code = "REGISTRY_AUTH_FAILED"


def urllib_http(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def to_pep440(version: SemVer) -> str:
    core = f"{version.major}.{version.minor}.{version.patch}"
    if not version.prerelease:
        return core
    pre = version.prerelease
    if len(pre) == 2 and pre[0] in ("rc", "a", "b") and pre[1].isdigit():
        return f"{core}{pre[0]}{pre[1]}"
    raise ConfigError(f"{version} has no PEP 440 equivalent (only -rc.N/-a.N/-b.N)")


def from_pep440(text: str) -> SemVer | None:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:(rc|a|b)(\d+))?", text)
    if not m:
        return None
    major, minor, patch, label, num = m.groups()
    pre = (label, num) if label else ()
    return SemVer(int(major), int(minor), int(patch), pre)


class _Links(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


class PypiRegistry:
    artifact_kind = "wheel"

    def __init__(
        self,
        url: str,
        *,
        username_env: str,
        password_env: str,
        http: HttpTransport = urllib_http,
        attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.url = url.rstrip("/")
        self._username_env = username_env
        self._password_env = password_env
        self._http = http
        self._attempts = attempts
        self._sleep = sleep

    # ------------------------------------------------------------------ reads
    def _files(self, name: str) -> dict[SemVer, tuple[str, str, str]]:
        """``{version: (filename, sha256, absolute_url)}`` from the simple index."""
        index_url = f"{self.url}/simple/{normalize(name)}/"
        status, body = self._read(index_url)
        if status == 404:
            return {}
        if status != 200:
            raise IntegrationError(f"Index read failed: {status}", details={"url": index_url})
        parser = _Links()
        parser.feed(body.decode("utf-8", errors="replace"))
        prefix = re.sub(r"[-_.]+", "_", name).lower() + "-"
        files: dict[SemVer, tuple[str, str, str]] = {}
        for href in parser.links:
            target, _, fragment = href.partition("#")
            filename = urllib.parse.unquote(target.rsplit("/", 1)[-1])
            if not filename.lower().startswith(prefix) or not filename.endswith(".whl"):
                continue
            version = from_pep440(filename[len(prefix) :].split("-", 1)[0])
            digest = fragment[len("sha256=") :] if fragment.startswith("sha256=") else ""
            if version is not None:
                files[version] = (filename, digest, urllib.parse.urljoin(index_url, target))
        return files

    def _read(self, url: str) -> tuple[int, bytes]:
        last: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                status, body = self._http("GET", url, self._auth_headers(required=False), None)
                if status < 500:
                    return status, body
                last = IntegrationError(f"registry returned {status}")
            except OSError as exc:
                last = exc
            if attempt < self._attempts:
                self._sleep(2.0 ** (attempt - 1))
        raise IntegrationError(
            f"Registry unreachable: {last}", details={"url": url, "retryable": True}
        )

    def list_versions(self, name: str) -> list[SemVer]:
        return sorted(self._files(name))

    def get(self, name: str, version: SemVer) -> PublishedArtifact | None:
        entry = self._files(name).get(version)
        if entry is None:
            return None
        filename, digest, url = entry
        return PublishedArtifact(
            name, str(version), digest, {"filename": filename, "url": url, "registry": self.url}
        )

    def fetch(self, name: str, version: SemVer) -> bytes:
        artifact = self.get(name, version)
        if artifact is None:
            raise IntegrationError(f"{name} {version} is not in the index")
        status, body = self._read(str(artifact.metadata["url"]))
        if status != 200:
            raise IntegrationError(f"Download failed: {status}")
        if not artifact.sha256 or hashlib.sha256(body).hexdigest() != artifact.sha256:
            raise ImmutableVersionError(
                f"Digest mismatch downloading {name} {version}: artifact cannot be trusted"
            )
        return body

    # ------------------------------------------------------------------ publish
    def publish(
        self, name: str, version: SemVer, payload: bytes, metadata: dict[str, Any]
    ) -> tuple[PublishedArtifact, bool]:
        digest = hashlib.sha256(payload).hexdigest()
        filename = str(metadata.get("filename", ""))
        if not filename.endswith(".whl"):
            raise ConfigError("PyPI publication needs the wheel filename in metadata['filename']")
        existing = self.get(name, version)
        if existing is not None:
            return self._settle(existing, digest), False

        for attempt in range(1, self._attempts + 1):
            outcome = "no-response"
            try:
                status, _body = self._upload(name, version, filename, payload, digest)
                outcome = str(status)
                if status in (401, 403):
                    raise RegistryAuthError(
                        f"Registry rejected credentials ({status}); nothing was published",
                        details={"username_env": self._username_env},
                    )
            except OSError:
                pass  # response lost: the upload may or may not have been applied
            # Authoritative answer: the index, never the upload response.
            current = self.get(name, version)
            if current is not None:
                settled = self._settle(current, digest)
                if outcome in ("200", "201"):
                    return settled, True
                # We can't know whose upload landed, only that the content is ours.
                return PublishedArtifact(
                    settled.name,
                    settled.version,
                    settled.sha256,
                    {**settled.metadata, "reconciled_after": outcome},
                ), True
            if attempt < self._attempts:
                self._sleep(2.0 ** (attempt - 1))
        raise IntegrationError(
            f"{name} {version} is still absent from the index after {self._attempts} uploads",
            details={"retryable": True, "last_outcome": outcome},
        )

    @staticmethod
    def _settle(artifact: PublishedArtifact, digest: str) -> PublishedArtifact:
        if artifact.sha256 != digest:
            raise ImmutableVersionError(
                f"{artifact.name} {artifact.version} already exists with different content",
                details={"existing_sha256": artifact.sha256, "attempted_sha256": digest},
            )
        return artifact

    def _auth_headers(self, *, required: bool) -> dict[str, str]:
        user = os.environ.get(self._username_env, "")
        password = os.environ.get(self._password_env, "")
        if not user or not password:
            if required:
                raise RegistryAuthError(
                    f"Registry credentials missing: set {self._username_env} and "
                    f"{self._password_env}; nothing was published"
                )
            return {}
        import base64

        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def _upload(
        self, name: str, version: SemVer, filename: str, payload: bytes, digest: str
    ) -> tuple[int, bytes]:
        boundary = uuid.uuid4().hex
        fields = {
            ":action": "file_upload",
            "protocol_version": "1",
            "name": name,
            "version": to_pep440(version),
            "filetype": "bdist_wheel",
            "pyversion": "py3",
            "sha256_digest": digest,
        }
        out = io.BytesIO()
        for key, value in fields.items():
            out.write(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n".encode()
            )
        out.write(
            f'--{boundary}\r\nContent-Disposition: form-data; name="content"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
        )
        out.write(payload)
        out.write(f"\r\n--{boundary}--\r\n".encode())
        headers = {
            **self._auth_headers(required=True),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        return self._http("POST", f"{self.url}/", headers, out.getvalue())


# --------------------------------------------------------------------------- artifacts
def build_wheel(git: Git, rev: str, *, expected_version: SemVer) -> tuple[str, bytes]:
    """Reproducible wheel of the committed tree at ``rev`` (no network, no untracked files)."""
    commit_time = git.out("log", "-1", "--format=%ct", rev)
    with temp_dir("lro-wheel-") as tmp:
        src, out = tmp / "src", tmp / "out"
        git.archive_to(rev, src)
        env = {**os.environ, "SOURCE_DATE_EPOCH": commit_time, "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--no-index",
                "-q",
                "-w",
                str(out),
                str(src),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=600,
        )
        if proc.returncode != 0:
            raise BlockedError(
                "Wheel build failed",
                reason="ARTIFACT_BUILD_FAILED",
                details={"output_tail": (proc.stdout + proc.stderr)[-2000:]},
            )
        wheels = sorted(out.glob("*.whl"))
        if len(wheels) != 1:
            raise BlockedError(
                f"Expected one wheel, got {[w.name for w in wheels]}",
                reason="ARTIFACT_BUILD_FAILED",
            )
        wheel = wheels[0]
        built_version = wheel.name.split("-")[1]
        if built_version != to_pep440(expected_version):
            raise BlockedError(
                f"Wheel version {built_version} does not match release {expected_version}",
                reason="ARTIFACT_VERSION_MISMATCH",
            )
        return wheel.name, wheel.read_bytes()


def extract_artifact(kind: str, payload: bytes, dest: Path) -> None:
    """Unpack so the importable package root is ``<dest>/src`` for either artifact kind."""
    if kind == "wheel":
        with zipfile.ZipFile(io.BytesIO(payload)) as wheel:
            for member in wheel.namelist():
                if member.startswith("/") or ".." in Path(member).parts:
                    raise ImmutableVersionError(f"Unsafe path in wheel: {member}")
            wheel.extractall(dest / "src")  # noqa: S202 - members validated above
    else:
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
            tar.extractall(dest, filter="data")
