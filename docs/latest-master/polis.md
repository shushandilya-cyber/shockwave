# polis — CoreShipping/polis @ master `ee5dd5df`

**Verdict: additive, internal change. Nothing outside polis is affected.** A new enum constant `ORANGECONNEX` lets
API-mode jobs resolve that provider. No other repo imports the enum, and the repo's suite passes at the commit.

| | |
|---|---|
| Commit | [`ee5dd5df`](https://github.corp.ebay.com/CoreShipping/polis/commit/ee5dd5df7faf6a8873503ae4d062adb6e398bce0) — "SHIPLLP-634: Add ORANGECONNEX to LocationProvider enum (#271)", gprabhala, 2026-09-23 |
| Risk class | `BEHAVIORAL` (new enum constant) |
| Coverage | `local`: not registered in Code Knowledge |
| Pipeline run | [`runs/latest-master/polis/report.md`](../../runs/latest-master/polis/report.md) |

## What changed
In `src/main/java/com/ebay/raptor/pudo/provider/LocationProvider.java` (+3/−1), after `DPD` the enum gains
`ORANGECONNEX;` with a Javadoc line. The commit body explains it: "Enable POLIS API-mode jobs with
locationProvider=OrangeConnex to resolve the provider enum (same pattern as RoyalMail) before calling LocBridge."

## Did it alter anything?
Yes, it adds one value. Existing values and their order are unchanged, so no existing path behaves differently.
A job configured with `locationProvider=OrangeConnex`, which previously couldn't resolve, now resolves.

## Breaking changes
None. A new enum value can break consumers that deserialize the enum strictly or switch over it exhaustively. The
pipeline flags that generically, but there are no such consumers:

- `rg "com\.ebay\.raptor\.pudo\.provider"` across all 36 repos in `~/Documents/projects` (excluding polis) finds
  nothing. Matches on the bare name `LocationProvider` in pudef, PUDO and PickUpSvc are different classes, such as
  `GeoLocationProvider` and `ThirdPartyGeoCodeProvider`.
- polis is a job application built on `raptor-io-parent`. It isn't a library other repos depend on.

## Repos it can affect
None found in the local index. Because polis isn't in Code Knowledge, repos outside that index are **unknown**.

## Test evidence
- **Source repo at `ee5dd5df`:** PASS, 112 tests, Zulu 17.
- **Verdict:** NeedsReview (rule 4). The integration-style test `PolisApplicationIT` mocks LocBridge, so the suite
  confirms the contract shape, not live behaviour with OrangeConnex.

## Summary
Safe to ship. The behaviour that matters, an OrangeConnex job calling LocBridge end to end, is only covered by
mocks. Watch the first real OrangeConnex job run.
