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
        only_repos: list[str] | None = None, project_override: str | None = None, cfg: Config = CONFIG) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = Path(run_dir or Path.cwd() / "runs" / f"{org}-{repo}-{commit[:8]}-{stamp}")
    d.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    rs = RepoSource(cfg)
    kc = KnowledgeClient(cfg, backend=backend)

    def step(name, fn):
        t = time.time()
        r = fn()
        timings[name] = round(time.time() - t, 1)
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

    stop_reason = None
    if br["notIndexed"]:
        stop_reason = "source repo not indexed in Code Knowledge — blast radius UNKNOWN"
    elif not br["impactedRepos"]:
        stop_reason = "no downstream consumers found (indexed source; confirmed empty blast radius)"

    if stop_reason and not br["notIndexed"]:
        man, trs, verdicts = {"entries": []}, [], []
    else:
        man = step("4-resolve", lambda: s4_resolve.resolve(br, rs, cfg)) if not br["notIndexed"] else {"entries": []}
        if only_repos:
            keep = {r.lower() for r in only_repos}
            man["entries"] = [e for e in man["entries"] if f"{e['org']}/{e['repo']}".lower() in keep]
        trs = step("5-tests", lambda: s5_runner.run_many(man["entries"], br.get("sourceApps", []), cfg,
                                                         skip="test execution disabled for this run (--skip-tests)" if skip_tests else None,
                                                         source_repo=br.get("sourceRepo")))
        # §2: pass changesSummary on the blast dict so aggregate() can use the risk class
        # for the priority matrix. This avoids renaming blast fields.
        br_with_summary = {**br, "changesSummary": cs.get("changesSummary")}
        verdicts = step("6-verdict", lambda: s6_verdict.aggregate(br_with_summary, trs, man))
        if only_repos and not br["notIndexed"]:
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

