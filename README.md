# shockwave — blast-radius regression & Jira triage

Implements the spec in `~/Downloads/blast-radius-regression-triage` (Stories 1–9) as:

1. **A local engine**: Python package + CLI. Every story runs on its own, JSON in and JSON out.
2. **An Obsidian integration surface**: `shockwave-mcp` exposes each story as a deterministic MCP tool, and the
   GraphFlow definition plus node prompts live in `docs/obsidian/`.

The two pieces are built and tested separately, then merged (see *Merge & test plan*).

```
ChangeEvent ─► ChangedSymbols ─► BlastRadius ─► Manifest ─► TestResult[] ─► Verdict[] ─► JiraTicket[]
   (S1)            (S2)             (S3)          (S4)        (S5 fan-out)   (S6 fan-in)     (S7)
```

## Setup
```bash
cd ~/Documents/shockwave
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,mcp]'
# VPN required for Code Knowledge / git / Jira. Credentials: env GITHUB_TOKEN / JIRA_PAT, else read in-memory
# from ~/.claude.json git-server / jira-server entries (never printed or written).
```

## Run each story on its own (local)
```bash
S=.venv/bin/shockwave
$S trigger --in fixtures/s1/waypoint_queryindex.raw.json --out /tmp/sw/01.json
$S diff    --in /tmp/sw/01.json --out /tmp/sw/02.json
$S blast   --in /tmp/sw/02.json --out /tmp/sw/03.json            # --backend mcp = via hosted codemcp
$S resolve --in /tmp/sw/03.json --out /tmp/sw/04.json
$S test    --in /tmp/sw/04.json --repo CoreShipping/LocBridge --blast /tmp/sw/03.json --out /tmp/sw/05.json
$S verdict --blast /tmp/sw/03.json --tests /tmp/sw/05.json --manifest /tmp/sw/04.json --out /tmp/sw/06.json
$S jira    --in /tmp/sw/06.json --blast /tmp/sw/03.json --event /tmp/sw/01.json      # dry-run; add --create --project TST
```
End to end: `$S run --org CoreShipping --repo waypointservice --commit 9d60af39 [--skip-tests] [--only-repo org/repo] [--create-jira --project TST]`
writes `runs/<id>/01..07-*.json` plus `report.md`.

## Tests
```bash
.venv/bin/pytest -q            # offline unit tests for all 7 stories (fixture git repos, fake graph client, fake Jira)
.venv/bin/pytest -q -m live    # real Maven PASS/FAIL fixtures for Story 5
```

## How blast radius is computed (Story 3), and why it differs from the spec
Probing the live graph showed that the spec's recipe (`vectorSearch(tag)` → `graphSearch(has('vertex_key', vkey).in('CALLS'))`) **finds zero
downstream repos**. Cross-repo edges are FQN-based: each consumer has its own vertex with the same `fqn`. The engine therefore fuses:

| Signal | Confidence | How |
|---|---|---|
| Direct callers | high | `has('fqn', within(changed)).in('CALLS').has('git_repo', neq(src))` |
| Transitive → external | high | upward `CALLS` walk inside the source repo (6 hops), then direct callers of that set |
| OpenAPI, method-matched | high | inbound chain whose `targetClass.targetMethod` is reached by the changed code |
| Repo dependency | medium | `getRepoDependencies(upstream)` |
| Local clone scan | medium | `git grep` imports of changed classes across `~/Documents/projects` (covers repos that aren't indexed) |
| OpenAPI, not linked | low | other inbound API consumers |

Guarantees: `notIndexed` means UNKNOWN (never "no impact"). Unresolved symbols, unmapped API callers and failed lookups are
listed as coverage gaps. Non-code or additive-only changes are not ticketed (their consumers go into `contextRepos`).

Upstream quirks handled (see `clients/knowledge.py`): vector filter key is `filters` (codemcp sends `filter`); 5000-char
Gremlin limit (chunking); `fqn` text predicates time out; hosted `codemcp.getOpenapiConnections` always fails and
reports the failure as success. The last one is why the Obsidian design is hybrid (`docs/obsidian/ADR-001-integration-architecture.md`).

## Golden validation (CoreShipping/waypointservice @ 9d60af39, JTS spatial index for radial queries)
- S2: 26 symbols / 11 files (overloads, nested classes, removed `FindOptions`); test files excluded.
- S3: 15 repos. **High:** LocBridge, PostOrderPlanProcessingService, aftersalesexpsvc, via
  `queryWaypoints → … → WaypointV2Resource.listWaypoints → GET /waypoint`. 1 unmapped app also hits that endpoint.
- Edge cases: config-only commit `e152b7a1` gives 0 tickets; unindexed `StorePickerExperienceService` gives a loud UNKNOWN plus 1 rule-1 NeedsReview.
- Obsidian surface: `scripts/mcp_graphflow_sim.py` drives shockwave-mcp node by node → **MATCH 15/15 repos, 3/3 high**.

## Merge & test plan (for you)
1. **Local piece**: `shockwave run … --only-repo CoreShipping/LocBridge` and read `runs/<id>/report.md`.
2. **Jira piece**: pick a throwaway project, then run `shockwave jira --in runs/<id>/06-verdicts.json --blast runs/<id>/03-blast-radius.json --create --project <KEY>`.
   Run it twice and confirm the second run **comments** instead of creating a duplicate.
3. **Obsidian surface, locally**: `.venv/bin/python scripts/mcp_graphflow_sim.py --org CoreShipping --repo waypointservice --commit 9d60af39 --expect runs/golden-skip/03-blast-radius.json`
   (stdio). Over HTTP: `.venv/bin/shockwave-mcp --http --port 8765`, then add `--url http://127.0.0.1:8765/mcp`.
4. **Merge**: host `shockwave-mcp --http` (container with git creds, Maven, JDK 8/17/21, `~/.m2/settings.xml`, VPN-reachable),
   register it as an MCP server on the Obsidian team, create the GraphFlow from `docs/obsidian/graphflow.yaml` with the prompts in
   `docs/obsidian/nodes/`, fire the Story 1 `curl` at the GraphFlow, and compare its Story 3 output with
   `scripts/compare_blast.py`.
5. Only then wire the real CD pipeline webhook, starting with one repo.

## Open decisions for you
- Jira: throwaway project key, plus real `jira-team-mapping.yaml` entries (team → project/component).
- Hosting for shockwave-mcp (Tess app vs Obsidian-provided runner), and its service identity for GHE/Jira.
- Whether to file the upstream codemcp fixes (listed in the ADR). They would make an all-native Story 3 viable.

