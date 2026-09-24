"""Markdown report for a run directory (also the Story 9 PR/Slack summary body).

§2 additions: confidence legend, risk-class legend, priority matrix.
§1 additions: changesSummary section at the top of the report.
§3 additions: LIVE/MOCKED/NONE live-signal column in the test table.
"""
from __future__ import annotations

import json
from pathlib import Path

from .contracts import CONFIDENCE_LEGEND, PRIORITY_MATRIX


def _j(d: Path, name: str, default=None):
    p = d / name
    return json.loads(p.read_text()) if p.exists() else default


def summary_table(verdicts: list[dict], tickets: list[dict], source: str, commit: str) -> str:
    tk = {t.get("repo", "").lower(): t for t in tickets or []}
    rows = [f"### Blast Radius Check — {source}@{commit[:8]}", "",
            "| Repo | Confidence | Risk | Priority | Verdict | Ticket |",
            "|---|---|---|---|---|---|"]
    for v in verdicts:
        t = tk.get(f"{v['org']}/{v['repo']}".lower())
        tick = (t.get("key") or ("dry-run" if t.get("dryRun") else t.get("action", "—"))) if t else "—"
        risk = v.get("riskClass") or "—"
        rows.append(
            f"| {v['org']}/{v['repo']} | {v.get('confidence', '—')} | {risk} | {v.get('priority', '—')} "
            f"| {v['verdict']} | {tick} |"
        )
    return "\n".join(rows)


def _render_changes_summary(cs: dict) -> list[str]:
    """§1: render the changesSummary block."""
    if not cs:
        return []
    L = ["## Changes Summary", ""]
    L.append(f"**Intent:** {cs.get('intent') or '_no commit message_'}")
    L.append("")
    if cs.get("jiraKeys"):
        L.append(f"**Jira:** {', '.join(cs['jiraKeys'])}")
        L.append("")
    L.append(f"**Overall risk class:** `{cs.get('overallRiskClass', '—')}`")
    L.append("")
    groups = cs.get("groups") or {}
    group_labels = {
        "contractChanges": "Contract changes (endpoint/signature/DTO)",
        "behaviorChanges": "Behavior changes (logic inside existing exposed path)",
        "internalOnly": "Internal only (private/helper/test/logging)",
        "configDataOnly": "Config/data only (JSON/YAML/properties)",
    }
    if groups:
        L.append("| Group | Risk class | Why | Symbols |")
        L.append("|---|---|---|---|")
        for gkey, glabel in group_labels.items():
            g = groups.get(gkey)
            if not g:
                continue
            syms = ", ".join(f"`{s}`" for s in g.get("symbols", [])[:5])
            if len(g.get("symbols", [])) > 5:
                syms += f" … (+{len(g['symbols']) - 5} more)"
            L.append(f"| {glabel} | **{g['riskClass']}** | {g['reason'][:120]} | {syms} |")
    L.append("")
    eps = cs.get("exposedVia") or []
    if eps:
        L.append("**Exposed via:** endpoints the changed code is reachable from.")
        L.append("")
        for ep in eps[:10]:
            bits = [f"`{ep.get('method','')} {ep.get('path','')}`".strip()]
            if ep.get("operationId"):
                bits.append(f"({ep['operationId']})")
            if ep.get("targetMethod"):
                verb = "reaches" if ep.get("reachedVia") == "direct" else "transitively reaches"
                bits.append(f"— {verb} `{ep['targetMethod']}`")
            L.append("- " + " ".join(bits))
        if len(eps) > 10:
            L.append(f"- … and {len(eps) - 10} more")
        L.append("")
    return L


def _confidence_legend() -> list[str]:
    """§2: confidence legend and priority matrix."""
    L = [
        "## Legend",
        "",
        "### Blast-Radius Confidence",
        "",
        "Confidence = how strong the evidence is that a **code path in the consumer actually reaches the changed code**.",
        "It is **not** the likelihood of breakage — that depends on the risk class of the change.",
        "",
    ]
    for level, desc in CONFIDENCE_LEGEND.items():
        L.append(f"- **{level.capitalize()}:** {desc}")
    L.append("")
    L.append("### Risk Class × Confidence → Priority")
    L.append("")
    L.append("| Risk class | High | Medium | Unknown | Low |")
    L.append("|---|---|---|---|---|")
    for risk in ("BREAKING", "BEHAVIORAL", "SAFE"):
        cells = " | ".join(PRIORITY_MATRIX.get((risk, c), "P3") for c in ("high", "medium", "unknown", "low"))
        L.append(f"| {risk} | {cells} |")
    L.append("")
    L.append("**P1** = same-day investigation  **P2** = review within sprint  **P3** = awareness only")
    L.append("")
    L.append("Unknown outranks Low on purpose: Low means we looked and found no link to the changed code, "
             "whereas Unknown means we could not look at all.")
    L.append("")
    L.append("### Live Signal (§3)")
    L.append("")
    L.append("- **LIVE** — tests reference the changed service's staging host. A PASS means behaviour is unchanged; a FAIL is real evidence of breakage.")
    L.append("- **MOCKED** — integration-style tests mock the service (WireMock/Mockito). A PASS means the contract *shape* still matches; behaviour change is unverified.")
    L.append("- **NONE** — no tests reference the service. Test results are a generic sanity check only.")
    L.append("")
    return L


def render_run(d: Path) -> str:
    ev = _j(d, "01-change-event.json", {})
    cs = _j(d, "02-changed-symbols.json", {})
    br = _j(d, "03-blast-radius.json", {})
    man = _j(d, "04-manifest.json", {"entries": []})
    trs = _j(d, "05-test-results.json", [])
    vs = _j(d, "06-verdicts.json", [])
    tk = _j(d, "07-jira.json", [])
    meta = _j(d, "run-meta.json", {})
    src, commit = br.get("sourceRepo", f"{ev.get('org')}/{ev.get('repo')}"), br.get("commit", ev.get("commit", ""))

    L = [f"# Blast Radius Report — {src}@{commit[:8]}", ""]
    L += [f"- Triggered: {ev.get('triggeredAt')}  |  env: {ev.get('environment')}  |  knowledge backend: {meta.get('backend')}",
          f"- Timings (s): {meta.get('timings')}", ""]

    # §1: Changes Summary (top of report)
    changes_summary = cs.get("changesSummary")
    if changes_summary:
        L += _render_changes_summary(changes_summary)

    if br.get("notIndexed"):
        L += ["> **BLAST RADIUS UNKNOWN** — source repo is not registered in Code Knowledge. This is NOT 'no impact'.", ""]
        if br.get("notIndexedHints"):
            L += [f"> Unverified repo-level hints: {', '.join(br['notIndexedHints'])}", ""]
    elif not br.get("impactedRepos") and br.get("contextRepos"):
        L += ["> **Not impacted:** no existing code symbols or endpoints changed. "
              f"{len(br['contextRepos'])} known consumers listed below for awareness only.", "",
              f"> Consumers: {', '.join(br['contextRepos'])}", ""]
    elif not br.get("impactedRepos"):
        L += ["> Source is indexed and no downstream consumers were found (confirmed empty blast radius).", ""]

    L += [f"_{len(cs.get('changedFiles', []))} files, {len(cs.get('changedSymbols', []))} symbols, "
          f"{len(cs.get('changedApiEndpoints', []))} endpoints changed — "
          f"full list in the appendix._", ""]
    for n in cs.get("notes", []):
        L.append(f"- _note: {n}_")

    L += ["", "## Blast Radius", f"Indexed as `{br.get('indexedRef')}` · resolved symbols: {len(br.get('resolvedSymbols', []))} · "
          f"transitive internal callers: {br.get('transitiveCallerCount', 0)} · tool calls: {br.get('toolCalls')}", "",
          "| # | Repo | Confidence | Priority | Sources | Key evidence |",
          "|---|---|---|---|---|---|"]
    # §2: find risk class from changesSummary for each verdict's priority display
    risk_by_repo = {f"{v['org']}/{v['repo']}".lower(): v.get("riskClass") for v in vs}
    from .contracts import priority_for as _pf
    for i, r in enumerate(br.get("impactedRepos", []), 1):
        ev0 = next((e for e in r["evidence"] if e.get("matchedChangedCode") or e["source"] == "graphSearch"), r["evidence"][0])
        srcs = ", ".join(sorted({e["source"] for e in r["evidence"]}))
        key = f"{r['org']}/{r['repo']}".lower()
        risk = risk_by_repo.get(key) or changes_summary and changes_summary.get("overallRiskClass") or "—"
        pri = _pf(risk if risk != "—" else None, r["confidence"])
        L.append(f"| {i} | {r['org']}/{r['repo']} | **{r['confidence']}** | {pri} | {srcs} | {ev0['detail'].replace('|', '/')[:160]} |")

    if br.get("unresolvedSymbols"):
        L += ["", "**Graph blind spots (unresolved — NOT 'no callers'):**"] + [f"- `{s}`" for s in br["unresolvedSymbols"]]
    um = br.get("unmappedApiCallers") or []
    if um:
        hit = [u for u in um if u.get("matchedChangedCode")]
        L += ["", f"**API callers with no repo mapping:** {len(um)} ({len(hit)} reach changed code)"]
        for u in hit[:10]:
            L.append(f"- apps {u.get('apps')} → {u.get('api')}")
    if br.get("ignoredRepos"):
        L += ["", f"_Ignored infra repos: {', '.join(br['ignoredRepos'])}_"]
    for n in br.get("notes", []):
        L.append(f"- _note: {n}_")

    if man.get("entries"):
        L += ["", "## Owners & Runnability", "| Repo | Team | Build | Runnable | Branch | Access |", "|---|---|---|---|---|---|"]
        for e in man["entries"]:
            L.append(f"| {e['org']}/{e['repo']} | {e.get('team') or '_unassigned_'} | {e['buildTool']} | "
                     f"{'yes' if e['runnable'] else 'no — ' + (e.get('notRunnableReason') or '')} | {e.get('defaultBranch') or '?'} | {e.get('repoAccess')} |")

    if trs:
        # §3: show LIVE/MOCKED/NONE column
        L += ["", "## Tests", "| Repo | Status | Live Signal | Duration | Detail |", "|---|---|---|---|---|"]
        for t in trs:
            # prefer new liveSignal, fall back to bool
            ls = t.get("liveSignal") or ("LIVE" if t.get("hadLiveIntegrationSignal") else "NONE")
            status = t["status"]
            # flag NOT_RUN prominently
            status_display = f"**{status}**" if status in ("NOT_RUN", "FAIL") else status
            det = t.get("skippedReason") or t.get("notRunReason") or (", ".join(t.get("failedTests", [])[:3]) or f"{t.get('testsRun')} tests")
            L.append(f"| {t['org']}/{t['repo']} | {status_display} | {ls} | {t['durationSeconds']}s | {str(det)[:120]} |")
        # Show integration-test repos discovered (§3)
        it_repos = []
        for t in trs:
            if t.get("integrationTestRepos"):
                it_repos += t["integrationTestRepos"]
        if it_repos:
            L += ["", f"_Integration-test repos discovered: {', '.join(sorted(set(it_repos)))}_"]

    if vs:
        L += ["", "## Verdicts & Tickets", "", summary_table(vs, tk, src, commit), ""]
        for v in vs:
            L.append(f"- **{v['org']}/{v['repo']}** → {v['verdict']} (rule {v.get('rule')}, {v.get('priority')}): {v['reasoning'][:400]}")
    if tk:
        L += ["", f"Jira mode: {'DRY-RUN (payloads in 07-jira.json)' if all(t.get('dryRun') for t in tk) else 'LIVE'}"]
        for t in tk:
            if t.get("error"):
                L.append(f"- {t.get('repo')}: {t.get('action')} — {t['error']}")

    L += ["", "---"]
    L += _changed_symbols_appendix(cs)
    L += _confidence_legend()

    return "\n".join(L) + "\n"


def _changed_symbols_appendix(cs: dict) -> list[str]:
    """§1: the raw symbol list, collapsed. Nobody triages from it, but it has to stay
    available for whoever is actually reading the diff."""
    syms = cs.get("changedSymbols") or []
    eps = cs.get("changedApiEndpoints") or []
    files = cs.get("changedFiles") or []
    if not (syms or eps or files):
        return []
    L = ["## Appendix — raw changed symbols", "", "<details>",
         f"<summary>{len(files)} files, {len(syms)} symbols, {len(eps)} endpoints</summary>", ""]
    for e in eps:
        L.append(f"- endpoint `{e['method']} {e['path']}`")
    for s in syms:
        L.append(f"- `{s['changeType']}` {s['kind']} `{s['name']}`")
    L += ["", "**Files**", ""] + [f"- `{f}`" for f in files]
    L += ["", "</details>", ""]
    return L
