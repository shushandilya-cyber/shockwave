"""§3 tests for s5_runner: NOT_RUN status, tri-state live signal, Gradle/npm runners,
integration-test repo discovery, explicit-SKIPPED gate.

Offline unit tests only (no network/Maven required except @pytest.mark.live).
"""
import pytest

from shockwave.clients.repos import RepoSource
from shockwave.stories.s5_runner import (
    live_integration_scan,
    parse_surefire,
    parse_gradle_results,
    parse_npm_results,
    discover_integration_test_repos,
    run_one,
    run_many,
)
from conftest import commit_all, make_repo

E_MAVEN = {
    "org": "O", "repo": "r", "confidence": "high", "team": None,
    "buildTool": "maven", "testCommand": "mvn -B test",
    "runnable": True, "defaultBranch": "master",
}
E_GRADLE = {**E_MAVEN, "buildTool": "gradle", "testCommand": "./gradlew test --no-daemon"}
E_NPM = {**E_MAVEN, "buildTool": "npm", "testCommand": "npm test"}


# ── §3 SKIPPED gate ───────────────────────────────────────────────────────────────

def test_not_runnable_returns_not_run_not_skipped(cfg):
    """runnable=False must produce NOT_RUN, never SKIPPED (SKIPPED requires --skip-tests)."""
    t = run_one({**E_MAVEN, "runnable": False, "notRunnableReason": "no pom"}, [], RepoSource(cfg), cfg)
    assert t["status"] == "NOT_RUN"
    assert "no pom" in (t["notRunReason"] or "")
    assert t["attempted"] is False


def test_unknown_build_tool_returns_not_run(cfg):
    t = run_one({**E_MAVEN, "buildTool": "unknown", "testCommand": None, "runnable": True}, [], RepoSource(cfg), cfg)
    assert t["status"] == "NOT_RUN"
    assert "not yet supported" in (t["notRunReason"] or "")


def test_explicit_skip_returns_skipped(cfg):
    """Only run_many(..., skip=...) produces SKIPPED — and it labels it prominently."""
    results = run_many([E_MAVEN], [], cfg, skip="user passed --skip-tests")
    assert results[0]["status"] == "SKIPPED"
    assert "[USER REQUESTED SKIP]" in (results[0]["skippedReason"] or "")


def test_skipped_only_from_user_flag(cfg):
    """A non-runnable repo via run_one must NOT produce SKIPPED."""
    t = run_one({**E_MAVEN, "buildTool": "unsupported-lang"}, [], RepoSource(cfg), cfg)
    assert t["status"] == "NOT_RUN"


def test_pipeline_not_run_reason_is_not_labelled_user_skip(cfg):
    results = run_many([E_MAVEN], [], cfg, not_run="empty commit — nothing to test")
    assert results[0]["status"] == "NOT_RUN" and "USER REQUESTED" not in (results[0]["notRunReason"] or "")


def _report(root, cls, skipped=False):
    d = root / "target/surefire-reports"
    d.mkdir(parents=True, exist_ok=True)
    body = "<skipped/>" if skipped else ""
    (d / f"TEST-{cls}.xml").write_text(f'<testsuite tests="1"><testcase classname="{cls}" name="t">{body}</testcase></testsuite>')


def test_live_signal_only_counts_tests_that_executed(tmp_path):
    # INC-010: polis PolisApplicationIT names the service but surefire excludes **/*IT.java
    from shockwave.stories.s5_runner import confirm_live, executed_test_classes
    it = tmp_path / "src/test/java/a/PolisApplicationIT.java"
    it.parent.mkdir(parents=True)
    it.write_text('class PolisApplicationIT { String h = "polis.vip.qa.ebay.com"; }')
    _report(tmp_path, "a.OtherTest")
    signal, note = confirm_live(tmp_path, ["polis"], executed_test_classes(tmp_path))
    assert signal == "NONE" and "did not execute" in note and "PolisApplicationIT" in note
    _report(tmp_path, "a.PolisApplicationIT")
    assert confirm_live(tmp_path, ["polis"], executed_test_classes(tmp_path))[0] == "LIVE"


def test_skipped_testcases_do_not_count_as_executed(tmp_path):
    from shockwave.stories.s5_runner import executed_test_classes
    _report(tmp_path, "a.SkippedIT", skipped=True)
    _report(tmp_path, "a.RanTest")
    assert executed_test_classes(tmp_path) == {"RanTest"}


def test_jdk_is_matched_exactly_then_closest_newer():
    from shockwave.stories.s5_runner import pick_jdk
    jdks = {8: "/j8", 17: "/j17", 21: "/j21", 26: "/j26"}
    assert pick_jdk(8, jdks) == "/j8"
    assert pick_jdk(11, jdks) == "/j17"
    assert pick_jdk(27, jdks) is None


def test_installed_jdks_parses_java_home_listing(monkeypatch):
    import subprocess as sp
    from shockwave.stories import s5_runner
    listing = ('Matching Java Virtual Machines (2):\n'
               '    26.0.1 (arm64) "Oracle Corporation" - "Java SE 26.0.1" /Library/Java/JavaVirtualMachines/jdk-26.jdk/Contents/Home\n'
               '    1.8.0_481 (arm64) "Azul Systems, Inc." - "Zulu 8.91.0.12" /Library/Java/JavaVirtualMachines/zulu-8.jdk/Contents/Home\n'
               '/Library/Java/JavaVirtualMachines/jdk-26.jdk/Contents/Home\n')
    monkeypatch.setattr(s5_runner.subprocess, "run", lambda *a, **k: sp.CompletedProcess(a, 0, "", listing))
    assert s5_runner.installed_jdks() == {26: "/Library/Java/JavaVirtualMachines/jdk-26.jdk/Contents/Home",
                                          8: "/Library/Java/JavaVirtualMachines/zulu-8.jdk/Contents/Home"}


# ── source repo is tested at the change; failures are baselined at the parent ────

def test_source_repo_clone_is_checked_out_at_the_commit(cfg, tmp_path):
    from conftest import git
    from shockwave.stories.s5_runner import _clone
    p = make_repo(cfg.local_repos_root, "pinned")
    (p / "v.txt").write_text("old"); old = commit_all(p, "1")
    (p / "v.txt").write_text("new"); commit_all(p, "2")
    dest = tmp_path / "c"
    how = _clone({"org": "TestOrg", "repo": "pinned", "commit": old}, dest, RepoSource(cfg))
    assert (dest / "v.txt").read_text() == "old" and old[:8] in how
    assert git(dest, "rev-parse", "HEAD").strip() == old


def test_baseline_splits_pre_existing_from_new_failures():
    from shockwave.stories.s5_runner import baseline_at_parent
    seen = {}

    def fake(entry, *a):
        seen.update(entry)
        return {"status": "FAIL", "failedTests": ["a.T#x"], "command": "mvn"}

    r = {"status": "FAIL", "failedTests": ["a.T#x", "a.U#y"]}
    bl = baseline_at_parent(r, E_MAVEN, "parentsha", [], runner=fake)
    assert seen["commit"] == "parentsha" and seen["testFilter"] == ["a.T", "a.U"]
    assert bl["preExisting"] == ["a.T#x"] and bl["newFailures"] == ["a.U#y"]


def test_baseline_only_for_red_suites_with_a_parent():
    from shockwave.stories.s5_runner import baseline_at_parent
    boom = lambda *a: (_ for _ in ()).throw(AssertionError("must not run"))
    assert baseline_at_parent({"status": "PASS", "failedTests": []}, E_MAVEN, "p", [], runner=boom) is None
    assert baseline_at_parent({"status": "FAIL", "failedTests": ["a#b"]}, E_MAVEN, None, [], runner=boom) is None
    assert baseline_at_parent({"status": "FAIL", "failedTests": ["a#b"]}, E_NPM, "p", [], runner=boom)["status"] == "NOT_RUN"


# ── §3 Tri-state live signal ──────────────────────────────────────────────────────

def test_live_signal_from_hostname(tmp_path):
    """LIVE: test file contains the service's staging hostname."""
    t = tmp_path / "src/test/java"; t.mkdir(parents=True)
    (t / "WaypointIT.java").write_text(
        'String url = "https://waypointservice.vip.qa.ebay.com/waypoint";'
    )
    signal, note = live_integration_scan(tmp_path, ["waypointservice"])
    assert signal == "LIVE"
    assert "WaypointIT.java" in note


def test_live_signal_mocked(tmp_path):
    """MOCKED: integration-style test mentions service but uses Mockito."""
    t = tmp_path / "src/test/java"; t.mkdir(parents=True)
    (t / "WaypointIT.java").write_text(
        "@Mock WaypointClient client; // tests waypointservice"
    )
    signal, note = live_integration_scan(tmp_path, ["waypointservice"])
    assert signal == "MOCKED"
    assert "mock" in note.lower()


def test_live_signal_none_when_no_ref(tmp_path):
    """NONE: no reference to the service at all."""
    t = tmp_path / "src/test/java"; t.mkdir(parents=True)
    (t / "FooTest.java").write_text("class FooTest { void a() {} }")
    signal, note = live_integration_scan(tmp_path, ["waypointservice"])
    assert signal == "NONE"


def test_live_signal_none_when_no_apps(tmp_path):
    """NONE: source_apps empty → NONE regardless of file content."""
    t = tmp_path / "src/test/java"; t.mkdir(parents=True)
    (t / "IT.java").write_text("https://waypointservice.vip.qa.ebay.com/waypoint")
    signal, _ = live_integration_scan(tmp_path, [])
    assert signal == "NONE"


def test_hadliveintegrationsignal_derived_from_livestate(tmp_path):
    """hadLiveIntegrationSignal must equal (liveSignal == 'LIVE')."""
    t = tmp_path / "src/test/java"; t.mkdir(parents=True)
    (t / "WaypointIT.java").write_text('String u = "https://waypointservice.vip.qa.ebay.com/x";')
    signal, _ = live_integration_scan(tmp_path, ["waypointservice"])
    assert signal == "LIVE"
    # _result helper must set hadLiveIntegrationSignal = True when liveSignal = LIVE
    from shockwave.stories.s5_runner import _result
    r = _result({"org": "O", "repo": "r"}, liveSignal="LIVE")
    assert r["hadLiveIntegrationSignal"] is True
    r2 = _result({"org": "O", "repo": "r"}, liveSignal="MOCKED")
    assert r2["hadLiveIntegrationSignal"] is False


# ── §3 Gradle / npm parsers ───────────────────────────────────────────────────────

def test_gradle_results_parse(tmp_path):
    d = tmp_path / "build/test-results/test"; d.mkdir(parents=True)
    (d / "TEST-com.acme.Foo.xml").write_text(
        '<testsuite tests="2"><testcase classname="com.acme.Foo" name="ok"/>'
        '<testcase classname="com.acme.Foo" name="bad"><failure/></testcase></testsuite>'
    )
    failed, run = parse_gradle_results(tmp_path)
    assert run == 2
    assert "com.acme.Foo#bad" in failed


def test_npm_results_parse_jest_style():
    log = "Tests: 2 failed, 5 passed\n  1) should do something\n  2) should fail"
    failed, run = parse_npm_results(log)
    assert run == 7
    assert len(failed) == 2


def test_surefire_parse(tmp_path):
    d = tmp_path / "mod/target/surefire-reports"; d.mkdir(parents=True)
    (d / "TEST-a.xml").write_text(
        '<testsuite tests="3"><testcase classname="a.T" name="ok"/>'
        '<testcase classname="a.T" name="bad"><failure/></testcase>'
        '<testcase classname="a.T" name="err"><error/></testcase></testsuite>'
    )
    assert parse_surefire(tmp_path) == (["a.T#bad", "a.T#err"], 3)


# ── §3 Integration-test repo discovery ───────────────────────────────────────────

def test_discover_integration_test_repos(cfg):
    """Repos with the source service hostname are discovered via git grep."""
    # set up a fake local repo that references waypointservice by hostname
    p = make_repo(cfg.local_repos_root, "PudoIntegrationTests", "CoreShipping")
    # Put the hostname reference in a committed file
    src = p / "src" / "test" / "java"; src.mkdir(parents=True)
    (src / "WaypointIT.java").write_text(
        'String url = "https://waypointservice.vip.qa.ebay.com/waypoint";'
    )
    commit_all(p, "add it")

    # another repo that doesn't reference it
    p2 = make_repo(cfg.local_repos_root, "Unrelated", "CoreShipping")
    (p2 / "Other.java").write_text('String url = "https://other.vip.qa.ebay.com/";')
    commit_all(p2, "other")

    rs = RepoSource(cfg)
    found = discover_integration_test_repos(
        rs, ["waypointservice"], exclude={"coreshipping/waypointservice"}
    )
    keys = [f["key"] for f in found]
    # should find PudoIntegrationTests, not Unrelated or the excluded source itself
    assert any("pudointegrationtests" in k.lower() for k in keys), f"not found in {keys}"
    assert not any("unrelated" in k.lower() for k in keys)
    assert not any(k.split("/")[-1].lower() == "waypointservice" for k in keys)
    # every hit cites the files that matched, so the report can show why (§ evidence)
    hit = next(f for f in found if "pudointegrationtests" in f["key"].lower())
    assert hit["evidenceFiles"] == ["src/test/java/WaypointIT.java"]


def test_discover_excludes_repos_already_in_manifest(cfg):
    # A repo Story 3 already found is run anyway; discovering it again would run it twice.
    p = make_repo(cfg.local_repos_root, "LocBridge", "CoreShipping")
    (p / "A.java").write_text('String u = "https://waypointservice.vip.qa.ebay.com/";')
    commit_all(p, "x")
    rs = RepoSource(cfg)
    assert discover_integration_test_repos(rs, ["waypointservice"]) != []
    assert discover_integration_test_repos(
        rs, ["waypointservice"], exclude={"coreshipping/locbridge"}
    ) == []


def test_discover_returns_empty_when_no_apps(cfg):
    rs = RepoSource(cfg)
    assert discover_integration_test_repos(rs, []) == []


# ── §3 Verdict rule for MOCKED ────────────────────────────────────────────────────

def test_verdict_mocked_is_needs_review():
    """PASS + MOCKED must produce NeedsReview, not NotImpacted."""
    from shockwave.stories.s6_verdict import decide
    tr = {
        "org": "O", "repo": "r", "status": "PASS",
        "liveSignal": "MOCKED", "hadLiveIntegrationSignal": False,
        "failedTests": [], "skippedReason": None,
        "liveIntegrationNote": "mocked via WireMock",
    }
    verdict, reasoning, rule = decide({"confidence": "high", "evidence": []}, tr)
    assert verdict == "NeedsReview"
    assert "mock" in reasoning.lower() or "MOCKED" in reasoning


def test_verdict_not_run_is_needs_review():
    """NOT_RUN (technical failure) produces NeedsReview (rule 6)."""
    from shockwave.stories.s6_verdict import decide
    tr = {
        "org": "O", "repo": "r", "status": "NOT_RUN",
        "liveSignal": "NONE", "hadLiveIntegrationSignal": False,
        "failedTests": [], "skippedReason": None, "notRunReason": "gradle toolchain missing",
        "liveIntegrationNote": None,
    }
    verdict, _, rule = decide({"confidence": "high", "evidence": []}, tr)
    assert verdict == "NeedsReview"
    assert rule == 6
