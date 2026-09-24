"""Story 3 with a fake KnowledgeClient — asserts fusion/merge/confidence rules offline."""
from shockwave.clients.repos import RepoSource
from shockwave.stories.s3_blast_radius import compute

P = "maven/com.acme/svc-core.com.acme.svc"


class FakeKC:
    backend_name = "fake"

    def __init__(self, indexed=True):
        self.indexed = indexed
        self.calls = []

    def indexed_ref(self, org, repo):
        return f"{org}:{repo}:master" if self.indexed else None

    def vertices_by_name(self, name, repo):
        return {"count": [{"fqn": f"{P}.OrderService.count", "vertex_key": "Method:1"},
                          {"fqn": "maven/x/other.com.other.OrderService.count", "vertex_key": "Method:9"}],
                "internalOnly": [{"fqn": f"{P}.OrderService.internalOnly(+1)", "vertex_key": "Method:2"}]}.get(name, [])

    def vector_search(self, q, limit=5, tag=""):
        return []

    def external_callers(self, fqns, repo):
        out = []
        if f"{P}.OrderService.count" in fqns:
            out.append({"git_org": "LibUser", "git_repo": "consumer-a", "fqn": "maven/a/a.com.a.Foo.bar", "target_fqn": f"{P}.OrderService.count"})
        if f"{P}.OrderResource.list" in fqns:
            out.append({"git_org": "LibUser", "git_repo": "consumer-b", "fqn": "maven/b/b.com.b.Baz.q", "target_fqn": f"{P}.OrderResource.list"})
        return out

    def transitive_internal_callers(self, fqns, repo):
        return [f"{P}.OrderResource.list"] if any("internalOnly" in f for f in fqns) else []

    def openapi_connections(self, org, repo, direction):
        return {"inbound": [
            {"callerOrg": "Api", "callerRepo": "api-precise", "callerClass": "C", "callerMethod": "m", "apiMethod": "GET",
             "apiPath": "/orders", "apiOperationId": "list", "targetClass": "OrderResource", "targetMethod": "list"},
            {"callerOrg": "Api", "callerRepo": "api-other", "callerClass": "C", "callerMethod": "m", "apiMethod": "POST",
             "apiPath": "/other", "apiOperationId": "other", "targetClass": "OtherResource", "targetMethod": "other"},
            {"callerOrg": "LibUser", "callerRepo": "consumer-a", "callerClass": "C", "callerMethod": "m", "apiMethod": "POST",
             "apiPath": "/other", "apiOperationId": "other", "targetClass": "OtherResource", "targetMethod": "other"},
            {"callerOrg": None, "callerRepo": None, "callerAppNames": ["ghost"], "apiMethod": "GET", "apiPath": "/orders",
             "apiOperationId": "list", "targetClass": "OrderResource", "targetMethod": "list"},
        ]}

    def repo_dependencies(self, org, repo, direction):
        return {"edges": [
            {"srcRepo": "https://github.corp.ebay.com/Dep/dep-only", "viaApps": [{"srcApp": "dep", "tgtApp": "svcapp"}]},
            {"srcRepo": "https://github.corp.ebay.com/Api/api-other", "viaApps": [{"srcApp": "o", "tgtApp": "svcapp"}]},
            {"srcRepo": "https://github.corp.ebay.com/ebayistio/istio.git", "viaApps": [{"srcApp": "gw", "tgtApp": "svcapp"}]},
        ]}


def sym(name, cls, member, change="modified", kind="method"):
    return {"file": "F.java", "kind": kind, "name": f"com.acme.svc.{cls}#{member}()", "changeType": change,
            "package": "com.acme.svc", "className": cls, "member": member, "params": ""}


CHANGED = {"org": "Src", "repo": "svc", "commit": "abc1234", "changedFiles": ["F.java"], "changedApiEndpoints": [],
           "changedSymbols": [sym("count", "OrderService", "count"), sym("internalOnly", "OrderService", "internalOnly"),
                              sym("ghost", "OrderService", "neverIndexed"), sym("new", "OrderService", "brandNew", "added")]}


def test_fusion_and_confidence(cfg):
    br = compute(CHANGED, FakeKC(), RepoSource(cfg), cfg, local_scan=False)
    by = {f"{r['org']}/{r['repo']}": r for r in br["impactedRepos"]}
    assert by["LibUser/consumer-a"]["confidence"] == "high"          # direct FQN caller
    assert by["LibUser/consumer-b"]["confidence"] == "high"          # transitive external caller
    assert by["Api/api-precise"]["confidence"] == "high"             # API chain reaches changed code
    assert by["Api/api-other"]["confidence"] == "medium"             # low (openapi) upgraded by repo dep
    assert by["Dep/dep-only"]["confidence"] == "medium"
    # consumer-a: high never downgraded by a later low openapi hit; evidence accumulates
    assert {e["source"] for e in by["LibUser/consumer-a"]["evidence"]} == {"graphSearch", "getOpenapiConnections"}
    assert "Src/svc" not in by
    assert br["ignoredRepos"] == ["ebayistio/istio"]
    # other-package vertex with same simple name was NOT treated as the changed symbol
    assert all("com.other" not in f for r in br["resolvedSymbols"] for f in r["fqns"])


def test_unresolved_and_new_symbols_are_visible(cfg):
    br = compute(CHANGED, FakeKC(), RepoSource(cfg), cfg, local_scan=False)
    assert br["unresolvedSymbols"] == ["com.acme.svc.OrderService#neverIndexed()"]
    assert br["newSymbols"] == ["com.acme.svc.OrderService#brandNew()"]
    assert br["unmappedApiCallers"][0]["apps"] == ["ghost"] and br["unmappedApiCallers"][0]["matchedChangedCode"]
    assert "svcapp" in br["sourceApps"]


def test_not_indexed_stops_with_flag(cfg):
    br = compute(CHANGED, FakeKC(indexed=False), RepoSource(cfg), cfg, local_scan=False)
    assert br["notIndexed"] is True and br["impactedRepos"] == []
    assert "Dep/dep-only" in br["notIndexedHints"]

