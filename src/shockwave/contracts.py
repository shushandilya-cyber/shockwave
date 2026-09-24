"""Story I/O contracts (mirrors 01..07 spec files) with lightweight validation.

Every story reads/writes plain JSON dicts so the same artifacts can be produced
by the local engine OR by an Obsidian node and compared byte-for-byte.
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
TEST_STATUS = {"PASS", "FAIL", "ERROR", "SKIPPED"}
VERDICTS = {"Impacted", "NotImpacted", "NeedsReview"}


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
    return t


def validate_verdicts(vs: list) -> list:
    for v in vs:
        _req(v, ["org", "repo", "verdict", "reasoning"], "Verdict")
        _enum(v["verdict"], VERDICTS, "Verdict.verdict")
    return vs


def validate_jira_ticket(j: dict) -> dict:
    _req(j, ["summary"], "JiraTicket")
    return j

