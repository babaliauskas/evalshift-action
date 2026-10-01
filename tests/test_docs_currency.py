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

#: Exact substrings of claims that stopped being true when the plan preflight moved
#: to `POST /runs/preflight`. The old preflight looked the project up through the org's
#: project list, which needs `project:read`, then called a per-project route; the
#: documented CI key could do neither, and the prose said that was fine. The default
#: `fail-on: policy` gate reads `GET /runs/{id}/policy-check`, which needs
#: `policy:read`, so "the gate needs no extra scope" left that gate silently falling
#: back to regression mode.
RETIRED_CLAIMS: tuple[str, ...] = (
    "/orgs/<org>/projects",
    "/orgs/{org}/projects",
    # The old route. Not bare "ci-preflight": that is also the DOCS.md heading anchor
    # `#plan-limits-and-the-ci-preflight`, which other sites link to.
    "/ci-preflight",
    "ci-preflight call",
    "without `project:read`",
    "lacks `project:read`",
    "needs no extra scope",
    "needs no scope beyond",
    "needs no further scope",
)

PROSE_FILES: tuple[str, ...] = ("README.md", "DOCS.md", "llms-full.txt")


@pytest.mark.parametrize("name", PROSE_FILES)
@pytest.mark.parametrize("term", RETIRED_TERMS)
def test_prose_does_not_describe_a_removed_feature(name: str, term: str) -> None:
    text = (REPO_ROOT / name).read_text(encoding="utf-8")

    assert term not in text, f"{name} still describes the removed {term!r}"


@pytest.mark.parametrize("name", PROSE_FILES)
@pytest.mark.parametrize("claim", RETIRED_CLAIMS)
def test_prose_does_not_repeat_a_retired_claim(name: str, claim: str) -> None:
    text = (REPO_ROOT / name).read_text(encoding="utf-8")

    assert claim not in text, f"{name} still says {claim!r}, which is no longer true"


#: What each prose file must still say, the other half of the tripwire: a doc that
#: dropped the scope table or the preflight section entirely would pass every check
#: above.
REQUIRED_FACTS: tuple[str, ...] = (
    "policy:read",
    "POST /runs/preflight",
)


@pytest.mark.parametrize("name", PROSE_FILES)
@pytest.mark.parametrize("fact", REQUIRED_FACTS)
def test_prose_states_the_current_key_scopes_and_preflight(name: str, fact: str) -> None:
    text = (REPO_ROOT / name).read_text(encoding="utf-8")

    assert fact in text, f"{name} no longer mentions {fact!r}"
