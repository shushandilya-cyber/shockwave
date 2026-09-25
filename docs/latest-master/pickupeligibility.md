# pickupeligibility — Ship-AST/PickupEligibilityService @ master `d19abca5`

**Verdict: logging-only change, nothing altered for callers.** Three CAL log statements were added in two
methods. With the log calls removed, the old and new method bodies are identical. All six consumers' suites
pass except one, whose failure is a Spring context-load test that is unrelated to this change on the evidence
available.

| | |
|---|---|
| Commit | [`d19abca5`](https://github.corp.ebay.com/Ship-AST/PickupEligibilityService/commit/d19abca5ac4e7d5157a344d12cb7e835530b1858) — "[POODLE-663] add log for dual-read (#495)", qwang15, 2026-09-24 |
| Risk class | `SAFE` (logging-only rule) |
| Coverage | `graph+local`: Code Knowledge index `Ship-AST:PickupEligibilityService:master` plus the local index |
| Pipeline run | [`runs/latest-master-v3/pickupeligibility/report.md`](../../runs/latest-master-v3/pickupeligibility/report.md) |

## What changed
Four files, 93 lines added and none removed:

- `UPCSFrameworkLib/.../ULOSDalUtilProxy.java`, in `runDualRead`: adds
  `CalEventHelper.writeLog("NUDOC_DUAL_READ_RUN_SEND_BES", …, "userId=" + event.getUserId() + ", offerId=" + event.getOfferId(), "")`.
- `upcsDal/.../NuDocUserProgEligibilityAndIntentAdapter.java`, in `findEligibilitiesAndIntents`: two
  `CalEventHelper.writeLog(…)` calls on the existing "records not found, ignore" branches.
- Two new test classes: `ULOSDalUtilProxyTest` (+31 lines) and `NuDocUserProgEligibilityAndIntentAdapterUnitTest` (+58 lines).

## Did it alter anything?
No, nothing is observable to callers. The pipeline's logging-only check stripped every log/CAL statement from the
old and new method bodies and found them identical, which is recorded as `loggingOnly: true` on both symbols. The only new
runtime work is two getter calls inside the log message.

Earlier versions of the pipeline labelled this BEHAVIORAL ("logic change in private helper"). This run fixed that.

## Breaking changes
None.

## Repos it can affect
The consumers are real, but none of them is reached by the changed code, because no code changed. So all are P3.

| Repo | Confidence | Evidence | Tests (default branch) | Verdict |
|---|---|---|---|---|
| CoreShipping/xperience | medium | app dependency `xperience → pickupeligibility` (graph) | PASS, 642 tests | NeedsReview, P3 |
| Selling/apisellingio | medium | app dependency `apisellingio → pickupeligibility` (graph) | "PASS", but **0 tests ran**, so there's no evidence either way | NeedsReview, P3 |
| Ship-AST/PickUpSvc | medium | `PickupLpsClient.fetchEligiblePrograms` calls `POST /internal/buy/guest/PICKUP`, plus an app dependency and `application-Dev.properties:11` | PASS, 162 tests | NeedsReview, P3 |
| Ship-AST/PudoTools | medium | `application-Dev.properties:13` calls `pickupeligibility.vip.qa.ebay.com` | PASS, 1 test | NeedsReview, P3 |
| Ship-AST/shipprefxsvc | medium | `PudoClient.getUserOfferStatusesInternal` calls `GET /internal/{public_user_id}/PICKUP`, plus the PUT variant | PASS, 25 tests | NeedsReview, P3 |
| Selling/userpreferences | low | `PickupEligibilitySvcClient.getPickupUserOfferList` calls `GET /internal/{public_user_id}/PICKUP` | FAIL: `UserpreferencesApplicationTest#contextLoads` (1431 tests ran) | NeedsReview, P3, rule 5 |

Also:

- `ship-ast/pickupevalbatch` passed (2 tests).
- `ship-ast/pudomonitor` is `NOT_RUN` because there's no build file at the repo root.
- The graph has one blind spot: `ULOSDalUtilProxy#runDualRead` couldn't be resolved in Code Knowledge, so callers of
  that one symbol are **unknown**, not absent. Because the change is logging-only, that doesn't change the verdict.

## Test evidence
- **Source repo at `d19abca5`:** PASS, 752 tests. Verdict: NeedsReview (rule 7).
  - The live integration test `GuestUserBuyingFlowIntegrationTest` did **not** run. `upcsService/pom.xml` sets
    `skipIntegrationTests=true` and only runs `**/*IntegrationTest.java` through failsafe.
  - An earlier run counted it anyway and reported NotImpacted (rule 3). The pipeline now counts only tests that
    executed.
  - For a logging-only change, the unit suite passing is enough.
- **userpreferences:** it ran at its own default branch, which can't contain a PickupEligibilityService commit, and
  the failing test is a whole-application context load. The root cause of that failure is **unknown**. The captured
  log shows only `BUILD FAILURE` in `userpreferencesService`. Treat it as a pre-existing environment issue in that
  repo, not as evidence against this commit.

## Summary
Safe. There is no action for consumers. Two points for the service team:

- The new log line reads `event.getUserId()` inside `runDualRead`. It relies on `event` being non-null, as the
  surrounding code already assumes.
- `apisellingio` runs 0 tests, so it gives no protection as a consumer.
