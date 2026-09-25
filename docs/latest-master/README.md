# Latest master commit — impact reports (2026-09-25)

Every claim links to a commit, a file:line, or a pipeline artifact. Those are under `runs/latest-master/<name>/`,
except polis and pickupeligibility, which are under `runs/latest-master-v3/<name>/` after the live-signal fix. Runs
were read-only, with Jira in dry-run.

- **How the source repo was tested:** at the analysed commit, with a parent-commit re-run whenever tests failed.
- **How consumers were tested:** at their default branch.
- **Local context index:** the 36 repos in `~/Documents/projects`.

| Repo | Master tip | Altered anything? | Risk | Consumers flagged | Breaking | Report |
|---|---|---|---|---|---|---|
| punotif (Ship-AST/PickupNotificationService) | `56f03852` | yes: new error metric on one branch | BREAKING by rule, not realised | 1 (PudoTools, low) | new abstract interface method, but no external implementer found | [punotif.md](punotif.md) |
| pickupeligibility (Ship-AST/PickupEligibilityService) | `d19abca5` | no: logging only | SAFE | 6 (all P3) | none | [pickupeligibility.md](pickupeligibility.md) |
| pickupsvc (Ship-AST/PickUpSvc) | `ddc33832` | runtime only: raptor-io-parent 4.0.3→4.0.4 | BEHAVIORAL | 0 | none | [pickupsvc.md](pickupsvc.md) |
| cls (CoreShipping/locnapi) | `07f0e235` | **no: empty commit** (tree = parent `2e9cb2a4`) | SAFE | 0 | none | [cls-locnapi.md](cls-locnapi.md) |
| polis (CoreShipping/polis) | `ee5dd5df` | yes: new enum constant `ORANGECONNEX` | BEHAVIORAL | 0 | none | [polis.md](polis.md) |

## Test results

| Repo | Suite at its commit | Parent baseline |
|---|---|---|
| punotif | 137 tests, 2 fail | both failures also fail at the parent, a timezone-dependent assertion (rule 8) |
| pickupeligibility | 752 tests, pass | not needed |
| pickupsvc | 162 tests, pass | not needed |
| polis | 112 tests, pass | not needed |
| cls (locnapi) | `NOT_RUN`: empty commit, nothing to test | not applicable (rule 0) |

Consumer suites all pass except `Selling/userpreferences` `contextLoads`, which is unrelated; see the pickupeligibility report.

No commit on master today introduces a failure or breaks a consumer. The follow-ups are:

- punotif's timezone-dependent test;
- a startup check for pickupsvc's platform bump;
- polis's new provider has no end-to-end test that runs under `mvn test`.

The live-integration tests in polis, pickupeligibility and locnapi are all excluded from `mvn test`. Each of those
repos has no test in its default build that exercises the service live. See the
[backtest analysis](../backtest/five-repos-analysis.md) for what that meant for past incidents.
