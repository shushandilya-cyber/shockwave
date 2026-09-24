"""§4 — one live-marked test per backtested incident, plus offline tests for the harness.

The live tests need VPN (Code Knowledge) and the local clones under
``~/Documents/projects``. Run them with:

    .venv/bin/pytest -q -m live tests/test_backtest_incidents.py

They pin the behaviour each incident exposed, so a regression in classification or in
source-repo evaluation fails here rather than silently in the generated doc.
"""
from pathlib import Path

import pytest
import yaml

from shockwave.stories.backtest import (
    FIX_FOR_MISS,
    MISS_SELF_IMPACT,
    MISS_SOURCE_NOT_INDEXED,
    MISS_VICTIM_NOT_INDEXED,
    IncidentResult,
    backtest_incident,
    rank_fixes,
)

INCIDENTS = Path(__file__).resolve().parents[1] / "incidents.yaml"


def _incident(inc_id: str) -> dict:
    data = yaml.safe_load(INCIDENTS.read_text())
    inc = next((i for i in data["incidents"] if i["id"] == inc_id), None)
    assert inc, f"{inc_id} not found in incidents.yaml"
    return inc


# ── the incident file itself is part of the contract ─────────────────────────────

class TestIncidentFile:
    def test_every_incident_has_the_required_fields(self):
        for inc in yaml.safe_load(INCIDENTS.read_text())["incidents"]:
            for f in ("id", "title", "culprit_repo", "culprit_commit", "victim_repos"):
                assert inc.get(f) is not None, f"{inc.get('id')} missing {f}"

    def test_unverified_victim_lists_explain_themselves(self):
        # An unverified list that does not say why is indistinguishable from a guess.
        for inc in yaml.safe_load(INCIDENTS.read_text())["incidents"]:
            if inc.get("victims_verified") is False:
                assert inc.get("verification"), f"{inc['id']} is unverified but has no note"

    def test_repo_orgs_are_plausible(self):
        # The first run of this backtest was wrong because three repos were recorded under
        # CoreShipping when they live in Ship-AST.
        for inc in yaml.safe_load(INCIDENTS.read_text())["incidents"]:
            repos = [inc["culprit_repo"], *inc["victim_repos"]]
            for r in repos:
                assert "/" in r, f"{inc['id']}: {r!r} is not org/repo"


# ── fix ranking is derived, not authored ──────────────────────────────────────────

class TestRankFixes:
    def _res(self, inc_id, categories):
        return IncidentResult(
            incident_id=inc_id, title="t", culprit_repo="O/c", culprit_commit="abc1234",
            victim_repos=["O/v"],
            miss_reasons=[{"repo": "O/v", "category": c, "detail": "d"} for c in categories],
        )

    def test_ranked_by_victims_recovered(self):
        ranked = rank_fixes([
            self._res("INC-1", [MISS_VICTIM_NOT_INDEXED, MISS_SELF_IMPACT]),
            self._res("INC-2", [MISS_VICTIM_NOT_INDEXED]),
        ])
        assert ranked[0]["category"] == MISS_VICTIM_NOT_INDEXED
        assert ranked[0]["victimsRecovered"] == 2
        assert ranked[0]["incidents"] == ["INC-1", "INC-2"]
        assert ranked[1]["victimsRecovered"] == 1

    def test_no_misses_means_no_fixes(self):
        assert rank_fixes([self._res("INC-1", [])]) == []

    def test_every_miss_category_maps_to_a_fix(self):
        for cat in (MISS_SELF_IMPACT, MISS_SOURCE_NOT_INDEXED, MISS_VICTIM_NOT_INDEXED):
            assert FIX_FOR_MISS.get(cat), f"{cat} has no fix"


# ── live: one test per backtested incident ────────────────────────────────────────

@pytest.mark.live
class TestIncidentsLive:
    """Needs VPN + local clones. Each asserts the specific lesson the incident taught."""

    def test_inc001_breaking_signature_change_is_detected(self):
        r = backtest_incident(_incident("INC-001"))
        # Map<String,String> -> Map<String,Object> on a public method is source-incompatible
        assert r.actual_risk_class == "BREAKING"
        assert r.source_indexed is True

    def test_inc001_unindexed_victim_is_reported_as_unknown_not_absent(self):
        r = backtest_incident(_incident("INC-001"))
        pudo = next((m for m in r.miss_reasons if m["repo"].lower() == "ship-ast/pudo"), None)
        assert pudo, "PUDO should be diagnosed, not silently dropped"
        assert pudo["category"] == MISS_VICTIM_NOT_INDEXED

    def test_inc002_runtime_data_change_is_behavioral(self):
        # The whole point of INC-002: a Hubs.json-only commit caused a live incident.
        r = backtest_incident(_incident("INC-002"))
        assert r.actual_risk_class == "BEHAVIORAL", \
            "a src/main/resources data change must not be classified SAFE"

    def test_inc003_is_not_backtestable_and_says_so(self):
        r = backtest_incident(_incident("INC-003"))
        assert r.culprit_commit == "unknown"
        assert any("not backtestable" in n for n in r.pipeline_notes)

    def test_inc004_self_impact_is_caught_by_evaluating_the_source_repo(self):
        r = backtest_incident(_incident("INC-004"))
        assert r.recall == 1.0, f"self-impact victim missed: {r.miss_reasons}"

    def test_inc005_self_impact_is_caught_and_classified_breaking(self):
        r = backtest_incident(_incident("INC-005"))
        assert r.recall == 1.0, f"self-impact victim missed: {r.miss_reasons}"
        assert r.actual_risk_class == "BREAKING"
