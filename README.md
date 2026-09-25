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

Backtest against recorded incidents: `$S backtest --incidents incidents.yaml [--run-tests] [--source-tests-only] [--only INC-006 ...] [--doc path.md]`
→ `docs/backtest/pudo-incidents.md`. Incidents may declare `catch_signals` (e.g. `environmentGap:Sandbox`,
`sourceTestsFail`); an incident counts as *caught* only when one of those signals fires, so a lucky flag for the
wrong reason doesn't count.

## Tests
```bash
.venv/bin/pytest -q            # offline unit tests for all 7 stories (fixture git repos, fake graph client, fake Jira)
.venv/bin/pytest -q -m live    # real Maven PASS/FAIL fixtures for Story 5, plus one test per backtested incident
```

## How a change is described (Story 2 `changesSummary`)
Raw symbol names don't answer "do I need to act?", so Story 2 emits a `changesSummary` that leads the report;
the full symbol list moves to a collapsed appendix. Classification is rule-based — no LLM — and the `intent`
sentence is taken from the commit subject with its Jira key kept as the citation.

Changes are grouped into **contract** / **behavior** / **internal** / **config-data**, each with a risk class:

| Risk class | Meaning |
|---|---|
| BREAKING | existing callers stop compiling or 4xx: removed endpoint/class/method, changed signature, removed DTO field |
| BEHAVIORAL | callers still compile but observe something different: logic change in an exposed path, new endpoint/field |
| SAFE | nothing externally observable: private helpers, logging, metrics, test fixtures, build descriptors |

Non-source files are **not** uniformly SAFE, which is the lesson of INC-002: test fixtures are SAFE, build
descriptors are SAFE, an OpenAPI spec is a *contract* change, and data under `src/main/resources` (or in a
`*cfg` repo) is BEHAVIORAL because the service serves it to callers with no code change at all.

`exposedVia` is backfilled from Story 3's transitive walk, so it names endpoints that reach the changed code
even when the endpoint's own source was untouched.

## What confidence means (and what it does not)
Confidence answers **"how strong is the evidence that a code path in the consumer reaches the changed code?"**
It is *not* the likelihood of breakage — that is the risk class above.

| Level | Evidence required |
|---|---|
| high | a concrete path: direct FQN call edge, transitive edge, or an OpenAPI chain whose target method is reached by changed code |
| medium | depends on the source (app/repo dependency, or imports a changed class), but no path to the changed code was shown |
| low | calls some API of the source, but none of the endpoints it calls are linked to the changed code |
| unknown | not indexed, lookup failed, or unmapped caller — never folded into low |

Risk class × confidence gives the triage priority. **Unknown outranks Low on purpose**: Low means we looked and
found no link, Unknown means we could not look, so a BREAKING change we cannot trace is P2 rather than P3.

| Risk class | High | Medium | Unknown | Low |
|---|---|---|---|---|
| BREAKING | P1 | P2 | P2 | P3 |
| BEHAVIORAL | P2 | P2 | P3 | P3 |
| SAFE | P3 | P3 | P3 | P3 |

## Tests never silently disappear (Story 5)
`SKIPPED` now means exactly one thing: the user passed `--skip-tests`, and the report says so in every row.
Everything else is `NOT_RUN` with a specific, actionable reason ("gradle toolchain missing on the runner",
"repo not found or not visible to the runner identity", "package.json has no 'test' script"). A build that
*ran* and failed to compile is `ERROR`, not `NOT_RUN` — that is a real signal about the consumer, whereas a
broken runner is not.

Runners: Maven, Gradle (`./gradlew test`) and npm, with the right JDK per repo for Maven. Set
`SHOCKWAVE_TARGETED_TESTS=1` to run only the test classes that mention the changed service (`-Dtest=…`).

The live-integration signal is tri-state and is **always** computed, even when tests are skipped or cannot run
(it falls back to a static scan of the local clone):

- **LIVE** — tests reference the service's staging host, so a FAIL is real evidence and a PASS is meaningful.
- **MOCKED** — tests mock it (Mockito/WireMock); a PASS means the contract *shape* holds, not that behaviour is unchanged. Own verdict rule.
- **NONE** — nothing references the service; the run is a generic sanity check.

Dedicated integration-test repos (e.g. `Ship-AST/PudoIntegrationTests`) are discovered by scanning local clones
for the source's staging hosts, and are **run**, not merely listed. The source repo itself is also evaluated:
Story 3 is downstream-only, so without that the changed repo would be the one repo whose tests never run.

The source repo is tested **at the analysed commit** (`TestResult.commit`), and consumers at their default
branch. When the source suite is red, the failing classes are re-run at the parent commit (`TestResult.baseline`),
which separates failures the commit introduced from failures that were already there (for example, a timezone-
dependent assertion that fails on any machine outside UTC). That gives the source repo three extra verdict rules:

| Rule | Condition | Verdict |
|---|---|---|
| 0 | empty commit: tree identical to parent | NotImpacted (tests `NOT_RUN`, nothing to test) |
| 8 | every failure also fails at the parent | decided as if the suite passed, reasoning says "pre-existing" |
| 9 | at least one failure is new relative to the parent | Impacted |

An inconclusive baseline (build failed at the parent, non-JVM repo) leaves rule 5 in place.

## Environment-profile gaps (Story 2)
Moving from legacy `META-INF/configuration/<Env>/` to `application-<Env>.properties|yml` is a common source of
"works in QA, dead in Sandbox". Story 2 flags a module that has profile files and a legacy `<Env>` directory but
no profile for that `<Env>`. Only gaps **introduced by this commit** (not present at the parent) are reported, as a
BREAKING `breakingChanges` entry with `kind: "environment"`, because every caller in that environment is affected.

## Local context index
`~/.cache/shockwave/context` holds a read-only index of every repo under `~/Documents/projects` (artifacts,
dependencies, HTTP clients, call sites). Story 3 merges it with the Code Knowledge graph and records which sources
answered in `coverage`: `graph+local`, `graph`, `local`, or `none`. `none` means UNKNOWN, never "no impact".

## Portal (`shockwave serve`)
```bash
.venv/bin/shockwave serve --port 8765     # http://127.0.0.1:8765
```
Enter a repo (`org/repo`, a GitHub URL, a unique bare name, or an alias: `punotif`, `pickupeligibility`,
`pickupsvc`, `cls`, `polis`) and optionally a commit and branch (blank commit = branch tip). A repo that isn't
cloned locally is cloned into the cache and indexed first, then the full S1–S7 pipeline runs with Jira in dry-run.
Jobs run one at a time and persist under `runs/portal/<id>/`; the page shows stage progress, the summary and
the rendered report. API: `POST /api/runs`, `GET /api/runs[/<id>[/report]]`, `GET /api/repos`.

## Incident backtest (`docs/backtest/pudo-incidents.md`)
Each incident in `incidents.yaml` is replayed at its culprit commit (read-only, Jira dry-run) and scored for
recall, precision, risk class and live signal. The ranked fix list is **derived** from diagnosed miss
categories, so re-running changes it.

Two rules keep the numbers honest:
- Incidents whose victim list could not be corroborated carry `victims_verified: false` and are **excluded**
  from the headline recall. A miss against an unverified list says nothing about the tool. INC-001 and INC-002
  are currently in this state: neither `Ship-AST/PUDO` nor `Ship-AST/PickupEligibilityService` references
  waypoint anywhere on any branch, and Jira returned HTTP 401 for the tickets, so the attribution is unresolved.
- A repo flagged High that is not on an incident's victim list is reported as *unconfirmed*, not as a false
  positive — nobody re-checked it — so precision is a lower bound.

Current state: **2/2 verified incidents fully caught, risk class correct 4/4.** Both verified incidents are
self-impact, caught by evaluating the source repo. The open gaps are an unindexed consumer (`Ship-AST/PUDO`)
and consumers with no discoverable link to the source, both of which only affect the unverified incidents.

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

