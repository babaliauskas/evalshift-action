"""Every mention of the pinned EvalShift CLI version must equal the action.yml default.

The regex list lives in ``scripts/bump_cli_pin.py`` (``PIN_SITES``) so the bump script
and this test can never disagree about where the pin is written down. Adding a doc site
is one line there.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from _manifest import REPO_ROOT, manifest_input_default
from bump_cli_pin import PIN_SITES, find_pins

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
