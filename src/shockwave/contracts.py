"""Story I/O contracts (mirrors 01..07 spec files) with lightweight validation.

Every story reads/writes plain JSON dicts so the same artifacts can be produced
by the local engine OR by an Obsidian node and compared byte-for-byte.

Backward-compatibility rule: new fields are always ADDED, never renamed.
"""
from __future__ import annotations

from typing import Any


class ContractError(ValueError):
    pass


def _req(obj: dict, keys: list[str], name: str) -> None:
    missing = [k for k in keys if obj.get(k) in (None, "")]
    if missing:
        raise ContractError(f"{name}: missing required field(s): {', '.join(missing)}")


def _enum(val: Any, allowed: set, name: str) -> None:
    if val not in allowed:
        raise ContractError(f"{name}: {val!r} not in {sorted(allowed)}")


CONFIDENCE = {"high", "medium", "low"}
EVIDENCE_SOURCES = {"graphSearch", "vectorSearch", "getRepoDependencies", "getOpenapiConnections", "localScan"}
SYMBOL_KINDS = {"class", "method", "field", "endpoint"}
CHANGE_TYPES = {"added", "modified", "removed"}
BUILD_TOOLS = {"maven", "gradle", "npm", "unknown"}
# NOT_RUN replaces SKIPPED for technical failures; SKIPPED is reserved for explicit --skip-tests.
TEST_STATUS = {"PASS", "FAIL", "ERROR", "SKIPPED", "NOT_RUN"}
VERDICTS = {"Impacted", "NotImpacted", "NeedsReview"}

# ── §1 Risk classes ──────────────────────────────────────────────────────────────
RISK_CLASSES = {"BREAKING", "BEHAVIORAL", "SAFE"}
CHANGE_GROUPS = {"contractChanges", "behaviorChanges", "internalOnly", "configDataOnly"}

# ── §2 Confidence: what each level PROVES (and what it does NOT prove) ───────────
CONFIDENCE_LEGEND = {
    "high": (
        "A concrete call path from consumer code to the changed symbol was found: "
        "a direct FQN call edge, a transitive call edge through the source repo, or "
        "an OpenAPI inbound chain whose target method is reached by the changed code. "
        "Evidence names the consumer class.method → API/method → changed symbol. "
        "Does NOT prove the path is reachable at runtime (could be behind a feature flag)."
    ),
    "medium": (
        "The consumer depends on the source (app/repo dependency, or it imports a changed class), "
        "but no specific call path to the changed code has been shown. "
        "Does NOT prove impact — only that an impact is plausible."
    ),
    "low": (
        "The consumer calls some API of the source, but none of the endpoints it calls "
        "are linked to the changed code. Awareness-only signal."
    ),
    "unknown": (
        "Not indexed in Code Knowledge, lookup failed, or unmapped API caller. "
        "Never folded into Low — always shown separately so the gap is visible."
    ),
}

# ── §2 Priority matrix: risk class × confidence ──────────────────────────────────
#
# Decision logic for an on-call engineer:
#   P1 = requires same-day investigation; the evidence is strong AND the change is breaking
#   P2 = review within the sprint; either the evidence is strong OR the change is breaking
#   P3 = awareness only; likely no action needed unless tests start failing
#
# "unknown" is NOT the bottom of the scale. Unknown means we could not see whether a
# path exists, so a BREAKING change with unknown coverage cannot be ruled out and must be
# looked at — that is precisely the shape of the PUDO incidents where the consumer repo
# was not indexed. Ranking it P3 alongside a proven-irrelevant consumer would bury it.
PRIORITY_MATRIX: dict[tuple[str, str], str] = {
    ("BREAKING",   "high"):    "P1",
    ("BREAKING",   "medium"):  "P2",
    ("BREAKING",   "unknown"): "P2",
    ("BREAKING",   "low"):     "P3",
    ("BEHAVIORAL", "high"):    "P2",
    ("BEHAVIORAL", "medium"):  "P2",
    ("BEHAVIORAL", "unknown"): "P3",
    ("BEHAVIORAL", "low"):     "P3",
    ("SAFE",       "high"):    "P3",
    ("SAFE",       "medium"):  "P3",
    ("SAFE",       "unknown"): "P3",
    ("SAFE",       "low"):     "P3",
}

CONFIDENCE_LEVELS = ("high", "medium", "low", "unknown")


def priority_for(risk_class: str | None, confidence: str | None) -> str:
    """Combine risk class (§1) with blast-radius confidence (§2) into a triage priority.

    Returns "P1", "P2", or "P3". Missing inputs are treated as the least alarming
    combination (SAFE / low) so an absent field can never manufacture a P1.
    Unit-tested in tests/test_s2_changes_summary.py.
    """
    return PRIORITY_MATRIX.get((risk_class or "SAFE", confidence or "low"), "P3")


# ── §3 Live-integration signal tri-state ─────────────────────────────────────────
LIVE_SIGNAL = {"LIVE", "MOCKED", "NONE"}
# LIVE   = tests reference the changed service's staging host (real network call expected)
# MOCKED = integration-style tests mention the service but mock it (contract only, not behaviour)
# NONE   = no tests reference the service at all (generic sanity check only)


# ── Validators ───────────────────────────────────────────────────────────────────

def validate_change_event(e: dict) -> dict:
    _req(e, ["org", "repo", "branch", "commit", "environment", "triggeredAt"], "ChangeEvent")
    return e


def validate_changed_symbols(c: dict) -> dict:
    _req(c, ["org", "repo", "commit"], "ChangedSymbols")
    for k in ("changedFiles", "changedSymbols", "changedApiEndpoints"):
        if not isinstance(c.get(k), list):
            raise ContractError(f"ChangedSymbols: {k} must be a list")
    for s in c["changedSymbols"]:
        _req(s, ["file", "kind", "name", "changeType"], "ChangedSymbols.changedSymbols[]")
        _enum(s["kind"], SYMBOL_KINDS, "changedSymbols[].kind")
        _enum(s["changeType"], CHANGE_TYPES, "changedSymbols[].changeType")
    # changesSummary is optional (added by §1); validate structure if present
    cs = c.get("changesSummary")
    if cs is not None:
        if not isinstance(cs, dict):
            raise ContractError("ChangedSymbols.changesSummary must be a dict")
        if "overallRiskClass" in cs:
            _enum(cs["overallRiskClass"], RISK_CLASSES, "changesSummary.overallRiskClass")
        for gname, gdata in (cs.get("groups") or {}).items():
            _enum(gname, CHANGE_GROUPS, f"changesSummary.groups key")
            if "riskClass" in gdata:
                _enum(gdata["riskClass"], RISK_CLASSES, f"changesSummary.groups.{gname}.riskClass")
    return c


def validate_blast_radius(b: dict) -> dict:
    _req(b, ["sourceRepo", "commit"], "BlastRadius")
    if not isinstance(b.get("notIndexed"), bool):
        raise ContractError("BlastRadius: notIndexed must be bool")
    for r in b.get("impactedRepos", []):
        _req(r, ["org", "repo", "confidence"], "BlastRadius.impactedRepos[]")
        _enum(r["confidence"], CONFIDENCE, "impactedRepos[].confidence")
        for ev in r.get("evidence", []):
            _enum(ev.get("source"), EVIDENCE_SOURCES, "impactedRepos[].evidence[].source")
    if not isinstance(b.get("unresolvedSymbols"), list):
        raise ContractError("BlastRadius: unresolvedSymbols must be a list")
    return b


def validate_manifest(m: dict) -> dict:
    if not isinstance(m.get("entries"), list):
        raise ContractError("ImpactedRepoManifest: entries must be a list")
    for e in m["entries"]:
        _req(e, ["org", "repo", "confidence", "teamSource", "buildTool"], "ImpactedRepoManifest.entries[]")
        _enum(e["buildTool"], BUILD_TOOLS, "entries[].buildTool")
        if not isinstance(e.get("runnable"), bool):
            raise ContractError("entries[].runnable must be bool")
    return m


def validate_test_result(t: dict) -> dict:
    _req(t, ["org", "repo", "status"], "TestResult")
    _enum(t["status"], TEST_STATUS, "TestResult.status")
    if not isinstance(t.get("hadLiveIntegrationSignal"), bool):
        raise ContractError("TestResult.hadLiveIntegrationSignal must be bool")
    # liveSignal is the new tri-state; validate if present
    if t.get("liveSignal") is not None:
        _enum(t["liveSignal"], LIVE_SIGNAL, "TestResult.liveSignal")
    return t


def validate_verdicts(vs: list) -> list:
    for v in vs:
        _req(v, ["org", "repo", "verdict", "reasoning"], "Verdict")
        _enum(v["verdict"], VERDICTS, "Verdict.verdict")
    return vs


def validate_jira_ticket(j: dict) -> dict:
    _req(j, ["summary"], "JiraTicket")
    return j
