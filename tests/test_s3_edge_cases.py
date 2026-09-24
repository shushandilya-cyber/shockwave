"""Regression: non-code / additive-only change must not flag coarse consumers as impacted."""
from shockwave.clients.repos import RepoSource
from shockwave.stories.s3_blast_radius import compute
from test_s3_blast_radius import FakeKC, sym


def test_config_only_change_is_not_impacted(cfg):
    cs = {"org": "Src", "repo": "svc", "commit": "abc1234", "changedFiles": ["src/main/resources/Hubs.json"],
          "changedSymbols": [], "changedApiEndpoints": []}
    br = compute(cs, FakeKC(), RepoSource(cfg), cfg, local_scan=False)
    assert br["impactedRepos"] == []
    assert "Dep/dep-only" in br["contextRepos"] and br["notIndexed"] is False
    assert any("awareness only" in n for n in br["notes"])


def test_additive_only_change_is_not_impacted(cfg):
    cs = {"org": "Src", "repo": "svc", "commit": "abc1234", "changedFiles": ["F.java"], "changedApiEndpoints": [],
          "changedSymbols": [sym("n", "OrderService", "brandNew", "added")]}
    assert compute(cs, FakeKC(), RepoSource(cfg), cfg, local_scan=False)["impactedRepos"] == []


def test_changed_endpoint_keeps_api_consumers(cfg):
    cs = {"org": "Src", "repo": "svc", "commit": "abc1234", "changedFiles": ["openapi.yaml"], "changedSymbols": [],
          "changedApiEndpoints": [{"method": "GET", "path": "/orders", "operationId": None}]}
    by = {f"{r['org']}/{r['repo']}": r["confidence"] for r in compute(cs, FakeKC(), RepoSource(cfg), cfg, local_scan=False)["impactedRepos"]}
    assert by.get("Api/api-precise") == "high"

