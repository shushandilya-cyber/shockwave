"""shockwave MCP server — the Obsidian integration surface.

Each story is exposed as one deterministic tool with the same JSON contract as the
CLI, so an Obsidian GraphFlow node is a thin "call tool X with the previous node's
output" step. LLM reasoning stays in the nodes where it adds value (triage narrative),
while graph traversal / verdict rules stay deterministic and unit-tested.

Run locally (stdio, for Claude Code / Cursor / MCP inspector):
    shockwave-mcp
Run as a hosted streamable-HTTP server (for Obsidian):
    shockwave-mcp --http --host 0.0.0.0 --port 8765      # endpoint: /mcp
"""
from __future__ import annotations

import argparse

from fastmcp import FastMCP

from .clients.knowledge import KnowledgeClient
from .clients.repos import RepoSource
from .config import CONFIG
from .report import summary_table
from .stories import s1_trigger, s2_diff, s3_blast_radius, s4_resolve, s5_runner, s6_verdict, s7_jira

mcp = FastMCP("shockwave")


@mcp.tool()
def ingest_change_event(raw_event: dict) -> dict:
    """Story 1. Normalize a staging-deploy custom trigger payload (full Obsidian body, eventContext,
    or bare payload) into a ChangeEvent. Fails with a clear message naming missing fields."""
    return s1_trigger.ingest(raw_event)


@mcp.tool()
def extract_changed_symbols(change_event: dict, include_tests: bool = False) -> dict:
    """Story 2. ChangeEvent -> ChangedSymbols (files, Java class/method/field symbols, REST endpoints)."""
    return s2_diff.extract(change_event, RepoSource(CONFIG), CONFIG, include_tests=include_tests)


@mcp.tool()
def compute_blast_radius(changed_symbols: dict, local_scan: bool = False) -> dict:
    """Story 3. ChangedSymbols -> BlastRadius. Fuses FQN-level graph callers, transitive internal walk
    to exposed APIs, OpenAPI inbound chains, repo dependencies (and optional local-clone scan).
    notIndexed=true means blast radius UNKNOWN (never 'no impact')."""
    return s3_blast_radius.compute(changed_symbols, KnowledgeClient(CONFIG, backend="http"), RepoSource(CONFIG), CONFIG,
                                   local_scan=local_scan)


@mcp.tool()
def resolve_impacted_repos(blast_radius: dict) -> dict:
    """Story 4. BlastRadius -> ImpactedRepoManifest (CODEOWNERS team, build tool, test command, runnable)."""
    return s4_resolve.resolve(blast_radius, RepoSource(CONFIG), CONFIG)


@mcp.tool()
def run_downstream_tests(manifest_entry: dict, source_apps: list[str]) -> dict:
    """Story 5 (one repo — fan out one call per manifest entry). Clones + runs `mvn -B test`
    with a hard timeout; returns TestResult incl. hadLiveIntegrationSignal. Maven-only in v1."""
    return s5_runner.run_one(manifest_entry, source_apps, RepoSource(CONFIG), CONFIG)


@mcp.tool()
def aggregate_verdicts(blast_radius: dict, test_results: list[dict], manifest: dict | None = None) -> list[dict]:
    """Story 6 (pure logic). Apply the conservative hybrid decision table -> Verdict[]."""
    return s6_verdict.aggregate(blast_radius, test_results, manifest)


@mcp.tool()
def render_jira_story(verdict: dict, blast_radius: dict, change_event: dict | None = None) -> dict | None:
    """Story 7 (dry-run). Render the exact Jira issue payload (project/component via jira-team-mapping.yaml,
    dedupe label, live-vs-generic caveat). Use the Jira MCP to create it, or create_jira_story."""
    return s7_jira.create_one(verdict, blast_radius, change_event, CONFIG, dry_run=True)


@mcp.tool()
def create_jira_story(verdict: dict, blast_radius: dict, change_event: dict | None = None, project_override: str | None = None) -> dict | None:
    """Story 7 (live). Create or comment-on-duplicate a Jira story on jirap using the server's JIRA_PAT."""
    return s7_jira.create_one(verdict, blast_radius, change_event, CONFIG, dry_run=False, project_override=project_override)


@mcp.tool()
def summarize(verdicts: list[dict], tickets: list[dict], source_repo: str, commit: str) -> str:
    """Story 9. Markdown summary table (repo | confidence | verdict | ticket) for a PR/Slack comment."""
    return summary_table(verdicts, tickets, source_repo, commit)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if a.http:
        mcp.run(transport="http", host=a.host, port=a.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()

