# Node: blast-radius (Story 3)

**Tool:** `shockwave.compute_blast_radius`

## Prompt (Option B)
> Call `compute_blast_radius` with `changed_symbols = {{ diff.output }}`. Output the JSON verbatim.
> If `notIndexed` is true, say loudly: "blast radius UNKNOWN — source repo not in Code Knowledge" (never "no impact").
> Surface `unresolvedSymbols`, `notes` and `unmappedApiCallers` in the node's summary text; they are coverage gaps.

## Option A (hosted `codemcp` only) — validated recipe with the pitfalls handled
1. `findRepo(service=repo)` → find the item with `id` starting `org:repo:`. None → `notIndexed:true`, stop.
2. For each changed symbol with `changeType != added`:
   `graphSearch(gremlinQuery="g.V().has('name','<member or ClassName for ctor/class>').has('git_repo','<repo>').limit(200).valueMap('fqn','vertex_key')", vkey="unused")`
   and keep FQNs ending in `.<package>.<className>.<member>` (optionally followed by `(+N)` for overloads).
   **Do not rely on `vectorSearch(tag=…)`**: its tag filter is ignored server-side. If you use it, keep only results whose `tag` equals `org:repo:branch`.
   Symbols with no FQN go to `unresolvedSymbols`.
3. Direct cross-repo callers (confidence **high**). Cross-repo edges are FQN-based:
   `g.V().has('fqn', within('<fqn1>','<fqn2>',…)).as('t').in('CALLS').has('git_repo', neq('<repo>')).as('c').select('t','c').by('fqn').by(valueMap('fqn','git_org','git_repo')).limit(500)`
   **Keep each query under 5000 characters** (chunk the FQN list).
4. Transitive walk up to exposed APIs:
   `g.V().has('fqn', within(…)).has('git_repo','<repo>').repeat(__.in('CALLS').has('git_repo','<repo>').simplePath()).emit().times(6).dedup().limit(500).values('fqn')`
   Re-run step 3 on these FQNs (high, transitive).
5. `getOpenapiConnections(org, repo, "inbound")`. ⚠️ **In this deployment it currently always returns
   `{"error": "...FailedRetriesException..."}` with isError=false.** Treat that as a coverage gap: add a note
   "OpenAPI consumers not evaluated". Do not treat it as zero consumers. When it works: a chain whose `targetClass.targetMethod`
   is in the step-4 set is **high**; any other chain is **low**; chains with no `callerRepo` go to `unmappedApiCallers`.
6. `getRepoDependencies(org, repo, "upstream")` → each `srcRepo` is **medium**. Collect `viaApps[].tgtApp` into `sourceApps`.
7. Merge: one entry per repo; evidence accumulates; confidence only goes up; drop the source repo and `ebayistio/istio`.

Validation: `python scripts/compare_blast.py <local 03-blast-radius.json> <node output>`.

