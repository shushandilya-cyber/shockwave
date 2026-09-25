import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from shockwave.portal import InputError, Portal, make_handler, md_to_html, resolve_commit, resolve_repo


KNOWN = {"ship-ast/pickupsvc": Path("/nonexistent/PickUpSvc"), "coreshipping/locnapi": Path("/nonexistent/locnapi")}


def test_alias_and_org_repo_and_url_resolve():
    assert resolve_repo("cls", KNOWN) == ("CoreShipping", "locnapi")
    assert resolve_repo("punotif", {}) == ("Ship-AST", "PickupNotificationService")
    assert resolve_repo("NewOrg/new-repo", {}) == ("NewOrg", "new-repo")
    assert resolve_repo("https://github.corp.ebay.com/NewOrg/new-repo.git", {}) == ("NewOrg", "new-repo")


def test_bare_name_needs_to_be_known_or_unique():
    assert resolve_repo("locnapi", KNOWN) == ("coreshipping", "locnapi")
    with pytest.raises(InputError, match="org/repo so it can be cloned"):
        resolve_repo("nosuchrepo", KNOWN)
    with pytest.raises(InputError, match="ambiguous"):
        resolve_repo("x", {"a/x": Path("/n"), "b/x": Path("/n")})
    with pytest.raises(InputError):
        resolve_repo("--upload-pack=evil", KNOWN)


def _repo(tmp_path: Path) -> tuple[Path, str]:
    origin = tmp_path / "origin"
    origin.mkdir()
    g = lambda *a, cwd=origin: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout
    g("init", "-q", "-b", "master")
    (origin / "a.txt").write_text("1")
    g("add", ".")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c1")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return clone, g("rev-parse", "HEAD").strip()


def test_resolve_commit_short_sha_and_tip(tmp_path):
    clone, sha = _repo(tmp_path)
    assert resolve_commit(clone, sha[:8], "master") == (sha, "master")
    assert resolve_commit(clone, "", None) == (sha, "master")
    with pytest.raises(InputError, match="not found"):
        resolve_commit(clone, "deadbeef", "master")


def _wait(p: Portal, jid: str, timeout=5.0) -> dict:
    t = time.time()
    while time.time() - t < timeout:
        j = p.get(jid)
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_job_lifecycle_and_persistence(tmp_path):
    def fake(portal, job):
        portal._stage(job, "0-repo", "done")
        d = Path(job["runDir"])
        d.mkdir(parents=True)
        (d / "02-changed-symbols.json").write_text(json.dumps({"changedFiles": ["x"], "changesSummary": {
            "overallRiskClass": "BREAKING", "breakingChanges": [{"member": "m"}]}}))
        (d / "03-blast-radius.json").write_text(json.dumps({"coverage": "local", "impactedRepos": [
            {"org": "U", "repo": "c", "confidence": "high"}]}))
        (d / "06-verdicts.json").write_text(json.dumps([{"verdict": "NeedsReview"}]))
        (d / "report.md").write_text("# Title\n\n| a | b |\n|---|---|\n| `x` | **y** |\n")

    p = Portal(root=tmp_path / "portal", runner=fake)
    j = _wait(p, p.submit("cls", "abcd1234", "", True)["id"])
    assert j["status"] == "done"
    assert j["summary"]["risk"] == "BREAKING" and j["summary"]["breaking"] == 1
    assert j["summary"]["byConfidence"] == {"high": 1} and j["summary"]["verdicts"] == {"NeedsReview": 1}
    assert "<table>" in md_to_html(p.report(j["id"]))
    reloaded = Portal(root=tmp_path / "portal", runner=fake)
    assert reloaded.get(j["id"])["status"] == "done"


def test_failed_runner_is_reported(tmp_path):
    def boom(portal, job):
        portal._stage(job, "0-repo", "running")
        raise InputError("'zz' is not in the local context index")

    p = Portal(root=tmp_path / "portal", runner=boom)
    j = _wait(p, p.submit("zz")["id"])
    assert j["status"] == "failed" and "not in the local context index" in j["error"]
    assert next(s for s in j["stages"] if s["name"] == "0-repo")["status"] == "failed"


def test_submit_validates_input(tmp_path):
    p = Portal(root=tmp_path / "portal", runner=lambda *a: None)
    with pytest.raises(InputError):
        p.submit("")
    with pytest.raises(InputError):
        p.submit("a/b", "not-a-sha")
    with pytest.raises(InputError):
        p.submit("a/b", "", "-evil")


def test_http_api(tmp_path):
    p = Portal(root=tmp_path / "portal", runner=lambda *a: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(p))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert b"shockwave" in urllib.request.urlopen(base + "/").read()
        req = urllib.request.Request(base + "/api/runs", data=json.dumps({"repo": "a/b", "commit": "zz"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 400
        req = urllib.request.Request(base + "/api/runs", data=json.dumps({"repo": "a/b"}).encode(), method="POST")
        jid = json.loads(urllib.request.urlopen(req).read())["id"]
        assert json.loads(urllib.request.urlopen(f"{base}/api/runs/{jid}").read())["id"] == jid
        assert any(r["id"] == jid for r in json.loads(urllib.request.urlopen(base + "/api/runs").read()))
    finally:
        srv.shutdown()


def test_markdown_renderer_escapes_html():
    h = md_to_html("## H\n\n- <script>x</script>\n- [link](https://a.b/c)\n\n> warn\n")
    assert "<h2>H</h2>" in h and "&lt;script&gt;" in h and '<a href="https://a.b/c"' in h and "<blockquote>" in h
