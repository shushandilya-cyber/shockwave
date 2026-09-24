import yaml

from shockwave.stories.s7_jira import create_all, dedupe_label

BR = {"sourceRepo": "CoreShipping/waypointservice", "commit": "9d60af39aaaa", "unresolvedSymbols": ["x#y()"]}
EV = {"environment": "staging", "prNumber": "1228"}


def v(verdict, team="O/team-a", repo="r"):
    return {"org": "O", "repo": repo, "team": team, "verdict": verdict, "reasoning": "because", "confidence": "high", "priority": "P1",
            "evidence": {"blastRadius": [{"source": "graphSearch", "detail": "A|B"}],
                         "testResult": {"status": "PASS", "hadLiveIntegrationSignal": False, "failedTests": []}}}


class FakeJira:
    def __init__(self):
        self.created, self.comments, self.labels = [], [], {}

    def find_by_label(self, label):
        return self.labels.get(label)

    def create(self, fields):
        key = f"TEST-{len(self.created) + 1}"
        self.created.append(fields)
        self.labels[[l for l in fields["labels"] if l.startswith("br-")][0]] = key
        return key

    def comment(self, key, body):
        self.comments.append(key)


def test_dry_run_payload(cfg):
    cfg.jira_mapping_file.write_text(yaml.safe_dump({"teams": {"O/team-a": {"project": "TST", "component": "c1"}}}))
    ts = create_all([v("Impacted"), v("NeedsReview", team=None, repo="r2"), v("NotImpacted", repo="r3")], BR, EV, cfg=cfg)
    assert len(ts) == 2  # NotImpacted skipped
    a, b = ts
    f = a["payload"]["fields"]
    assert f["summary"] == "[Blast Radius] CoreShipping/waypointservice@9d60af39 may impact O/r"
    assert f["project"] == {"key": "TST"} and f["components"] == [{"name": "c1"}] and f["issuetype"]["name"] == "Story"
    assert "blast-radius-auto" in f["labels"] and "needs-review" not in f["labels"]
    assert "GENERIC SANITY CHECK ONLY" in f["description"] and "pull/1228" in f["description"]
    assert "needs-review" in b["payload"]["fields"]["labels"]
    assert "shared triage" in b["payload"]["fields"]["description"]


def test_create_then_comment_on_duplicate(cfg, monkeypatch):
    from shockwave.stories import s7_jira
    cfg.jira_mapping_file.write_text(yaml.safe_dump({"default": {"project": "TRIAGE"}}))
    fj = FakeJira()
    mapping = s7_jira.load_mapping(cfg.jira_mapping_file)
    t1 = s7_jira.create_one(v("Impacted"), BR, EV, cfg, dry_run=False, mapping=mapping, jira=fj)
    t2 = s7_jira.create_one(v("Impacted"), BR, EV, cfg, dry_run=False, mapping=mapping, jira=fj)
    assert (t1["action"], t1["key"]) == ("created", "TEST-1")
    assert (t2["action"], t2["key"]) == ("commented", "TEST-1")
    assert len(fj.created) == 1 and fj.comments == ["TEST-1"]


def test_dedupe_label_deterministic():
    assert dedupe_label("A/b", "c", "D/e") == dedupe_label("a/B", "c", "d/E")
    assert dedupe_label("A/b", "c", "D/e") != dedupe_label("A/b", "c2", "D/e")

