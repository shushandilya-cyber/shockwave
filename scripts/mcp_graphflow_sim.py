"""Independent test of the Obsidian integration surface: drives the shockwave MCP server
exactly the way an Obsidian GraphFlow would (tool-by-tool, JSON passed between nodes),
over a real MCP transport, and diffs the result against the local CLI run.

  python scripts/mcp_graphflow_sim.py --org CoreShipping --repo waypointservice --commit 9d60af39 \
      [--url http://127.0.0.1:8765/mcp] [--expect runs/golden-skip/03-blast-radius.json] [--run-tests]
Without --url it spawns the server over stdio.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from fastmcp import Client


def _data(res):
    if getattr(res, "data", None) is not None:
        return res.data
    txt = "".join(getattr(c, "text", "") for c in res.content)
    return json.loads(txt) if txt else None


async def main(a):
    target = a.url or {"mcpServers": {"shockwave": {"command": str(Path(sys.executable).parent / "shockwave-mcp"), "args": []}}}
    async with Client(target, timeout=1800) as c:
        tools = sorted(t.name for t in await c.list_tools())
        print("tools:", tools)
        call = lambda name, **kw: c.call_tool(name, kw)
        ev = _data(await call("ingest_change_event", raw_event={"eventContext": {"payload": {
            "org": a.org, "repo": a.repo, "branch": "staging", "commit": a.commit, "environment": "staging"}}}))
        print("[1] ChangeEvent", ev["org"], ev["repo"], ev["commit"])
        cs = _data(await call("extract_changed_symbols", change_event=ev))
        print("[2] ChangedSymbols", len(cs["changedSymbols"]), "symbols")
        br = _data(await call("compute_blast_radius", changed_symbols=cs, local_scan=True))
        print("[3] BlastRadius", len(br["impactedRepos"]), "repos; notIndexed", br["notIndexed"], "notes", br["notes"])
        if br["notIndexed"] or not br["impactedRepos"]:
            print("terminal no-op node (Story 8 rule)"); return 0
        man = _data(await call("resolve_impacted_repos", blast_radius=br))
        print("[4] Manifest", [(e["repo"], e["team"], e["runnable"]) for e in man["entries"]][:6], "...")
        entries = [e for e in man["entries"] if e["repo"] in (a.only or [e["repo"]])] if a.only else man["entries"]
        if a.run_tests:
            sem = asyncio.Semaphore(4)

            async def one(e):
                async with sem:
                    return _data(await call("run_downstream_tests", manifest_entry=e, source_apps=br["sourceApps"]))
            trs = await asyncio.gather(*(one(e) for e in entries))   # fan-out
        else:
            trs = [{"org": e["org"], "repo": e["repo"], "status": "SKIPPED", "hadLiveIntegrationSignal": False,
                    "skippedReason": "tests disabled in simulation", "failedTests": []} for e in entries]
        print("[5] TestResults", [(t["repo"], t["status"]) for t in trs])
        vs = _data(await call("aggregate_verdicts", blast_radius=br, test_results=trs, manifest=man))  # fan-in
        if a.only:
            vs = [v for v in vs if v["repo"] in a.only]
        print("[6] Verdicts", [(v["repo"], v["verdict"]) for v in vs])
        tks = []
        for v in vs:
            if v["verdict"] != "NotImpacted":
                t = _data(await call("render_jira_story", verdict=v, blast_radius=br, change_event=ev))
                tks.append({k: t[k] for k in ("repo", "summary", "project", "dryRun", "action")})
        print("[7] Jira (dry-run)", len(tks), "payloads; e.g.", tks[0]["summary"] if tks else None)
        print(_data(await call("summarize", verdicts=vs, tickets=tks, source_repo=br["sourceRepo"], commit=br["commit"])))
        if a.expect:
            Path("/tmp/sw").mkdir(exist_ok=True)
            Path("/tmp/sw/03-via-mcp.json").write_text(json.dumps(br))
            import subprocess
            return subprocess.call([sys.executable, str(Path(__file__).parent / "compare_blast.py"), a.expect, "/tmp/sw/03-via-mcp.json"])
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", required=True); ap.add_argument("--repo", required=True); ap.add_argument("--commit", required=True)
    ap.add_argument("--url"); ap.add_argument("--expect"); ap.add_argument("--run-tests", action="store_true")
    ap.add_argument("--only", action="append")
    sys.exit(asyncio.run(main(ap.parse_args())))

