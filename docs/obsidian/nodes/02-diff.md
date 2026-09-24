# Node: diff (Story 2)

**Tool:** `shockwave.extract_changed_symbols`

## Prompt (Option B)
> Call `extract_changed_symbols` with `change_event = {{ trigger.output }}`. Output the JSON verbatim.
> An empty `changedSymbols` is a valid result (docs/config-only change), not an error.

## Option A (GitHub MCP only)
> Fetch the diff for `commit` in `org/repo` (use the PR diff if `prNumber` is set, else compare against the commit's first parent).
> For each non-test `.java` file, use the hunk ranges to find the enclosing class/method/field declarations
> in the new file (modified/added) and old file (removed). Output `ChangedSymbols` where each symbol has
> `file, kind, name ("pkg.Class#method(ParamTypes)"), changeType, package, className (Outer.Inner), member, params`.
> The `package`/`className`/`member` fields are **required by Story 3's resolver**.
> Extract JAX-RS/Spring `@Path/@GET/@GetMapping...` pairs on changed methods into `changedApiEndpoints`.

Note: the local engine parses Java with brace tracking plus literal/comment sanitizing (`src/shockwave/javaparse.py`).
An LLM doing this from raw hunks is noticeably less reliable on nested/overloaded members. Prefer Option B.

