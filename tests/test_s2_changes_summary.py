"""Tests for §1 Changes Summary and §2 priority matrix.

Every rule in s2_changes_summary and contracts.priority_for() is covered here.
Fixtures are minimal inline dicts (no git / network required).
"""
import pytest

from shockwave.stories.s2_changes_summary import (
    classify_changes, classify_config_file, _classify_symbol, _intent_from_msg,
)
from shockwave.contracts import priority_for, PRIORITY_MATRIX


# ── helpers ───────────────────────────────────────────────────────────────────────

def sym(kind="method", change="modified", cls="MyService", member="doSomething",
        pkg="com.acme", file="src/main/java/com/acme/MyService.java"):
    return {
        "kind": kind, "changeType": change, "className": cls,
        "member": member, "package": pkg, "file": file,
        "name": f"{pkg}.{cls}#{member}",
    }


def changed(symbols=None, endpoints=None, files=None, commit="abc123", msg=None):
    return {
        "org": "O", "repo": "r", "commit": commit,
        "changedFiles": files or ([s["file"] for s in (symbols or [])]),
        "changedSymbols": symbols or [],
        "changedApiEndpoints": endpoints or [],
    }


# ── §1 risk class rules ───────────────────────────────────────────────────────────

class TestClassifySymbol:
    def test_removed_endpoint_is_breaking(self):
        s = {"kind": "endpoint", "changeType": "removed", "name": "DELETE /waypoints/{id}", "file": "a.java"}
        group, risk, reason = _classify_symbol(s, set())
        assert group == "contractChanges"
        assert risk == "BREAKING"

    def test_added_endpoint_is_behavioral(self):
        s = {"kind": "endpoint", "changeType": "added", "name": "GET /waypoints/v2", "file": "a.java"}
        group, risk, reason = _classify_symbol(s, set())
        assert group == "contractChanges"
        assert risk == "BEHAVIORAL"

    def test_runtime_data_is_behavioral_not_safe(self):
        # Data under src/main/resources is served to callers, so editing it changes what
        # consumers see with no code change. This is the INC-002 shape.
        s = sym(file="src/main/resources/Hubs.json")
        group, risk, _ = _classify_symbol(s, set())
        assert group == "configDataOnly"
        assert risk == "BEHAVIORAL"

    def test_yaml_config_is_safe(self):
        s = sym(file="config/application.yaml")
        group, risk, _ = _classify_symbol(s, set())
        assert group == "configDataOnly"
        assert risk == "SAFE"

    def test_test_file_is_internal(self):
        s = sym(file="src/test/java/com/acme/MyTest.java")
        group, risk, _ = _classify_symbol(s, set())
        assert group == "internalOnly"
        assert risk == "SAFE"

    def test_removed_public_method_in_resource_class_is_breaking(self):
        s = sym(kind="method", change="removed", cls="WaypointResource", member="listWaypoints")
        group, risk, _ = _classify_symbol(s, {"WaypointResource"})
        assert group == "contractChanges"
        assert risk == "BREAKING"

    def test_modified_method_in_resource_class_is_behavioral(self):
        s = sym(kind="method", change="modified", cls="WaypointResource", member="listWaypoints")
        group, risk, _ = _classify_symbol(s, {"WaypointResource"})
        assert group == "behaviorChanges"
        assert risk == "BEHAVIORAL"

    def test_added_method_in_resource_class_is_behavioral(self):
        s = sym(kind="method", change="added", cls="WaypointResource", member="newMethod")
        group, risk, _ = _classify_symbol(s, {"WaypointResource"})
        assert group == "contractChanges"
        assert risk == "BEHAVIORAL"

    def test_removed_dto_getter_is_breaking(self):
        s = sym(kind="method", change="removed", cls="WaypointResponse", member="getHubId")
        group, risk, _ = _classify_symbol(s, set())
        assert group == "contractChanges"
        assert risk == "BREAKING"

    def test_added_dto_getter_is_behavioral(self):
        s = sym(kind="method", change="added", cls="WaypointResponse", member="getCapabilities")
        group, risk, _ = _classify_symbol(s, set())
        assert group == "contractChanges"
        assert risk == "BEHAVIORAL"

    def test_removed_dto_field_is_breaking(self):
        s = sym(kind="field", change="removed", cls="HubRequest", member="hubId")
        group, risk, _ = _classify_symbol(s, set())
        assert group == "contractChanges"
        assert risk == "BREAKING"

    def test_added_dto_field_is_behavioral(self):
        s = sym(kind="field", change="added", cls="HubRequest", member="maxWeight")
        group, risk, _ = _classify_symbol(s, set())
        assert group == "contractChanges"
        assert risk == "BEHAVIORAL"

    def test_removed_internal_field_is_safe(self):
        s = sym(kind="field", change="removed", cls="InternalHelper", member="cacheSize")
        group, risk, _ = _classify_symbol(s, set())
        assert group == "internalOnly"
        assert risk == "SAFE"

    def test_removed_class_is_breaking(self):
        s = sym(kind="class", change="removed", cls="", member="WaypointFactory")
        group, risk, _ = _classify_symbol(s, set())
        assert group == "contractChanges"
        assert risk == "BREAKING"

    def test_modified_enum_is_behavioral(self):
        # For kind="class", className holds the class name (not the outer class)
        s = sym(kind="class", change="modified", cls="HubStatus", member=None,
                file="src/main/java/com/acme/HubStatus.java")
        group, risk, _ = _classify_symbol(s, set())
        assert risk == "BEHAVIORAL"

    def test_generic_removed_method_is_breaking(self):
        s = sym(kind="method", change="removed", cls="SomeHelper", member="compute")
        group, risk, _ = _classify_symbol(s, set())
        assert group == "contractChanges"
        assert risk == "BREAKING"

    def test_generic_modified_method_is_behavioral(self):
        s = sym(kind="method", change="modified", cls="SomeHelper", member="compute")
        group, risk, _ = _classify_symbol(s, set())
        assert group == "behaviorChanges"
        assert risk == "BEHAVIORAL"


class TestClassifyChanges:
    def test_additive_only_commit_is_behavioral(self):
        """New endpoint + new method only → BEHAVIORAL (nothing removed)."""
        eps = [{"method": "GET", "path": "/waypoints/v3", "operationId": "listV3"}]
        c = changed(endpoints=eps)
        cs = classify_changes(c)
        assert cs["overallRiskClass"] == "BEHAVIORAL"

    def test_config_only_commit_touching_runtime_data_is_behavioral(self):
        """The INC-002 culprit: only Hubs.json changed, yet it caused a live incident
        because the service serves that data. Must not be classified SAFE."""
        c = changed(files=["src/main/resources/waypoint-hubs/QA/Hubs.json"])
        cs = classify_changes(c)
        assert cs["overallRiskClass"] == "BEHAVIORAL"
        assert cs["groups"]["configDataOnly"]["riskClass"] == "BEHAVIORAL"

    def test_test_fixture_only_commit_is_safe(self):
        c = changed(files=["src/test/resources/test-data/hubs/Hubs.json"])
        cs = classify_changes(c)
        assert cs["overallRiskClass"] == "SAFE"
        assert "configDataOnly" not in cs["groups"]
        assert "Hubs.json" in cs["groups"]["internalOnly"]["reason"]

    def test_removed_endpoint_symbol_forces_breaking(self):
        """An endpoint symbol with changeType=removed → BREAKING.

        changedApiEndpoints items don't carry changeType; removed endpoints must be
        represented as changedSymbols with kind='endpoint' and changeType='removed'.
        """
        syms = [
            {"kind": "endpoint", "changeType": "removed",
             "name": "DELETE /waypoints/v2/{id}", "file": "src/main/java/WaypointResource.java",
             "className": "WaypointResource", "member": None, "package": "com.acme"},
        ]
        c = changed(symbols=syms, files=["src/main/java/WaypointResource.java"])
        cs = classify_changes(c)
        assert cs["overallRiskClass"] == "BREAKING"

    def test_endpoints_in_changed_api_endpoints_are_behavioral(self):
        """Endpoints in changedApiEndpoints (no changeType) are classified BEHAVIORAL."""
        eps = [{"method": "GET", "path": "/waypoints/v2", "operationId": None}]
        c = changed(endpoints=eps)
        cs = classify_changes(c)
        assert cs["overallRiskClass"] == "BEHAVIORAL"

    def test_intent_uses_commit_msg(self):
        c = changed()
        msg = "[SHIPLLP-514] Use JTS spatial index for radial waypoint queries"
        cs = classify_changes(c, msg)
        assert "JTS spatial index" in cs["intent"]
        assert "O/r@" in cs["intent"]

    def test_intent_fallback_no_msg(self):
        c = changed(symbols=[sym()])
        cs = classify_changes(c, None)
        assert "No commit message" in cs["intent"]

    def test_exposed_via_populated(self):
        eps = [{"method": "GET", "path": "/waypoint", "operationId": "listWaypoints"}]
        c = changed(endpoints=eps)
        cs = classify_changes(c)
        assert len(cs["exposedVia"]) == 1
        assert cs["exposedVia"][0]["path"] == "/waypoint"

    def test_empty_change_is_safe(self):
        c = changed()
        cs = classify_changes(c)
        assert cs["overallRiskClass"] == "SAFE"

    def test_groups_contain_correct_symbols(self):
        syms = [
            sym(kind="method", change="removed", cls="MyService", member="gone"),
            sym(kind="method", change="modified", cls="InternalHelper", member="compute"),
        ]
        c = changed(symbols=syms)
        cs = classify_changes(c)
        # removed method -> contractChanges BREAKING
        assert "contractChanges" in cs["groups"]
        # modified internal -> behaviorChanges BEHAVIORAL
        assert "behaviorChanges" in cs["groups"]

    def test_mixed_risks_worst_wins(self):
        """SAFE + BEHAVIORAL + BREAKING → overall BREAKING."""
        syms = [
            sym(kind="method", change="removed", cls="API", member="gone"),   # BREAKING
            sym(kind="method", change="modified", cls="Svc", member="doIt"),  # BEHAVIORAL
        ]
        c = changed(symbols=syms, files=["src/main/resources/app.yaml"])
        cs = classify_changes(c)
        assert cs["overallRiskClass"] == "BREAKING"

    def test_evidence_block_in_summary(self):
        syms = [sym()]
        c = changed(symbols=syms)
        cs = classify_changes(c)
        assert cs["_evidence"]["symbolCount"] == 1
        assert cs["_evidence"]["org"] == "O"


class TestClassifyConfigFile:
    """Not every non-source file is inert; each kind has a different blast radius."""

    @pytest.mark.parametrize("path,group,risk", [
        # test fixtures change nothing a caller can see
        ("src/test/resources/test-data/hubs/Hubs.json", "internalOnly", "SAFE"),
        ("src/test/resources/expectedResponses/listAllHubsResponse.json", "internalOnly", "SAFE"),
        # the API spec IS the published contract
        ("waypoint-types/src/main/resources/api/openapi.yaml", "contractChanges", "BEHAVIORAL"),
        ("docs/swagger.json", "contractChanges", "BEHAVIORAL"),
        # build descriptors affect how the artifact is built, not what callers see
        ("pom.xml", "internalOnly", "SAFE"),
        ("build.gradle.kts", "internalOnly", "SAFE"),
        # runtime data is served to callers
        ("waypoint-app/src/main/resources/waypoint-hubs/QA/Hubs.json", "configDataOnly", "BEHAVIORAL"),
        # anything else stays the conservative default
        ("config/application.yaml", "configDataOnly", "SAFE"),
    ])
    def test_classification(self, path, group, risk):
        g, r, _ = classify_config_file(path)
        assert (g, r) == (group, risk)

    def test_config_repo_data_is_behavioral(self):
        # pudocfg / PickupEligibilityCfg push behaviour changes with no code at all
        g, r, why = classify_config_file("hubs/liquidation.json", repo="pudocfg")
        assert (g, r) == ("configDataOnly", "BEHAVIORAL")
        assert "config repo" in why

    def test_api_spec_beats_runtime_data_path(self):
        # openapi.yaml lives under src/main/resources; the contract reading must win
        g, _, _ = classify_config_file("x/src/main/resources/api/openapi.yaml")
        assert g == "contractChanges"

    def test_test_path_beats_everything(self):
        g, r, _ = classify_config_file("src/test/resources/api/openapi.yaml")
        assert (g, r) == ("internalOnly", "SAFE")


# ── §2 priority matrix ────────────────────────────────────────────────────────────

class TestPriorityMatrix:
    @pytest.mark.parametrize("risk,conf,expected", [
        ("BREAKING",   "high",   "P1"),
        ("BEHAVIORAL", "high",   "P2"),
        ("BREAKING",   "medium", "P2"),
        ("BEHAVIORAL", "medium", "P2"),
        ("BREAKING",   "low",    "P3"),
        ("BEHAVIORAL", "low",    "P3"),
        ("SAFE",       "high",   "P3"),
        ("SAFE",       "medium", "P3"),
        ("SAFE",       "low",    "P3"),
        # Unknown coverage is not the bottom of the scale: a BREAKING change we could
        # not trace cannot be ruled out, so it outranks a traced-but-unlinked consumer.
        ("BREAKING",   "unknown", "P2"),
        ("BEHAVIORAL", "unknown", "P3"),
        ("SAFE",       "unknown", "P3"),
    ])
    def test_matrix(self, risk, conf, expected):
        assert priority_for(risk, conf) == expected

    def test_unknown_is_never_ranked_below_low(self):
        for risk in ("BREAKING", "BEHAVIORAL", "SAFE"):
            unknown = priority_for(risk, "unknown")
            low = priority_for(risk, "low")
            assert unknown <= low, f"{risk}: unknown={unknown} must not rank below low={low}"

    def test_unknown_inputs_default_to_p3(self):
        assert priority_for(None, None) == "P3"
        assert priority_for("BREAKING", None) == "P3"
        assert priority_for(None, "high") == "P3"

    def test_all_matrix_entries_covered(self):
        """Every documented matrix entry matches the function."""
        for (risk, conf), pri in PRIORITY_MATRIX.items():
            assert priority_for(risk, conf) == pri


# ── §2: s6_verdict uses risk×confidence ──────────────────────────────────────────

class TestS6UsesRiskMatrix:
    """Smoke tests that aggregate() picks priority from the matrix, not legacy confidence."""

    def _blast(self, risk_class: str | None, confidence: str = "high"):
        br = {
            "sourceRepo": "O/s", "commit": "abc", "notIndexed": False,
            "unresolvedSymbols": [],
            "impactedRepos": [{"org": "O", "repo": "r", "confidence": confidence,
                                "evidence": [{"source": "graphSearch", "detail": "d"}]}],
        }
        if risk_class:
            br["changesSummary"] = {"overallRiskClass": risk_class}
        return br

    def _tr(self, status="SKIPPED"):
        return {"org": "O", "repo": "r", "status": status, "hadLiveIntegrationSignal": False,
                "skippedReason": "skip", "failedTests": []}

    def test_breaking_high_is_p1(self):
        from shockwave.stories.s6_verdict import aggregate
        vs = aggregate(self._blast("BREAKING", "high"), [self._tr()])
        assert vs[0]["priority"] == "P1"

    def test_behavioral_high_is_p2(self):
        from shockwave.stories.s6_verdict import aggregate
        vs = aggregate(self._blast("BEHAVIORAL", "high"), [self._tr()])
        assert vs[0]["priority"] == "P2"

    def test_safe_high_is_p3(self):
        from shockwave.stories.s6_verdict import aggregate
        vs = aggregate(self._blast("SAFE", "high"), [self._tr()])
        assert vs[0]["priority"] == "P3"

    def test_no_risk_class_falls_back_to_p3(self):
        from shockwave.stories.s6_verdict import aggregate
        vs = aggregate(self._blast(None, "high"), [self._tr()])
        # SAFE (default) + high = P3
        assert vs[0]["priority"] == "P3"

    def test_source_repo_gets_its_own_verdict(self):
        """Story 3 is downstream-only, so without this the changed repo is the one repo
        whose tests never run (INC-004, INC-005 were exactly that)."""
        from shockwave.stories.s6_verdict import aggregate
        br = self._blast("BREAKING", "high")
        br["sourceRepo"] = "O/s"
        man = {"entries": [{"org": "O", "repo": "s", "isSourceRepo": True, "team": "t"}]}
        src_tr = {"org": "O", "repo": "s", "status": "FAIL", "liveSignal": "LIVE",
                  "hadLiveIntegrationSignal": True, "failedTests": ["a.B#c"]}
        vs = aggregate(br, [src_tr, self._tr()], man)
        src = next(v for v in vs if v.get("isSourceRepo"))
        assert (src["org"], src["repo"]) == ("O", "s")
        assert src["verdict"] == "Impacted"
        assert src["reasoning"].startswith("[source repo]")

    def test_source_repo_evidence_has_the_same_shape_as_the_rest(self):
        # s7_jira iterates evidence.blastRadius; a bare string crashes ticket rendering.
        from shockwave.stories.s6_verdict import aggregate
        br = self._blast("BREAKING", "high")
        br["sourceRepo"] = "O/s"
        man = {"entries": [{"org": "O", "repo": "s", "isSourceRepo": True}]}
        src = next(v for v in aggregate(br, [], man) if v.get("isSourceRepo"))
        evs = src["evidence"]["blastRadius"]
        assert isinstance(evs, list)
        assert all(isinstance(e, dict) and "detail" in e for e in evs)

    def test_not_indexed_breaking_source_is_p2_not_p3(self):
        """A breaking change in a repo we cannot trace is the INC-001 shape: it must
        not be filed at the same priority as an irrelevant consumer."""
        from shockwave.stories.s6_verdict import aggregate
        br = {"sourceRepo": "CoreShipping/PUDO", "commit": "abc", "notIndexed": True,
              "changesSummary": {"overallRiskClass": "BREAKING"}}
        vs = aggregate(br, [])
        assert vs[0]["priority"] == "P2"
        assert vs[0]["verdict"] == "NeedsReview"


# ── §1 Jira keys and exposedVia enrichment ───────────────────────────────────────

class TestJiraKeysAndExposedVia:
    def test_jira_keys_extracted_and_intent_cites_them(self):
        c = changed(symbols=[sym()])
        cs = classify_changes(c, "SHIPLLP-514: use a spatial index for radial queries (#1159)")
        assert cs["jiraKeys"] == ["SHIPLLP-514"]
        assert cs["intent"].startswith("use a spatial index for radial queries.")
        assert "SHIPLLP-514" in cs["intent"]
        assert "(#1159)" not in cs["intent"]

    def test_lowercase_hyphenated_words_are_not_jira_keys(self):
        cs = classify_changes(changed(symbols=[sym()]), "fix utf-8 handling in re-try path")
        assert cs["jiraKeys"] == []

    def test_revert_subject_is_kept_as_intent(self):
        # A revert's subject says what is being undone, which is exactly the intent.
        cs = classify_changes(
            changed(symbols=[sym()]),
            'Revert "VOODOO-7377 pickupsvc RaptorIO Migration (#399)" (#406)',
        )
        assert "Revert" in cs["intent"]
        assert "RaptorIO Migration" in cs["intent"]
        assert '"' not in cs["intent"]
        assert cs["jiraKeys"] == ["VOODOO-7377"]

    def test_exposed_via_merges_story3_walk(self):
        from shockwave.stories.s2_changes_summary import enrich_exposed_via
        cs = classify_changes(changed(symbols=[sym()]))
        assert cs["exposedVia"] == []          # Story 2 alone sees no changed endpoints
        blast = {"exposedVia": [{
            "method": "GET", "path": "/waypoint", "operationId": "listWaypoints",
            "targetMethod": "WaypointV2Resource.listWaypoints",
            "reachedVia": "transitive", "source": "story3",
        }]}
        enrich_exposed_via(cs, blast)
        assert len(cs["exposedVia"]) == 1
        assert cs["exposedVia"][0]["reachedVia"] == "transitive"

    def test_exposed_via_enrichment_is_idempotent(self):
        from shockwave.stories.s2_changes_summary import enrich_exposed_via
        cs = classify_changes(changed(
            symbols=[sym()],
            endpoints=[{"method": "GET", "path": "/waypoint", "changeType": "modified"}],
        ))
        blast = {"exposedVia": [{"method": "GET", "path": "/waypoint",
                                 "operationId": "listWaypoints", "targetMethod": "R.list",
                                 "reachedVia": "direct", "source": "story3"}]}
        enrich_exposed_via(cs, blast)
        enrich_exposed_via(cs, blast)
        # the endpoint Story 2 already knew about is enriched, not duplicated
        assert len(cs["exposedVia"]) == 1
        assert cs["exposedVia"][0]["operationId"] == "listWaypoints"
