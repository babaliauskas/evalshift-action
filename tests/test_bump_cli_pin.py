from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bump_cli_pin as bump_script
from _manifest import REPO_ROOT, manifest_input_default

PIN_FILES = (*bump_script.PIN_SITES, "pyproject.toml")


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """The real pin-bearing files, copied so tests never touch the repo."""
    for name in PIN_FILES:
        shutil.copy(REPO_ROOT / name, tmp_path / name)
    return tmp_path


def test_current_pin_matches_manifest_default(repo_copy: Path) -> None:
    assert bump_script.current_pin(repo_copy) == manifest_input_default("evalshift-version")


def test_bump_rewrites_every_site_and_only_those_files(repo_copy: Path) -> None:
    old = bump_script.current_pin(repo_copy)
    new = "9.8.7"
    assert new != old

    changed = bump_script.bump(repo_copy, new)

    assert sorted(path.name for path in changed) == sorted(PIN_FILES)
    assert bump_script.current_pin(repo_copy) == new
    assert manifest_input_default("evalshift-version", repo_copy / "action.yml") == new
    for name, patterns in bump_script.PIN_SITES.items():
        text = (repo_copy / name).read_text(encoding="utf-8")
        assert set(bump_script.find_pins(text, patterns, label=name)) == {new}, name
        assert old not in text, f"{name} still mentions {old}"


def test_bump_only_touches_the_version_group(repo_copy: Path) -> None:
    before = (repo_copy / "README.md").read_text(encoding="utf-8")
    old = bump_script.current_pin(repo_copy)

    bump_script.bump(repo_copy, "9.8.7")

    after = (repo_copy / "README.md").read_text(encoding="utf-8")
    # Only one line differs, and it differs only by the pin literal.
    diff = [(a, b) for a, b in zip(before.splitlines(), after.splitlines(), strict=True) if a != b]
    assert len(diff) == 1
    assert diff[0][0].replace(old, "9.8.7") == diff[0][1]


def test_bump_increments_pyproject_patch(repo_copy: Path) -> None:
    (repo_copy / "pyproject.toml").write_text('[project]\nversion = "0.3.9"\n', encoding="utf-8")

    bump_script.bump(repo_copy, "9.8.7")

    assert 'version = "0.3.10"' in (repo_copy / "pyproject.toml").read_text(encoding="utf-8")


def test_bump_is_a_noop_when_already_at_version(repo_copy: Path) -> None:
    current = bump_script.current_pin(repo_copy)
    snapshot = {name: (repo_copy / name).read_bytes() for name in PIN_FILES}

    assert bump_script.bump(repo_copy, current) == []
    assert {name: (repo_copy / name).read_bytes() for name in PIN_FILES} == snapshot


def test_bump_rejects_non_semver(repo_copy: Path) -> None:
    with pytest.raises(ValueError, match=r"expected X\.Y\.Z"):
        bump_script.bump(repo_copy, "latest")


def test_bump_fails_loudly_when_a_site_disappears(repo_copy: Path) -> None:
    docs = repo_copy / "DOCS.md"
    docs.write_text(docs.read_text(encoding="utf-8").replace("needs Python", "wants Python"))

    with pytest.raises(bump_script.PinSiteError, match=r"DOCS\.md"):
        bump_script.bump(repo_copy, "9.8.7")


def test_main_reports_already_at_current_pin(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, repo_copy: Path
) -> None:
    monkeypatch.setattr(bump_script, "REPO_ROOT", repo_copy)
    current = bump_script.current_pin(repo_copy)

    assert bump_script.main([current]) == 0
    assert capsys.readouterr().out.strip() == f"already at {current}"


def test_main_prints_changed_files(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, repo_copy: Path
) -> None:
    monkeypatch.setattr(bump_script, "REPO_ROOT", repo_copy)

    assert bump_script.main(["9.8.7"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].endswith("-> 9.8.7")
    assert sorted(lines[1:]) == sorted(PIN_FILES)


def test_main_usage_error() -> None:
    assert bump_script.main([]) == 2
