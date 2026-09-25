import pytest

from shockwave.stories.s6_verdict import aggregate

BR = {"sourceRepo": "S/s", "commit": "abc", "notIndexed": False, "unresolvedSymbols": [],
      "impactedRepos": [{"org": "O", "repo": "r", "confidence": "low", "evidence": [{"source": "getOpenapiConnections", "detail": "d"}]}]}


def tr(status, live, **kw):
    return {"org": "O", "repo": "r", "status": status, "hadLiveIntegrationSignal": live, "failedTests": ["T#a"] if status == "FAIL" else [],
            "skippedReason": kw.get("reason"), "liveIntegrationNote": "n"}


@pytest.mark.parametrize("status,live,verdict,rule", [
    # §3 updated rule numbers: MOCKED added as rule 4; others shifted by 1
    ("FAIL", True, "Impacted", 2),
    ("PASS", True, "NotImpacted", 3),
    # rule 4 is now PASS+MOCKED; FAIL/ERROR without live is rule 5
    ("FAIL", False, "NeedsReview", 5),
    ("ERROR", False, "NeedsReview", 5),
    ("SKIPPED", False, "NeedsReview", 6),
    ("PASS", False, "NeedsReview", 7),
])
def test_decision_table(status, live, verdict, rule):
    v = aggregate(BR, [tr(status, live, reason="no runner")])[0]
    assert (v["verdict"], v["rule"]) == (verdict, rule), f"got rule {v['rule']}: {v['reasoning']}"
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



SRC_MAN = {"entries": [{"org": "S", "repo": "s", "isSourceRepo": True}]}


def src_tr(status, baseline=None):
    return {"org": "S", "repo": "s", "status": status, "hadLiveIntegrationSignal": False,
            "failedTests": ["a.T#x", "a.T#y"] if status == "FAIL" else [], "baseline": baseline}


def _src(vs):
    return next(v for v in vs if v.get("isSourceRepo"))


def test_empty_commit_is_not_impacted_with_evidence():
    v = _src(aggregate({**BR, "impactedRepos": [], "emptyCommit": "2e9cb2a4ffff"}, [src_tr("NOT_RUN")], SRC_MAN))
    assert (v["verdict"], v["rule"]) == ("NotImpacted", 0) and "2e9cb2a4" in v["reasoning"]


def test_failures_that_also_fail_at_parent_are_pre_existing():
    bl = {"commit": "59b6a1bf00", "status": "FAIL", "preExisting": ["a.T#x", "a.T#y"], "newFailures": []}
    v = _src(aggregate({**BR, "impactedRepos": []}, [src_tr("FAIL", bl)], SRC_MAN))
    assert v["rule"] == 8 and v["verdict"] == "NeedsReview"
    assert "pre-existing" in v["reasoning"] and "59b6a1bf" in v["reasoning"]
    assert v["evidence"]["testResult"]["baseline"] == bl


def test_failure_new_at_this_commit_is_impacted():
    bl = {"commit": "p", "status": "FAIL", "preExisting": ["a.T#x"], "newFailures": ["a.T#y"]}
    v = _src(aggregate({**BR, "impactedRepos": []}, [src_tr("FAIL", bl)], SRC_MAN))
    assert (v["verdict"], v["rule"]) == ("Impacted", 9) and "a.T#y" in v["reasoning"]


def test_inconclusive_baseline_keeps_rule_5():
    bl = {"commit": "p", "status": "NOT_RUN", "reason": "clone failed"}
    v = _src(aggregate({**BR, "impactedRepos": []}, [src_tr("FAIL", bl)], SRC_MAN))
    assert v["rule"] == 5


def test_not_indexed_but_local_context_is_not_rule_1():
    br = {"sourceRepo": "CoreShipping/polis", "commit": "c", "notIndexed": True, "coverage": "local",
          "unresolvedSymbols": [], "impactedRepos": [{"org": "A", "repo": "b", "confidence": "high",
                                                     "evidence": [{"source": "localCallSite", "detail": "x"}]}]}
    vs = aggregate(br, [], {"entries": []})
    assert not any(v.get("rule") == 1 for v in vs)
    assert any(v["repo"] == "b" for v in vs)
