"""Local web portal: enter a repo + commit, get the full blast-radius run.

  shockwave serve [--host 127.0.0.1] [--port 8765]

Runs are queued and executed one at a time (Story 5 builds are heavy). A repo that is not in
the local context index is cloned into the shockwave cache and indexed before the pipeline
starts. Everything stays read-only: Jira is always dry-run, nothing is pushed.
"""
from __future__ import annotations

import html
import json
import queue
import re
import subprocess
import threading
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import CONFIG, Config

ALIASES = {
    "punotif": "Ship-AST/PickupNotificationService",
    "pickupeligibility": "Ship-AST/PickupEligibilityService",
    "pickupeligibilitysvc": "Ship-AST/PickupEligibilityService",
    "pickupsvc": "Ship-AST/PickUpSvc",
    "cls": "CoreShipping/locnapi",
    "polis": "CoreShipping/polis",
}
_REPO = re.compile(r"^[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*$")
_NAME = re.compile(r"^[A-Za-z0-9][\w.-]*$")
_SHA = re.compile(r"^[0-9a-fA-F]{4,40}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][\w./-]*$")
PREP_STAGES = ["0-repo", "0-context"]
PIPELINE_STAGES = ["1-trigger", "2-diff", "3-blast", "4-resolve", "5-tests", "6-verdict", "7-jira"]


class InputError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_repo(text: str, known: dict[str, Path]) -> tuple[str, str]:
    """Accept org/repo, a GitHub URL, an alias, or a bare repo name that is already known locally."""
    t = (text or "").strip().rstrip("/")
    if t.endswith(".git"):
        t = t[:-4]
    if "://" in t or t.startswith("git@"):
        t = "/".join(re.split(r"[/:]", t)[-2:])
    t = ALIASES.get(t.lower(), t)
    if _REPO.match(t):
        o, r = t.split("/", 1)
        return _real_case(known[t.lower()], o, r) if t.lower() in known else (o, r)
    if _NAME.match(t):
        hits = [k for k in known if k.split("/", 1)[1] == t.lower()]
        if len(hits) == 1:
            o, r = hits[0].split("/", 1)
            return _real_case(known[hits[0]], o, r)
        if len(hits) > 1:
            raise InputError(f"'{t}' is ambiguous: {', '.join(sorted(hits))} — use org/repo")
        raise InputError(f"'{t}' is not in the local context index — enter it as org/repo so it can be cloned")
    raise InputError("repo must look like org/repo")


def _real_case(path: Path, o: str, r: str) -> tuple[str, str]:
    from .clients.repos import git, parse_remote
    try:
        pr = parse_remote(git(path, "remote", "get-url", "origin"))
        if pr:
            return pr[0], pr[1]
    except Exception:
        pass
    return o, r


def _git(path: Path, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout,
                          env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
                               "HOME": str(Path.home())})


def resolve_commit(path: Path, commit: str, branch: str | None) -> tuple[str, str]:
    """Full SHA + branch. An empty commit means the tip of the branch (default branch if none given)."""
    from .context import default_ref
    if branch is None or not branch:
        ref = default_ref(path)
        branch = ref.split("/", 1)[1] if ref.startswith("origin/") else ref
    _git(path, "fetch", "--quiet", "origin", branch)
    if not commit:
        r = _git(path, "rev-parse", f"origin/{branch}^{{commit}}")
        if r.returncode != 0:
            raise InputError(f"branch '{branch}' not found on origin")
        return r.stdout.strip(), branch
    r = _git(path, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    if r.returncode != 0 and len(commit) == 40:
        _git(path, "fetch", "--quiet", "origin", commit)
        r = _git(path, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    if r.returncode != 0:
        raise InputError(f"commit {commit} not found in {path.name} (fetched origin/{branch}; use the full 40-char SHA "
                         "for commits outside that branch)")
    return r.stdout.strip(), branch


def run_summary(d: Path) -> dict:
    def j(name, default):
        p = d / name
        return json.loads(p.read_text()) if p.exists() else default
    cs, br, vs = j("02-changed-symbols.json", {}), j("03-blast-radius.json", {}), j("06-verdicts.json", [])
    meta = j("run-meta.json", {})
    summ = cs.get("changesSummary") or {}
    conf: dict[str, int] = {}
    for e in br.get("impactedRepos", []):
        conf[e.get("confidence", "?")] = conf.get(e.get("confidence", "?"), 0) + 1
    return {
        "risk": summ.get("overallRiskClass"),
        "intent": summ.get("intent"),
        "breaking": len(summ.get("breakingChanges") or []),
        "coverage": br.get("coverage"),
        "impacted": len(br.get("impactedRepos", [])),
        "byConfidence": conf,
        "verdicts": {v["verdict"]: sum(1 for x in vs if x["verdict"] == v["verdict"]) for v in vs},
        "changedFiles": len(cs.get("changedFiles") or []),
        "stopReason": meta.get("stopReason"),
    }


class Portal:
    def __init__(self, cfg: Config = CONFIG, root: Path | None = None, runner=None):
        self.cfg = cfg
        self.root = root or Path.cwd() / "runs" / "portal"
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.q: queue.Queue[str] = queue.Queue()
        self.runner = runner or self._run_pipeline
        self._load()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- persistence ------------------------------------------------------
    def _load(self):
        for f in sorted(self.root.glob("*/job.json")):
            try:
                job = json.loads(f.read_text())
            except Exception:
                continue
            if job.get("status") in ("queued", "running"):
                job["status"] = "interrupted"
                job["error"] = "portal restarted before the run finished"
            self.jobs[job["id"]] = job

    def _save(self, job: dict):
        d = self.root / job["id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "job.json").write_text(json.dumps(job, indent=2) + "\n")

    def _update(self, job: dict, **kw):
        with self.lock:
            job.update(kw)
            self._save(job)

    def _stage(self, job: dict, name: str, status: str, note: str | None = None):
        with self.lock:
            st = next((s for s in job["stages"] if s["name"] == name), None)
            if st is None:
                st = {"name": name}
                job["stages"].append(st)
            st["status"] = status
            st["at"] = _now()
            if note:
                st["note"] = note
            self._save(job)

    def _log(self, job: dict, msg: str):
        with self.lock:
            job["log"].append(f"{_now()} {msg}")
            self._save(job)

    # ---- API --------------------------------------------------------------
    def submit(self, repo: str, commit: str = "", branch: str = "", skip_tests: bool = False) -> dict:
        commit, branch = (commit or "").strip(), (branch or "").strip()
        if not (repo or "").strip():
            raise InputError("repo is required")
        if commit and not _SHA.match(commit):
            raise InputError("commit must be a hex SHA (4–40 chars) or empty for the branch tip")
        if branch and not _BRANCH.match(branch):
            raise InputError("invalid branch name")
        jid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        job = {"id": jid, "input": {"repo": repo.strip(), "commit": commit, "branch": branch, "skipTests": skip_tests},
               "status": "queued", "created": _now(), "stages": [{"name": s, "status": "pending"} for s in
                                                                PREP_STAGES + PIPELINE_STAGES],
               "log": [], "runDir": str(self.root / jid / "run")}
        with self.lock:
            self.jobs[jid] = job
            self._save(job)
        self.q.put(jid)
        return job

    def list(self) -> list[dict]:
        with self.lock:
            return sorted(({k: v for k, v in j.items() if k != "log"} for j in self.jobs.values()),
                          key=lambda j: j["created"], reverse=True)

    def get(self, jid: str) -> dict | None:
        with self.lock:
            j = self.jobs.get(jid)
            return json.loads(json.dumps(j)) if j else None

    def report(self, jid: str) -> str | None:
        j = self.jobs.get(jid)
        p = Path(j["runDir"]) / "report.md" if j else None
        return p.read_text() if p and p.exists() else None

    # ---- worker -----------------------------------------------------------
    def _worker(self):
        while True:
            jid = self.q.get()
            job = self.jobs.get(jid)
            if not job:
                continue
            self._update(job, status="running", started=_now())
            try:
                self.runner(self, job)
                self._update(job, status="done", finished=_now(), summary=run_summary(Path(job["runDir"])))
            except InputError as e:
                self._log(job, f"input error: {e}")
                self._update(job, status="failed", finished=_now(), error=str(e))
            except Exception as e:
                self._log(job, traceback.format_exc()[-2000:])
                self._update(job, status="failed", finished=_now(), error=f"{type(e).__name__}: {e}")
            finally:
                for s in job["stages"]:
                    if s["status"] == "running":
                        self._stage(job, s["name"], "failed")

    @staticmethod
    def _run_pipeline(portal: "Portal", job: dict):
        from .context import ContextIndex
        from .pipeline import run
        inp = job["input"]
        ci = ContextIndex(portal.cfg)

        portal._stage(job, "0-repo", "running")
        org, repo = resolve_repo(inp["repo"], ci.clone_roots())
        key = f"{org}/{repo}".lower()
        known = key in ci.clone_roots()
        if not known:
            portal._log(job, f"{org}/{repo} is not in the local context index — cloning it into the shockwave cache")
        path = ci.ensure_local(org, repo)
        commit, branch = resolve_commit(path, inp["commit"], inp["branch"] or None)
        portal._update(job, org=org, repo=repo, commit=commit, branch=branch, newRepo=not known)
        portal._stage(job, "0-repo", "done", ("cloned " if not known else "found ") + str(path))
        portal._log(job, f"{org}/{repo}@{commit[:8]} on {branch}")

        portal._stage(job, "0-context", "running")
        ctx = ci.load_or_build(key, path)
        stats = ctx.get("stats") or {}
        portal._stage(job, "0-context", "done",
                      f"{ctx['ref']}@{ctx['commit'][:8]} · " + ", ".join(f"{k}={v}" for k, v in stats.items()))

        def on_step(name, status):
            portal._stage(job, name, status)

        res = run(org=org, repo=repo, commit=commit, branch=branch, run_dir=job["runDir"],
                  skip_tests=inp["skipTests"], create_jira=False, cfg=portal.cfg, on_step=on_step)
        for s in job["stages"]:
            if s["name"] in PIPELINE_STAGES and s["status"] == "pending":
                portal._stage(job, s["name"], "skipped")
        if res.get("stopReason"):
            portal._log(job, res["stopReason"])


# ---- markdown → HTML (enough for report.md: headings, tables, lists, code, links) ----------
def _inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    return s


def md_to_html(md: str) -> str:
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(html.escape(lines[i]))
                i += 1
            out.append("<pre><code>" + "\n".join(buf) + "</code></pre>")
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            i += 1
            continue
        if ln.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            cells = lambda row: [c.strip() for c in row.strip().strip("|").split("|")]
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells(ln)) + "</tr></thead><tbody>")
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells(lines[i])) + "</tr>")
                i += 1
            out.append("</tbody></table>")
            continue
        if re.match(r"^\s*([-*]|\d+\.)\s+", ln):
            tag = "ol" if re.match(r"^\s*\d+\.", ln) else "ul"
            out.append(f"<{tag}>")
            while i < len(lines) and re.match(r"^\s*([-*]|\d+\.)\s+", lines[i]):
                out.append("<li>" + _inline(re.sub(r"^\s*([-*]|\d+\.)\s+", "", lines[i])) + "</li>")
                i += 1
            out.append(f"</{tag}>")
            continue
        if ln.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(_inline(lines[i].lstrip("> ")))
                i += 1
            out.append("<blockquote>" + "<br>".join(buf) + "</blockquote>")
            continue
        if ln.strip() in ("---", "***"):
            out.append("<hr>")
        elif ln.strip():
            out.append(f"<p>{_inline(ln)}</p>")
        i += 1
    return "\n".join(out)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>shockwave · blast radius</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#e6e8ee;--mute:#8b93a7;--acc:#6ea8fe;--ok:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:18px 28px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:14px}
header h1{font-size:18px;margin:0}header span{color:var(--mute)}
main{display:grid;grid-template-columns:360px 1fr;gap:20px;padding:20px 28px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
label{display:block;color:var(--mute);font-size:12px;margin:10px 0 4px}
input[type=text]{width:100%;padding:9px 10px;border-radius:7px;border:1px solid var(--line);background:#0c0e12;color:var(--fg);font:13px ui-monospace,Menlo,monospace}
input:focus{outline:1px solid var(--acc)}
.row{display:flex;align-items:center;gap:8px;margin-top:12px;color:var(--mute)}
button{margin-top:14px;width:100%;padding:10px;border:0;border-radius:7px;background:var(--acc);color:#081222;font-weight:600;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
.err{color:var(--bad);margin-top:10px;min-height:1em}
.hint{color:var(--mute);font-size:12px;margin-top:8px}
.jobs{margin-top:18px}.job{padding:10px;border:1px solid var(--line);border-radius:8px;margin-bottom:8px;cursor:pointer}
.job:hover,.job.sel{border-color:var(--acc)}.job .t{font:12px ui-monospace,Menlo,monospace}.job .m{color:var(--mute);font-size:12px}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;font-weight:600;border:1px solid var(--line)}
.done{color:var(--ok)}.failed,.interrupted{color:var(--bad)}.running{color:var(--acc)}.queued,.pending,.skipped{color:var(--mute)}
.stages{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}.stage{padding:4px 9px;border-radius:6px;border:1px solid var(--line);font-size:12px}
.stage.running{border-color:var(--acc);animation:p 1.2s infinite}@keyframes p{50%{opacity:.5}}
.stage.done{border-color:#23422b}.stage.failed{border-color:var(--bad)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:12px 0}
.card{background:#0c0e12;border:1px solid var(--line);border-radius:8px;padding:10px}.card b{display:block;font-size:18px}.card small{color:var(--mute)}
.BREAKING{color:var(--bad)}.BEHAVIORAL{color:var(--warn)}.SAFE,.ADDITIVE{color:var(--ok)}
#report{margin-top:14px;border-top:1px solid var(--line);padding-top:6px;overflow-x:auto}
#report table{border-collapse:collapse;margin:8px 0;font-size:13px}#report th,#report td{border:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
#report th{background:#0c0e12}#report code{background:#0c0e12;padding:1px 4px;border-radius:4px;font-size:12px}
#report pre{background:#0c0e12;padding:10px;border-radius:7px;overflow:auto}#report blockquote{border-left:3px solid var(--warn);margin:8px 0;padding:4px 12px;color:#e3c27a}
#report a{color:var(--acc)}#report h1{font-size:20px}#report h2{font-size:16px;margin-top:22px}#report h3{font-size:14px}
pre.log{background:#0c0e12;padding:10px;border-radius:7px;max-height:180px;overflow:auto;font-size:12px;color:var(--mute)}
.empty{color:var(--mute);padding:40px;text-align:center}
</style></head><body>
<header><h1>shockwave</h1><span>commit → downstream blast radius, breaking changes, verdicts (read-only · Jira dry-run)</span></header>
<main>
<section>
 <div class="panel">
  <form id="f">
   <label>Repository</label><input type="text" id="repo" list="repos" placeholder="org/repo, alias (punotif, cls…) or name" required>
   <datalist id="repos"></datalist>
   <label>Commit SHA <span class="hint">(empty = branch tip)</span></label><input type="text" id="commit" placeholder="e.g. 56f03852">
   <label>Branch <span class="hint">(empty = default branch)</span></label><input type="text" id="branch" placeholder="master">
   <div class="row"><input type="checkbox" id="skip"><label for="skip" style="margin:0">Skip test execution (fast static run)</label></div>
   <button id="go">Analyze commit</button><div class="err" id="err"></div>
   <div class="hint">Repos outside the local index are cloned and indexed first. Runs are queued one at a time.</div>
  </form>
 </div>
 <div class="jobs" id="jobs"></div>
</section>
<section class="panel" id="detail"><div class="empty">Submit a commit or pick a run.</div></section>
</main>
<script>
const $=s=>document.querySelector(s);let sel=null,timer=null;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(p,o){const r=await fetch(p,o);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.statusText);return j}
async function loadRepos(){try{const r=await api('/api/repos');$('#repos').innerHTML=r.map(x=>`<option value="${esc(x)}">`).join('')}catch(e){}}
async function loadJobs(){const js=await api('/api/runs');$('#jobs').innerHTML=js.map(j=>`<div class="job ${j.id===sel?'sel':''}" onclick="show('${j.id}')">
 <div class="t">${esc(j.org?j.org+'/'+j.repo:j.input.repo)} @ ${esc((j.commit||j.input.commit||'tip').slice(0,8))}</div>
 <div class="m"><span class="pill ${j.status}">${j.status}</span> ${esc(j.created)} ${j.summary&&j.summary.risk?`· <span class="${j.summary.risk}">${j.summary.risk}</span> · ${j.summary.impacted} impacted`:''}</div></div>`).join('')}
async function show(id){sel=id;clearTimeout(timer);const j=await api('/api/runs/'+id);loadJobs();
 const s=j.summary||{};let h=`<h2 style="margin:0 0 4px">${esc(j.org?j.org+'/'+j.repo:j.input.repo)} <span class="pill ${j.status}">${j.status}</span></h2>
 <div class="hint">${esc(j.commit||j.input.commit||'')} ${j.branch?'· '+esc(j.branch):''} ${j.newRepo?'· newly cloned + indexed':''} · run dir <code>${esc(j.runDir)}</code></div>
 <div class="stages">${j.stages.map(x=>`<span class="stage ${x.status}" title="${esc(x.note||'')}">${esc(x.name)}</span>`).join('')}</div>`;
 if(j.error)h+=`<div class="err">${esc(j.error)}</div>`;
 if(j.summary)h+=`<div class="cards"><div class="card"><small>Risk</small><b class="${s.risk}">${esc(s.risk||'—')}</b></div>
  <div class="card"><small>Breaking changes</small><b>${s.breaking}</b></div><div class="card"><small>Impacted repos</small><b>${s.impacted}</b>
  <small>${Object.entries(s.byConfidence||{}).map(([k,v])=>v+' '+k).join(' · ')}</small></div>
  <div class="card"><small>Coverage</small><b>${esc(s.coverage||'—')}</b></div><div class="card"><small>Verdicts</small>
  <b style="font-size:13px">${Object.entries(s.verdicts||{}).map(([k,v])=>v+' '+k).join('<br>')||'—'}</b></div></div>`;
 if(j.log&&j.log.length)h+=`<pre class="log">${esc(j.log.join('\n'))}</pre>`;
 if(j.status==='done')h+=`<div id="report">loading report…</div>`;
 $('#detail').innerHTML=h;
 if(j.status==='done'){const r=await fetch('/api/runs/'+id+'/report');$('#report').innerHTML=r.ok?await r.text():'no report'}
 if(j.status==='queued'||j.status==='running')timer=setTimeout(()=>show(id),2500)}
$('#f').onsubmit=async e=>{e.preventDefault();$('#err').textContent='';$('#go').disabled=true;
 try{const j=await api('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({repo:$('#repo').value,commit:$('#commit').value,branch:$('#branch').value,skipTests:$('#skip').checked})});show(j.id)}
 catch(x){$('#err').textContent=x.message}finally{$('#go').disabled=false}};
loadRepos();loadJobs();setInterval(loadJobs,5000);
</script></body></html>"""


def make_handler(portal: Portal):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: str | bytes, ctype: str = "application/json"):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj))

        def do_GET(self):
            p = urlparse(self.path).path.rstrip("/") or "/"
            if p == "/":
                return self._send(200, PAGE, "text/html")
            if p == "/api/runs":
                return self._json(200, portal.list())
            if p == "/api/repos":
                from .context import ContextIndex
                keys = ContextIndex(portal.cfg).clone_roots()
                return self._json(200, sorted(set(ALIASES) | {_real_key(k, v) for k, v in keys.items()}))
            m = re.match(r"^/api/runs/([\w-]+)(/report)?$", p)
            if m:
                if m.group(2):
                    md = portal.report(m.group(1))
                    return self._send(200, md_to_html(md), "text/html") if md is not None else self._json(404, {"error": "no report"})
                j = portal.get(m.group(1))
                return self._json(200, j) if j else self._json(404, {"error": "unknown run"})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path.rstrip("/") != "/api/runs":
                return self._json(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 10_000)) or b"{}")
                job = portal.submit(str(body.get("repo", "")), str(body.get("commit", "")), str(body.get("branch", "")),
                                    bool(body.get("skipTests")))
                self._json(202, job)
            except InputError as e:
                self._json(400, {"error": str(e)})
            except json.JSONDecodeError:
                self._json(400, {"error": "body must be JSON"})
    return H


def _real_key(key: str, path: Path) -> str:
    o, r = key.split("/", 1)
    o, r = _real_case(path, o, r)
    return f"{o}/{r}"


def serve(host: str = "127.0.0.1", port: int = 8765, cfg: Config = CONFIG):
    portal = Portal(cfg)
    srv = ThreadingHTTPServer((host, port), make_handler(portal))
    print(f"shockwave portal on http://{host}:{port}  (runs stored in {portal.root})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
