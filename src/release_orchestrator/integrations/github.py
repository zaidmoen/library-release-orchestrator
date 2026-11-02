"""GitHub REST adapter for draft pull requests.

Status: implemented and unit-tested against a fake transport; **not verified against
live GitHub**. It never pushes (the orchestrator's Git wrapper forbids push) and has no
merge method. If the head branch is not on the remote yet, it stops with an actionable
``BRANCH_NOT_PUSHED`` blocker that tells the human the exact push command.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from release_orchestrator.errors import BlockedError, ConfigError, IntegrationError
from release_orchestrator.integrations.pull_requests import PullRequestRef, PullRequestSpec

# (method, url, headers, json_body) -> (status, parsed_json)
Transport = Callable[[str, str, dict[str, str], dict[str, Any] | None], tuple[int, Any]]


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None
) -> tuple[int, Any]:
    if not url.startswith("https://"):
        raise IntegrationError("GitHub API URL must use https")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = response.read().decode() or "null"
            return response.status, json.loads(payload)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode(errors="replace") or "null"
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, {"message": payload[:300]}
    except urllib.error.URLError as exc:
        raise IntegrationError(f"GitHub request failed: {exc.reason}") from None


class GitHubPullRequestProvider:
    name = "github"

    def __init__(
        self,
        repositories: dict[str, str],
        *,
        api_url: str = "https://api.github.com",
        token: str | None = None,
        transport: Transport = urllib_transport,
    ) -> None:
        self.repositories = repositories
        self.api_url = api_url.rstrip("/")
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self._transport = transport
        if not self._token:
            raise ConfigError("GITHUB_TOKEN is not set; the GitHub PR provider needs it")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "library-release-orchestrator",
        }

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        status, data = self._transport(method, f"{self.api_url}{path}", self._headers(), body)
        if status >= 500 or status in (401, 403):
            message = data.get("message", "") if isinstance(data, dict) else ""
            # Never echo headers (they contain the token).
            raise IntegrationError(f"GitHub {method} {path} failed with {status}: {message}")
        return status, data

    def ensure(self, spec: PullRequestSpec) -> PullRequestRef:
        full = self.repositories.get(spec.repository)
        if not full or "/" not in full:
            raise ConfigError(f"No GitHub repository mapped for '{spec.repository}'")
        owner = full.split("/", 1)[0]
        quoted = urllib.parse.quote(spec.head, safe="")
        status, _ = self._call("GET", f"/repos/{full}/branches/{quoted}")
        if status == 404:
            raise BlockedError(
                f"Branch '{spec.head}' is not on the remote yet",
                reason="BRANCH_NOT_PUSHED",
                details={"push_command": f"git push origin refs/heads/{spec.head}",
                         "note": "Push is a human step; the orchestrator never pushes."},
            )
        query = urllib.parse.urlencode({"head": f"{owner}:{spec.head}", "state": "open"})
        status, existing = self._call("GET", f"/repos/{full}/pulls?{query}")
        if status == 200 and isinstance(existing, list) and existing:
            pr = existing[0]
            return PullRequestRef(spec.repository, str(pr["number"]), pr["html_url"], spec.head,
                                  pr["base"]["ref"], pr["state"], created=False)
        status, pr = self._call("POST", f"/repos/{full}/pulls", {
            "title": spec.title, "head": spec.head, "base": spec.base, "body": spec.body,
            "draft": True,
        })
        if status != 201:
            message = pr.get("message", "") if isinstance(pr, dict) else ""
            raise IntegrationError(f"Creating PR failed with {status}: {message}")
        return PullRequestRef(spec.repository, str(pr["number"]), pr["html_url"], spec.head,
                              spec.base, pr["state"], created=True)
