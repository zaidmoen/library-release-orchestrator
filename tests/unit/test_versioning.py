import pytest

from release_orchestrator.errors import InvalidInputError
from release_orchestrator.models import BumpLevel, CommitInfo
from release_orchestrator.versioning.policy import bump_for_commit, recommend_version
from release_orchestrator.versioning.semver import SemVer


def commit(subject: str, body: str = "") -> CommitInfo:
    return CommitInfo("a" * 40, (), "n", "e", subject, body, ())


@pytest.mark.parametrize("text", ["1.0.0", "0.0.1", "1.2.3-rc.1", "1.2.3-alpha.beta+build.5"])
def test_parse_roundtrip(text):
    assert str(SemVer.parse(text)) == text


@pytest.mark.parametrize("text", ["1.0", "01.0.0", "1.0.0-", "v1.0.0", "1.0.0-01", ""])
def test_parse_rejects_invalid(text):
    with pytest.raises(InvalidInputError):
        SemVer.parse(text)


def test_precedence_matches_semver_spec():
    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.10.0",
    ]
    parsed = [SemVer.parse(v) for v in ordered]
    assert sorted(reversed(parsed)) == parsed
    assert SemVer.parse("1.0.0+a") == SemVer.parse("1.0.0+b")


@pytest.mark.parametrize(
    ("subject", "body", "level"),
    [
        ("FIN-1: feat: add x", "", BumpLevel.MINOR),
        ("fix(payments): y", "", BumpLevel.PATCH),
        ("feat!: drop z", "", BumpLevel.MAJOR),
        ("refactor: q", "BREAKING CHANGE: removed q", BumpLevel.MAJOR),
        ("random words", "", BumpLevel.PATCH),
    ],
)
def test_bump_for_commit(subject, body, level):
    assert bump_for_commit(commit(subject, body))[0] is level


def test_version_from_highest_published_not_base():
    decision = recommend_version(
        [commit("feat: a")], published=[SemVer.parse("1.1.0")], tagged=[SemVer.parse("1.0.0")]
    )
    assert str(decision.proposed) == "1.2.0" and not decision.collides


def test_prerelease_tags_do_not_count_as_released():
    decision = recommend_version(
        [commit("fix: a")], published=[SemVer.parse("1.0.0")], tagged=[SemVer.parse("1.0.1-rc.1")]
    )
    assert str(decision.proposed) == "1.0.1" and not decision.collides


def test_removed_public_symbol_forces_major():
    decision = recommend_version(
        [commit("fix: tidy")],
        published=[SemVer.parse("1.4.2")],
        tagged=[],
        api_removals=["zaidlibrary.payments.calculate_total"],
    )
    assert decision.bump is BumpLevel.MAJOR and str(decision.proposed) == "2.0.0"
