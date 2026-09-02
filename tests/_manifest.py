"""Shared helpers for reading ``action.yml`` in tests without a YAML dependency."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "action.yml"


def manifest_input_default(name: str, manifest: Path = MANIFEST) -> str:
    """Read an input default out of action.yml without a YAML dependency."""
    lines = manifest.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip() != f"{name}:":
            continue
        for follower in lines[index + 1 :]:
            stripped = follower.strip()
            if stripped.startswith("default:"):
                return stripped.removeprefix("default:").strip().strip("\"'")
            if follower and not follower.startswith("    "):
                break
    raise AssertionError(f"no default found for input '{name}' in {manifest}")
