# punotif — Ship-AST/PickupNotificationService @ master `56f03852`

**Verdict: the commit alters code, but nothing outside the repo is affected on current evidence.** It adds a
Prometheus counter on one error branch. The interface change is technically BREAKING, but no implementer
exists outside this repo in anything we can see. The only red tests are two timezone-dependent assertions that
fail the same way at the parent commit.

| | |
|---|---|
| Commit | [`56f03852`](https://github.corp.ebay.com/Ship-AST/PickupNotificationService/commit/56f038522f6df9a2a868610d4776b9f5a6c25aa6) — "Poodle-428 : Metric for 4XX by Provider Name (#602)", shushandilya, 2026-09-16 |
| Parent | `59b6a1bf` |
| Risk class | `BREAKING` (by rule; see "Breaking changes" for why it is not realised) |
| Coverage | `local`: not registered in Code Knowledge, so repos outside the 36-repo local index are UNKNOWN |
| Pipeline run | [`runs/latest-master/punotif/report.md`](../../runs/latest-master/punotif/report.md), JSON `01..07-*.json` alongside |

## What changed
Three files in `PuNotifService`, 18 lines added and none removed:

- `IMonitoringService.java:23` adds the abstract method `recordPartnerIntegrationRequestErrorCountWithProviderName(PartnerIntegrationApiType, String)`.
- `MonitoringService.java` implements it by incrementing the `PunotifRequestCounterByProvider` counter.
- `FindOrdersPerStoreProcessor.findStoreOrders` calls it in the "no orders for this store" branch
  (`STORE_PACKAGE_STATUS_NOT_FOUND`), before the existing error counter. The provider name is null-guarded
  (`pudoProvider != null ? … : "null"`), and the response and status code are unchanged.

## Did it alter anything?
Yes, the code changed. For callers, the only observable effect is a new metric series:

- Response bodies, status codes and endpoints are unchanged. The pipeline found 0 endpoints changed.
- The pipeline labels `findStoreOrders` BEHAVIORAL because metric calls aren't recognised as logging. On
  reading the diff, the change only emits a metric, which is my review on top of the automatic label.

## Breaking changes
| Change | Rule | Realised? |
|---|---|---|
| New abstract method on `IMonitoringService` | an interface gaining an abstract method breaks every implementer outside the repo | **No implementer observed.** The only implementation, `MonitoringService`, is updated in the same commit, and the suite compiled (137 tests ran). No repo in the local index depends on the `PuNotifService` artifact (`rg '<artifactId>PuNotifService</artifactId>'` across `~/Documents/projects` finds none). `Ship-AST/PickUpSvc` has its own `IMonitoringService` in a different package (`com.ebay.shipping.ast.pickup.common.prometheus`), so it's a name collision, not a consumer. |

Repos outside the local index can't be checked because the repo isn't in Code Knowledge. That gap is why the
classification stays BREAKING instead of being downgraded on inference.

## Repos it can affect
| Repo | Confidence | Evidence | Tests | Verdict |
|---|---|---|---|---|
| Ship-AST/PudoTools | low | `src/main/resources/application-Dev.properties:17` calls `punotif.vip.qa.ebay.com`, but no punotif endpoint reaches the changed code | PASS (1 test, default branch) | NeedsReview, P3 (awareness only) |

## Test evidence
- **Source repo at `56f03852`:** FAIL. 137 tests ran and 2 failed:
  `EventBridgeOrderPickupProcessorTest#testRfp` and `#testUkRfp`.
- **Parent baseline:** the same class re-run at `59b6a1bf` fails with the same 2 tests, so both failures are
  pre-existing (verdict rule 8). The assertion is `expected 03.12.2016 but was 04.12.2016`, a date formatted in the
  runner's local timezone (IST), so it fails on any machine far enough from UTC.
- **Verdict for the source repo:** NeedsReview under rule 8. With the pre-existing failures set aside, the suite
  passes but has no test that exercises the new metric.

## Summary
- Safe to ship as far as callers are concerned.
- Follow-ups for the team:
  - Pin the timezone in `EventBridgeOrderPickupProcessorTest`, for example with `-Duser.timezone=UTC` in surefire. It is red on developer machines outside UTC.
  - Add a unit test that asserts the new counter fires on the empty-store path.
