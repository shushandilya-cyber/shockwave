"""Code Knowledge client with two interchangeable backends.

* ``http`` — calls knowledgehub REST directly (fast, used for local dev).
* ``mcp``  — calls the hosted ``codemcp`` MCP server over streamable HTTP, i.e. the
  exact tool surface an Obsidian workflow node gets. Running the pipeline with this
  backend proves the Obsidian tool-call recipe works end-to-end.

Known upstream quirks handled here (discovered while probing, 2026-09):
1. Vector search scoping key is ``filters`` (plural). Both code-mcp and codemcp send
   ``filter`` which is silently ignored -> results come from random repos. With the
   MCP backend we therefore post-filter results by ``tag`` client-side.
2. Cross-repo links are *FQN-based*: each consumer repo has its own vertex for an
   external symbol with the same ``fqn``. ``CALLS`` edges into the *source* repo's
   vertex only come from the source repo itself. So caller lookups must be
   ``g.V().has('fqn', within(...)).in('CALLS')`` — not ``has('vertex_key', vkey)``.
3. ``TextP.endingWith`` on ``fqn`` times out (no index) — use exact ``name`` +
   ``git_repo`` lookups and filter FQN suffixes client-side.
4. ``openapi-connections`` can take ~50s — cached on disk.
"""
from __future__ import annotations

import hashlib
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..config import CONFIG, Config

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


class KnowledgeError(RuntimeError):
    pass


def gq(s: str) -> str:
    """Quote a string literal for Gremlin."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


class _Cache:
    def __init__(self, cfg: Config):
        self.dir = cfg.cache_dir / "knowledge"
        self.ttl = cfg.cache_ttl_seconds
        self.dir.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str):
        return self.dir / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def get(self, key: str):
        p = self._p(key)
        if p.exists() and time.time() - p.stat().st_mtime < self.ttl:
            try:
                return json.loads(p.read_text())
            except Exception:
                return None
        return None

    def put(self, key: str, val: Any):
        self._p(key).write_text(json.dumps(val))


class _HttpBackend:
    def __init__(self, cfg: Config):
        self.base = cfg.knowledge_base.rstrip("/")
        self.timeout = cfg.http_timeout

    def _do(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        rq = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(rq, context=_CTX, timeout=self.timeout) as r:
                return json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"_status": 404}
            raise KnowledgeError(f"{method} {path} -> HTTP {e.code}: {e.read().decode()[:300]}") from e
        except Exception as e:  # timeouts, DNS (VPN off), ...
            raise KnowledgeError(f"{method} {path} failed: {e}") from e

    def find_repo(self, service):
        return self._do("GET", "/v1/repos", {"search": service, "lastSuccessfulState": ["Completed", "DocCompleted"]})

    def vector(self, query, limit, tag):
        body = {"input": query, "topK": limit, "category": "code", "index": "Knowledgevector"}
        if tag:
            body["filters"] = {"tag": tag}
        return self._do("POST", "/v1/search/vector", body=body)

    def graph(self, gremlin, vkey):
        return self._do("POST", "/v1/search/graph", body={"query": gremlin, "bindings": {"vkey": vkey}})

    def repo_deps(self, org, repo, direction):
        return self._do("GET", f"/v1/apps/repos/{org}/{repo}/dependencies", {"direction": direction})

    def openapi(self, org, repo, direction):
        return self._do("GET", f"/v1/apps/repos/{org}/{repo}/openapi-connections", {"direction": direction})


class _McpBackend:
    """Minimal MCP streamable-HTTP client for hosted codemcp (JSON-RPC 2.0)."""

    def __init__(self, cfg: Config):
        self.url = cfg.codemcp_url
        self.timeout = cfg.http_timeout
        self.sid: str | None = None
        self._id = 0

    def _post(self, payload: dict):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        rq = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=h)
        try:
            with urllib.request.urlopen(rq, context=_CTX, timeout=self.timeout) as r:
                self.sid = r.headers.get("Mcp-Session-Id") or self.sid
                raw = r.read().decode()
        except Exception as e:
            raise KnowledgeError(f"codemcp {payload.get('method')} failed: {e}") from e
        if not raw.strip():
            return None
        if "data:" in raw:  # SSE framing
            raw = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")][-1]
        return json.loads(raw)

    def _init(self):
        if self.sid:
            return
        self._id += 1
        self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "shockwave", "version": "0.1"}}})
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        except KnowledgeError:
            pass

    def call(self, tool: str, args: dict, retries: int = 1):
        self._init()
        self._id += 1
        resp = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call", "params": {"name": tool, "arguments": args}})
        if not resp or "error" in resp:
            raise KnowledgeError(f"codemcp {tool} error: {resp and resp.get('error')}")
        res = resp["result"]
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            if "404" in text:
                return {"_status": 404}
            raise KnowledgeError(f"codemcp {tool} tool error: {text[:300]}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise KnowledgeError(f"codemcp {tool} returned non-JSON: {text[:200]}")
        # codemcp reports backend failures as a *successful* result {"error": "..."} (isError=false)
        if isinstance(data, dict) and set(data) == {"error"}:
            if "404" in str(data["error"]) or "Not Found" in str(data["error"]):
                return {"_status": 404}
            if retries > 0:
                return self.call(tool, args, retries - 1)
            raise KnowledgeError(f"codemcp {tool} backend error (reported as success): {str(data['error'])[:250]}")
        return data

    def find_repo(self, service):
        return self.call("findRepo", {"service": service})

    def vector(self, query, limit, tag):
        # server ignores tag (sends wrong key) -> over-fetch and post-filter
        res = self.call("vectorSearch", {"query": query, "limit": max(limit * 6, 30), "tag": tag})
        if tag and isinstance(res, list):
            res = [r for r in res if r.get("tag") == tag]
        return res[:limit] if isinstance(res, list) else res

    def graph(self, gremlin, vkey):
        return self.call("graphSearch", {"gremlinQuery": gremlin, "vkey": vkey})

    def repo_deps(self, org, repo, direction):
        return self.call("getRepoDependencies", {"org": org, "repo": repo, "direction": direction})

    def openapi(self, org, repo, direction):
        return self.call("getOpenapiConnections", {"org": org, "repo": repo, "direction": direction})


class KnowledgeClient:
    def __init__(self, cfg: Config = CONFIG, backend: str | None = None):
        self.cfg = cfg
        self.backend_name = backend or cfg.knowledge_backend
        self.b = _McpBackend(cfg) if self.backend_name == "mcp" else _HttpBackend(cfg)
        self.cache = _Cache(cfg)
        self.calls: list[dict] = []  # audit trail of tool calls (surfaced in outputs)

    def _cached(self, key: str, fn, *a):
        hit = self.cache.get(key)
        t0 = time.time()
        if hit is not None:
            self.calls.append({"call": key[:160], "cached": True})
            return hit
        val = fn(*a)
        self.cache.put(key, val)
        self.calls.append({"call": key[:160], "cached": False, "seconds": round(time.time() - t0, 2)})
        return val

    # ---- raw tools -------------------------------------------------------
    def find_repo(self, service: str) -> dict:
        return self._cached(f"findRepo|{service}", self.b.find_repo, service)

    def vector_search(self, query: str, limit: int = 5, tag: str = "") -> list:
        r = self._cached(f"vector|{self.backend_name}|{query}|{limit}|{tag}", self.b.vector, query, limit, tag)
        return r if isinstance(r, list) else []

    def graph(self, gremlin: str, vkey: str = "unused") -> list:
        r = self._cached(f"graph|{gremlin}|{vkey}", self.b.graph, gremlin, vkey)
        return (r or {}).get("results", []) if isinstance(r, dict) else []

    def repo_dependencies(self, org: str, repo: str, direction: str = "upstream") -> dict | None:
        r = self._cached(f"repoDeps|{org}/{repo}|{direction}", self.b.repo_deps, org, repo, direction)
        return None if (isinstance(r, dict) and r.get("_status") == 404) else r

    def openapi_connections(self, org: str, repo: str, direction: str = "inbound") -> dict | None:
        r = self._cached(f"openapi|{org}/{repo}|{direction}", self.b.openapi, org, repo, direction)
        return None if (isinstance(r, dict) and r.get("_status") == 404) else r

    # ---- helpers built on the raw tools ---------------------------------
    def indexed_ref(self, org: str, repo: str) -> str | None:
        """Return 'org:repo:branch' if the repo is indexed in Code Knowledge."""
        d = self.find_repo(repo) or {}
        for it in d.get("items", []):
            rid = it.get("id") or f"{it.get('organization')}:{it.get('repo')}:{it.get('branch')}"
            parts = rid.split(":")
            if len(parts) >= 3 and parts[0].lower() == org.lower() and parts[1].lower() == repo.lower():
                return rid
        return None

    def vertices_by_name(self, name: str, repo: str) -> list[dict]:
        q = f"g.V().has('name', {gq(name)}).has('git_repo', {gq(repo)}).limit(200).valueMap('fqn','vertex_key','git_org','git_repo')"
        return [_flat(v) for v in self.graph(q)]

    def external_callers(self, fqns: list[str], source_repo: str) -> list[dict]:
        """Vertices in OTHER repos that CALL any vertex whose fqn is in ``fqns``."""
        out: list[dict] = []
        tmpl = ("g.V().has('fqn', within({lst})).as('t').in('CALLS').has('git_repo', neq(" + gq(source_repo) + "))"
                ".as('c').select('t','c').by('fqn').by(valueMap('fqn','git_org','git_repo')).limit(500)")
        for chunk in _chunks(fqns, MAX_QUERY_CHARS - len(tmpl)):
            for row in self.graph(tmpl.replace("{lst}", ",".join(gq(f) for f in chunk))):
                c = _flat(row.get("c", {}))
                c["target_fqn"] = row.get("t")
                out.append(c)
        return out

    def transitive_internal_callers(self, fqns: list[str], repo: str) -> list[str]:
        """All FQNs in ``repo`` that (transitively) call any of ``fqns`` (upward walk)."""
        if not fqns:
            return []
        tmpl = ("g.V().has('fqn', within({lst})).has('git_repo', " + gq(repo) + ")"
                ".repeat(__.in('CALLS').has('git_repo', " + gq(repo) + ").simplePath()).emit().times(" + str(self.cfg.transitive_depth) + ")"
                ".dedup().limit(" + str(self.cfg.transitive_limit) + ").values('fqn')")
        found: set[str] = set()
        for chunk in _chunks(fqns, MAX_QUERY_CHARS - len(tmpl)):
            for r in self.graph(tmpl.replace("{lst}", ",".join(gq(f) for f in chunk))):
                v = r.get("value") if isinstance(r, dict) else r
                if v:
                    found.add(v)
        return sorted(found)


MAX_QUERY_CHARS = 4800  # NuGraph rejects queries > 5000 chars


def _chunks(items: list[str], budget: int):
    cur, size = [], 0
    for it in items:
        cost = len(gq(it)) + 1
        if cur and size + cost > budget:
            yield cur
            cur, size = [], 0
        cur.append(it)
        size += cost
    if cur:
        yield cur


def _flat(v: dict) -> dict:
    return {k: (val[0] if isinstance(val, list) and val else val) for k, val in (v or {}).items()}

