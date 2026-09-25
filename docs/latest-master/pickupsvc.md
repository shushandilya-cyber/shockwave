# pickupsvc — Ship-AST/PickUpSvc @ master `ddc33832`

**Verdict: runtime platform upgrade, no API or code change.** It bumps `raptor-io-parent` 4.0.3 → 4.0.4 and drops an
unused property. The repo's own suite passes at the commit. No consumer is affected at the code level. The
remaining risk is runtime behaviour of the new platform release, which only a deployment exercises.

| | |
|---|---|
| Commit | [`ddc33832`](https://github.corp.ebay.com/Ship-AST/PickUpSvc/commit/ddc338327504612a0cbdfaf4eb6977707f020c46) — "POODLE-531 Upgrade by UaaS (#501)", raptor upgrade bot, 2026-09-07 |
| Risk class | `BEHAVIORAL` (runtime dependency change) |
| Coverage | `graph+local`: Code Knowledge index `Ship-AST:PickUpSvc:master` plus the local index |
| Pipeline run | [`runs/latest-master/pickupsvc/report.md`](../../runs/latest-master/pickupsvc/report.md) |

## What changed
`pom.xml` only, with 2 lines added and 2 removed:

- The parent `com.ebay.raptorio.platform:raptor-io-parent` goes from `4.0.3-RELEASE` to `4.0.4-RELEASE`.
- The property `<commonoperational.component.version>4.0.1-RELEASE</commonoperational.component.version>` is removed.
  `rg commonoperational` across every `*.xml` in the repo finds no remaining reference, so the build doesn't depend
  on it. The test run below confirms that.

## Did it alter anything?
- **Code:** no. 0 symbols and 0 endpoints changed.
- **Runtime:** yes. Every transitive platform library managed by `raptor-io-parent` can move. That's why the risk class is
  BEHAVIORAL and not SAFE. INC-008 (VOODOO-9044) was exactly this pattern: an SWU parent bump in this repo stopped
  the service from starting.

## Breaking changes
None found.

## Repos it can affect
No consumer references changed code. Two repos are known consumers and are listed for awareness only:
`CoreShipping/LogisticEvaluationService` and `Ship-AST/PudoIntegrationTests`.

## Test evidence
- **Source repo at `ddc33832`:** PASS, 162 tests, built with Zulu 17. Verdict: NeedsReview (rule 7), because the
  suite passes but doesn't start the application against real dependencies.

## Summary
Low risk, but not zero. The unit suite can't show whether the service **starts**. Before production, confirm the
QA or staging deployment of this commit came up healthy (startup logs, a `/health` check). That is the failure
mode INC-008 hit.
