"""Story 3 — Blast Radius Computation: ChangedSymbols -> BlastRadius.

Algorithm (derived from probing the live graph — see clients/knowledge.py docstring):

  1. findRepo              -> is the source indexed? (else notIndexed=true, stop)
  2. resolve symbols       -> exact graph lookup by (name, git_repo) + FQN-suffix match;
                              vectorSearch fallback (score-gated). Unresolved are reported.
  3. direct callers  HIGH  -> vertices in OTHER repos that CALL the same FQN
                              (how shared-library consumers show up)
  4. transitive      HIGH  -> walk CALLS upward inside the source repo (N hops), then
                              (a) external callers of any vertex on that path, and
                              (b) OpenAPI inbound chains whose target method is on that path
                                  (method-precise API consumer detection)
  5. repo deps       MED   -> getRepoDependencies(upstream)       (coarse safety net)
  6. openapi (other) LOW   -> inbound API consumers not linked to changed code
  7. localScan       MED   -> `git grep` import of changed classes in local clones
                              (catches consumers not indexed in Code Knowledge)

Merge rule: one entry per repo; evidence accumulates; confidence only goes up.
"""
from __future__ import annotations

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

from ..clients.knowledge import KnowledgeClient, KnowledgeError
from ..clients.repos import RepoSource, git, parse_remote
from ..config import CONFIG, Config
from ..contracts import validate_blast_radius

_RANK = {"low": 0, "medium": 1, "high": 2}
_OVERLOAD = re.compile(r"\(\+\d+\)$")


def _strip(fqn: str) -> str:
    return _OVERLOAD.sub("", fqn or "")


def _short(fqn: str) -> str:
    """'maven/g/a.com.x.Foo.bar(+1)' -> 'Foo.bar'"""
    parts = _strip(fqn).split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else fqn


def _repo_from_url(url: str) -> tuple[str, str] | None:
    parts = (url or "").rstrip("/").removesuffix(".git").split("/")
    return (parts[-2], parts[-1]) if len(parts) >= 2 else None


# infra "consumers" that route traffic but don't consume the contract (configurable)
IGNORED_REPOS = {r.strip().lower() for r in os.getenv("SHOCKWAVE_IGNORE_REPOS", "ebayistio/istio").split(",") if r.strip()}


class _Acc:
    def __init__(self, source_org: str, source_repo: str):
        self.src = (source_org.lower(), source_repo.lower())
        self.repos: dict[str, dict] = {}
        self.ignored: set[str] = set()

    def add(self, org: str | None, repo: str | None, confidence: str, source: str, detail: str, **extra):
        if not org or not repo or (org.lower(), repo.lower()) == self.src:
            return
        key = f"{org}/{repo}".lower()
        if key in IGNORED_REPOS:
            self.ignored.add(f"{org}/{repo}")
            return
        e = self.repos.setdefault(key, {"org": org, "repo": repo, "confidence": confidence, "evidence": []})
        if _RANK[confidence] > _RANK[e["confidence"]]:
            e["confidence"] = confidence
        ev = {"source": source, "detail": detail, **extra}
        if ev not in e["evidence"] and len(e["evidence"]) < 25:
            e["evidence"].append(ev)

    def out(self) -> list[dict]:
        return sorted(self.repos.values(), key=lambda r: (-_RANK[r["confidence"]], r["org"].lower(), r["repo"].lower()))


def _resolve_symbol(kc: KnowledgeClient, sym: dict, repo: str, tag: str, cfg: Config) -> dict:
    """-> {symbol, fqns:[...], via: graphSearch|vectorSearch|None}"""
    pkg, cls, member = sym.get("package") or "", sym.get("className") or "", sym.get("member")
    simple = cls.split(".")[-1]
    if sym["kind"] == "class" or (member and member == simple):  # constructors are keyed by the class FQN
        suffix = f"{pkg}.{cls}" if pkg else cls
        name = simple
    else:
        suffix = f"{pkg}.{cls}.{member}" if pkg else f"{cls}.{member}"
        name = member
    res = {"symbol": sym["name"], "fqns": [], "via": None}
    if not name:
        return res
    try:
        verts = kc.vertices_by_name(name, repo)
    except KnowledgeError as e:
        res["error"] = str(e)
        verts = []
    fqns = sorted({v["fqn"] for v in verts if v.get("fqn") and _strip(v["fqn"]).endswith("." + suffix)})
    if fqns:
        res.update(fqns=fqns, via="graphSearch")
        return res
    # fallback: vector search, scoped (client-side verified) to the source repo
    try:
        hits = kc.vector_search(f"{cls}.{member}" if member else cls, limit=10, tag=tag)
    except KnowledgeError:
        hits = []
    for h in hits:
        fq = (h.get("content") or "").split("Fully Qualified Name: ")[-1].split("\n")[0].strip()
        if h.get("tag") == tag and h.get("score", 0) >= cfg.vector_min_score and _strip(fq).endswith("." + suffix):
            res["fqns"].append(fq)
    if res["fqns"]:
        res["via"] = "vectorSearch"
    return res


def _local_scan(rs: RepoSource, source: tuple[str, str], symbols: list[dict]) -> dict[str, list[str]]:
    """{ 'org/repo': [evidence...] } for local clones importing a changed class."""
    classes = {}
    for s in symbols:
        if s.get("package") and s.get("className") and s["changeType"] != "added":
            top = s["className"].split(".")[0]
            classes[f"{s['package']}.{top}"] = s["package"]
    if not classes:
        return {}
    pats = []
    for fq, pkg in classes.items():
        pats += ["-e", f"import {fq};", "-e", f"import static {fq}.", "-e", f"import {pkg}.*;"]
    hits: dict[str, list[str]] = {}
    for key, path in rs.local_index().items():
        if key == f"{source[0]}/{source[1]}".lower():
            continue
        r = subprocess.run(["git", "-C", str(path), "grep", "-I", "-l", "-F", *pats, "--", "*.java", "*.kt"],
                           capture_output=True, text=True, timeout=60)
        files = [f for f in r.stdout.splitlines() if f]
        if files:
            hits[key] = files[:10]
    return hits


def compute(changed: dict, kc: KnowledgeClient | None = None, rs: RepoSource | None = None,
            cfg: Config = CONFIG, local_scan: bool = True) -> dict:
    kc = kc or KnowledgeClient(cfg)
    rs = rs or RepoSource(cfg)
    org, repo, commit = changed["org"], changed["repo"], changed["commit"]
    out = {"sourceRepo": f"{org}/{repo}", "commit": commit, "impactedRepos": [], "unresolvedSymbols": [],
           "notIndexed": False, "indexedRef": None, "resolvedSymbols": [], "newSymbols": [],
           "unmappedApiCallers": [], "sourceApps": [], "notes": [], "backend": kc.backend_name}

    ref = kc.indexed_ref(org, repo)
    if not ref:
        out["notIndexed"] = True
        out["notes"].append(f"{org}/{repo} is not registered in Code Knowledge — blast radius UNKNOWN (not 'no impact').")
        # hints only (not claims): repo-level edges sometimes exist for unindexed repos
        deps = kc.repo_dependencies(org, repo, "upstream") or {}
        out["notIndexedHints"] = sorted({f"{p[0]}/{p[1]}" for e in deps.get("edges", []) if (p := _repo_from_url(e.get("srcRepo")))})
        return validate_blast_radius(out)
    out["indexedRef"] = ref
    acc = _Acc(org, repo)

    # ---- 2. resolve changed symbols ------------------------------------
    existing = [s for s in changed["changedSymbols"] if s["changeType"] != "added"]
    out["newSymbols"] = [s["name"] for s in changed["changedSymbols"] if s["changeType"] == "added"]
    with ThreadPoolExecutor(6) as ex:
        resolved = list(ex.map(lambda s: _resolve_symbol(kc, s, repo, ref, cfg), existing))
    fqn_to_symbol: dict[str, str] = {}
    for r in resolved:
        if r["fqns"]:
            out["resolvedSymbols"].append(r)
            for f in r["fqns"]:
                fqn_to_symbol[f] = r["symbol"]
        else:
            out["unresolvedSymbols"].append(r["symbol"])
    changed_fqns = sorted(fqn_to_symbol)

    # ---- 3. direct external callers ------------------------------------
    def _safe(label, fn, *a):
        try:
            return fn(*a)
        except KnowledgeError as e:
            out["notes"].append(f"{label} failed (coverage gap, not 'no impact'): {e}")
            return []

    if changed_fqns:
        for c in _safe("direct caller lookup", kc.external_callers, changed_fqns, repo):
            acc.add(c.get("git_org"), c.get("git_repo"), "high", "graphSearch",
                    f"{_short(c.get('target_fqn'))} (changed: {fqn_to_symbol.get(c.get('target_fqn'), '?')}) is called from {_short(c.get('fqn'))} in {c.get('git_repo')}",
                    callerFqn=c.get("fqn"), targetFqn=c.get("target_fqn"))

    # ---- 4. transitive: internal upward walk -> external + API ---------
    closure = _safe("transitive caller walk", kc.transitive_internal_callers, changed_fqns, repo) if changed_fqns else []
    out["transitiveCallerCount"] = len(closure)
    extra = [f for f in closure if f not in fqn_to_symbol][:300]
    if extra:
        for c in _safe("transitive external caller lookup", kc.external_callers, extra, repo):
            acc.add(c.get("git_org"), c.get("git_repo"), "high", "graphSearch",
                    f"{_short(c.get('fqn'))} in {c.get('git_repo')} calls {_short(c.get('target_fqn'))}, which transitively reaches changed code",
                    callerFqn=c.get("fqn"), targetFqn=c.get("target_fqn"), transitive=True)

    reach_short = {_short(f) for f in (changed_fqns + closure)}
    changed_eps = {(e["method"].upper(), e["path"]) for e in changed.get("changedApiEndpoints", [])}
    changed_ep_paths = {p for _, p in changed_eps}

    # ---- 6 (+4b). OpenAPI inbound chains --------------------------------
    try:
        oa = kc.openapi_connections(org, repo, "inbound") or {}
    except KnowledgeError as e:
        oa = {}
        out["notes"].append(f"openapi-connections unavailable: {e}")
    for ch in oa.get("inbound", []) or []:
        tgt = f"{ch.get('targetClass')}.{ch.get('targetMethod')}"
        api = f"{ch.get('apiMethod')} {ch.get('apiPath')} ({ch.get('apiOperationId')})"
        matched = tgt in reach_short or (ch.get("apiMethod", "").upper(), ch.get("apiPath")) in changed_eps or ch.get("apiPath") in changed_ep_paths
        if not ch.get("callerRepo"):
            out["unmappedApiCallers"].append({"apps": ch.get("callerAppNames"), "api": api, "matchedChangedCode": matched})
            continue
        if matched:
            acc.add(ch.get("callerOrg"), ch.get("callerRepo"), "high", "getOpenapiConnections",
                    f"{ch.get('callerClass')}.{ch.get('callerMethod')} calls {api} -> {tgt}, which reaches changed code",
                    api=api, matchedChangedCode=True)
        else:
            acc.add(ch.get("callerOrg"), ch.get("callerRepo"), "low", "getOpenapiConnections",
                    f"{ch.get('callerClass')}.{ch.get('callerMethod')} calls {api} (endpoint not linked to changed code)",
                    api=api, matchedChangedCode=False)

    # ---- 5. repo dependencies (upstream = who depends on us) ------------
    try:
        deps = kc.repo_dependencies(org, repo, "upstream") or {}
    except KnowledgeError as e:
        deps = {}
        out["notes"].append(f"repo dependencies unavailable: {e}")
    apps = set()
    for e in deps.get("edges", []) or []:
        p = _repo_from_url(e.get("srcRepo"))
        via = ", ".join(f"{a.get('srcApp')}->{a.get('tgtApp')}" for a in e.get("viaApps", []))
        apps.update(a.get("tgtApp") for a in e.get("viaApps", []) if a.get("tgtApp"))
        if p:
            acc.add(p[0], p[1], "medium", "getRepoDependencies", f"app dependency {via}")
    out["sourceApps"] = sorted(apps | {repo.lower()})

    # ---- 7. local clone scan --------------------------------------------
    if local_scan:
        for key, files in _local_scan(rs, (org, repo), changed["changedSymbols"]).items():
            o, r = key.split("/", 1)
            p = rs.local_path(o, r)
            try:
                real = parse_remote(git(p, "remote", "get-url", "origin")) or (o, r)
            except Exception:
                real = (o, r)
            acc.add(real[0], real[1], "medium", "localScan", f"imports changed class(es) in: {', '.join(files[:5])}", files=files)

    impacted = acc.out()
    # Non-code / additive-only change: nothing existing changed, so coarse repo-level signals
    # (repo deps, unlinked OpenAPI consumers) are NOT evidence of impact. Report for awareness only.
    if not existing and not changed_eps:
        out["contextRepos"] = [f"{r['org']}/{r['repo']}" for r in impacted]
        impacted = [r for r in impacted if r["confidence"] == "high"]
        out["notes"].append(
            f"No existing code symbols or endpoints changed (non-code or additive-only change). "
            f"{len(out['contextRepos'])} repo-level consumers listed in contextRepos for awareness only — not treated as impacted.")
    out["impactedRepos"] = impacted
    out["ignoredRepos"] = sorted(acc.ignored)
    out["toolCalls"] = len(kc.calls)
    return validate_blast_radius(out)


