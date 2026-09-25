"""Local context index (context.py) and the local half of Story 3 (s3_local.py).

Every rule gets a real git repo in tmp: the index reads the default branch with
``git grep <ref>``, so a fake dict would not exercise the part that can break.
"""
from pathlib import Path

import pytest

from conftest import commit_all, make_repo
from shockwave.clients.repos import RepoSource
from shockwave.context import ContextIndex, app_from_host, build
from shockwave.stories.s3_blast_radius import _Acc, coverage
from shockwave.stories.s3_local import compute_local, type_closure

POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <groupId>{g}</groupId><artifactId>{a}</artifactId>
  <dependencies>{deps}</dependencies>
</project>"""
DEP = "<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId></dependency>"


def write(p: Path, rel: str, text: str):
    f = p / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


def lib_repo(root: Path) -> Path:
    """Source: a library module plus a service module that serves one endpoint."""
    p = make_repo(root, "lib", org="Src")
    write(p, "README.md", "Application name: `libsvc`\n")
    write(p, "pom.xml", POM.format(g="com.src", a="lib-parent", deps=""))
    write(p, "core/pom.xml", POM.format(g="com.src", a="lib-core", deps=""))
    write(p, "updated-pom.xml", POM.format(g="com.other", a="not-ours", deps=""))
    write(p, "core/src/main/java/com/src/core/Pricer.java", """package com.src.core;
public class Pricer {
    public int quote(int x) { return x; }
    private int helper() { return 1; }
}
""")
    write(p, "core/src/main/java/com/src/core/Notifier.java", """package com.src.core;
public interface Notifier {
    void send(String m);
}
""")
    write(p, "core/src/main/java/com/src/core/QuoteResource.java", """package com.src.core;
import javax.ws.rs.*;
@Path("/quotes")
public class QuoteResource {
    private Pricer pricer;
    @GET
    @Path("/special_offer/{id}")
    public String special(String id) { return "" + pricer.quote(1); }
}
""")
    write(p, "core/src/test/java/com/src/core/PricerTest.java", "package com.src.core;\nclass PricerTest {}\n")
    commit_all(p, "v1")
    return p


def consumer(root: Path, name: str, java: str, deps: list[tuple[str, str]], props: str = "") -> Path:
    p = make_repo(root, name, org="Use")
    write(p, "pom.xml", POM.format(g="com.use", a=name, deps="".join(DEP.format(g=g, a=a) for g, a in deps)))
    write(p, f"src/main/java/com/use/{name}/Main.java", java)
    if props:
        write(p, "src/main/resources/application.properties", props)
    commit_all(p, "c")
    return p


@pytest.fixture
def world(cfg):
    root = cfg.local_repos_root
    lib_repo(root)
    consumer(root, "caller", "package com.use.caller;\nimport com.src.core.Pricer;\n"
             "class Main { int f(Pricer p) { return p.quote(2); } }\n", [("com.src", "lib-core")])
    consumer(root, "importer", "package com.use.importer;\nimport com.src.core.Pricer;\nclass Main { Pricer p; }\n",
             [("com.src", "lib-core")])
    consumer(root, "nopath", "package com.use.nopath;\nimport com.src.core.Pricer;\n"
             "class Main { int f(Pricer p) { return p.quote(2); } }\n", [("com.elsewhere", "jar")])
    consumer(root, "implementer", "package com.use.implementer;\nimport com.src.core.Notifier;\n"
             "class Main implements Notifier { public void send(String m) {} }\n", [("com.src", "lib-core")])
    consumer(root, "http", "package com.use.http;\nclass Main { String u = \"/special_offer/\"; }\n", [],
             props="svc.endpointUri=https://libsvc-stage.qa.ebay.com/quotes\n")
    consumer(root, "httpvague", "package com.use.httpvague;\nclass Main {}\n", [],
             props="svc.endpointUri=https://libsvc.vip.ebay.com/quotes\n")
    ci = ContextIndex(cfg, RepoSource(cfg))
    ci.build_all()
    return ci


def run_local(ci, symbols, files=None, endpoints=None):
    changed = {"org": "Src", "repo": "lib", "commit": "x", "changedSymbols": symbols,
               "changedFiles": files or [s["file"] for s in symbols], "changedApiEndpoints": endpoints or []}
    acc, out = _Acc("Src", "lib"), {"notes": []}
    compute_local(changed, acc, out, ci)
    return {f"{r['org']}/{r['repo']}": r for r in acc.out()}, out


def jsym(cls, member, change="modified", kind="method", **kw):
    return {"file": f"core/src/main/java/com/src/core/{cls}.java", "kind": kind, "changeType": change,
            "name": f"com.src.core.{cls}#{member}()", "package": "com.src.core", "className": cls,
            "member": member, "visibility": "public", **kw}


# ── the index itself ──────────────────────────────────────────────────────────

def test_app_from_host_strips_env_and_rejects_pools_and_infra():
    assert app_from_host("locnapi-stage.qa.ebay.com") == "locnapi"
    assert app_from_host("locnapi.stratus.ebay.com") == "locnapi"
    assert app_from_host("pickupeligibility.vip.qa.ebay.com") == "pickupeligibility"
    assert app_from_host("slcpickupsvc21-1028257.stratus.ebay.com") is None
    assert app_from_host("pickupsvc-phx-2-web-envf7jokk59vh.vip.ebay.com") is None
    assert app_from_host("api.ebay.com") is None


def test_build_reads_the_default_branch_not_the_working_tree(cfg):
    p = lib_repo(cfg.local_repos_root)
    ctx = build(p, "Src", "lib")
    assert ctx["commit"] and "com.src.core.Pricer" in ctx["types"]
    # an uncommitted file on disk must not appear
    write(p, "core/src/main/java/com/src/core/Draft.java", "package com.src.core;\nclass Draft {}\n")
    assert "com.src.core.Draft" not in build(p, "Src", "lib")["types"]


def test_build_records_only_real_poms_main_types_and_app_names(cfg):
    ctx = build(lib_repo(cfg.local_repos_root), "Src", "lib")
    arts = {f"{a['groupId']}:{a['artifactId']}" for a in ctx["artifacts"]}
    assert arts == {"com.src:lib-parent", "com.src:lib-core"}  # updated-pom.xml is generated, not ours
    assert "com.src.core.PricerTest" not in ctx["types"]
    assert "libsvc" in ctx["appNames"]
    eps = {(e["method"], e["path"]) for e in ctx["endpoints"]}
    assert ("GET", "/quotes/special_offer/{id}") in eps


def test_context_cache_is_reused_until_the_branch_moves(world, cfg):
    first = world.get("Src", "lib")["builtAt"]
    again = ContextIndex(cfg, RepoSource(cfg))
    again.build_all()
    assert again.get("Src", "lib")["builtAt"] == first


# ── local blast radius rules ─────────────────────────────────────────────────

def test_call_to_changed_member_is_high(world):
    got, _ = run_local(world, [jsym("Pricer", "quote")])
    assert got["Use/caller"]["confidence"] == "high"
    ev = got["Use/caller"]["evidence"][0]
    assert ev["source"] == "localCallSite" and "Main.java:3" in ev["detail"]


def test_import_without_call_is_medium(world):
    got, _ = run_local(world, [jsym("Pricer", "quote")])
    assert got["Use/importer"]["confidence"] == "medium"
    assert {e["source"] for e in got["Use/importer"]["evidence"]} >= {"localScan"}


def test_same_name_import_without_maven_path_is_ignored(world):
    got, out = run_local(world, [jsym("Pricer", "quote")])
    assert "Use/nopath" not in got
    assert "Use/nopath" in out["localContext"]["importsWithoutMavenPath"]


def test_maven_dependency_on_changed_module_is_medium(world):
    got, _ = run_local(world, [jsym("Pricer", "quote")])
    srcs = {e["source"] for e in got["Use/importer"]["evidence"]}
    assert "localMavenDependency" in srcs


def test_implementer_of_interface_gaining_abstract_method_is_high(world):
    got, _ = run_local(world, [jsym("Notifier", "flush", change="added", classKind="interface", abstract=True)])
    ev = next(e for e in got["Use/implementer"]["evidence"] if e["source"] == "localCallSite")
    assert got["Use/implementer"]["confidence"] == "high" and "implements `Notifier`" in ev["detail"]


def test_http_consumer_naming_a_directly_changed_endpoint_is_high(world):
    got, out = run_local(world, [jsym("QuoteResource", "special")])
    assert [e["reachedVia"] for e in out["localContext"]["exposedVia"]] == ["direct"]
    assert got["Use/http"]["confidence"] == "high"


def test_http_consumer_that_cannot_be_tied_to_an_endpoint_is_medium(world):
    got, _ = run_local(world, [jsym("QuoteResource", "special")])
    assert got["Use/httpvague"]["confidence"] == "medium"


def test_http_consumer_is_low_when_no_endpoint_reaches_the_change(world):
    got, _ = run_local(world, [jsym("Notifier", "send")])
    assert got["Use/httpvague"]["confidence"] == "low"


def test_type_closure_walks_references_inside_the_source(world):
    ctx = world.get("Src", "lib")
    assert type_closure(ctx, {"com.src.core.Pricer"}) == {"com.src.core.Pricer": 0, "com.src.core.QuoteResource": 1}


def test_transitive_exposure_never_yields_high(world):
    got, out = run_local(world, [jsym("Pricer", "helper", visibility="private")])
    assert [e["reachedVia"] for e in out["localContext"]["exposedVia"]] == ["transitive"]
    assert got["Use/http"]["confidence"] == "medium"


def test_coverage_states():
    assert coverage({"notIndexed": False, "localContext": {"x": 1}}) == "graph+local"
    assert coverage({"notIndexed": False}) == "graph"
    assert coverage({"notIndexed": True, "localContext": {"x": 1}}) == "local"
    assert coverage({"notIndexed": True}) == "none"
