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

    L += _impact_summary(br, cs, vs)
    L += _breaking_changes(br, changes_summary or {})
    L += _per_repo_impact(br, vs)

    lc = br.get("localContext") or {}
    if br.get("notIndexed") and lc:
        L += [f"> **Code Knowledge has no index for {src}.** The blast radius below comes from the local context index "
              f"({lc.get('reposScanned')} repos at their default branch, source at `{lc.get('sourceRef')}`). "
              "Consumers outside that set are UNKNOWN, not unaffected.", ""]
        if br.get("notIndexedHints"):
            L += [f"> Unverified repo-level hints: {', '.join(br['notIndexedHints'])}", ""]
    elif br.get("notIndexed"):
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

    L += ["", "## Blast Radius", f"Coverage: **{br.get('coverage', '—')}** · indexed as `{br.get('indexedRef')}` · "
          f"resolved symbols: {len(br.get('resolvedSymbols', []))} · transitive internal callers: {br.get('transitiveCallerCount', 0)} · "
          f"local type closure: {lc.get('typeClosureSize', '—')} · tool calls: {br.get('toolCalls')}", "",
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
            at = f" @ `{t['commit'][:8]}`" if t.get("commit") else ""
            L.append(f"| {t['org']}/{t['repo']}{at} | {status_display} | {ls} | {t['durationSeconds']}s | {str(det)[:120]} |")
        for t in trs:
            bl = t.get("baseline")
            if not bl:
                continue
            L += ["", f"**Parent baseline for {t['org']}/{t['repo']}** — failing classes re-run at parent "
                      f"`{str(bl.get('commit'))[:8]}` (`{bl.get('command') or '-'}`): {bl.get('status')}"]
            if bl.get("reason"):
                L.append(f"- inconclusive: {bl['reason']}")
            else:
                L.append(f"- pre-existing (also fail at parent): {', '.join(bl.get('preExisting') or []) or 'none'}")
                L.append(f"- new at this commit: {', '.join(bl.get('newFailures') or []) or 'none'}")
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


_CONF_ORDER = {"high": 0, "medium": 1, "unknown": 2, "low": 3}


def _priority(risk: str | None, conf: str) -> str:
    from .contracts import priority_for
    return priority_for(risk, conf)


def _impact_summary(br: dict, cs: dict, vs: list[dict]) -> list[str]:
    """Item 3: the answer in five lines — how far it reaches, how bad, what to look at first."""
    summ = cs.get("changesSummary") or {}
    risk = summ.get("overallRiskClass")
    repos = br.get("impactedRepos") or []
    by_conf: dict[str, int] = {}
    for r in repos:
        by_conf[r["confidence"]] = by_conf.get(r["confidence"], 0) + 1
    pri: dict[str, list[str]] = {}
    for r in repos:
        pri.setdefault(_priority(risk, r["confidence"]), []).append(f"{r['org']}/{r['repo']}")
    src_v = next((v for v in vs if v.get("isSourceRepo")), None)
    L = ["## Impact Summary", ""]
    if br.get("coverage") == "none" or (br.get("notIndexed") and not br.get("localContext")):
        L += ["- **Reach: UNKNOWN** — neither Code Knowledge nor the local context index could look. Not 'no impact'.", ""]
        return L
    conf_txt = ", ".join(f"{n} {c}" for c, n in sorted(by_conf.items(), key=lambda x: _CONF_ORDER.get(x[0], 9)))
    L.append(f"- **Reach:** {len(repos)} downstream repo(s) impacted" + (f" ({conf_txt})" if repos else "")
             + f" · coverage `{br.get('coverage', '—')}`")
    L.append(f"- **Severity:** overall risk class `{risk or '—'}` · {len(summ.get('breakingChanges') or [])} breaking change(s) · "
             f"{len(summ.get('exposedVia') or br.get('exposedVia') or [])} exposed endpoint(s)")
    for p in ("P1", "P2", "P3"):
        if pri.get(p):
            L.append(f"- **{p}:** {', '.join(pri[p][:8])}" + (f" (+{len(pri[p]) - 8} more)" if len(pri[p]) > 8 else ""))
    if src_v:
        L.append(f"- **Source repo itself:** {src_v['verdict']} ({src_v.get('priority')}) — tests in the changed repo are the first line of defence")
    if not repos:
        L.append("- No downstream consumer references any changed code in the scanned repos.")
    L.append("")
    return L


def _hits_breaking(entry: dict, b: dict) -> list[dict]:
    """Evidence items in ``entry`` that reference breaking change ``b``."""
    out = []
    for e in entry.get("evidence", []):
        if b["symbol"] in (e.get("symbols") or []):
            out.append(e)
        elif b.get("path") and b["path"] in str(e.get("api", "")):
            out.append(e)
        elif b.get("className") and b.get("member") and str(e.get("targetFqn", "")).startswith(b["className"]) \
                and f".{b['member']}" in str(e.get("targetFqn", "")):
            out.append(e)
        elif b.get("className") and b["kind"] == "class" and b["className"] in str(e.get("targetFqn", "")):
            out.append(e)
    return out


def _breaking_changes(br: dict, summ: dict) -> list[str]:
    """Item 2: every BREAKING change, with the consumers that reference it."""
    items = summ.get("breakingChanges") or []
    L = ["## Breaking Changes", ""]
    if not items:
        L += [f"_None. Overall risk class is `{summ.get('overallRiskClass', '—')}`: nothing was removed or had its contract changed "
              "in a way existing callers depend on._", ""]
        return L
    L += ["| Change | Why it breaks | Where | Consumers that reference it |", "|---|---|---|---|"]
    for b in items:
        users = [f"{r['org']}/{r['repo']}" for r in br.get("impactedRepos", []) if _hits_breaking(r, b)]
        where = f"`{b['file']}:{b.get('line') or ''}`" if b.get("file") else "API spec"
        who = (", ".join(users) if users else f"every caller of this service in {b['environment']}"
               if b.get("kind") == "environment" else "_none found in graph or local context_")
        L.append(f"| `{b['symbol'][:90]}` | {b['reason'][:110].replace('|', '/')} | {where} | {who} |")
    L.append("")
    return L


def _per_repo_impact(br: dict, vs: list[dict]) -> list[str]:
    """Item 1: for each impacted repo, what exactly it uses and how we know."""
    repos = br.get("impactedRepos") or []
    if not repos:
        return []
    risk_by = {f"{v['org']}/{v['repo']}".lower(): v for v in vs}
    L = ["## What Is Impacted, Per Repo", ""]
    for r in sorted(repos, key=lambda r: _CONF_ORDER.get(r["confidence"], 9)):
        v = risk_by.get(f"{r['org']}/{r['repo']}".lower()) or {}
        L.append(f"### {r['org']}/{r['repo']} — {r['confidence']} confidence · {v.get('priority', '—')} · {v.get('verdict', '—')}")
        syms = sorted({s for e in r["evidence"] for s in (e.get("symbols") or [])} |
                      {e["targetFqn"] for e in r["evidence"] if e.get("targetFqn")})
        apis = sorted({e["api"] for e in r["evidence"] if e.get("api") and e.get("matchedChangedCode")})
        if syms:
            L.append(f"- **Changed code it uses:** " + ", ".join(f"`{s.split('/')[-1][:80]}`" for s in syms[:6])
                     + (f" (+{len(syms) - 6} more)" if len(syms) > 6 else ""))
        if apis:
            L.append(f"- **Endpoints it calls that reach the change:** " + ", ".join(f"`{a}`" for a in apis[:5]))
        for e in sorted(r["evidence"], key=lambda e: not e.get("matchedChangedCode"))[:5]:
            L.append(f"- `{e['source']}` — {e['detail'][:260]}")
        if len(r["evidence"]) > 5:
            L.append(f"- … {len(r['evidence']) - 5} more evidence item(s) in `03-blast-radius.json`")
        L.append("")
    return L


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
