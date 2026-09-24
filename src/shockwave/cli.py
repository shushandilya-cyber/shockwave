"""shockwave CLI — every story is runnable in isolation: JSON in -> JSON out.

  shockwave trigger   --in raw_event.json          [--out change_event.json]
  shockwave diff      --in change_event.json       [--out changed_symbols.json]
  shockwave blast     --in changed_symbols.json    [--out blast_radius.json] [--backend http|mcp]
  shockwave resolve   --in blast_radius.json       [--out manifest.json]
  shockwave test      --in manifest.json [--repo org/repo] [--blast blast_radius.json] [--out test_results.json]
  shockwave verdict   --blast blast_radius.json --tests test_results.json [--manifest manifest.json]
  shockwave jira      --in verdicts.json --blast blast_radius.json [--event change_event.json] [--create]
  shockwave run       --org O --repo R --commit SHA [--branch B] [--skip-tests] [--create-jira]
  shockwave report    --run-dir runs/<id>
  shockwave backtest  --incidents incidents.yaml [--run-dir-root runs/backtest] [--write-docs]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(p: str | None):
    if not p:
        return None
    return json.load(sys.stdin) if p == "-" else json.loads(Path(p).read_text())


def _emit(obj, out: str | None):
    s = json.dumps(obj, indent=2)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(s + "\n")
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="shockwave", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, *flags):
        p = sub.add_parser(name)
        for f in flags:
            p.add_argument(f"--{f}")
        p.add_argument("--out")
        return p

    add("trigger", "in")
    p = add("diff", "in"); p.add_argument("--include-tests", action="store_true")
    p = add("blast", "in", "backend"); p.add_argument("--no-local-scan", action="store_true")
    add("resolve", "in")
    p = add("test", "in", "repo", "blast", "source-apps")
    add("verdict", "blast", "tests", "manifest")
    p = add("jira", "in", "blast", "event", "tests", "project"); p.add_argument("--create", action="store_true")
    p = add("run", "org", "repo", "commit", "branch", "pr", "backend", "run-dir", "project")
    p.add_argument("--skip-tests", action="store_true"); p.add_argument("--create-jira", action="store_true")
    p.add_argument("--no-local-scan", action="store_true"); p.add_argument("--only-repo", action="append")
    add("report", "run-dir")
    p = sub.add_parser("backtest")
    p.add_argument("--incidents", default="incidents.yaml")
    p.add_argument("--run-dir-root", default="runs/backtest")
    p.add_argument("--write-docs", action="store_true", default=True)
    p.add_argument("--no-write-docs", dest="write_docs", action="store_false")
    # Tests are skipped by default: replaying every incident's full suite needs a build
    # toolchain per repo. The live signal is still computed statically (see s5_runner).
    p.add_argument("--skip-tests", action="store_true", default=True)
    p.add_argument("--run-tests", dest="skip_tests", action="store_false")
    p.add_argument("--out")

    a = ap.parse_args(argv)
    from .contracts import ContractError
    try:
        if a.cmd == "trigger":
            from .stories.s1_trigger import ingest
            _emit(ingest(_load(a.__dict__["in"])), a.out)
        elif a.cmd == "diff":
            from .stories.s2_diff import extract
            _emit(extract(_load(a.__dict__["in"]), include_tests=a.include_tests), a.out)
        elif a.cmd == "blast":
            from .clients.knowledge import KnowledgeClient
            from .stories.s3_blast_radius import compute
            _emit(compute(_load(a.__dict__["in"]), kc=KnowledgeClient(backend=a.backend), local_scan=not a.no_local_scan), a.out)
        elif a.cmd == "resolve":
            from .stories.s4_resolve import resolve
            _emit(resolve(_load(a.__dict__["in"])), a.out)
        elif a.cmd == "test":
            from .stories.s5_runner import run_many
            m = _load(a.__dict__["in"])
            entries = m["entries"] if "entries" in m else [m]
            if a.repo:
                entries = [e for e in entries if f"{e['org']}/{e['repo']}".lower() == a.repo.lower()]
            blast = _load(a.blast)
            apps = (a.source_apps.split(",") if a.source_apps else None) or (blast or {}).get("sourceApps") or []
            _emit(run_many(entries, source_apps=apps,
                           source_repo=(blast or {}).get("sourceRepo") or m.get("sourceRepo")), a.out)
        elif a.cmd == "verdict":
            from .stories.s6_verdict import aggregate
            _emit(aggregate(_load(a.blast), _load(a.tests), _load(a.manifest)), a.out)
        elif a.cmd == "jira":
            from .stories.s7_jira import create_all
            _emit(create_all(_load(a.__dict__["in"]), _load(a.blast), _load(a.event), _load(a.tests),
                             dry_run=not a.create, project_override=a.project), a.out)
        elif a.cmd == "run":
            from .pipeline import run
            res = run(org=a.org, repo=a.repo, commit=a.commit, branch=a.branch or "staging", pr=a.pr,
                      backend=a.backend, run_dir=a.run_dir, skip_tests=a.skip_tests, create_jira=a.create_jira,
                      local_scan=not a.no_local_scan, only_repos=a.only_repo, project_override=a.project)
            print(res["reportPath"])
        elif a.cmd == "report":
            from .report import render_run
            print(render_run(Path(a.run_dir)))
        elif a.cmd == "backtest":
            from .stories.backtest import run_backtest
            summary = run_backtest(
                Path(a.incidents),
                run_dir_root=Path(a.run_dir_root),
                skip_tests=a.skip_tests,
                write_docs=a.write_docs,
            )
            _emit(summary, a.out)
    except ContractError as e:
        print(f"CONTRACT ERROR: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

