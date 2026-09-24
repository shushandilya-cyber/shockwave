"""Story 6 — Impact Verdict Aggregation (pure logic, no I/O).

Decision table (first match wins) — intentionally conservative:
  1. source notIndexed                               -> NeedsReview (source repo only)
  2. FAIL  + live signal LIVE                        -> Impacted
  3. PASS  + live signal LIVE                        -> NotImpacted
  4. PASS  + live signal MOCKED                      -> NeedsReview  (contract shape ok, behaviour unverified)
  5. FAIL/ERROR + no live signal (MOCKED or NONE)    -> NeedsReview
  6. SKIPPED or NOT_RUN (or no TestResult)           -> NeedsReview
  7. PASS  + no live signal NONE                     -> NeedsReview  (passing unrelated tests proves nothing)

§2 Priority: uses risk class from changesSummary (§1) × blast-radius confidence.
Matrix (unit-tested in tests/test_s2_changes_summary.py):
  BREAKING + High = P1
  BEHAVIORAL + High = P2
  BREAKING/BEHAVIORAL + Medium = P2
  anything + Low = P3
  SAFE + any = P3

§3 Live signal tri-state (LIVE / MOCKED / NONE):
  LIVE   = tests reference the changed service's staging host; result is a real behaviour signal.
  MOCKED = contract shape still matches (WireMock/Mockito); behaviour may still differ.
  NONE   = no tests reference the service; result is a generic sanity check only.
"""
from __future__ import annotations

from ..contracts import priority_for, validate_verdicts

# Legacy confidence-only priority (kept for backward compat; §2 matrix now used instead)
_LEGACY_PRIORITY = {"high": "P1", "medium": "P2", "low": "P3"}


def _ev_summary(repo_entry: dict | None) -> str:
    if not repo_entry:
        return "no blast-radius evidence"
    ev = repo_entry.get("evidence", [])
    srcs = sorted({e["source"] for e in ev})
    top = next((e for e in ev if e.get("matchedChangedCode") or e["source"] == "graphSearch"), ev[0] if ev else None)
    return f"confidence={repo_entry['confidence']} via {', '.join(srcs)}" + (f"; e.g. {top['detail']}" if top else "")


def _live_state(tr: dict | None) -> str:
    """Return the tri-state live signal: LIVE | MOCKED | NONE.

    Reads the new ``liveSignal`` field first; falls back to the legacy
    ``hadLiveIntegrationSignal`` bool for backward compatibility.
    """
    if tr is None:
        return "NONE"
    ls = tr.get("liveSignal")
    if ls in ("LIVE", "MOCKED", "NONE"):
        return ls
    # legacy: bool field
    return "LIVE" if tr.get("hadLiveIntegrationSignal") else "NONE"


def decide(blast_entry: dict | None, tr: dict | None) -> tuple[str, str, int]:
    """Return (verdict, reasoning, rule_number).

    Does not compute priority — that is done in aggregate() using the §2 matrix.
    """
    conf = (blast_entry or {}).get("confidence", "low")
    br = _ev_summary(blast_entry)
    live = _live_state(tr)

    if tr is None:
        return "NeedsReview", f"No test result was produced for this repo. Blast radius: {br}.", 6

    status = tr["status"]
    failed = ", ".join(tr.get("failedTests", [])[:10]) or "n/a"
    note = tr.get("liveIntegrationNote") or ""

    if status == "FAIL" and live == "LIVE":
        return "Impacted", (
            f"Tests that exercise the changed service live FAILED ({failed}). "
            f"{note} Blast radius: {br}."
        ), 2

    if status == "PASS" and live == "LIVE":
        return "NotImpacted", (
            f"Live-integration tests against the changed service PASSED. {note}"
        ), 3

    if status == "PASS" and live == "MOCKED":
        return "NeedsReview", (
            f"Tests PASSED but they mock the changed service (WireMock/Mockito) — "
            f"contract shape still matches, but behaviour change is unverified. {note} "
            f"Blast radius: {br}."
        ), 4

    if status in ("FAIL", "ERROR"):
        what = (
            f"tests failed ({failed})" if status == "FAIL"
            else "the build/test run errored (compile/env/timeout)"
        )
        live_note = f" (live signal: {live})" if live != "NONE" else ""
        return "NeedsReview", (
            f"{what[0].upper() + what[1:]}{live_note}, but none of them are known to exercise "
            f"the changed service directly — failure may be unrelated. Needs human judgment. "
            f"Blast radius: {br}."
        ), 5

    if status in ("SKIPPED", "NOT_RUN"):
        reason = tr.get("skippedReason") or tr.get("notRunReason") or "not run"
        return "NeedsReview", f"Tests not run: {reason}. Blast radius: {br}.", 6

    # status == PASS, live == NONE
    return "NeedsReview", (
        f"Tests passed but none exercise the changed service directly — cannot confirm no impact "
        f"(blast-radius confidence: {conf}). Blast radius: {br}."
    ), 7


def aggregate(blast: dict, test_results: list[dict] | None, manifest: dict | None = None) -> list[dict]:
    test_results = test_results or []
    teams = {f"{e['org']}/{e['repo']}".lower(): e for e in (manifest or {}).get("entries", [])}

    # §2: extract overall risk class from changesSummary if present.
    # The blast dict doesn't carry changesSummary, but it may be in a sibling artifact.
    # We accept it as an optional key ``changesSummary`` on the blast dict for pipeline use.
    risk_class = (blast.get("changesSummary") or {}).get("overallRiskClass") or None

    if blast.get("notIndexed"):
        org, repo = blast["sourceRepo"].split("/", 1)
        hints = blast.get("notIndexedHints") or []
        return validate_verdicts([{
            "org": org, "repo": repo, "team": None, "verdict": "NeedsReview", "rule": 1,
            "priority": priority_for(risk_class, "unknown"),
            "reasoning": (
                "Source repo not registered in Code Knowledge, blast radius could not be computed."
                + (f" Unverified repo-level hints: {', '.join(hints)}." if hints else "")
            ),
            "evidence": {"blastRadius": "notIndexed", "testResult": None}, "isSourceRepo": True,
        }])

    trs = {f"{t['org']}/{t['repo']}".lower(): t for t in test_results}
    out = []

    # The changed repo itself. Story 3 is downstream-only, so it never appears in
    # impactedRepos, yet a change that breaks its own repo is the commonest failure mode
    # of all (INC-004, INC-005). Its verdict is produced from its own test result.
    src_key = (blast.get("sourceRepo") or "").lower()
    src_entry = teams.get(src_key)
    if src_entry and src_entry.get("isSourceRepo"):
        tr = trs.get(src_key)
        verdict, reasoning, rule = decide(
            {"confidence": "high",
             "evidence": [{"source": "changeEvent", "detail": "the change was made in this repo"}]},
            tr,
        )
        org, repo = blast["sourceRepo"].split("/", 1)
        out.append({
            "org": org, "repo": repo, "team": src_entry.get("team"),
            "verdict": verdict, "rule": rule, "confidence": "high",
            "priority": priority_for(risk_class, "high"),
            "riskClass": risk_class, "isSourceRepo": True,
            "reasoning": f"[source repo] {reasoning}",
            # Same shape as every other verdict: consumers of this field iterate it.
            "evidence": {
                "blastRadius": [{"source": "changeEvent",
                                 "detail": "the change was made in this repo",
                                 "matchedChangedCode": True}],
                "testResult": tr and {k: tr.get(k) for k in (
                    "status", "failedTests", "liveSignal", "liveIntegrationNote", "notRunReason")},
            },
        })

    for r in blast.get("impactedRepos", []):
        key = f"{r['org']}/{r['repo']}".lower()
        tr = trs.get(key)
        verdict, reasoning, rule = decide(r, tr)
        m = teams.get(key, {})
        # §2: combine risk class (from §1 changesSummary) × blast confidence
        pri = priority_for(risk_class, r["confidence"])
        out.append({
            "org": r["org"], "repo": r["repo"], "team": m.get("team"),
            "verdict": verdict, "reasoning": reasoning, "rule": rule,
            "confidence": r["confidence"],
            # §2: priority now from risk×confidence matrix, not confidence alone
            "priority": pri,
            "riskClass": risk_class,  # carry through so the report can show it
            "evidence": {
                "blastRadius": r.get("evidence", []),
                "testResult": tr and {k: tr.get(k) for k in (
                    "status", "failedTests", "skippedReason", "hadLiveIntegrationSignal",
                    "liveSignal", "liveIntegrationNote", "durationSeconds", "logExcerpt",
                )},
            },
        })
    return validate_verdicts(out)
