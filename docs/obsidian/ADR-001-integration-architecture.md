# Obsidian integration — architecture decision (ADR-001)

**Status:** Proposed (backed by measurements taken 2026-09-24)
**Question:** How should the Obsidian GraphFlow use the blast-radius pieces?

## Options

| | **A. Native LLM nodes** | **B. Hybrid: shockwave MCP + thin nodes** (recommended) | **C. External runner job** |
|---|---|---|---|
| What it is | Each story is an Obsidian node prompt that calls `codemcp` / GitHub / Jira MCPs itself (the spec's literal design) | Obsidian orchestrates. Each node calls **one deterministic shockwave MCP tool** and passes JSON on. The LLM is used only where judgment adds value (triage narrative, NeedsReview summary) | Obsidian fires a Jenkins/Tess job that runs `shockwave run`; Obsidian only files Jira |
| Hosting | none | shockwave-mcp (streamable HTTP) on Tess/K8s, with Maven + JDK 8/17/21 + git creds | Jenkins agent image |
| Accuracy | ❌ **Silently loses the OpenAPI signal** (see evidence) and relies on the LLM doing Gremlin, FQN matching and a 6-rule table correctly every run | ✅ Same code as the unit-tested local engine; **proven equal** (sim: 15/15 repos, 3/3 high) | ✅ same engine |
| Determinism / testability | low (prompt drift, token-length variance) | high: every tool has a JSON contract + pytest | high |
| Story 5 (clone + mvn) | ❌ Obsidian runners have no Maven/JDK toolchain | ✅ runs in the MCP server container | ✅ |
| Cost / latency | high (hundreds of tool calls through an LLM) | low (~10 tool calls per run) | low |
| Obsidian-native visibility | ✅ | ✅ each story is a visible node with inputs/outputs in "View Runs" | ⚠️ black box |
| Effort | low to start, high to make correct | medium | medium |

## Evidence that decides it

1. **Hosted `codemcp.getOpenapiConnections` fails 100% of the time**, even for small repos. Its internal
   KnowledgeHub client gives up at ~60s, while the endpoint takes 40–50s (direct REST succeeds). The failure
   comes back as `isError:false` with body `{"error":"...FailedRetriesException..."}`, so an LLM node sees a "successful" empty
   result. In the golden run this drops **3 of 3 high-confidence repos** (LocBridge, PostOrderPlanProcessingService,
   aftersalesexpsvc) and 3 low ones. Measured: `shockwave blast --backend mcp` gave 10 repos and 0 high; `--backend http` gave 15 repos and 3 high.
2. **`vectorSearch` tag scoping is broken** in both codemcp builds (they send `filter`; the API expects `filters`).
   Results come from random repos, so an LLM following the spec's step 2 literally will resolve the wrong vertices.
3. **Cross-repo edges are FQN-based**, so the spec's `g.V().has('vertex_key', vkey).in('CALLS')` only ever returns callers
   inside the source repo. The correct pattern (`has('fqn', within(...)).in('CALLS').has('git_repo', neq(src))`, chunked
   under the 5000-char query limit, plus a transitive walk up to exposed endpoints) is fiddly for an LLM to get right every run.
4. Story 5 needs Maven, several JDKs (repos pin 8/17/21) and git credentials, which only exist in a container we control.

## Decision

**B (Hybrid).** Obsidian GraphFlow = orchestration, fan-out/fan-in, run history, Jira/Slack side-effects.
shockwave-mcp = deterministic computation. **A** remains a documented fallback for Stories 1/2/4/6/7
(prompts in `nodes/`), usable today without hosting, as long as Story 3 accepts the OpenAPI gap and says so.

## Upstream fixes worth filing (these would make Option A viable for Story 3)
- codemcp `vectorSearch`: send `filters` instead of `filter`.
- codemcp `getOpenapiConnections`: raise the KnowledgeHub client timeout to at least 120s, and return `isError:true` on failure.
- KnowledgeHub: add an index for `fqn` suffix/text queries, or a "callers by FQN across repos" endpoint.

