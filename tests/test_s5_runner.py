import pytest

from shockwave.clients.repos import RepoSource
from shockwave.stories.s5_runner import live_integration_scan, parse_surefire, run_one
from conftest import commit_all, make_repo

E = {"org": "O", "repo": "r", "confidence": "high", "team": None, "buildTool": "maven", "testCommand": "mvn -B test",
     "runnable": True, "defaultBranch": "master"}


def test_not_runnable_skips_without_clone(cfg):
    t = run_one({**E, "runnable": False, "notRunnableReason": "no pom"}, [], RepoSource(cfg), cfg)
    assert (t["status"], t["attempted"], t["skippedReason"]) == ("SKIPPED", False, "no pom")


def test_non_maven_skipped(cfg):
    t = run_one({**E, "buildTool": "npm", "testCommand": "npm test"}, [], RepoSource(cfg), cfg)
    assert t["status"] == "SKIPPED" and "npm not yet supported" in t["skippedReason"]


def test_surefire_parse(tmp_path):
    d = tmp_path / "mod/target/surefire-reports"; d.mkdir(parents=True)
    (d / "TEST-a.xml").write_text('<testsuite tests="3"><testcase classname="a.T" name="ok"/>'
                                  '<testcase classname="a.T" name="bad"><failure/></testcase>'
                                  '<testcase classname="a.T" name="err"><error/></testcase></testsuite>')
    assert parse_surefire(tmp_path) == (["a.T#bad", "a.T#err"], 3)


def test_live_scan(tmp_path):
    t = tmp_path / "src/test/java"; t.mkdir(parents=True)
    (t / "FooTest.java").write_text("class FooTest { @Mock WaypointClient c; }")
    assert live_integration_scan(tmp_path, ["waypointservice"])[0] is False
    (t / "WaypointIT.java").write_text('String u = "https://waypointservice.vip.qa.ebay.com/waypoint";')
    live, note = live_integration_scan(tmp_path, ["waypointservice"])
    assert live and "WaypointIT.java" in note
    assert live_integration_scan(tmp_path, [])[0] is False


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

