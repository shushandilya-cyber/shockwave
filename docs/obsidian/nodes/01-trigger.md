# Node: trigger (Story 1)

**Tool:** `shockwave.ingest_change_event`

## Prompt (Option B)
> You receive the raw custom-event payload as `{{ state }}`. Call `ingest_change_event` with
> `raw_event = {{ state }}` and output its JSON result **verbatim** as this node's output.
> If the tool errors (missing field / non-SHA commit), stop the workflow and output only the error
> message. Do not continue to downstream nodes.

## Option A (no shockwave server): pure prompt
> Read `{{ state }}.eventContext.payload`. Required: `org, repo, branch, commit, environment`.
> If any is missing or empty, fail with `ChangeEvent: missing required field(s): <names>`.
> Output exactly: `{"org","repo","branch","commit","environment","prNumber"(string|null),"triggeredAt"(UTC ISO8601)}`.

## CD pipeline hook (last step, only after deploy is healthy)
```bash
curl -sS -X POST https://obsidianwfengine.vip.qa.ebay.com/obsidian/workflows/trigger \
  -H "Authorization: Bearer $PAAS_SESSION_TOKEN" -H "Content-Type: application/json" \
  -d @- <<EOF
{"teamId":"<team-id>","workflowConfigs":[{"id":"<team>:blast-radius-regression-triage"}],
 "tags":["staging","auto-triggered"],
 "eventContext":{"source":"custom","target":"tess","targetSysWorkflowId":"tess-dispatch","user":"ci-bot",
   "payload":{"org":"$ORG","repo":"$REPO","branch":"staging","commit":"$GIT_SHA","environment":"staging","prNumber":"$PR"}}}
EOF
```
Contract fixture: `fixtures/s1/waypoint_queryindex.raw.json`.

