import pytest

from shockwave.clients.repos import RepoSource
from shockwave.stories.s5_runner import (
    _classify_clone_error,
    classify_build_failure,
    live_integration_scan,
    parse_surefire,
    run_one,
    targeted_test_classes,
)
from conftest import commit_all, make_repo

E = {"org": "O", "repo": "r", "confidence": "high", "team": None, "buildTool": "maven", "testCommand": "mvn -B test",
     "runnable": True, "defaultBranch": "master"}


def test_not_runnable_skips_without_clone(cfg):
    # §3: not-runnable is NOT_RUN (SKIPPED only on explicit --skip-tests)
    t = run_one({**E, "runnable": False, "notRunnableReason": "no pom"}, [], RepoSource(cfg), cfg)
    assert t["status"] == "NOT_RUN"
    assert t["attempted"] is False
    assert "no pom" in (t["notRunReason"] or "")


def test_unsupported_build_tool_is_not_run(cfg):
    # §3: unsupported build tool → NOT_RUN, not SKIPPED. Returns before any clone,
    # so this stays offline.
    t = run_one({**E, "buildTool": "unknown", "testCommand": "make test"}, [], RepoSource(cfg), cfg)
    assert t["status"] == "NOT_RUN"
    assert "not yet supported" in (t["notRunReason"] or "")


def test_clone_error_is_specific_and_actionable(cfg):
    # §3: every non-execution names a cause someone can act on. "Repository not found"
    # must not be reported as an auth failure — they need different fixes.
    assert "not visible" in _classify_clone_error("remote: Repository not found.")
    assert "not authorised" in _classify_clone_error("fatal: Authentication failed for 'x'")
    assert "credentials" in _classify_clone_error("could not read Username for 'https://x'")
    assert "branch" in _classify_clone_error("fatal: Remote branch nope not found in upstream origin")
    assert "VPN" in _classify_clone_error("fatal: unable to access: Could not resolve host: x")
    assert _classify_clone_error("something odd") == "clone failed"


@pytest.mark.parametrize("log,tool,expect_status,expect_text", [
    ("[ERROR] Failed to execute: Could not resolve dependencies for project x", "mvn",
     "NOT_RUN", "dependency resolution"),
    ("npm ERR! Missing script: \"test\"", "npm", "NOT_RUN", "no 'test' script"),
    ("npm ERR! Cannot find module 'jest'", "npm", "NOT_RUN", "dependencies are not installed"),
    ("[ERROR] COMPILATION ERROR : cannot find symbol", "mvn", "ERROR", "failed to compile"),
    ("No matching toolchains found for Java 21", "gradle", "NOT_RUN", "toolchain"),
    ("weird unparseable output", "mvn", "NOT_RUN", "no recognised cause"),
])
def test_build_failure_classification(log, tool, expect_status, expect_text):
    # A compile break is a real signal (ERROR -> NeedsReview); a broken runner is not
    # (NOT_RUN). Collapsing them would let env problems masquerade as evidence.
    status, reason = classify_build_failure(log, tool, 1)
    assert status == expect_status
    assert expect_text in reason


def test_targeted_test_classes(tmp_path):
    t = tmp_path / "src/test/java/x"; t.mkdir(parents=True)
    (t / "WaypointClientTest.java").write_text("class X { WaypointService s; }")
    (t / "UnrelatedTest.java").write_text("class Y {}")
    (t / "Helper.java").write_text("class H { waypointservice; }")  # not a test class
    assert targeted_test_classes(tmp_path, ["waypointservice"]) == ["WaypointClientTest"]
    assert targeted_test_classes(tmp_path, []) == []


def test_surefire_parse(tmp_path):
    d = tmp_path / "mod/target/surefire-reports"; d.mkdir(parents=True)
    (d / "TEST-a.xml").write_text('<testsuite tests="3"><testcase classname="a.T" name="ok"/>'
                                  '<testcase classname="a.T" name="bad"><failure/></testcase>'
                                  '<testcase classname="a.T" name="err"><error/></testcase></testsuite>')
    assert parse_surefire(tmp_path) == (["a.T#bad", "a.T#err"], 3)


def test_live_scan(tmp_path):
    # §3: tri-state live signal — FooTest.java with @Mock references service name
    # but file is not named *IT so it stays NONE (not MOCKED, because _IT_NAME doesn't match)
    t = tmp_path / "src/test/java"; t.mkdir(parents=True)
    (t / "FooTest.java").write_text("class FooTest { @Mock WaypointClient c; }")
    assert live_integration_scan(tmp_path, ["waypointservice"])[0] == "NONE"
    # WaypointIT.java with a real hostname → LIVE
    (t / "WaypointIT.java").write_text('String u = "https://waypointservice.vip.qa.ebay.com/waypoint";')
    live, note = live_integration_scan(tmp_path, ["waypointservice"])
    assert live == "LIVE" and "WaypointIT.java" in note
    # empty apps → NONE
    assert live_integration_scan(tmp_path, [])[0] == "NONE"


POM = """<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<groupId>t</groupId><artifactId>t</artifactId><version>1</version>
<properties><maven.compiler.release>17</maven.compiler.release><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
</project>"""


@pytest.mark.live
@pytest.mark.parametrize("assertion,expected", [("assertEquals(1, 1)", "PASS"), ("assertEquals(1, 2)", "FAIL")])
def test_real_maven_pass_and_fail(cfg, assertion, expected):
    p = make_repo(cfg.local_repos_root, f"mvnfix{expected}", "O")
    (p / "pom.xml").write_text(POM)
    t = p / "src/test/java/t"; t.mkdir(parents=True)
    (t / "ATest.java").write_text(f"package t; import static org.junit.Assert.*; public class ATest {{ @org.junit.Test public void a() {{ {assertion}; }} }}")
    commit_all(p, "1")
    r = run_one({**E, "org": "O", "repo": f"mvnfix{expected}"}, ["waypointservice"], RepoSource(cfg), cfg)
    assert r["status"] == expected, r["logExcerpt"][-1500:]
    if expected == "FAIL":
        assert r["failedTests"] == ["t.ATest#a"]

