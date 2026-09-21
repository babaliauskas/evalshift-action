"""Every version the docs quote must equal its source of truth.

Two independent versions live in this repo and both are written down in prose:
the pinned EvalShift CLI (``action.yml``'s ``evalshift-version`` default) and the
action's own release version (``pyproject.toml``). The regex lists live in
``scripts/bump_cli_pin.py`` (``PIN_SITES`` and ``ACTION_VERSION_SITES``) so the bump
script and this test can never disagree about where a version is written down.
Adding a doc site is one line there.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from _manifest import REPO_ROOT, manifest_input_default
from bump_cli_pin import (
    ACTION_VERSION_SITES,
    EXAMPLE_TAG_SITES,
    PIN_SITES,
    current_action_version,
    find_pins,
)

DOC_SITES = {name: patterns for name, patterns in PIN_SITES.items() if name != "action.yml"}


def test_pin_sites_cover_the_documented_files() -> None:
    assert set(DOC_SITES) == {"README.md", "DOCS.md", "llms-full.txt"}


def test_bump_script_reads_the_same_pin_as_the_manifest() -> None:
    pinned = manifest_input_default("evalshift-version")
    text = (REPO_ROOT / "action.yml").read_text(encoding="utf-8")

    assert find_pins(text, PIN_SITES["action.yml"], label="action.yml") == [pinned]


@pytest.mark.parametrize("name", sorted(DOC_SITES))
def test_every_documented_pin_matches_action_manifest(name: str) -> None:
    pinned = manifest_input_default("evalshift-version")
    text = (REPO_ROOT / name).read_text(encoding="utf-8")

    stale = [found for found in find_pins(text, DOC_SITES[name], label=name) if found != pinned]

    assert stale == [], f"{name} still mentions {sorted(set(stale))}; action.yml pins {pinned}"


def test_action_version_sites_cover_the_documented_files() -> None:
    assert set(ACTION_VERSION_SITES) == {"DOCS.md", "llms-full.txt"}


@pytest.mark.parametrize("name", sorted(ACTION_VERSION_SITES))
def test_every_documented_action_version_matches_pyproject(name: str) -> None:
    released = current_action_version(REPO_ROOT)
    text = (REPO_ROOT / name).read_text(encoding="utf-8")

    stale = [
        found
        for found in find_pins(text, ACTION_VERSION_SITES[name], label=name)
        if found != released
    ]

    assert stale == [], (
        f"{name} still advertises version {sorted(set(stale))}; pyproject.toml says {released}"
    )


def test_example_tag_sites_cover_the_documented_files() -> None:
    assert set(EXAMPLE_TAG_SITES) == {"README.md", "DOCS.md", "llms-full.txt"}


@pytest.mark.parametrize("name", sorted(EXAMPLE_TAG_SITES))
def test_every_example_tag_matches_pyproject(name: str) -> None:
    """The "pin to an exact tag" example must name a tag that exists and is current.

    It sat at v0.3.0 across five releases -- v0.3.x through v0.5.1 -- because it
    was hand-written prose that no bump touched. Advice to pin is advice to pin
    to *something*; two minors behind, the example reads as the recommendation.
    """
    released = current_action_version(REPO_ROOT)
    text = (REPO_ROOT / name).read_text(encoding="utf-8")

    stale = [
        found for found in find_pins(text, EXAMPLE_TAG_SITES[name], label=name) if found != released
    ]

    assert stale == [], (
        f"{name}'s example tag is @v{sorted(set(stale))}; pyproject.toml says {released}"
    )
