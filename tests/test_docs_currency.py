"""The docs must not describe flows the product has removed.

`thresholds:` was deleted from `evalshift.yaml` in CLI 1.1.0 -- a config that
still sets it fails to load, by name -- and `policy:configure` was deleted from
the server's permission catalog along with the single write it guarded. This
repo's README, DOCS.md and llms-full.txt described both for five releases after
they were gone, and llms-full.txt is copied verbatim to
https://www.evalshift.dev/ci-llms-full.txt, so the stale instructions were being
served to coding agents as current guidance.

Nothing else noticed, because every existing docs test checks a version literal
rather than a claim. This is the tripwire for claims: a term retired from the
product must not reappear in prose.
"""

from __future__ import annotations

import pytest
from _manifest import REPO_ROOT

#: Exact substrings that named a removed feature. Retiring something else from
#: the product? Append it here in the same commit that removes it.
RETIRED_TERMS: tuple[str, ...] = (
    "policy:configure",
    "thresholds:",
)

PROSE_FILES: tuple[str, ...] = ("README.md", "DOCS.md", "llms-full.txt")


@pytest.mark.parametrize("name", PROSE_FILES)
@pytest.mark.parametrize("term", RETIRED_TERMS)
def test_prose_does_not_describe_a_removed_feature(name: str, term: str) -> None:
    text = (REPO_ROOT / name).read_text(encoding="utf-8")

    assert term not in text, f"{name} still describes the removed {term!r}"
