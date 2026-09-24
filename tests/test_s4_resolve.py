import json

from shockwave.clients.repos import RepoSource
from shockwave.stories.s4_resolve import _pattern_matches, owners_for, parse_codeowners, resolve
from conftest import commit_all, make_repo

CO = """# comment
*       @Org/default-team
/src/main/java/com/x/api/   @Org/api-team
*.yaml  @someuser
docs/   @Org/docs
"""


def test_codeowners_matching():
    rules = parse_codeowners(CO)
    assert owners_for(rules, []) == (["@Org/default-team"], "*")
    assert owners_for(rules, ["src/main/java/com/x/api/Foo.java"])[0] == ["@Org/api-team"]
    assert owners_for(rules, ["deploy/app.yaml"])[0] == ["@someuser"]
    assert owners_for(rules, ["a/docs/x.md"])[0] == ["@Org/docs"]
    assert not _pattern_matches("/src/main/java/com/x/api/", "other/src/main/java/com/x/api/A.java")


def test_resolve_manifest(cfg):
    root = cfg.local_repos_root
    a = make_repo(root, "maven-with-co", "O"); (a / "pom.xml").write_text("<project/>")
    (a / ".github").mkdir(); (a / ".github/CODEOWNERS").write_text("* @O/team-a @bob\n"); commit_all(a, "1")
    b = make_repo(root, "npm-no-test", "O"); (b / "package.json").write_text(json.dumps({"scripts": {"test": "echo \"Error: no test specified\" && exit 1"}})); commit_all(b, "1")
    c = make_repo(root, "npm-ok", "O"); (c / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}})); commit_all(c, "1")
    blast = {"sourceRepo": "S/s", "commit": "abc", "impactedRepos": [
        {"org": "O", "repo": "maven-with-co", "confidence": "high", "evidence": []},
        {"org": "O", "repo": "npm-no-test", "confidence": "low", "evidence": []},
        {"org": "O", "repo": "npm-ok", "confidence": "medium", "evidence": []},
        {"org": "Nope", "repo": "missing", "confidence": "low", "evidence": []}]}
    cfg.github_token = ""
    m = {f"{e['org']}/{e['repo']}": e for e in resolve(blast, RepoSource(cfg), cfg)["entries"]}
    e = m["O/maven-with-co"]
    assert (e["team"], e["teamSource"], e["buildTool"], e["testCommand"], e["runnable"], e["defaultBranch"]) == \
        ("O/team-a", "codeowners", "maven", "mvn -B test", True, "master")
    assert m["O/npm-no-test"]["runnable"] is False and m["O/npm-no-test"]["teamSource"] == "unassigned"
    assert m["O/npm-ok"]["testCommand"] == "npm test"
    assert m["Nope/missing"]["repoAccess"] == "none" and not m["Nope/missing"]["runnable"]

