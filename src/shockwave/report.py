"""Markdown report for a run directory (also the Story 9 PR/Slack summary body)."""
from __future__ import annotations

import json
from pathlib import Path


def _j(d: Path, name: str, default=None):
    p = d / name
    return json.loads(p.read_text()) if p.exists() else default


def summary_table(verdicts: list[dict], tickets: list[dict], source: str, commit: str) -> str:
    tk = {t.get("repo", "").lower(): t for t in tickets or []}
    rows = [f"### Blast Radius Check — {source}@{commit[:8]}", "", "| Repo | Confidence | Verdict | Ticket |", "|---|---|---|---|"]
    for v in verdicts:
        t = tk.get(f"{v['org']}/{v['repo']}".lower())
        tick = (t.get("key") or ("dry-run" if t.get("dryRun") else t.get("action", "—"))) if t else "—"
        rows.append(f"| {v['org']}/{v['repo']} | {v.get('confidence', '—')} | {v['verdict']} | {tick} |")
    return "\n".join(rows)


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

    L += ["## 1. What changed", f"{len(cs.get('changedFiles', []))} files, {len(cs.get('changedSymbols', []))} symbols, "
          f"{len(cs.get('changedApiEndpoints', []))} endpoints.", ""]
    for s in cs.get("changedSymbols", [])[:30]:
        L.append(f"- `{s['changeType']}` {s['kind']} `{s['name']}`")
    for e in cs.get("changedApiEndpoints", []):
        L.append(f"- endpoint `{e['method']} {e['path']}`")
    for n in cs.get("notes", []):
        L.append(f"- _note: {n}_")

    L += ["", "## 2. Blast radius", f"Indexed as `{br.get('indexedRef')}` · resolved symbols: {len(br.get('resolvedSymbols', []))} · "
          f"transitive internal callers: {br.get('transitiveCallerCount', 0)} · tool calls: {br.get('toolCalls')}", "",
          "| # | Repo | Confidence | Sources | Key evidence |", "|---|---|---|---|---|"]
    for i, r in enumerate(br.get("impactedRepos", []), 1):
        ev0 = next((e for e in r["evidence"] if e.get("matchedChangedCode") or e["source"] == "graphSearch"), r["evidence"][0])
        srcs = ", ".join(sorted({e["source"] for e in r["evidence"]}))
        L.append(f"| {i} | {r['org']}/{r['repo']} | **{r['confidence']}** | {srcs} | {ev0['detail'].replace('|', '/')[:160]} |")
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
        L += ["", "## 3. Owners & runnability", "| Repo | Team | Build | Runnable | Branch | Access |", "|---|---|---|---|---|---|"]
        for e in man["entries"]:
            L.append(f"| {e['org']}/{e['repo']} | {e.get('team') or '_unassigned_'} | {e['buildTool']} | "
                     f"{'yes' if e['runnable'] else 'no — ' + (e.get('notRunnableReason') or '')} | {e.get('defaultBranch') or '?'} | {e.get('repoAccess')} |")
    if trs:
        L += ["", "## 4. Tests", "| Repo | Status | Live signal | Duration | Detail |", "|---|---|---|---|---|"]
        for t in trs:
            det = t.get("skippedReason") or (", ".join(t.get("failedTests", [])[:3]) or f"{t.get('testsRun')} tests")
            L.append(f"| {t['org']}/{t['repo']} | {t['status']} | {'yes' if t['hadLiveIntegrationSignal'] else 'no'} | {t['durationSeconds']}s | {str(det)[:120]} |")
    if vs:
        L += ["", "## 5. Verdicts & tickets", "", summary_table(vs, tk, src, commit), ""]
        for v in vs:
            L.append(f"- **{v['org']}/{v['repo']}** → {v['verdict']} (rule {v.get('rule')}): {v['reasoning'][:400]}")
    if tk:
        L += ["", f"Jira mode: {'DRY-RUN (payloads in 07-jira.json)' if all(t.get('dryRun') for t in tk) else 'LIVE'}"]
        for t in tk:
            if t.get("error"):
                L.append(f"- {t.get('repo')}: {t.get('action')} — {t['error']}")
    return "\n".join(L) + "\n"

