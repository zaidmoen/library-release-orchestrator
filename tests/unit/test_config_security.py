"""Configuration is trusted input, so it must be strictly validated (security review)."""

from __future__ import annotations

from pathlib import Path

import pytest

from release_orchestrator.config import load_config, validate_registry_url
from release_orchestrator.errors import ConfigError

BASE = """schema_version: 1
library: {name: lib, repo: ., package_dir: src/lib}
validation:
  library:
    - {name: t, argv: ["{python}", "-V"]}
"""


def _load(tmp_path: Path, extra: str):  # type: ignore[no-untyped-def]
    path = tmp_path / "c.yaml"
    path.write_text(BASE + extra, encoding="utf-8")
    return load_config(path)


def _pypi(url: str, production: str = "false", extra: str = "") -> str:
    return (
        f"registry:\n  provider: pypi\n  url: {url}\n  production: {production}\n"
        f"  username_env: REG_USER\n  password_env: REG_PASS\n{extra}"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost.evil.example/simple",  # prefix trick
        "http://127.0.0.1.evil.example",  # prefix trick
        "http://pkgs.internal.example",  # plain http to a remote host
        "https://user:secret@pkgs.example",  # embedded credentials
        "https://token@pkgs.example",  # embedded user/token
        "ftp://pkgs.example",  # wrong scheme
        "https:///no-host",  # malformed
        "https://pkgs.example:99999",  # malformed port
        "https://pkgs.example/simple?token=x",  # query
        "pkgs.example",  # not absolute
    ],
)
def test_unsafe_registry_urls_are_rejected(url):
    with pytest.raises(ConfigError):
        validate_registry_url(url)


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("http://127.0.0.1:8080", "127.0.0.1"),
        ("http://localhost:3141/root", "localhost"),
        ("http://[::1]:8080", "::1"),
        ("https://pkgs.example/simple", "pkgs.example"),
    ],
)
def test_safe_registry_urls_are_accepted(url, host):
    assert validate_registry_url(url) == host


def test_remote_index_must_be_explicitly_classified(tmp_path):
    no_flag = (
        "registry:\n  provider: pypi\n  url: https://pkgs.example\n"
        "  username_env: REG_USER\n  password_env: REG_PASS\n"
    )
    with pytest.raises(ConfigError, match="explicitly"):
        _load(tmp_path, no_flag)
    with pytest.raises(ConfigError, match="nonproduction_hosts"):
        _load(tmp_path, _pypi("https://pkgs.example"))  # remote host labelled non-prod
    ok = _load(
        tmp_path,
        _pypi(
            "https://staging-pkgs.example", extra="  nonproduction_hosts: [staging-pkgs.example]\n"
        ),
    )
    assert ok.registry_production is False
    assert _load(tmp_path, _pypi("https://pkgs.example", "true")).registry_production is True
    assert _load(tmp_path, _pypi("http://127.0.0.1:9")).registry_production is False


def test_credentials_must_be_env_var_names_not_values(tmp_path):
    bad = _pypi("http://127.0.0.1:9").replace("REG_PASS", "hunter2-actual-password!")
    with pytest.raises(ConfigError, match="NAME"):
        _load(tmp_path, bad)


@pytest.mark.parametrize("value", ["true", "false", "0", "11", "'2'"])
def test_required_approvals_rejects_bools_and_out_of_range(tmp_path, value):
    with pytest.raises(ConfigError):
        _load(
            tmp_path, f"registry: {{path: ./r}}\napproval_policy:\n  required_approvals: {value}\n"
        )


def test_policy_rejects_duplicate_checks_unknown_keys_and_bad_permission(tmp_path):
    reg = "registry: {path: ./r}\n"
    with pytest.raises(ConfigError, match="duplicates"):
        _load(tmp_path, reg + "approval_policy:\n  required_checks: [ci, ci]\n")
    with pytest.raises(ConfigError, match="unknown keys"):
        _load(tmp_path, reg + "approval_policy:\n  required_aprovals: 2\n")
    with pytest.raises(ConfigError):
        _load(tmp_path, reg + "approval_policy:\n  min_reviewer_permission: superuser\n")
    policy = _load(tmp_path, reg + "approval_policy:\n  required_approvals: 2\n").approval_policy
    assert policy.required_approvals == 2 and policy.min_reviewer_permission == "write"


@pytest.mark.parametrize(
    "snippet",
    [
        "environments:\n  - {name: prep, required_approvals: true}\n",
        "issue_tracker:\n  max_age_minutes: true\n",
    ],
)
def test_bools_are_never_numbers(tmp_path, snippet):
    with pytest.raises(ConfigError):
        _load(tmp_path, "registry: {path: ./r}\n" + snippet)


def test_unknown_registry_provider_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        _load(tmp_path, "registry: {provider: s3, path: ./r}\n")
