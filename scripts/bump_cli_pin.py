#!/usr/bin/env python3
"""Rewrite every mention of the pinned EvalShift CLI version.

``action.yml`` is the single source of truth for the pin. The docs repeat it in a
handful of known places; ``PIN_SITES`` below enumerates them, and every pattern is
scoped to its sentence or table row so a bump never touches an unrelated ``X.Y.Z``.
``tests/test_pin_consistency.py`` reads the same table to fail on any stale mention.

Usage::

    python scripts/bump_cli_pin.py 0.13.1

A pin bump is a patch release of the action, so ``pyproject.toml``'s ``version``
patch component is bumped alongside. Changed files are printed one per line.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

VERSION = r"(?P<version>\d+\.\d+\.\d+)"

# One entry per file, one regex per site. Each regex names the pin ``version`` and must
# match at least once, so a doc rewrite that drops a sentence fails loudly instead of
# silently shrinking the list. Adding a doc site is one line here. Compiled with
# ``re.MULTILINE``: ``^``/``$`` anchor to lines.
PIN_SITES: Mapping[str, tuple[str, ...]] = {
    "action.yml": (rf'^  evalshift-version:\n(?:    (?!default:).*\n)*    default: "{VERSION}"$',),
    "README.md": (rf"^\| `evalshift-version` +\| no +\| `{VERSION}` +\|",),
    "DOCS.md": (
        rf"evalshift=={VERSION}",
        rf"^\| `evalshift-version` \| no \| `{VERSION}` \|",
        rf"3\.11 for {VERSION}\)",
        rf"EvalShift {VERSION} needs Python",
    ),
    "llms-full.txt": (
        rf"evalshift=={VERSION}",
        rf"^\| evalshift-version \| no \| {VERSION} \|",
        rf"3\.11 for {VERSION}\)",
    ),
}

PYPROJECT_VERSION = re.compile(
    r'^version = "(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"$', re.M
)
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class PinSiteError(Exception):
    """A documented pin site no longer matches its file."""


def find_pins(text: str, patterns: Iterable[str], *, label: str = "") -> list[str]:
    """Return every pinned version ``patterns`` match in ``text``.

    Raises:
        PinSiteError: if any pattern matches nothing.
    """
    found: list[str] = []
    for pattern in patterns:
        matches = list(re.finditer(pattern, text, re.M))
        if not matches:
            raise PinSiteError(f"{label or 'text'}: pattern matched nothing: {pattern!r}")
        found.extend(match.group("version") for match in matches)
    return found


def replace_pins(text: str, patterns: Iterable[str], new_version: str, *, label: str = "") -> str:
    """Rewrite the ``version`` group of every match, leaving the rest of each site intact."""

    def swap(match: re.Match[str]) -> str:
        start, end = match.span("version")
        return match.string[match.start() : start] + new_version + match.string[end : match.end()]

    for pattern in patterns:
        if not re.search(pattern, text, re.M):
            raise PinSiteError(f"{label or 'text'}: pattern matched nothing: {pattern!r}")
        text = re.sub(pattern, swap, text, flags=re.M)
    return text


def current_pin(root: Path = REPO_ROOT) -> str:
    """Read the pinned CLI version from ``action.yml``."""
    manifest = root / "action.yml"
    pins = find_pins(
        manifest.read_text(encoding="utf-8"), PIN_SITES["action.yml"], label="action.yml"
    )
    return pins[0]


def bump_pyproject_patch(text: str) -> str:
    """Increment the patch component of ``version = "X.Y.Z"`` in a pyproject."""
    match = PYPROJECT_VERSION.search(text)
    if match is None:
        raise PinSiteError('pyproject.toml: no version = "X.Y.Z" line found')
    patch = int(match.group("patch")) + 1
    replacement = f'version = "{match.group("major")}.{match.group("minor")}.{patch}"'
    return text[: match.start()] + replacement + text[match.end() :]


def bump(root: Path, new_version: str) -> list[Path]:
    """Rewrite every pin site under ``root`` to ``new_version`` and return the changed files.

    Returns an empty list (writing nothing) when the pin already equals ``new_version``.
    """
    if not SEMVER.match(new_version):
        raise ValueError(f"expected X.Y.Z, got {new_version!r}")
    if current_pin(root) == new_version:
        return []

    changed: list[Path] = []
    for name, patterns in PIN_SITES.items():
        path = root / name
        before = path.read_text(encoding="utf-8")
        after = replace_pins(before, patterns, new_version, label=name)
        if after != before:
            path.write_text(after, encoding="utf-8")
            changed.append(path)

    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        bump_pyproject_patch(pyproject.read_text(encoding="utf-8")), encoding="utf-8"
    )
    changed.append(pyproject)
    return changed


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``bump_cli_pin.py <new-version>``."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: bump_cli_pin.py <new-version>", file=sys.stderr)
        return 2
    new_version = args[0]
    try:
        current = current_pin()
        if current == new_version:
            print(f"already at {current}")
            return 0
        changed = bump(REPO_ROOT, new_version)
    except (ValueError, PinSiteError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"bumped evalshift pin {current} -> {new_version}")
    for path in changed:
        print(path.relative_to(REPO_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
