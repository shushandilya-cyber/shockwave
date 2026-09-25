# Backtest: 5 high-priority incidents in punotif, pickupeligibility, pickupsvc, cls, polis

**Result: shockwave would have caught 2 of the 5 incidents at commit time, for the right reason:** INC-006 and
INC-009, both missing Sandbox profiles. The other three are misses:

- INC-008 and INC-010 were startup failures that the repos' own `mvn test` suites can't see, because neither suite
  boots the application.
- INC-007's culprit is unconfirmed, and no test covers the suspected mechanism.

The raw, auto-generated per-incident output is in [five-repos-incidents.md](five-repos-incidents.md). The run
artifacts are in `runs/backtest-five-v3/backtest-<id>-<sha>/`, and the summary is in `runs/backtest-five-v3/summary.json`.

## How it was run
```bash
shockwave backtest --incidents incidents.yaml --only INC-006 … --only INC-010 \
  --run-tests --source-tests-only --run-dir-root runs/backtest-five-v3
```
- **Stages:** each incident replays S1–S7 at its culprit commit. The changed repo's suite is built and run at that
  commit in a temp clone. When it fails, the failing classes are re-run at the parent commit to separate new
  failures from pre-existing ones.
- **Safety:** read-only, Jira in dry-run, nothing pushed.
- **What counts as caught:** each incident declares `catch_signals` that match its real failure mechanism, and it
  counts only if one of those fired. Being named in the blast radius isn't enough, because the source repo always is.

## Incidents
All five are P2 Bugs in Jira. There were no P1s for these repos. Selection criteria and rejected candidates are in
`incidents.yaml`.

| ID | Jira | Repo | Culprit → fix | What happened | How long until detected |
|---|---|---|---|---|---|
| INC-006 | DELINS-147 | punotif | `a196d8b4` → `8b4067e2` | The RaptorIO migration added profiles for Dev, QA, Staging, Pre-Production and Production, but not Sandbox, so the Sandbox deploy failed | ~4 h (deploy alert) |
| INC-007 | POODLE-55 | pickupeligibility | `fb138960` (suspected) → unknown | B2C PUDO-eligible listings fell 15–20% week over week; the suspect is a minimal Sale readset that drops `flags12` | ~2 weeks (weekly business review) |
| INC-008 | VOODOO-9044 | pickupsvc | `95c0f4b7` → revert `d4db914` | The SWU `raptor-io-parent` 3.1.2→3.1.3 broke the release pipeline, and the service "won't start up on local" | 5 days |
| INC-009 | CBTSHIPP-2026 | cls (locnapi) | `f8da817` → `0419015` | The RaptorIO migration added no Sandbox profile, so Sandbox `createInventoryLocation(WAREHOUSE)` returned 25001 | 7 days (partner report) |
| INC-010 | RAPTOR-42994 / DELINS-565 | polis | `dbcb94f` → `cb4034e` | Spring Boot 3 SWU; startup fails with `NoClassDefFoundError` (`…/UpdateReturnShippingCarrierRequest`) in `returnSvcV2Client` | 1 day |

## Results
| ID | Caught? | Signal that fired | Risk class (expected → actual) | Source tests at culprit (JDK) | Source verdict |
|---|---|---|---|---|---|
| INC-006 | **yes** | `environmentGap:Sandbox` (module `PuNotifService`) | BREAKING → BREAKING | FAIL: 129 run, 2 failed; both also fail at parent `cf20f237` (Zulu 17) | NeedsReview, rule 8 (pre-existing) |
| INC-007 | no | none (needed `sourceTestsFail`) | BEHAVIORAL → BEHAVIORAL | PASS: 245 run (Zulu 17) | NeedsReview, rule 7 |
| INC-008 | no | none (needed `sourceTestsFail`) | BEHAVIORAL → BEHAVIORAL | PASS: 155 run (Zulu 17) | NeedsReview, rule 7 |
| INC-009 | **yes** | `environmentGap:Sandbox` and `environmentGap:LnP` | BREAKING → BREAKING | PASS: 71 run (Zulu 17) | NeedsReview, rule 7 |
| INC-010 | no | none (needed `sourceTestsFail`) | BEHAVIORAL → **BREAKING** | PASS: 43 run (Zulu 17) | NeedsReview, rule 7 |

Headline numbers:

- **Caught for the right reason:** 2/5.
- **Risk class correct:** 4/5.
- **Pipeline errors:** 0.
- **Recall:** 100% on the 4 verified incidents, but that's automatic for self-impact incidents, so read the
  "Caught?" column instead.

## Per incident

### INC-006 punotif, caught
- **Evidence:** at `a196d8b4`, module `PuNotifService` has the legacy directory
  `src/main/resources/META-INF/configuration/Sandbox/` and profiles `dev, pre-production, production, qa, staging`,
  but no `application-Sandbox.properties`. That gap wasn't present at the parent. It shows up in the report as a
  BREAKING change of kind `environment`: "every caller of this service in Sandbox".
- **The fix confirms it:** `8b4067e2` adds exactly that file and nothing else. Running the rule on `8b4067e2`
  reports no gap.
- **Would have saved:** the ~4 h of failed Sandbox deploys, because the gap is reported when the commit lands,
  before any deploy.
- **Tests:** the two failures (`EventBridgeOrderPickupProcessorTest#testRfp`, `#testUkRfp`) are the same
  timezone-dependent assertion that still fails on master today. The parent baseline marks them pre-existing, so
  they don't produce a false "Impacted".

### INC-009 cls (locnapi), caught
- **Evidence:** at `f8da817`, the root module keeps `META-INF/configuration/Sandbox/` and `…/LnP/`, but the
  migration added profiles only for `dev, integration, pre-production, production, qa, unit`.
- **The fix confirms it:** `0419015` (PR #293) adds `application-Sandbox.properties`, and the rule reports no
  Sandbox gap there.
- **Would have saved:** 7 days, since the incident was found by a 3P partner.
- **Second gap:** the LnP gap was also flagged. It has no recorded incident. It's either unused or still latent,
  and **someone should check whether LnP runs on this service**.

### INC-008 pickupsvc, missed
- **What the pipeline saw:** only a `buildChange:parent:raptor-io-parent` signal and BEHAVIORAL. Every monthly SWU
  looks like that, so it isn't counted as a catch.
- **Why the tests passed:** 155 tests passed at `95c0f4b7`. No test in the repo at that commit boots a Spring
  context: `git grep` for `SpringBootTest|SpringRunner|SpringExtension|ContextConfiguration` finds nothing. A
  failure at application startup is invisible to `mvn test` in this repo by construction.
- **Root cause:** **unknown**. The revert PR #441 says only "The ECD pipeline failed … won't start up on local".

### INC-010 polis, missed
- **Why the tests passed:** 43 tests passed at `dbcb94f`. The only test that could start the app,
  `src/test/java/com/ebay/raptor/samples/it/PolisApplicationIT.java`, has two problems:
  1. The `pom.xml` surefire config excludes `**/*IT.java`, so it doesn't run under `mvn test`.
  2. Even under failsafe, its body is a no-op unless `testHost` is set.

  The `NoClassDefFoundError` only appears when the Spring context builds `returnSvcV2Client`, and nothing in the
  build does that.
- **Risk class mismatch:** the pipeline said BREAKING because the OpenRewrite changes altered two signatures,
  `PolisAppConfiguration(JobBuilderFactory,StepBuilderFactory)` and `Writer#write(List<? extends String>)`. polis
  is a job application, not a library, so no external caller exists. The label is an overstatement, though not a
  harmful one here.

### INC-007 pickupeligibility, missed (unverified culprit)
- **Why it isn't counted:** the culprit comes from an automated triage comment on POODLE-55. The human follow-up
  says only "some code changes which were reverted". So this incident is excluded from the headline, and the miss
  says little.
- **What the pipeline saw:** BEHAVIORAL is correct. `EligibleForPudoDataLoader` now reads `Sale` with
  `READSET_MINIMUM`.
- **Why the tests passed:** 245 tests passed. None asserts eligibility on a `Sale` with `flags12` set. Whether a
  field is in a readset is invisible to static analysis: the signature and the call site are unchanged.

## A pipeline bug this backtest exposed, now fixed
The first backtest pass gave INC-007, INC-009 and INC-010 the source verdict **NotImpacted (rule 3, "live
integration tests passed")**. In all three, the "live" test never ran:

| Repo | Test | Why it didn't run |
|---|---|---|
| polis | `PolisApplicationIT` | surefire excludes `**/*IT.java` |
| pickupeligibility | `GuestUserBuyingFlowIntegrationTest` | `upcsService/pom.xml` sets `skipIntegrationTests=true` and runs `*IntegrationTest` only through failsafe |
| locnapi | `LocnApiIntegrationTest` (`@Tag("IntegrationTest")`) | not in the surefire reports; the exclusion is probably inherited from the parent POM, and exactly where it's configured is unknown |

The live and mocked signals were computed by scanning test *sources*. They now count only test classes that appear,
non-skipped, in the JUnit XML reports (`confirm_live` and `executed_test_classes` in `s5_runner.py`, with 2 unit
tests). The report names the tests that were not counted.

Two other fixes came out of the same backtest:

- **JDK selection:** `java_home -v 8` and `-v 11` silently return JDK 26, so Java 8 code was being built on the
  wrong JDK. It now matches the major version exactly, otherwise the closest newer installed JDK (2 unit tests).
- **Stalled clones:** git now aborts transfers that stall, via `GIT_HTTP_LOW_SPEED_*`. An overnight clone of
  `ship-ast/pudotools` hung for 5 h, because subprocess timeouts don't advance while the laptop sleeps.

## What would catch the misses
In order of payoff, based on the evidence above. None of these is implemented yet.

1. **A startup check for the source repo** (INC-008, INC-010). Build the application jar and start it under a test
   profile for a bounded time. Alternatively, where the team's integration tests don't need a remote host, run
   them with `mvn verify -DskipIntegrationTests=false`.
   - Caveat: Raptor apps may need environment secrets to boot locally. Whether that works for these repos is
     **untested**.
2. **A classpath-completeness check** (INC-010). A `NoClassDefFoundError` is a property of the packaged classpath,
   so `jdeps --missing-deps` on the built artifact could find it without starting anything. This is a proposal
   and hasn't been verified on `dbcb94f`.
3. **Team-side:** add a default-build `contextLoads`-style test to PickUpSvc and polis. Today neither default
   build starts the application, which is why both SWU incidents reached deploy.
4. **A readset rule** (INC-007). Flag a changed `READSET_*` argument on a DAO read as a data-shape change, and
   list which fields the old and new readsets carry. This needs the readset definitions, so it's a proposal.
5. **Rate in-repo signature changes lower for applications** (the INC-010 risk class). Don't rate them BREAKING
   when the module isn't consumed as a library. Only do this when coverage is `graph+local`, so a real external
   caller isn't hidden.

## What this means for the current master commits
Both "caught" incidents came from the same RaptorIO-migration pattern, and the rule is active for every commit. The
two misses share one gap that is still present on master today:

- polis, PickUpSvc, pickupeligibility and locnapi all run no test in `mvn test` that exercises the service live.
- A startup failure from the next SWU would reach deploy the same way.

See [../latest-master/README.md](../latest-master/README.md).
