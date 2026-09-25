"""Report sections for the three questions a reader asks: how far, what breaks, what exactly."""
import json

from shockwave.report import render_run


def _run_dir(tmp_path, br, cs, vs=()):
    d = tmp_path / "run"
    d.mkdir()
    for name, obj in {"01-change-event.json": {"org": "S", "repo": "svc", "commit": "abcdef12"},
                      "02-changed-symbols.json": cs, "03-blast-radius.json": br,
                      "06-verdicts.json": list(vs), "run-meta.json": {}}.items():
        (d / name).write_text(json.dumps(obj))
    return d


BREAK = {"symbol": "com.s.Pricer#quote(int)", "kind": "method", "changeType": "removed", "reason": "Pricer.quote removed",
         "file": "src/main/java/com/s/Pricer.java", "line": 7, "className": "com.s.Pricer", "member": "quote"}


def test_breaking_change_is_mapped_to_the_consumer_that_references_it(tmp_path):
    cs = {"changedFiles": [BREAK["file"]], "changedSymbols": [], "changedApiEndpoints": [],
          "changesSummary": {"overallRiskClass": "BREAKING", "groups": {}, "breakingChanges": [BREAK], "exposedVia": []}}
    br = {"sourceRepo": "S/svc", "commit": "abcdef12", "notIndexed": True, "coverage": "local",
          "localContext": {"reposScanned": 3, "sourceRef": "origin/master@abc"},
          "impactedRepos": [
              {"org": "U", "repo": "caller", "confidence": "high", "evidence": [
                  {"source": "localCallSite", "detail": "U/caller@1:Main.java:3 calls quote", "symbols": [BREAK["symbol"]]}]},
              {"org": "U", "repo": "bystander", "confidence": "low", "evidence": [
                  {"source": "localHttpClient", "detail": "calls svc"}]}]}
    md = render_run(_run_dir(tmp_path, br, cs, [{"org": "U", "repo": "caller", "verdict": "NeedsReview", "priority": "P1",
                                                  "rule": 5, "reasoning": "tests not run"}]))
    assert "## Impact Summary" in md and "2 downstream repo(s) impacted (1 high, 1 low)" in md
    row = next(l for l in md.splitlines() if l.startswith("| `com.s.Pricer#quote"))
    assert "U/caller" in row and "U/bystander" not in row
    assert "### U/caller — high confidence · P1" in md
    assert "Code Knowledge has no index for S/svc" in md and "BLAST RADIUS UNKNOWN" not in md


def test_no_breaking_changes_says_so(tmp_path):
    cs = {"changedFiles": [], "changedSymbols": [], "changedApiEndpoints": [],
          "changesSummary": {"overallRiskClass": "SAFE", "groups": {}, "breakingChanges": []}}
    br = {"sourceRepo": "S/svc", "commit": "abcdef12", "notIndexed": False, "coverage": "graph", "impactedRepos": []}
    md = render_run(_run_dir(tmp_path, br, cs))
    assert "## Breaking Changes" in md and "_None." in md


def test_unknown_coverage_is_loud(tmp_path):
    cs = {"changedFiles": [], "changedSymbols": [], "changedApiEndpoints": [], "changesSummary": {}}
    br = {"sourceRepo": "S/svc", "commit": "abcdef12", "notIndexed": True, "coverage": "none", "impactedRepos": []}
    md = render_run(_run_dir(tmp_path, br, cs))
    assert "Reach: UNKNOWN" in md and "BLAST RADIUS UNKNOWN" in md
