"""End-to-end local orchestration (local equivalent of the Story 8 GraphFlow).

Writes every story's artifact to a run dir so any step can be re-run in isolation:
  01-change-event.json 02-changed-symbols.json 03-blast-radius.json
  04-manifest.json 05-test-results.json 06-verdicts.json 07-jira.json report.md
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .clients.knowledge import KnowledgeClient
from .clients.repos import RepoSource
from .config import CONFIG, Config
from .report import render_run
from .stories import (
    s1_trigger, s2_changes_summary, s2_diff, s3_blast_radius,
    s4_resolve, s5_runner, s6_verdict, s7_jira,
)


def _w(d: Path, name: str, obj):
    (d / name).write_text(json.dumps(obj, indent=2) + "\n")


def run(org: str, repo: str, commit: str, branch: str = "staging", pr: str | None = None, backend: str | None = None,
        run_dir: str | None = None, skip_tests: bool = False, create_jira: bool = False, local_scan: bool = True,
        only_repos: list[str] | None = None, project_override: str | None = None, cfg: Config = CONFIG,
        on_step=None) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = Path(run_dir or Path.cwd() / "runs" / f"{org}-{repo}-{commit[:8]}-{stamp}")
    d.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    rs = RepoSource(cfg)
    kc = KnowledgeClient(cfg, backend=backend)

    def step(name, fn):
        t = time.time()
        if on_step:
            on_step(name, "running")
        r = fn()
        timings[name] = round(time.time() - t, 1)
        if on_step:
            on_step(name, "done")
        return r

    ev = step("1-trigger", lambda: s1_trigger.ingest({"org": org, "repo": repo, "branch": branch, "commit": commit,
                                                     "environment": "staging", "prNumber": pr}))
    _w(d, "01-change-event.json", ev)
    cs = step("2-diff", lambda: s2_diff.extract(ev, rs, cfg))
    _w(d, "02-changed-symbols.json", cs)
    br = step("3-blast", lambda: s3_blast_radius.compute(cs, kc, rs, cfg, local_scan=local_scan))
    _w(d, "03-blast-radius.json", br)
    # §1: "Exposed via" needs Story 3's reachability walk, so the Story 2 artifact is
    # rewritten once the blast radius is known. Re-running Story 2 alone is still valid;
    # it just yields the narrower directly-changed-endpoint view.
    if cs.get("changesSummary"):
        s2_changes_summary.enrich_exposed_via(cs["changesSummary"], br)
        _w(d, "02-changed-symbols.json", cs)
    _w(d, "03-tool-calls.json", kc.calls)

    unknown = br.get("coverage", "none" if br["notIndexed"] else "graph") == "none"
    empty = cs.get("emptyCommit")
    stop_reason = None
    if empty:
        stop_reason = f"empty commit — tree identical to parent {empty[:8]}; nothing was altered"
    elif unknown:
        stop_reason = "source repo not indexed in Code Knowledge and no local context — blast radius UNKNOWN"
    elif not br["impactedRepos"]:
        stop_reason = (f"no downstream consumers found (coverage: {br.get('coverage')}; "
                       + ("confirmed empty blast radius)" if not br["notIndexed"] else "repos outside the local context index are UNKNOWN)"))

    def tests(entries):
        trs = s5_runner.run_many(entries, br.get("sourceApps", []), cfg,
                                 skip="test execution disabled for this run (--skip-tests)" if skip_tests else None,
                                 source_repo=br.get("sourceRepo"),
                                 not_run=f"empty commit (tree identical to parent {empty[:8]}) — nothing to test" if empty else None)
        src = next((e for e in entries if e.get("isSourceRepo")), None)
        for t in trs:
            if src and f"{t['org']}/{t['repo']}".lower() == f"{src['org']}/{src['repo']}".lower():
                bl = s5_runner.baseline_at_parent(t, src, cs.get("parent"), br.get("sourceApps", []), cfg)
                if bl:
                    t["baseline"] = bl
        return trs

    blast_for_verdict = {**br, "changesSummary": cs.get("changesSummary"), "emptyCommit": empty}
    if stop_reason and not unknown:
        # the changed repo is still evaluated: self-impact is the most common incident shape
        man = step("4-resolve", lambda: s4_resolve.resolve(br, rs, cfg))
        man["entries"] = [e for e in man["entries"] if e.get("isSourceRepo")]
        trs = step("5-tests", lambda: tests(man["entries"]))
        verdicts = step("6-verdict", lambda: s6_verdict.aggregate(blast_for_verdict, trs, man))
    else:
        man = step("4-resolve", lambda: s4_resolve.resolve(br, rs, cfg)) if not unknown else {"entries": []}
        if only_repos:
            keep = {r.lower() for r in only_repos}
            man["entries"] = [e for e in man["entries"] if f"{e['org']}/{e['repo']}".lower() in keep]
        trs = step("5-tests", lambda: tests(man["entries"]))
        # §2: pass changesSummary on the blast dict so aggregate() can use the risk class
        # for the priority matrix. This avoids renaming blast fields.
        br_with_summary = blast_for_verdict
        verdicts = step("6-verdict", lambda: s6_verdict.aggregate(br_with_summary, trs, man))
        if only_repos and not unknown:
            keep = {r.lower() for r in only_repos}
            verdicts = [v for v in verdicts if f"{v['org']}/{v['repo']}".lower() in keep]
    _w(d, "04-manifest.json", man)
    _w(d, "05-test-results.json", trs)
    _w(d, "06-verdicts.json", verdicts)
    tickets = step("7-jira", lambda: s7_jira.create_all(verdicts, br, ev, trs, cfg, dry_run=not create_jira,
                                                        project_override=project_override))
    _w(d, "07-jira.json", tickets)
    meta = {"timings": timings, "stopReason": stop_reason, "backend": kc.backend_name, "skipTests": skip_tests,
            "createJira": create_jira, "onlyRepos": only_repos}
    _w(d, "run-meta.json", meta)
    (d / "report.md").write_text(render_run(d))
    return {"runDir": str(d), "reportPath": str(d / "report.md"), **meta}

