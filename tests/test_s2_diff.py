from shockwave import javaparse
from shockwave.clients.repos import RepoSource
from shockwave.stories.s2_diff import extract
from conftest import commit_all, make_repo

SRC_V1 = '''package com.acme.svc;

import java.util.List;

@Path("/orders")
public class OrderResource {
    private final String name = "x;{";   // tricky literal
    /* comment with { brace */
    @GET
    @Path("/{id}")
    public Response getOrder(@PathParam("id") String id) throws IOException {
        if (id == null) { return null; }
        Runnable r = () -> { System.out.println("}"); };
        return Response.ok(id).build();
    }

    public int count(List<String> xs, final Map<String, List<Integer>> m) {
        return xs.size();
    }

    static class Helper {
        void help() { }
    }
}
'''

SRC_V2 = SRC_V1.replace("return xs.size();", "return xs.size() + 1;").replace(
    "void help() { }", "void help() { }\n        void added() { }").replace(
    "if (id == null) { return null; }", "if (id == null) { throw new IllegalArgumentException(); }")


def test_outline_nested_generics_annotations_literals():
    pkg, decls = javaparse.outline(SRC_V1)
    assert pkg == "com.acme.svc"
    names = {(d.kind, d.cls, d.name, d.params) for d in decls}
    assert ("class", "", "OrderResource", "") in names
    assert ("method", "OrderResource", "getOrder", "String") in names
    assert ("method", "OrderResource", "count", "List<String>,Map<String,List<Integer>>") in names
    assert ("class", "OrderResource", "Helper", "") in names
    assert ("method", "OrderResource.Helper", "help", "") in names
    assert ("field", "OrderResource", "name", "") in names
    assert not any(d.name in ("if", "println") for d in decls)


ANNOTATED = '''package com.acme.svc;
@Component
@ApiRef(api = "commercelocation")
@Path("/")
public class LocationResource implements LocationResourceApi {
  @Inject @Named(value = "ctx") private ContextBean contextBean;

  @Override
  @ApiMethod(resource = "location", name = "deleteLocation")
  public Response deleteLocation(String locationId) throws DalException {
    int a = 1;
    return null;
  }
}
'''


def test_annotation_arguments_do_not_hide_declarations():
    """`@ApiRef(api = "x")` used to be read as an assignment, dropping the whole class."""
    _, decls = javaparse.outline(ANNOTATED)
    got = {(d.kind, d.name) for d in decls}
    assert ("class", "LocationResource") in got
    assert ("method", "deleteLocation") in got
    assert ("field", "contextBean") in got


def test_endpoint_extraction_jaxrs():
    pkg, decls = javaparse.outline(SRC_V1)
    cls = next(d for d in decls if d.name == "OrderResource")
    m = next(d for d in decls if d.name == "getOrder")
    assert javaparse.endpoints_for(m, cls) == [{"method": "GET", "path": "/orders/{id}", "operationId": "getOrder"}]


def test_extract_modified_added_and_endpoint(cfg):
    p = make_repo(cfg.local_repos_root, "svc")
    f = p / "src/main/java/com/acme/svc/OrderResource.java"
    f.parent.mkdir(parents=True)
    f.write_text(SRC_V1); commit_all(p, "v1")
    f.write_text(SRC_V2); (p / "README.md").write_text("docs"); sha = commit_all(p, "v2")
    out = extract({"org": "TestOrg", "repo": "svc", "commit": sha, "branch": "master"}, RepoSource(cfg), cfg)
    got = {(s["changeType"], s["kind"], s["name"]) for s in out["changedSymbols"]}
    assert ("modified", "method", "com.acme.svc.OrderResource#count(List<String>,Map<String,List<Integer>>)") in got
    assert ("modified", "method", "com.acme.svc.OrderResource#getOrder(String)") in got
    assert ("added", "method", "com.acme.svc.OrderResource.Helper#added()") in got
    assert {"method": "GET", "path": "/orders/{id}", "operationId": "getOrder", "changeType": "modified"} in out["changedApiEndpoints"]
    assert "README.md" in out["changedFiles"]


def test_docs_only_commit_gives_empty_arrays(cfg):
    p = make_repo(cfg.local_repos_root, "svc2")
    (p / "README.md").write_text("a"); commit_all(p, "1")
    (p / "README.md").write_text("b"); sha = commit_all(p, "2")
    out = extract({"org": "TestOrg", "repo": "svc2", "commit": sha}, RepoSource(cfg), cfg)
    assert out["changedFiles"] == ["README.md"] and out["changedSymbols"] == [] and out["changedApiEndpoints"] == []


def test_removed_method_and_test_files_ignored(cfg):
    p = make_repo(cfg.local_repos_root, "svc3")
    f = p / "src/main/java/com/acme/svc/OrderResource.java"; f.parent.mkdir(parents=True)
    t = p / "src/test/java/com/acme/svc/OrderResourceTest.java"; t.parent.mkdir(parents=True)
    f.write_text(SRC_V1); t.write_text("package x; class OrderResourceTest { void a() {} }"); commit_all(p, "1")
    f.write_text(SRC_V1.replace('''    public int count(List<String> xs, final Map<String, List<Integer>> m) {
        return xs.size();
    }
''', "")); t.write_text("package x; class OrderResourceTest { void a() { int i; } }")
    sha = commit_all(p, "2")
    out = extract({"org": "TestOrg", "repo": "svc3", "commit": sha}, RepoSource(cfg), cfg)
    names = [(s["changeType"], s["name"]) for s in out["changedSymbols"]]
    assert ("removed", "com.acme.svc.OrderResource#count(List<String>,Map<String,List<Integer>>)") in names
    assert not any("OrderResourceTest" in n for _, n in names)



def _two_commits(cfg, name, rel, v1, v2):
    p = make_repo(cfg.local_repos_root, name)
    f = p / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(v1); commit_all(p, "1")
    f.write_text(v2); sha = commit_all(p, "2")
    return extract({"org": "TestOrg", "repo": name, "commit": sha}, RepoSource(cfg), cfg)


def test_interface_method_is_public_and_abstract(cfg):
    out = _two_commits(cfg, "ifc", "src/main/java/a/IMon.java",
                       "package a;\npublic interface IMon {\n  void a();\n}\n",
                       "package a;\npublic interface IMon {\n  void a();\n  void b(String x);\n  default void c() {}\n}\n")
    by = {s["member"]: s for s in out["changedSymbols"] if s["kind"] == "method"}
    assert by["b"]["visibility"] == "public" and by["b"]["abstract"] is True and by["b"]["classKind"] == "interface"
    assert by["c"]["abstract"] is False
    assert out["changesSummary"]["overallRiskClass"] == "BREAKING"


_SVC = "package a;\npublic class Svc {\n  public int f(int x) {\n    int y = x + 1;\n%s    return y;\n  }\n}\n"


def test_added_log_statements_are_safe(cfg):
    # PickupEligibilityService d19abca5: two CalEventHelper.writeLog lines, nothing else
    log = ('    CalEventHelper.writeLog("A", "f",\n        "x=" + foo(x, ")"), "");\n'
           '    log.info("y={}", y);\n')
    out = _two_commits(cfg, "logonly", "src/main/java/a/Svc.java", _SVC % "", _SVC % log)
    s = next(s for s in out["changedSymbols"] if s["member"] == "f")
    assert s["loggingOnly"] is True
    assert out["changesSummary"]["overallRiskClass"] == "SAFE"


def test_log_plus_logic_change_is_not_logging_only(cfg):
    out = _two_commits(cfg, "logmix", "src/main/java/a/Svc.java", _SVC % "",
                       _SVC % '    log.info("y={}", y);\n    y = y * 2;\n')
    s = next(s for s in out["changedSymbols"] if s["member"] == "f")
    assert "loggingOnly" not in s and out["changesSummary"]["overallRiskClass"] == "BEHAVIORAL"


def test_log_call_used_as_a_value_is_not_stripped(cfg):
    out = _two_commits(cfg, "logexpr", "src/main/java/a/Svc.java", _SVC % "",
                       _SVC % '    if (log.isDebugEnabled()) { y = 0; }\n')
    assert out["changesSummary"]["overallRiskClass"] == "BEHAVIORAL"


def test_enum_constant_is_tagged_enum(cfg):
    out = _two_commits(cfg, "enm", "src/main/java/a/Prov.java",
                       "package a;\npublic enum Prov {\n  A,\n  B;\n  int x;\n}\n",
                       "package a;\npublic enum Prov {\n  A,\n  B,\n  C;\n  int x;\n}\n")
    assert any(s["classKind"] == "enum" for s in out["changedSymbols"])


def test_pom_version_changes_are_extracted(cfg):
    pom = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><parent><groupId>g</groupId><artifactId>p</artifactId>'
           '<version>{v}</version></parent><artifactId>x</artifactId></project>')
    out = _two_commits(cfg, "pomv", "pom.xml", pom.format(v="1.0"), pom.format(v="1.1"))
    assert out["buildChanges"] == [{"file": "pom.xml", "kind": "parent", "name": "g:p", "from": "1.0", "to": "1.1"}]
    assert out["changesSummary"]["overallRiskClass"] == "BEHAVIORAL"


def test_empty_commit_says_nothing_was_altered(cfg):
    p = make_repo(cfg.local_repos_root, "empty")
    (p / "a.txt").write_text("a"); commit_all(p, "1")
    from conftest import git
    git(p, "commit", "-q", "--allow-empty", "-m", "bot: no-op upgrade")
    sha = git(p, "rev-parse", "HEAD").strip()
    out = extract({"org": "TestOrg", "repo": "empty", "commit": sha}, RepoSource(cfg), cfg)
    assert out["changedFiles"] == [] and any("nothing was altered" in n for n in out["notes"])


def _migration(cfg, name, profiles_after):
    """Legacy META-INF/configuration per env, then a commit that adds Spring profiles (a RaptorIO migration)."""
    p = make_repo(cfg.local_repos_root, name)
    for env in ("Common", "QA", "Production", "Sandbox"):
        f = p / f"svc/src/main/webapp/META-INF/configuration/{env}/config/x.xml"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("<x/>")
    commit_all(p, "legacy")
    for env in profiles_after:
        (p / f"svc/src/main/resources/application-{env}.properties").parent.mkdir(parents=True, exist_ok=True)
        (p / f"svc/src/main/resources/application-{env}.properties").write_text("a=b\n")
    sha = commit_all(p, "migrate")
    return p, extract({"org": "TestOrg", "repo": name, "commit": sha}, RepoSource(cfg), cfg)


def test_migration_that_drops_an_environment_profile_is_breaking(cfg):
    # INC-006 / INC-009: RaptorIO migrations added profiles for every env except Sandbox
    _, out = _migration(cfg, "envgap", ["QA", "Production"])
    assert [(g["module"], g["environment"]) for g in out["environmentGaps"]] == [("svc", "Sandbox")]
    b = out["changesSummary"]["breakingChanges"]
    assert [x["kind"] for x in b] == ["environment"] and b[0]["environment"] == "Sandbox"
    assert "configuration/Sandbox" in b[0]["file"]
    assert out["changesSummary"]["overallRiskClass"] == "BREAKING"


def test_complete_migration_has_no_environment_gap(cfg):
    _, out = _migration(cfg, "envok", ["QA", "Production", "Sandbox"])
    assert out["environmentGaps"] == []


def test_pre_existing_environment_gap_is_not_blamed_on_a_later_commit(cfg):
    p, _ = _migration(cfg, "envold", ["QA", "Production"])
    (p / "svc/src/main/resources/application-QA.properties").write_text("a=c\n")
    sha = commit_all(p, "tweak qa")
    out = extract({"org": "TestOrg", "repo": "envold", "commit": sha}, RepoSource(cfg), cfg)
    assert out["environmentGaps"] == []


def test_operation_removed_from_openapi_spec_is_breaking(cfg):
    spec = "openapi: 3.0.0\npaths:\n  /a:\n    get:\n      operationId: getA\n{extra}"
    out = _two_commits(cfg, "spec", "src/main/resources/api/openapi.yaml",
                       spec.format(extra="  /b:\n    delete:\n      operationId: delB\n"), spec.format(extra=""))
    removed = [e for e in out["changedApiEndpoints"] if e.get("changeType") == "removed"]
    assert removed == [{"method": "DELETE", "path": "/b", "operationId": "delB", "changeType": "removed"}]
    assert out["changesSummary"]["overallRiskClass"] == "BREAKING"
