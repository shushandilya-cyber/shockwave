"""Story 3, local half: blast radius from the local context index (``context.py``).

Runs for every source. For a source Code Knowledge has not indexed it is the only signal,
and it is what turns "UNKNOWN" into an evidence-backed list. Every claim carries
``repo@commit:file:line`` so it can be checked by hand.

Signals and the confidence each one earns (same definitions as the graph half):

  localCallSite         high    consumer imports the changed type AND calls the changed member
                                (``.member(`` / ``new Type(``) in that file, or imports a removed type
  localHttpClient       high    consumer calls the source over HTTP and names an endpoint the change
                                reaches directly, by a path fragment unique to that endpoint
  localScan             medium  consumer imports a changed type (no call to the changed member shown)
  localMavenDependency  medium  consumer's pom depends on the artifact whose module was changed
  localHttpClient       medium  consumer calls the source over HTTP; the change reaches an endpoint,
                                but which endpoint this consumer calls is not established
  localHttpClient       low     consumer calls the source over HTTP; no endpoint reaches the change

Reachability inside the source is a *type-level* reference closure (files that name a
changed type, then files that name those, up to ``depth`` hops). It is coarser than the
graph's method-level CALLS walk, so it never produces ``high`` on its own.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..context import ContextIndex, _run
from ..config import CONFIG, Config

_TEST_PATH = re.compile(r"(^|/)(src/test|test|tests|it)/", re.I)
# names too generic to count as "calls the changed member" on a textual match
_GENERIC_MEMBERS = {"get", "set", "put", "add", "run", "call", "apply", "build", "create", "execute", "process",
                    "handle", "init", "close", "toString", "equals", "hashCode", "of", "from", "valueOf", "values",
                    "size", "isEmpty", "remove", "update", "delete", "find", "load", "save", "start", "stop"}


def _top_type(sym: dict) -> str | None:
    if sym.get("package") and sym.get("className"):
        return f"{sym['package']}.{sym['className'].split('.')[0]}"
    return None


def _git_grep_rows(path: Path, ref: str, args: list[str], pathspec: list[str]) -> list[tuple[str, int, str]]:
    out = _run(Path(path), "grep", "-n", "-I", *args, ref, "--", *pathspec, timeout=300)
    rows = []
    for ln in out.splitlines():
        if ln.startswith(ref + ":"):
            ln = ln[len(ref) + 1:]
        parts = ln.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            rows.append((parts[0], int(parts[1]), parts[2].strip()[:200]))
    return rows


def _cite(ctx: dict, f: str, line: int | None = None) -> str:
    return f"{ctx['org']}/{ctx['repo']}@{ctx['commit'][:8]}:{f}" + (f":{line}" if line else "")


def _artifact_for(ctx: dict, file: str) -> dict | None:
    best = None
    for a in ctx.get("artifacts", []):
        mod = a["moduleDir"]
        if (not mod or file.startswith(mod + "/")) and (best is None or len(mod) > len(best["moduleDir"])):
            best = a
    return best


def type_closure(ctx: dict, seeds: set[str], depth: int = 4, limit: int = 300) -> dict[str, int]:
    """FQN -> hop count for main-source types in ``ctx`` that (transitively) name a seed type."""
    by_file = {f: fq for fq, f in ctx.get("types", {}).items()}
    dist = {s: 0 for s in seeds if s in ctx.get("types", {})}
    frontier = set(dist)
    for hop in range(1, depth + 1):
        names = sorted({fq.rsplit(".", 1)[-1] for fq in frontier})
        if not names or len(dist) >= limit:
            break
        args = ["-l", "-w", "-F"]
        for n in names[:200]:
            args += ["-e", n]
        listed = _run(Path(ctx["path"]), "grep", "-I", *args, ctx["ref"], "--", "*.java", timeout=300)
        nxt = set()
        for ln in listed.splitlines():
            f = ln.split(":", 1)[1] if ln.startswith(ctx["ref"] + ":") else ln
            fq = by_file.get(f)
            if fq and fq not in dist and not _TEST_PATH.search(f):
                dist[fq] = hop
                nxt.add(fq)
        frontier = nxt
    return dist


def exposed_endpoints(ctx: dict, changed: dict, closure: dict[str, int]) -> list[dict]:
    direct_members = {(_top_type(s), s.get("member")) for s in changed.get("changedSymbols", [])
                      if s.get("kind") == "method" and s.get("changeType") != "added"}
    changed_eps = {(e["method"].upper(), e["path"]) for e in changed.get("changedApiEndpoints", [])}
    out = []
    for e in ctx.get("endpoints", []):
        key = (e["method"].upper(), e["path"])
        cls = e.get("className")
        if key in changed_eps or (cls, e.get("method_name")) in direct_members:
            via = "direct"
        elif cls and cls in closure:
            via = "transitive"
        else:
            continue
        out.append({"method": e["method"], "path": (e.get("basePath") or "") + e["path"], "rawPath": e["path"],
                    "operationId": e.get("operationId") or e.get("method_name"),
                    "targetMethod": f"{(cls or '?').rsplit('.', 1)[-1]}.{e.get('method_name') or e.get('operationId')}",
                    "reachedVia": via, "hops": 0 if via == "direct" else closure.get(cls), "source": "localContext",
                    "evidence": _cite(ctx, e.get("file") or e.get("spec"), e.get("line"))})
    return out


def _fragments(path: str) -> list[str]:
    """Static pieces of an endpoint path, most specific first: '/a/{id}/b_c' -> ['/b_c', 'b_c', '/a/']."""
    segs = [s for s in path.split("/") if s]
    frags = [s for s in segs if not s.startswith("{") and len(s) >= 5]
    return sorted(set(frags), key=lambda s: (-segs.index(s), -len(s)))


def maven_reach(ci: ContextIndex, consumer: dict) -> set[str]:
    """``groupId:artifactId`` the consumer can see on its classpath: its direct dependencies plus
    whatever those pull in, followed through every locally indexed pom."""
    deps_of: dict[str, set[str]] = {}
    for c in ci.repos.values():
        for a in c.get("artifacts", []):
            ga = f"{a['groupId']}:{a['artifactId']}"
            deps_of.setdefault(ga, set()).update(d for d, poms in c.get("dependencies", {}).items() if a["pom"] in poms)
    seen, todo = set(), list(consumer.get("dependencies", {}))
    while todo:
        ga = todo.pop()
        if ga in seen:
            continue
        seen.add(ga)
        todo += list(deps_of.get(ga, ()))
    return seen


def compute_local(changed: dict, acc, out: dict, ci: ContextIndex, cfg: Config = CONFIG) -> None:
    org, repo = changed["org"], changed["repo"]
    src = ci.get(org, repo)
    if src is None:
        try:
            p = ci.ensure_local(org, repo)
            src = ci.load_or_build(f"{org}/{repo}".lower(), p)
        except Exception as e:
            out["notes"].append(f"local context for {org}/{repo} unavailable: {e}")
            return
    others = {k: c for k, c in ci.repos.items() if k != f"{org}/{repo}".lower() and not c.get("error")}
    out["localContext"] = {"sourceRef": f"{src['ref']}@{src['commit'][:12]}", "reposScanned": len(others),
                           "appNames": src.get("appNames", [])}

    # an added abstract interface method is not "new code nobody uses": every implementer must add it
    syms = [s for s in changed.get("changedSymbols", [])
            if s.get("changeType") != "added" or (s.get("classKind") == "interface" and s.get("abstract"))]
    types: dict[str, list[dict]] = {}
    for s in syms:
        if (t := _top_type(s)):
            types.setdefault(t, []).append(s)
    pkgs = {t.rsplit(".", 1)[0] for t in types}

    # ---- imports and call sites ------------------------------------------------
    type_art = {t: _artifact_for(src, src["types"][t]) for t in types if t in src.get("types", {})}
    ignored_imports: dict[str, int] = {}
    for key, c in sorted(others.items()):
        imps = c.get("imports", {})
        own = c.get("types", {})
        # a consumer that defines the same FQN itself (copied or generated class) is not using ours
        hit_types = [t for t in types if t in imps and t not in own] + \
                    [t for t in types if f"{t.rsplit('.', 1)[0]}.*" in imps and t not in own]
        if not hit_types:
            continue
        # An import only resolves to *this* repo's class if the consumer has a Maven path to the
        # artifact that contains it; otherwise the same FQN is coming from some other jar.
        reach = maven_reach(ci, c) if src.get("artifacts") else None
        if reach is not None:
            kept = [t for t in hit_types if (a := type_art.get(t)) is None or f"{a['groupId']}:{a['artifactId']}" in reach]
            if len(kept) < len(set(hit_types)):
                ignored_imports[f"{c['org']}/{c['repo']}"] = len(set(hit_types) - set(kept))
            hit_types = kept
        if not hit_types:
            continue
        for t in dict.fromkeys(hit_types):
            files = imps.get(t) or imps.get(f"{t.rsplit('.', 1)[0]}.*") or []
            flist = sorted({f for f, _ in files})
            members = {s["member"] for s in types[t] if s.get("member") and s.get("visibility") != "private"}
            removed_cls = any(s["kind"] == "class" and s["changeType"] == "removed" for s in types[t])
            simple = t.rsplit(".", 1)[-1]
            new_abstract = [s["member"] for s in types[t] if s.get("classKind") == "interface" and s.get("abstract")
                            and s["changeType"] == "added"]
            calls = []
            pats = []
            for m in members - set(new_abstract):
                if m == simple:
                    pats += ["-e", f"new {simple}("]
                elif m not in _GENERIC_MEMBERS and len(m) >= 4:
                    pats += ["-e", f".{m}("]
            if pats and flist:
                calls = _git_grep_rows(Path(c["path"]), c["ref"], ["-F", *pats], flist[:200])
            sym_names = sorted({s["name"] for s in types[t]})
            impls = _git_grep_rows(Path(c["path"]), c["ref"],
                                   ["-E", r"(implements|extends)[^{]*[^A-Za-z0-9_.]" + simple + r"([^A-Za-z0-9_]|$)"],
                                   flist[:200]) if new_abstract and flist else []
            if impls:
                f, ln, text = impls[0]
                acc.add(c["org"], c["repo"], "high", "localCallSite",
                        f"{_cite(c, f, ln)} implements `{simple}`, which gains abstract method(s) "
                        f"{', '.join(f'`{m}`' for m in new_abstract)}: this class stops compiling",
                        files=[_cite(c, f, n) for f, n, _ in impls[:10]], symbols=sym_names, commit=c["commit"])
            if calls:
                f, ln, text = calls[0]
                acc.add(c["org"], c["repo"], "high", "localCallSite",
                        f"{_cite(c, f, ln)} calls changed `{simple}` member: `{text[:100]}`",
                        files=[_cite(c, f, n) for f, n, _ in calls[:10]], symbols=sym_names, commit=c["commit"])
            elif removed_cls:
                f, ln = files[0]
                acc.add(c["org"], c["repo"], "high", "localCallSite",
                        f"{_cite(c, f, ln)} imports `{simple}`, which this commit removes (compile break)",
                        files=[_cite(c, f, n) for f, n in files[:10]], symbols=sym_names, commit=c["commit"])
            else:
                f, ln = files[0]
                acc.add(c["org"], c["repo"], "medium", "localScan",
                        f"{_cite(c, f, ln)} imports changed type `{simple}` ({len(flist)} file(s)); no call to a changed member found",
                        files=[_cite(c, f, n) for f, n in files[:10]], symbols=sym_names, commit=c["commit"])

    if ignored_imports:
        out["localContext"]["importsWithoutMavenPath"] = ignored_imports
        out["notes"].append(
            f"Ignored same-name imports in {len(ignored_imports)} repo(s) with no Maven path to {org}/{repo}'s artifact "
            f"(the class comes from another jar): {', '.join(sorted(ignored_imports))}")

    # ---- Maven artifact dependency ---------------------------------------------
    arts: dict[str, dict] = {}
    for f in changed.get("changedFiles", []):
        if _TEST_PATH.search(f) or not f.endswith((".java", ".kt", ".xml", ".yaml", ".yml", ".properties", ".json")):
            continue
        if (a := _artifact_for(src, f)):
            arts[f"{a['groupId']}:{a['artifactId']}"] = a
    for key, c in sorted(others.items()):
        if not arts:
            break
        reach = maven_reach(ci, c)
        for ga, a in arts.items():
            poms = c.get("dependencies", {}).get(ga)
            if poms:
                acc.add(c["org"], c["repo"], "medium", "localMavenDependency",
                        f"{_cite(c, poms[0])} depends on `{ga}` (module `{a['moduleDir'] or '.'}` changed in this commit)",
                        files=[_cite(c, p) for p in poms[:5]], artifact=ga, commit=c["commit"])
            elif ga in reach:
                via = sorted(d for d in c.get("dependencies", {}) if ga in maven_reach(ci, {"dependencies": {d: []}}))
                acc.add(c["org"], c["repo"], "medium", "localMavenDependency",
                        f"{c['org']}/{c['repo']} pulls in `{ga}` transitively via {', '.join(f'`{v}`' for v in via[:3])} "
                        f"(module `{a['moduleDir'] or '.'}` changed in this commit)",
                        artifact=ga, transitive=True, commit=c["commit"])

    # ---- HTTP consumers -----------------------------------------------------------
    closure = type_closure(src, set(types)) if types else {}
    out["localContext"]["typeClosureSize"] = len(closure)
    exposed = exposed_endpoints(src, changed, closure)
    out["localContext"]["exposedVia"] = exposed
    frag_owner: dict[str, set] = {}
    for e in src.get("endpoints", []):
        for fr in _fragments(e["path"]):
            frag_owner.setdefault(fr, set()).add((e["method"], e["path"]))
    apps = set(src.get("appNames", []))
    for key, c in sorted(others.items()):
        calls = [o for o in c.get("outbound", []) if o["app"] in apps]
        if not calls:
            continue
        o0 = next((o for o in calls if not o["isTest"]), calls[0])
        base = f"{_cite(c, o0['file'], o0['line'])} calls `{o0['host']}{o0['basePath']}`"
        if not exposed:
            acc.add(c["org"], c["repo"], "low", "localHttpClient",
                    f"{base}; no endpoint of {repo} reaches the changed code", commit=c["commit"], matchedChangedCode=False)
            continue
        matched = None
        # the configured base URL names the service, not an endpoint
        base_segs = {s for o in calls for s in (o.get("basePath") or "").split("/") if s}
        for e in exposed:
            for fr in _fragments(e["rawPath"]):
                if len(frag_owner.get(fr, ())) != 1 or fr in base_segs:
                    continue  # shared by several endpoints: naming it doesn't say which one is called
                rows = _git_grep_rows(Path(c["path"]), c["ref"], ["-F", "-e", fr], ["*.java", "*.kt", "*.yaml", "*.yml", "*.json", "*.properties"])
                rows = [r for r in rows if not _TEST_PATH.search(r[0])] or rows
                if rows:
                    matched = (e, fr, rows[0])
                    break
            if matched:
                break
        if matched:
            e, fr, (f, ln, text) = matched
            conf = "high" if e["reachedVia"] == "direct" else "medium"
            acc.add(c["org"], c["repo"], conf, "localHttpClient",
                    f"{base} and names `{e['method']} {e['path']}` ({_cite(c, f, ln)}: `{text[:80]}`), "
                    f"which {'directly' if conf == 'high' else 'transitively (type-level)'} reaches `{e['targetMethod']}`",
                    api=f"{e['method']} {e['path']}", commit=c["commit"], matchedChangedCode=True)
        else:
            acc.add(c["org"], c["repo"], "medium", "localHttpClient",
                    f"{base}; the change reaches {len(exposed)} endpoint(s) of {repo} but which one this consumer calls is not established",
                    commit=c["commit"], matchedChangedCode=False)
