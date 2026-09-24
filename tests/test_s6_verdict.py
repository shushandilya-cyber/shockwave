import pytest

from shockwave.stories.s6_verdict import aggregate

BR = {"sourceRepo": "S/s", "commit": "abc", "notIndexed": False, "unresolvedSymbols": [],
      "impactedRepos": [{"org": "O", "repo": "r", "confidence": "low", "evidence": [{"source": "getOpenapiConnections", "detail": "d"}]}]}


def tr(status, live, **kw):
    return {"org": "O", "repo": "r", "status": status, "hadLiveIntegrationSignal": live, "failedTests": ["T#a"] if status == "FAIL" else [],
            "skippedReason": kw.get("reason"), "liveIntegrationNote": "n"}


@pytest.mark.parametrize("status,live,verdict,rule", [
    ("FAIL", True, "Impacted", 2),
    ("PASS", True, "NotImpacted", 3),
    ("FAIL", False, "NeedsReview", 4),
    ("ERROR", False, "NeedsReview", 4),
    ("SKIPPED", False, "NeedsReview", 5),
    ("PASS", False, "NeedsReview", 6),
])
def test_decision_table(status, live, verdict, rule):
    v = aggregate(BR, [tr(status, live, reason="no runner")])[0]
    assert (v["verdict"], v["rule"]) == (verdict, rule)
    assert "confidence=low" in v["reasoning"] or verdict == "NotImpacted"


def test_rule4_text_does_not_overclaim():
    v = aggregate(BR, [tr("FAIL", False)])[0]
    assert "may be unrelated" in v["reasoning"]


def test_not_indexed_single_source_verdict():
    vs = aggregate({**BR, "notIndexed": True, "notIndexedHints": ["X/y"]}, [])
    assert len(vs) == 1 and vs[0]["repo"] == "s" and vs[0]["verdict"] == "NeedsReview" and vs[0]["rule"] == 1
    assert "not registered in Code Knowledge" in vs[0]["reasoning"]


def test_missing_test_result_is_needs_review():
    assert aggregate(BR, [])[0]["verdict"] == "NeedsReview"


def test_empty_blast_radius_is_valid():
    assert aggregate({**BR, "impactedRepos": []}, []) == []

