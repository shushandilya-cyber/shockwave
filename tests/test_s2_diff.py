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
    assert {"method": "GET", "path": "/orders/{id}", "operationId": "getOrder"} in out["changedApiEndpoints"]
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

