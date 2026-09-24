from datetime import datetime, timezone

import pytest

from shockwave.contracts import ContractError
from shockwave.stories.s1_trigger import ingest
from conftest import load

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def test_full_obsidian_body_normalizes():
    ev = ingest(load("s1/waypoint_queryindex.raw.json"), now=NOW)
    assert ev == {"org": "CoreShipping", "repo": "waypointservice", "branch": "staging", "commit": "9d60af39",
                  "environment": "staging", "prNumber": None, "triggeredAt": "2026-09-24T12:00:00Z"}


def test_bare_payload_with_pr():
    ev = ingest({"org": "o", "repo": "r", "branch": "b", "commit": "abcdef1234", "environment": "staging", "prNumber": 42}, now=NOW)
    assert ev["prNumber"] == "42"


@pytest.mark.parametrize("missing", ["org", "repo", "branch", "commit", "environment"])
def test_missing_field_fails_fast(missing):
    p = {"org": "o", "repo": "r", "branch": "b", "commit": "abcdef1234", "environment": "staging"}
    p.pop(missing)
    with pytest.raises(ContractError, match=missing):
        ingest({"eventContext": {"payload": p}})


def test_non_sha_commit_rejected():
    with pytest.raises(ContractError, match="SHA"):
        ingest({"org": "o", "repo": "r", "branch": "b", "commit": "main", "environment": "staging"})

