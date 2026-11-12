"""PEP 503 registry against a real, isolated pypiserver (ADR-0012).

Covers the brief's cases: A absent, B same digest, C conflicting digest, D lost response,
E ambiguous response, F authentication failure, G concurrent publication, plus an
unreachable registry and a full lab release through the index.
"""

from __future__ import annotations

import hashlib
import io
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from release_orchestrator.errors import ImmutableVersionError, IntegrationError
from release_orchestrator.versioning.pypi import (
    PypiRegistry,
    RegistryAuthError,
    from_pep440,
    to_pep440,
    urllib_http,
)
from release_orchestrator.versioning.semver import SemVer

pytest.importorskip("pypiserver")
pytest.importorskip("passlib")
pytestmark = pytest.mark.integration

V = SemVer.parse("1.1.0")
USER, PASSWORD = "release-bot", "test-only-password"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    from passlib.apache import HtpasswdFile

    packages, htpasswd = tmp_path / "packages", tmp_path / "htpasswd"
    packages.mkdir()
    ht = HtpasswdFile(str(htpasswd), new=True)
    ht.set_password(USER, PASSWORD)
    ht.save()
    port = _free_port()
    proc = subprocess.Popen(  # no --overwrite: the server itself refuses re-uploads
        [
            sys.executable,
            "-m",
            "pypiserver",
            "run",
            "-i",
            "127.0.0.1",
            "-p",
            str(port),
            "-P",
            str(htpasswd),
            "-a",
            "update",
            "--hash-algo",
            "sha256",
            str(packages),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"{url}/simple/", timeout=1)  # noqa: S310
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("pypiserver did not start")
        monkeypatch.setenv("LRO_TEST_REGISTRY_USER", USER)
        monkeypatch.setenv("LRO_TEST_REGISTRY_PASSWORD", PASSWORD)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _registry(url: str, **kw) -> PypiRegistry:  # type: ignore[no-untyped-def]
    return PypiRegistry(
        url,
        username_env="LRO_TEST_REGISTRY_USER",
        password_env="LRO_TEST_REGISTRY_PASSWORD",
        sleep=lambda _s: None,
        **kw,
    )


def _wheel(marker: str, version: str = "1.1.0") -> tuple[str, bytes]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("zaidlibrary/__init__.py", date_time=(2026, 1, 1, 0, 0, 0))
        zf.writestr(info, f'__version__ = "{version}"  # {marker}\n')
    return f"zaidlibrary-{version}-py3-none-any.whl", buf.getvalue()


def _publish(reg: PypiRegistry, marker: str, version: SemVer = V):  # type: ignore[no-untyped-def]
    filename, payload = _wheel(marker, to_pep440(version))
    return reg.publish("zaidlibrary", version, payload, {"filename": filename}), payload


def test_a_absent_version_is_published_and_read_back(index):
    reg = _registry(index)
    (artifact, created), payload = _publish(reg, "one")
    assert created and artifact.sha256 == hashlib.sha256(payload).hexdigest()
    assert reg.list_versions("zaidlibrary") == [V]
    assert reg.fetch("zaidlibrary", V) == payload  # read-after-write, digest-verified


def test_b_same_digest_is_idempotent(index):
    reg = _registry(index)
    _publish(reg, "one")
    (_artifact, created), _ = _publish(reg, "one")
    assert created is False and reg.list_versions("zaidlibrary") == [V]


def test_c_conflicting_digest_is_refused_by_adapter_and_server(index):
    reg = _registry(index)
    (original, _), _ = _publish(reg, "one")
    with pytest.raises(ImmutableVersionError) as exc:
        _publish(reg, "two")
    assert exc.value.details["existing_sha256"] == original.sha256
    # Bypass the adapter's pre-check: the server itself must refuse to overwrite.
    filename, payload = _wheel("two")
    status, _ = reg._upload(
        "zaidlibrary", V, filename, payload, hashlib.sha256(payload).hexdigest()
    )
    assert status == 409
    assert reg.get("zaidlibrary", V).sha256 == original.sha256


def test_d_lost_response_is_reconciled_without_duplicate_upload(index):
    uploads: list[str] = []

    def lossy(method, url, headers, body):  # type: ignore[no-untyped-def]
        status, data = urllib_http(method, url, headers, body)
        if method == "POST":
            uploads.append(str(status))
            raise ConnectionResetError("connection reset after the server stored the file")
        return status, data

    reg = _registry(index, http=lossy)
    (artifact, created), payload = _publish(reg, "one")
    assert created is True and uploads == ["200"]  # exactly one upload, then reconciled
    assert artifact.metadata["reconciled_after"] == "no-response"
    assert _registry(index).fetch("zaidlibrary", V) == payload


def test_e_ambiguous_server_error_is_resolved_by_reading_the_index(index):
    def flaky(method, url, headers, body):  # type: ignore[no-untyped-def]
        status, data = urllib_http(method, url, headers, body)
        return (502, b"bad gateway") if method == "POST" else (status, data)

    (artifact, created), _ = _publish(_registry(index, http=flaky), "one")
    assert created and artifact.metadata["reconciled_after"] == "502"


def test_f_authentication_failure_fails_safely(index, monkeypatch):
    monkeypatch.setenv("LRO_TEST_REGISTRY_PASSWORD", "wrong")
    with pytest.raises(RegistryAuthError) as exc:
        _publish(_registry(index), "one")
    assert "wrong" not in str(exc.value.to_dict())
    assert _registry(index).list_versions("zaidlibrary") == []
    monkeypatch.delenv("LRO_TEST_REGISTRY_USER")
    with pytest.raises(RegistryAuthError):
        _publish(_registry(index), "one")


def test_g_concurrent_publication_has_one_winner(index):
    results: list[str] = []
    barrier = threading.Barrier(6)

    def attempt(marker: str) -> None:
        barrier.wait()
        try:
            _publish(_registry(index), marker)
            results.append(f"ok:{marker}")
        except ImmutableVersionError:
            results.append(f"conflict:{marker}")

    threads = [threading.Thread(target=attempt, args=(m,)) for m in ("x", "x", "y", "y", "z", "z")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = {r.split(":")[1] for r in results if r.startswith("ok")}
    assert len(winners) == 1  # one content won; its twin succeeded idempotently
    assert sum(r.startswith("ok") for r in results) == 2
    assert sum(r.startswith("conflict") for r in results) == 4
    published = _registry(index).fetch("zaidlibrary", V)
    assert published == _wheel(winners.pop())[1]


def test_unreachable_registry_is_retryable_not_success():
    with pytest.raises(IntegrationError) as exc:
        _registry(f"http://127.0.0.1:{_free_port()}").list_versions("zaidlibrary")
    assert exc.value.details["retryable"] is True


def test_release_candidate_versions_map_to_pep440(index):
    rc = SemVer.parse("1.1.0-rc.24")
    assert to_pep440(rc) == "1.1.0rc24" and from_pep440("1.1.0rc24") == rc
    reg = _registry(index)
    _publish(reg, "rc", rc)
    _publish(reg, "final", V)
    assert reg.list_versions("zaidlibrary") == [rc, V]


def test_full_lab_release_through_the_index(index, tmp_path):
    from release_orchestrator.lab import build_lab
    from tests.lab.test_lab_scenarios import Lab

    root = tmp_path / "lab"
    config = build_lab(root)
    config.write_text(
        config.read_text().replace(
            "registry:\n  provider: local\n  path: ./registry\n",
            f"registry:\n  provider: pypi\n  production: false\n  url: {index}\n"
            "  username_env: LRO_TEST_REGISTRY_USER\n  password_env: LRO_TEST_REGISTRY_PASSWORD\n",
        )
    )
    lab = Lab(root, config)
    plan = lab.plan("FIN-204")
    lab.release(plan)  # prepare, validate, pr, publish (reproducible wheel)
    assert lab.orch.publish(plan, approve=True)["status"] == "already_published"
    art = lab.orch.s.registry.get("zaidlibrary", V)
    assert art.metadata["filename"] == "zaidlibrary-1.1.0-py3-none-any.whl"
    summary = lab.orch.update_consumers(plan, approve=True, dry_run=False)
    [update] = summary["updated"]
    assert update["consumer"] == "payment-service" and update["validation"]["status"] == "PASSED"


def test_index_links_to_a_foreign_host_are_refused():
    page = (
        b'<a href="https://evil.example/zaidlibrary-1.1.0-py3-none-any.whl#sha256=00">'
        b"zaidlibrary-1.1.0-py3-none-any.whl</a>"
    )

    def http(method, url, headers, body):  # type: ignore[no-untyped-def]
        return 200, page

    with pytest.raises(IntegrationError) as exc:
        PypiRegistry(
            "https://pkgs.internal.example", username_env="U", password_env="P", http=http
        ).list_versions("zaidlibrary")
    assert "foreign host" in exc.value.message
