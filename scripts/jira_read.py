"""Read-only Jira lookup for incident evidence. Never writes.
  python scripts/jira_read.py KEY1 KEY2 ...           -> summary/type/status/priority/created/resolved/labels
  python scripts/jira_read.py --jql 'project = X ...' -> search
"""
import json
import ssl
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))
from shockwave.config import CONFIG  # noqa: E402

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
H = {"Authorization": f"Bearer {CONFIG.jira_pat}", "Accept": "application/json"}
F = "summary,issuetype,status,priority,created,resolutiondate,labels,components,issuelinks,description"


def get(path):
    rq = urllib.request.Request(CONFIG.jira_base.rstrip("/") + path, headers=H)
    try:
        with urllib.request.urlopen(rq, context=ctx, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_err": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"_err": str(e)[:200]}


def show(i):
    f = i["fields"]
    links = [(l.get("type", {}).get("name"), (l.get("outwardIssue") or l.get("inwardIssue") or {}).get("key")) for l in f.get("issuelinks") or []]
    desc = (f.get("description") or "").replace("\n", " ")[:500]
    print(f"{i['key']} | {f['issuetype']['name']} | {f['status']['name']} | {(f.get('priority') or {}).get('name')} | "
          f"created {f['created'][:10]} | resolved {(f.get('resolutiondate') or '')[:10]} | labels {f.get('labels')} | links {links}")
    print(f"   {f['summary']}")
    print(f"   desc: {desc}")


if sys.argv[1] == "--jql":
    q = urllib.parse.urlencode({"jql": sys.argv[2], "fields": F, "maxResults": int(sys.argv[3]) if len(sys.argv) > 3 else 30})
    d = get(f"/rest/api/2/search?{q}")
    if "_err" in d:
        print(d); sys.exit(1)
    print("total", d.get("total"))
    for i in d.get("issues", []):
        show(i)
else:
    for k in sys.argv[1:]:
        d = get(f"/rest/api/2/issue/{k}?fields={F}")
        print(d) if "_err" in d else show(d)

