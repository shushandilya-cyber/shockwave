"""Story 6 — Impact Verdict Aggregation (pure logic, no I/O).

Decision table (first match wins) — intentionally conservative:
  1. source notIndexed                         -> NeedsReview (source repo only)
  2. FAIL  + live integration signal           -> Impacted
  3. PASS  + live integration signal           -> NotImpacted
  4. FAIL/ERROR + no live signal               -> NeedsReview
  5. SKIPPED (or no TestResult)                -> NeedsReview
  6. PASS  + no live signal                    -> NeedsReview   (passing unrelated tests proves nothing)
"""
from __future__ import annotations

from ..contracts import validate_verdicts

_PRIORITY = {"high": "P1", "medium": "P2", "low": "P3"}


def _ev_summary(repo_entry: dict | None) -> str:
    if not repo_entry:
        return "no blast-radius evidence"
    ev = repo_entry.get("evidence", [])
    srcs = sorted({e["source"] for e in ev})
    top = next((e for e in ev if e.get("matchedChangedCode") or e["source"] == "graphSearch"), ev[0] if ev else None)
    return f"confidence={repo_entry['confidence']} via {', '.join(srcs)}" + (f"; e.g. {top['detail']}" if top else "")


def decide(blast_entry: dict | None, tr: dict | None) -> tuple[str, str, int]:
    """-> (verdict, reasoning, rule_number)"""
    conf = (blast_entry or {}).get("confidence", "low")
    br = _ev_summary(blast_entry)
    if tr is None:
        return "NeedsReview", f"No test result was produced for this repo. Blast radius: {br}.", 5
    status, live = tr["status"], tr.get("hadLiveIntegrationSignal", False)
    failed = ", ".join(tr.get("failedTests", [])[:10]) or "n/a"
    if status == "FAIL" and live:
        return "Impacted", (f"Tests that exercise the changed service live FAILED ({failed}). "
                            f"{tr.get('liveIntegrationNote') or ''} Blast radius: {br}."), 2
    if status == "PASS" and live:
        return "NotImpacted", (f"Live-integration tests against the changed service PASSED. "
                               f"{tr.get('liveIntegrationNote') or ''}"), 3
    if status in ("FAIL", "ERROR"):
        what = f"tests failed ({failed})" if status == "FAIL" else "the build/test run errored (compile/env/timeout)"
        return "NeedsReview", (f"{what[0].upper() + what[1:]}, but none of them are known to exercise the changed "
                               f"service directly — failure may be unrelated. Needs human judgment. Blast radius: {br}."), 4
    if status == "SKIPPED":
        return "NeedsReview", f"Tests not run: {tr.get('skippedReason') or 'skipped'}. Blast radius: {br}.", 5
    return "NeedsReview", (f"Tests passed but none exercise the changed service directly — cannot confirm no impact "
                           f"(blast-radius confidence: {conf}). Blast radius: {br}."), 6


def aggregate(blast: dict, test_results: list[dict] | None, manifest: dict | None = None) -> list[dict]:
    test_results = test_results or []
    teams = {f"{e['org']}/{e['repo']}".lower(): e for e in (manifest or {}).get("entries", [])}
    if blast.get("notIndexed"):
        org, repo = blast["sourceRepo"].split("/", 1)
        hints = blast.get("notIndexedHints") or []
        return validate_verdicts([{
            "org": org, "repo": repo, "team": None, "verdict": "NeedsReview", "rule": 1, "priority": "P2",
            "reasoning": "Source repo not registered in Code Knowledge, blast radius could not be computed."
                         + (f" Unverified repo-level hints: {', '.join(hints)}." if hints else ""),
            "evidence": {"blastRadius": "notIndexed", "testResult": None}, "isSourceRepo": True,
        }])
    trs = {f"{t['org']}/{t['repo']}".lower(): t for t in test_results}
    out = []
    for r in blast.get("impactedRepos", []):
        key = f"{r['org']}/{r['repo']}".lower()
        tr = trs.get(key)
        verdict, reasoning, rule = decide(r, tr)
        m = teams.get(key, {})
        out.append({
            "org": r["org"], "repo": r["repo"], "team": m.get("team"), "verdict": verdict, "reasoning": reasoning,
            "rule": rule, "confidence": r["confidence"], "priority": _PRIORITY[r["confidence"]],
            "evidence": {"blastRadius": r.get("evidence", []), "testResult": tr and {k: tr.get(k) for k in (
                "status", "failedTests", "skippedReason", "hadLiveIntegrationSignal", "liveIntegrationNote", "durationSeconds", "logExcerpt")}},
        })
    return validate_verdicts(out)

