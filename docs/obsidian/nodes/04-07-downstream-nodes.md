# Nodes: resolve-repos / run-tests / aggregate-verdict / create-jira / notify (Stories 4–7, 9)

## resolve-repos (Story 4) — `shockwave.resolve_impacted_repos`
> Call with `blast_radius = {{ blast-radius.output }}`; output verbatim.
Option A: GitHub MCP. Read `CODEOWNERS` in `/`, `.github/`, `docs/` (first hit wins). Prefer the rule matching an evidence
file path, else `*`. Detect build tool: `pom.xml` means maven (`mvn -B test`); `build.gradle*` means gradle; `package.json` means npm (`scripts.test`).

## run-tests (Story 5, fan-out) — `shockwave.run_downstream_tests`
> For each manifest entry call `run_downstream_tests(manifest_entry=item, source_apps={{ blast-radius.output.sourceApps }})`.
No Option A: Obsidian runners have no Maven/JDK toolchain. Without shockwave-mcp, emit
`{"status":"SKIPPED","skippedReason":"no runner available","hadLiveIntegrationSignal":false,...}` for each entry.

## aggregate-verdict (Story 6, fan-in) — `shockwave.aggregate_verdicts`
> Call with the BlastRadius, **all** run-tests outputs and the manifest; output verbatim. Never edit verdicts by hand.
Rules (first match wins): notIndexed → NeedsReview(source) · FAIL+live → Impacted · PASS+live → NotImpacted ·
FAIL/ERROR (no live signal) → NeedsReview · SKIPPED → NeedsReview · PASS (no live signal) → NeedsReview.

## create-jira (Story 7, fan-out over verdict != NotImpacted)
Option B-1 (server creates): `create_jira_story(verdict=item, blast_radius=…, change_event=…)`; the server uses its JIRA_PAT and deduplicates.
Option B-2 (Obsidian's Jira MCP creates):
> 1. `render_jira_story(...)` → `payload.fields`.
> 2. Search Jira for `labels = "<the br-xxxxxxxxxxxx label in fields.labels>"`. If an issue exists, add a comment with the new
>    verdict and reasoning. Otherwise create the issue with `fields` exactly as rendered.
> 3. You may prepend a 3-sentence plain-English triage summary to the description. Never remove the
>    "GENERIC SANITY CHECK ONLY" / "LIVE-INTEGRATION SIGNAL" caveat panel.
Use a throwaway project first (`project_override`).

## notify (Story 9) — `shockwave.summarize`
> Post the returned markdown as a PR comment (GitHub MCP) if `prNumber` is set, else to Slack; otherwise just log it.

