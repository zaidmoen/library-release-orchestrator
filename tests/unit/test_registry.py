import threading

import pytest

from release_orchestrator.errors import ImmutableVersionError
from release_orchestrator.versioning.registry import LocalRegistry
from release_orchestrator.versioning.semver import SemVer

V = SemVer.parse("1.1.0")


def test_publish_and_fetch(tmp_path):
    registry = LocalRegistry(tmp_path)
    artifact, created = registry.publish("zaidlibrary", V, b"payload", {"plan_id": "x"})
    assert created and registry.list_versions("zaidlibrary") == [V]
    assert registry.fetch("zaidlibrary", V) == b"payload"
    assert registry.get("zaidlibrary", V).sha256 == artifact.sha256


def test_registry_same_digest_is_idempotent(tmp_path):
    registry = LocalRegistry(tmp_path)
    registry.publish("zaidlibrary", V, b"payload", {})
    _, created = registry.publish("zaidlibrary", V, b"payload", {})
    assert created is False


def test_registry_refuses_overwrite(tmp_path):
    registry = LocalRegistry(tmp_path)
    registry.publish("zaidlibrary", V, b"payload", {})
    with pytest.raises(ImmutableVersionError):
        registry.publish("zaidlibrary", V, b"different", {})
    assert registry.fetch("zaidlibrary", V) == b"payload"


def test_registry_detects_tampered_artifact(tmp_path):
    registry = LocalRegistry(tmp_path)
    registry.publish("zaidlibrary", V, b"payload", {})
    (tmp_path / "zaidlibrary" / "1.1.0.tar").write_bytes(b"evil")
    with pytest.raises(ImmutableVersionError):
        registry.fetch("zaidlibrary", V)


def test_concurrent_publish_single_winner(tmp_path):
    registry = LocalRegistry(tmp_path)
    outcomes: list[str] = []
    barrier = threading.Barrier(8)

    def attempt(i: int) -> None:
        barrier.wait()
        try:
            _, created = registry.publish("zaidlibrary", V, f"build-{i}".encode(), {})
            outcomes.append("created" if created else "same")
        except ImmutableVersionError:
            outcomes.append("rejected")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("created") == 1
    assert outcomes.count("rejected") == 7
