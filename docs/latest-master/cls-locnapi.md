# cls — CoreShipping/locnapi @ master `07f0e235`

**Verdict: nothing was altered.** The latest master commit is empty: its tree is byte-identical to its parent's.

| | |
|---|---|
| Commit | [`07f0e235`](https://github.corp.ebay.com/CoreShipping/locnapi/commit/07f0e235ec40081cf0dfd1386c86fa82155f4d86) — "SHIPLLP-649: Patch - Raptor upgrade created by AutoPR (#398)", raptor upgrade bot, 2026-09-21 |
| Parent | [`2e9cb2a4`](https://github.corp.ebay.com/CoreShipping/locnapi/commit/2e9cb2a48e6d220e54a7ed44a1d2a118a0e38570) — "SHIPLLP-649: Patch - Raptor upgrade created by AutoPR (#399)", 2026-09-18 |
| Evidence | `git rev-parse 07f0e235^{tree} 07f0e235^1^{tree}` returns `6daa962d…` for both |
| Risk class | `SAFE` |
| Pipeline run | [`runs/latest-master/locnapi/report.md`](../../runs/latest-master/locnapi/report.md) |

## What changed
Nothing. The AutoPR bot opened two PRs for the same upgrade. #399 merged first on 2026-09-18, and #398 merged three
days later with no remaining diff.

For context, the actual upgrade is in the parent `2e9cb2a4`: `raptor-io-parent` 4.0.3 → 4.0.4 in `pom.xml`, plus
removal of the unused `commonoperational.component.version` property. It's the same change as PickUpSvc
`ddc33832`.

## Did it alter anything?
No. The pipeline found 0 files, 0 symbols and 0 endpoints. It recorded `emptyCommit: 2e9cb2a4`, skipped tests as
`NOT_RUN` ("empty commit … nothing to test", not a user skip), and gave the source repo verdict **NotImpacted, rule 0**.

## Breaking changes
None.

## Repos it can affect
None, because nothing changed. The local index lists locnapi's repo-level consumers for context only
(StorePickerExperienceService, polis, PickUpSvc, PickupNotificationService and others). locnapi isn't registered in
Code Knowledge, so a non-empty change would have had `local` coverage only.

## Summary
There's nothing to review for this commit. If you want a meaningful analysis of the most recent real change on
master, run the pipeline on `2e9cb2a4`, for example from the portal: repo `cls`, commit `2e9cb2a4`.
