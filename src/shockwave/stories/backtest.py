"""§4 Backtest harness — shockwave backtest --incidents incidents.yaml.

Runs the pipeline at the culprit commit of each documented incident (read-only;
Jira is always dry-run) and scores:
  - Recall:    every victim repo present in impactedRepos? At what confidence/priority?
  - Precision: how many non-victims were flagged High?
  - Risk class: did changesSummary classify the culprit correctly?
  - Live signal: would the live signal have fired (e.g. PudoIntegrationTests)?
  - Time:      pipeline duration.

Reads incidents from a YAML file (schema in incidents.yaml).

Output:
  {runDir}/backtest-results.json
  docs/backtest/pudo-incidents.md   (written if --write-docs)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

from ..config import CONFIG, Config
from ..contracts import priority_for


@dataclass
class IncidentResult:
    incident_id: str
    title: str
    culprit_repo: str
    culprit_commit: str
    victim_repos: list[str]
    victims_verified: bool = True
    verification: str | None = None

    # pipeline outputs
    impacted_repos: list[str] = field(default_factory=list)
    victims_found: list[dict] = field(default_factory=list)   # [{repo, confidence, priority}]
    victims_missed: list[str] = field(default_factory=list)

    # scoring
    recall: float = 0.0           # victims found / total victims
    precision: float | None = None # confirmed-victim high / all high (None if no high)
    unconfirmed_high: list[str] = field(default_factory=list)
    risk_class_correct: bool | None = None
    expected_risk_class: str | None = None
    actual_risk_class: str | None = None
    live_signal_note: str | None = None
    duration_s: float = 0.0

    # coverage gaps
    source_indexed: bool | None = None
    miss_reasons: list[dict] = field(default_factory=list)   # [{repo, category, detail}]
    pipeline_notes: list[str] = field(default_factory=list)

    # raw artifacts paths
    run_dir: str | None = None


# Miss categories. Each maps to exactly one fix, which is what makes the ranked fix
# list in the generated doc derivable instead of hand-written.
MISS_SOURCE_NOT_INDEXED = "source repo not indexed in Code Knowledge"
MISS_VICTIM_NOT_INDEXED = "victim repo not indexed in Code Knowledge"
MISS_DEMOTED_TO_CONTEXT = "victim demoted to contextRepos (non-code or additive-only change)"
MISS_SELF_IMPACT = "victim is the culprit repo itself (out of scope: blast radius is downstream-only)"
MISS_NO_PATH_FOUND = "both repos indexed but no call path, dependency or host reference was found"

FIX_FOR_MISS = {
    MISS_SOURCE_NOT_INDEXED: "Index the culprit repo/branch in Code Knowledge",
    MISS_VICTIM_NOT_INDEXED: "Index the victim repo/branch in Code Knowledge",
    MISS_DEMOTED_TO_CONTEXT: "Map config/data changes to the consumers of the data they change",
    MISS_SELF_IMPACT: "Report self-impact separately (the source repo's own tests)",
    MISS_NO_PATH_FOUND: "Add a host/service-name based consumer signal for HTTP-only callers",
}


def _diagnose_miss(victim: str, culprit_repo: str, br: dict, context: set[str], cfg: Config) -> dict:
    """Why was this victim not in the blast radius? Checked, not guessed.

    Returns {repo, category, detail} so the ranked fix list can be aggregated by category.
    """
    if victim.lower() == (culprit_repo or "").lower():
        return {"repo": victim, "category": MISS_SELF_IMPACT,
                "detail": "culprit and victim are the same repo"}
    if br.get("notIndexed"):
        return {"repo": victim, "category": MISS_SOURCE_NOT_INDEXED,
                "detail": f"{br.get('sourceRepo')} has no entry in Code Knowledge, so no query could be run"}
    if victim.lower() in context:
        return {"repo": victim, "category": MISS_DEMOTED_TO_CONTEXT,
                "detail": "found as a consumer but demoted to contextRepos because no existing "
                          "code symbol or endpoint changed"}
    # Distinguish "we cannot see the victim" from "we looked and found nothing".
    try:
        from ..clients.knowledge import KnowledgeClient
        org, repo = victim.split("/", 1)
        if not KnowledgeClient(cfg).indexed_ref(org, repo):
            return {"repo": victim, "category": MISS_VICTIM_NOT_INDEXED,
                    "detail": f"{victim} has no entry in Code Knowledge, so no inbound edge to it can exist"}
    except Exception as e:
        return {"repo": victim, "category": MISS_NO_PATH_FOUND,
                "detail": f"index check for {victim} failed: {e}"}
    return {"repo": victim, "category": MISS_NO_PATH_FOUND,
            "detail": f"{victim} is indexed but no CALLS edge, repo dependency, OpenAPI chain "
                      f"or local import was found linking it to the changed code"}


def _load_incidents(path: Path) -> list[dict]:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get("incidents", [])


def backtest_incident(
    inc: dict,
    cfg: Config = CONFIG,
    run_dir_root: Path | None = None,
    skip_tests: bool = True,
) -> IncidentResult:
    """Run the pipeline at the incident's culprit commit and score it.

    skip_tests defaults to True for backtest so we don't need build toolchains
    for every incident — the live signal is assessed from static scan only.
    """
    from .. import pipeline as _pipeline

    inc_id = inc.get("id", "?")
    title = inc.get("title", "")
    culprit_repo = inc.get("culprit_repo", "")
    culprit_commit = inc.get("culprit_commit", "")
    # Keep the recorded casing: Code Knowledge lookups are case-sensitive, so comparing
    # lower-cased names is fine but querying with them is not.
    victim_repos = list(inc.get("victim_repos") or [])
    victims_verified = bool(inc.get("victims_verified", True))
    verification = (inc.get("verification") or "").strip() or None
    expected_risk = inc.get("expected_risk_class")

    if not culprit_repo or not culprit_commit or culprit_commit == "unknown":
        return IncidentResult(
            incident_id=inc_id, title=title, culprit_repo=culprit_repo,
            culprit_commit=culprit_commit, victim_repos=victim_repos,
            victims_verified=victims_verified, verification=verification,
            pipeline_notes=["culprit commit unknown — incident recorded but not backtestable"],
        )

    org, repo = culprit_repo.split("/", 1) if "/" in culprit_repo else ("?", culprit_repo)

    run_dir = None
    if run_dir_root:
        run_dir = run_dir_root / f"backtest-{inc_id}-{culprit_commit[:8]}"
        run_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    try:
        result = _pipeline.run(
            org=org, repo=repo, commit=culprit_commit,
            branch=inc.get("culprit_branch", "main"),
            skip_tests=skip_tests,
            create_jira=False,
            local_scan=True,
            run_dir=str(run_dir) if run_dir else None,
            cfg=cfg,
        )
        run_dir_path = result.get("runDir")
    except Exception as e:
        return IncidentResult(
            incident_id=inc_id, title=title, culprit_repo=culprit_repo,
            culprit_commit=culprit_commit, victim_repos=victim_repos,
            victims_verified=victims_verified, verification=verification,
            duration_s=round(time.time() - t0, 1),
            pipeline_notes=[f"pipeline raised {type(e).__name__}: {e}"],
        )

    duration = round(time.time() - t0, 1)

    # ── load artifacts ────────────────────────────────────────────────────────
    d = Path(run_dir_path)
    br = json.loads((d / "03-blast-radius.json").read_text()) if (d / "03-blast-radius.json").exists() else {}
    cs_artifact = json.loads((d / "02-changed-symbols.json").read_text()) if (d / "02-changed-symbols.json").exists() else {}
    changes_summary = cs_artifact.get("changesSummary") or {}
    actual_risk = changes_summary.get("overallRiskClass")

    impacted = [f"{r['org']}/{r['repo']}".lower() for r in br.get("impactedRepos", [])]
    indexed = not br.get("notIndexed", True)
    notes = br.get("notes", [])

    # ── recall scoring ────────────────────────────────────────────────────────
    context = {c.lower() for c in (br.get("contextRepos") or [])}
    # The source repo is not in impactedRepos (that list is downstream-only) but it is
    # evaluated and gets a verdict, so a self-impact victim counts as found.
    verdicts = json.loads((d / "06-verdicts.json").read_text()) if (d / "06-verdicts.json").exists() else []
    src_verdict = next((v for v in verdicts if v.get("isSourceRepo")), None)
    src_key = (culprit_repo or "").lower()

    found, missed, miss_reasons = [], [], []
    for v in victim_repos:
        vl = v.lower()
        match = next((r for r in br.get("impactedRepos", []) if f"{r['org']}/{r['repo']}".lower() == vl), None)
        if match is None and src_verdict and vl == src_key:
            found.append({
                "repo": v, "confidence": "high",
                "priority": src_verdict.get("priority"),
                "evidence": [f"source repo evaluated directly: verdict {src_verdict['verdict']} "
                             f"(rule {src_verdict.get('rule')})"],
            })
            continue
        if match:
            found.append({
                "repo": v,
                "confidence": match["confidence"],
                "priority": priority_for(actual_risk, match["confidence"]),
                "evidence": [e.get("detail") for e in (match.get("evidence") or [])][:3],
            })
        else:
            missed.append(v)
            miss_reasons.append(_diagnose_miss(v, culprit_repo, br, context, cfg))

    recall = len(found) / len(victim_repos) if victim_repos else 1.0

    # ── precision ─────────────────────────────────────────────────────────────
    # An incident record lists only the victims we could evidence. A high-confidence repo
    # that is not on that list is therefore UNCONFIRMED, not proven wrong: nobody went
    # back and checked whether it was also affected. Reporting it as a false positive
    # would overstate what we know, so it is counted and labelled separately.
    high_repos = [r for r in br.get("impactedRepos", []) if r["confidence"] == "high"]
    victim_set = {v.lower() for v in victim_repos}
    confirmed_high = [r for r in high_repos if f"{r['org']}/{r['repo']}".lower() in victim_set]
    unconfirmed_high = [f"{r['org']}/{r['repo']}" for r in high_repos
                        if f"{r['org']}/{r['repo']}".lower() not in victim_set]
    precision = (len(confirmed_high) / len(high_repos)) if high_repos else None

    # ── risk class check ──────────────────────────────────────────────────────
    risk_correct = None
    if expected_risk and actual_risk:
        risk_correct = (actual_risk == expected_risk)

    # ── live signal check ─────────────────────────────────────────────────────
    trs = json.loads((d / "05-test-results.json").read_text()) if (d / "05-test-results.json").exists() else []
    live_notes = []
    it_repos: set[str] = set()
    for tr in trs:
        ls = tr.get("liveSignal", "NONE")
        if ls in ("LIVE", "MOCKED"):
            live_notes.append(f"{tr['org']}/{tr['repo']}: {ls}")
        # every result repeats the same discovered list; collect it once
        it_repos.update(tr.get("integrationTestRepos") or [])
    if it_repos:
        live_notes.append(f"integration-test repos discovered: {', '.join(sorted(it_repos))}")
    live_signal_note = "; ".join(live_notes) if live_notes else "NONE — no live-signal tests found"

    return IncidentResult(
        incident_id=inc_id, title=title, culprit_repo=culprit_repo,
        culprit_commit=culprit_commit, victim_repos=victim_repos,
        victims_verified=victims_verified, verification=verification,
        impacted_repos=impacted, victims_found=found, victims_missed=missed,
        recall=recall, precision=precision, unconfirmed_high=unconfirmed_high,
        risk_class_correct=risk_correct, expected_risk_class=expected_risk, actual_risk_class=actual_risk,
        live_signal_note=live_signal_note,
        duration_s=duration, source_indexed=indexed,
        miss_reasons=miss_reasons,
        pipeline_notes=notes[:10],
        run_dir=run_dir_path,
    )


def run_backtest(
    incidents_path: Path,
    cfg: Config = CONFIG,
    run_dir_root: Path | None = None,
    skip_tests: bool = True,
    write_docs: bool = True,
) -> dict[str, Any]:
    incidents = _load_incidents(incidents_path)
    results: list[IncidentResult] = []

    for inc in incidents:
        print(f"  backtesting {inc.get('id')} — {inc.get('title', '')[:60]} ...")
        r = backtest_incident(inc, cfg, run_dir_root, skip_tests)
        results.append(r)

    # ── aggregate stats ───────────────────────────────────────────────────────
    # Incidents with no backtestable culprit commit are excluded from the rates rather
    # than counted as failures: we never ran the tool on them, so they are not evidence
    # either way. They are still listed in the doc.
    # A crashed run is also excluded: it says nothing about detection quality, only that
    # the harness broke. It is surfaced loudly instead of being averaged in as a miss.
    errored = [r for r in results
               if any(n.startswith("pipeline raised") for n in r.pipeline_notes)]
    errored_ids = {r.incident_id for r in errored}
    scorable = [r for r in results
                if r.culprit_commit and r.culprit_commit != "unknown"
                and r.incident_id not in errored_ids]
    # Recall is only meaningful where the victim list itself is corroborated. Averaging in
    # an unverified list would let bad ground truth masquerade as a tool failure.
    verified = [r for r in scorable if r.victims_verified]
    unverified = [r for r in scorable if not r.victims_verified]
    total = len(results)
    caught = sum(1 for r in verified if r.recall == 1.0)
    avg_recall = sum(r.recall for r in verified) / len(verified) if verified else 0.0
    precisions = [r.precision for r in scorable if r.precision is not None]
    avg_precision = sum(precisions) / len(precisions) if precisions else None
    risk_scored = [r for r in scorable if r.risk_class_correct is not None]
    risk_correct = sum(1 for r in risk_scored if r.risk_class_correct)

    summary = {
        "total_incidents": total,
        "scorable_incidents": len(scorable),
        "verified_incidents": len(verified),
        "unverified_incidents": [r.incident_id for r in unverified],
        "not_backtestable": [r.incident_id for r in results
                             if (not r.culprit_commit or r.culprit_commit == "unknown")],
        "pipeline_errors": [{"id": r.incident_id, "error": r.pipeline_notes[0]} for r in errored],
        "fully_caught": caught,
        "avg_recall": round(avg_recall, 2),
        "avg_precision_of_high": round(avg_precision, 2) if avg_precision is not None else None,
        "risk_class_correct": f"{risk_correct}/{len(risk_scored)}" if risk_scored else "n/a",
        "misses": [(r.incident_id, r.victims_missed) for r in results if r.victims_missed],
        "ranked_fixes": rank_fixes(results),
        "results": [asdict(r) for r in results],
    }

    # ── write docs ────────────────────────────────────────────────────────────
    if write_docs:
        docs_path = Path("docs/backtest/pudo-incidents.md")
        docs_path.parent.mkdir(parents=True, exist_ok=True)
        docs_path.write_text(_render_incidents_md(results, summary))
        print(f"  wrote {docs_path}")

    return summary


def rank_fixes(results: list[IncidentResult]) -> list[dict]:
    """Rank fixes by how many missed victims each would actually recover.

    Derived from the diagnosed miss categories, never hand-written: a fix list that is
    not tied to observed misses is an opinion, and re-running the backtest would not
    change it.
    """
    by_cat: dict[str, dict] = {}
    for r in results:
        for m in r.miss_reasons:
            cat = m["category"]
            e = by_cat.setdefault(cat, {
                "category": cat, "fix": FIX_FOR_MISS.get(cat, "unclassified"),
                "victimsRecovered": 0, "incidents": [], "examples": [],
                "fromVerifiedIncidentsOnly": True,
            })
            e["victimsRecovered"] += 1
            if not r.victims_verified:
                e["fromVerifiedIncidentsOnly"] = False
            if r.incident_id not in e["incidents"]:
                e["incidents"].append(r.incident_id)
            if len(e["examples"]) < 3:
                e["examples"].append(f"{r.incident_id}: {m['repo']} — {m['detail']}")
    return sorted(
        by_cat.values(),
        key=lambda e: (-e["victimsRecovered"], -len(e["incidents"]), e["category"]),
    )


def _render_incidents_md(results: list[IncidentResult], summary: dict) -> str:
    prec = summary.get("avg_precision_of_high")
    L = [
        "# PUDO Blast-Radius Backtest — Incident Analysis",
        "",
        "Auto-generated by `shockwave backtest`. Do not edit manually.",
        "",
        "Each incident is replayed by running the pipeline at the **culprit commit** "
        "(read-only; Jira stays dry-run) and comparing the blast radius against the "
        "victims recorded in `incidents.yaml`.",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Incidents recorded | {summary['total_incidents']} |",
        f"| Backtestable (known culprit commit) | {summary['scorable_incidents']} |",
        f"| …of those, with a **verified** victim list | {summary['verified_incidents']} |",
        f"| Fully caught (verified incidents only) | {summary['fully_caught']}/{summary['verified_incidents']} |",
        f"| Avg recall (verified incidents only) | {summary['avg_recall']:.0%} |",
        f"| Avg precision of High | {f'{prec:.0%}' if prec is not None else 'n/a'} |",
        f"| Risk class classified as expected | {summary['risk_class_correct']} |",
        "",
        "**Reading precision:** an incident record lists only victims we could evidence. "
        "A High-confidence repo that is not on that list is *unconfirmed*, not a proven "
        "false positive — nobody went back to check whether it was affected too. The "
        "precision figure is therefore a lower bound.",
        "",
    ]
    if summary.get("not_backtestable"):
        L += [f"**Not backtestable:** {', '.join(summary['not_backtestable'])} "
              f"(no known culprit commit — excluded from the rates above, detailed below).", ""]
    if summary.get("unverified_incidents"):
        L += [f"**Unverified victim lists:** {', '.join(summary['unverified_incidents'])} — "
              f"excluded from the recall figure. The recorded victims could not be "
              f"corroborated from code or Jira, so a miss there says nothing about the "
              f"tool. See each incident's `verification` note below.", ""]
    if summary.get("pipeline_errors"):
        L += ["**Pipeline errors** (excluded from the rates — a crash is a harness bug, "
              "not a detection miss):", ""]
        for e in summary["pipeline_errors"]:
            L.append(f"- `{e['id']}`: {e['error'][:200]}")
        L.append("")

    L += [
        "## Per-Incident Results",
        "",
        "| ID | Title | Culprit | Victims found | Recall | Risk class | Live signal |",
        "|---|---|---|---|---|---|---|",
    ]

    errored = {r.incident_id for r in results
               if any(n.startswith("pipeline raised") for n in r.pipeline_notes)}
    for r in results:
        if not r.culprit_commit or r.culprit_commit == "unknown":
            L.append(f"| {r.incident_id} | {r.title[:40]} | _unknown_ | — | n/a | — | — |")
            continue
        if r.incident_id in errored:
            L.append(f"| {r.incident_id} | {r.title[:40]} | `{r.culprit_commit[:8]}` "
                     f"| _pipeline error_ | — | — | — |")
            continue
        caught = (f"{len(r.victims_found)}/{len(r.victim_repos)}"
                  if r.victims_found else ("none" if r.victim_repos else "—"))
        risk_ok = (
            f"{'yes' if r.risk_class_correct else 'NO'} ({r.actual_risk_class})"
            if r.risk_class_correct is not None
            else r.actual_risk_class or "—"
        )
        live = "LIVE" if "LIVE" in (r.live_signal_note or "") else (
            "MOCKED" if "MOCKED" in (r.live_signal_note or "") else "NONE")
        L.append(
            f"| {r.incident_id} | {r.title[:40]} | `{r.culprit_commit[:8]}` "
            f"| {caught} | {r.recall:.0%} | {risk_ok} | {live} |"
        )

    L += ["", "## Per-Incident Detail", ""]
    for r in results:
        L += [
            f"### {r.incident_id} — {r.title}",
            "",
            f"- **Culprit:** `{r.culprit_repo}@{r.culprit_commit[:8]}`",
            f"- **Source indexed:** {r.source_indexed}",
            f"- **Expected risk class:** {r.expected_risk_class or '—'}",
            f"- **Actual risk class:** {r.actual_risk_class or '—'}",
            f"- **Recall:** {r.recall:.0%}  ({len(r.victims_found)}/{len(r.victim_repos)} victims found)",
        ]
        if not r.victims_verified:
            L += ["- **Victim list NOT verified — excluded from the headline recall.**",
                  f"  > {r.verification or 'no verification note recorded'}"]
        if r.victims_found:
            L.append("- **Victims found in blast radius:**")
            for v in r.victims_found:
                L.append(f"  - `{v['repo']}` confidence={v['confidence']} priority={v['priority']}")
        if r.unconfirmed_high:
            L.append(f"- **Also flagged High (unconfirmed, not proven wrong):** "
                     f"{', '.join(f'`{x}`' for x in r.unconfirmed_high[:8])}")
        if r.victims_missed:
            L.append("- **Victims MISSED:**")
            for m in r.miss_reasons:
                L.append(f"  - `{m['repo']}` — **{m['category']}**: {m['detail']}")
        if r.pipeline_notes:
            L.append("- **Pipeline notes:**")
            for n in r.pipeline_notes[:5]:
                L.append(f"  - {n}")
        L += [f"- **Live signal:** {r.live_signal_note or 'NONE'}", ""]

    ranked = summary.get("ranked_fixes") or []
    L += [
        "## Fixes, ranked by missed victims recovered",
        "",
    ]
    if not ranked:
        L += ["No victims were missed in this run, so there is nothing to rank.", ""]
    else:
        L += ["| # | Fix | Victims recovered | Incidents | Ground truth |",
              "|---|---|---|---|---|"]
        for i, f in enumerate(ranked, 1):
            gt = "verified" if f.get("fromVerifiedIncidentsOnly") else "**unverified**"
            L.append(f"| {i} | {f['fix']} | {f['victimsRecovered']} | {', '.join(f['incidents'])} | {gt} |")
        L += ["", "A fix backed only by **unverified** incidents is not yet actionable: the "
                  "misses it would recover are misses against a victim list we could not "
                  "corroborate. Confirm the victims first, then act.", ""]
        for i, f in enumerate(ranked, 1):
            gt = ("" if f.get("fromVerifiedIncidentsOnly")
                  else " **Backed only by unverified incidents — confirm the victim list first.**")
            L += [f"**{i}. {f['fix']}**", "",
                  f"Diagnosis: _{f['category']}_ — would recover {f['victimsRecovered']} missed "
                  f"victim(s) across {', '.join(f['incidents'])}.{gt}", ""]
            for ex in f["examples"]:
                L.append(f"- {ex}")
            L.append("")

    return "\n".join(L) + "\n"
