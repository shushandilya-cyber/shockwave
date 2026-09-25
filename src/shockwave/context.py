"""Local cross-repo context index.

Code Knowledge only covers repos that have been submitted for indexing. Three of the
five services this pipeline is pointed at (punotif, polis, locnapi) are not, so the
blast radius for them would always be UNKNOWN. This module builds the missing context
from local clones, read-only, at each repo's *default-branch* commit (``git grep <ref>``),
so a working tree checked out on a feature branch is still analysed at master.

Per repo it records only facts that can be cited as ``repo@commit:file:line``:

* ``artifacts``     Maven ``groupId:artifactId`` published by each pom, with its module dir
* ``dependencies``  Maven ``groupId:artifactId`` each pom depends on
* ``types``         top-level main-source Java types (FQN -> file)
* ``imports``       non-JDK types imported (FQN or ``pkg.*`` -> files)
* ``endpoints``     REST endpoints served (JAX-RS / Spring annotations)
* ``outbound``      service URLs it calls (``http(s)://<app>…ebay.com/…``) with the app parsed out
* ``appNames``      the service's own application names (README, repo name)

Context is cached per (repo, commit) and rebuilt only when the default branch moves.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import javaparse
from .clients.repos import RepoSource, git, parse_remote
from .config import CONFIG, Config

# bump whenever extraction changes, so cached contexts built by older code are rebuilt
CONTEXT_VERSION = 5

_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(Path.home())}
_TEST_PATH = re.compile(r"(^|/)(src/test|test|tests|it)/", re.I)
_PKG = re.compile(r"^\s*package\s+([\w.]+)\s*;")
_IMPORT = re.compile(r"^\s*import\s+(static\s+)?([\w.]+?)(\.\*)?\s*;")
_JDK_IMPORT = re.compile(r"(java|javax|jakarta|kotlin|sun|jdk)\.")
_URL = re.compile(r"https?://([a-z0-9][a-z0-9.-]*\.ebay\.com)(/[\w/{}.~%-]*)?", re.I)
_APP_README = re.compile(r"application name\s*:\s*`?([a-z0-9][\w-]*)`?", re.I)
# host label suffixes that name an environment rather than the app (locnapi-stage.qa -> locnapi)
_ENV_SUFFIX = re.compile(r"(-(stage|staging|qa|pp|preprod|sandbox|sbx|dev|test|lnp|feature\w*))+$")
_INFRA_HOSTS = re.compile(r"(^|\.)(api|apiz|svcs|www|pages|jirap|wiki|github|artifactory|repo|maven|nexus|ebaycdn|ir|i)\.", re.I)
_MAVEN_NS = re.compile(r"^\{[^}]+\}")
# individual pools / hosts (slcpickupsvc21-1028257, pickupsvc-phx-2-web-env…) name a machine, not an app
_POOL_LABEL = re.compile(r"\d{4,}|-(phx|slc|lvs|rno|sjc)-|^(phx|slc|lvs|rno)[a-z]+\d")


def _run(path: Path, *args: str, timeout: int = 180) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout, env=_GIT_ENV)
    return r.stdout if r.returncode in (0, 1) else ""


def default_ref(path: Path) -> str:
    """Remote default branch (origin/HEAD), else origin/master|main, else local master|main, else HEAD."""
    ref = _run(path, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD").strip()
    if ref:
        return ref
    for cand in ("origin/master", "origin/main", "master", "main"):
        if _run(path, "rev-parse", "--verify", "--quiet", cand + "^{commit}").strip():
            return cand
    return "HEAD"


def refresh(path: Path, ref: str, timeout: int = 300) -> str | None:
    """``git fetch`` the default branch so 'latest commit on master' means the remote's. Read-only
    against the remote; only remote-tracking refs move, the user's working tree is untouched."""
    if not ref.startswith("origin/"):
        return None
    r = subprocess.run(["git", "-C", str(path), "fetch", "--quiet", "origin", ref.split("/", 1)[1]],
                       capture_output=True, text=True, timeout=timeout, env=_GIT_ENV)
    return None if r.returncode == 0 else (r.stderr.strip()[:200] or f"git fetch exited {r.returncode}")


def app_from_host(host: str) -> str | None:
    host = host.lower()
    if _INFRA_HOSTS.search(host + "."):
        return None
    label = host.split(".")[0]
    if _POOL_LABEL.search(label):
        return None
    label = _ENV_SUFFIX.sub("", label)
    return label or None


def _grep(path: Path, ref: str, args: list[str], pathspec: list[str]) -> list[tuple[str, int, str]]:
    """``git grep -n`` at ``ref`` -> [(file, line, text)]."""
    out = _run(path, "grep", "-n", "-I", *args, ref, "--", *pathspec, timeout=300)
    rows = []
    pre = ref + ":"
    for ln in out.splitlines():
        if ln.startswith(pre):
            ln = ln[len(pre):]
        parts = ln.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            rows.append((parts[0], int(parts[1]), parts[2]))
    return rows


def _pom_info(text: str) -> tuple[str | None, str | None, list[str]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None, None, []

    def child(el, name):
        for c in list(el):
            if _MAVEN_NS.sub("", c.tag) == name:
                return c
        return None

    def txt(el, name):
        c = child(el, name) if el is not None else None
        return (c.text or "").strip() if c is not None and c.text else None

    parent = child(root, "parent")
    gid = txt(root, "groupId") or txt(parent, "groupId")
    aid = txt(root, "artifactId")
    deps = []
    for el in root.iter():
        if _MAVEN_NS.sub("", el.tag) == "dependency":
            g, a = txt(el, "groupId"), txt(el, "artifactId")
            if g and a and "${" not in g + a:
                deps.append(f"{g}:{a}")
    return gid, aid, sorted(set(deps))


def _endpoints(path: Path, ref: str, files: list[str]) -> list[dict]:
    out = []
    for f in files[:400]:
        src = _run(path, "show", f"{ref}:{f}")
        if not src:
            continue
        pkg, decls = javaparse.outline(src)
        classes = {(d.cls + "." + d.name if d.cls else d.name): d for d in decls if d.kind == "class"}
        for d in decls:
            if d.kind != "method":
                continue
            for e in javaparse.endpoints_for(d, classes.get(d.cls)):
                out.append({**e, "className": javaparse.class_fqn(pkg, "", d.cls) if d.cls else pkg,
                            "method_name": d.name, "file": f, "line": d.start})
    return out


_HTTP_VERBS = ("get", "post", "put", "delete", "patch", "head", "options")


def _handlers(path: Path, ref: str) -> dict[str, dict]:
    """``operationId`` -> handler, for classes implementing a generated ``*Api`` interface."""
    listed = _run(path, "grep", "-l", "-I", "-E", r"implements[[:space:]]+[A-Za-z0-9_, .]*Api([^A-Za-z0-9_]|$)", ref, "--", "*.java")
    out: dict[str, dict] = {}
    for ln in listed.splitlines():
        f = ln.split(":", 1)[1] if ln.startswith(ref + ":") else ln
        if _TEST_PATH.search(f):
            continue
        src = _run(path, "show", f"{ref}:{f}")
        pkg, decls = javaparse.outline(src)
        for d in decls:
            if d.kind == "method" and d.cls:
                out.setdefault(d.name, {"className": javaparse.class_fqn(pkg, "", d.cls), "method_name": d.name,
                                        "file": f, "line": d.start})
    return out


def _openapi_endpoints(path: Path, ref: str, tree: list[str], names: set[str], handlers: dict[str, dict]) -> list[dict]:
    """Operations of the specs this repo *serves*. A spec counts as served when its ``servers`` host is
    one of the repo's own apps, or when one of its operationIds is implemented by a ``*Api`` handler.
    Client specs checked in for codegen (cos-base-types, a dependency's API) are therefore excluded."""
    import yaml

    out = []
    specs = [f for f in tree if re.search(r"\.(ya?ml|json)$", f) and "/src/main/" in "/" + f and not _TEST_PATH.search(f)
             and re.search(r"(openapi|swagger|api)", f, re.I)]
    for f in specs[:40]:
        text = _run(path, "show", f"{ref}:{f}")
        if "paths" not in text or not re.search(r"^(openapi|swagger)\s*:", text, re.M):
            continue
        try:
            doc = yaml.safe_load(text) if not f.endswith(".json") else json.loads(text)
        except Exception:
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
            continue
        hosts = {app_from_host(m.group(1)) for s in doc.get("servers") or [] if isinstance(s, dict)
                 for m in [_URL.match(str(s.get("url", "")))] if m}
        ops = []
        for p, item in doc["paths"].items():
            if not isinstance(item, dict):
                continue
            for verb in _HTTP_VERBS:
                op = item.get(verb)
                if isinstance(op, dict):
                    ops.append((verb.upper(), str(p), op.get("operationId")))
        served = bool(hosts & names) or any(o[2] in handlers for o in ops)
        if not served:
            continue
        base = next((m.group(2) or "" for s in doc.get("servers") or [] if isinstance(s, dict)
                     for m in [_URL.match(str(s.get("url", "")))] if m and app_from_host(m.group(1)) in names), "")
        for verb, p, op_id in ops:
            h = handlers.get(op_id or "") or {}
            out.append({"method": verb, "path": p, "operationId": op_id, "basePath": base, "spec": f,
                        "className": h.get("className"), "method_name": h.get("method_name"),
                        "file": h.get("file") or f, "line": h.get("line")})
    return out


def build(path: Path, org: str, repo: str, ref: str | None = None) -> dict:
    ref = ref or default_ref(path)
    commit = _run(path, "rev-parse", ref + "^{commit}").strip()
    ctx: dict = {"version": CONTEXT_VERSION, "org": org, "repo": repo, "ref": ref, "commit": commit,
                 "path": str(path), "builtAt": int(time.time()), "appNames": [], "artifacts": [],
                 "dependencies": {}, "types": {}, "imports": {}, "endpoints": [], "outbound": []}
    if not commit:
        ctx["error"] = f"cannot resolve {ref}"
        return ctx

    names = {repo.lower()}
    readme = _run(path, "show", f"{ref}:README.md")
    for m in _APP_README.finditer(readme):
        if not m.group(1).startswith("<"):
            names.add(m.group(1).lower())
    ctx["appNames"] = sorted(names)

    tree = [f for f in _run(path, "ls-tree", "-r", "--name-only", ref, timeout=120).splitlines() if f]
    # exactly pom.xml: flatten/versions plugins leave updated-pom.xml / dependency-reduced-pom.xml behind
    poms = [f for f in tree if Path(f).name == "pom.xml" and "/target/" not in f and not _TEST_PATH.search(f)]
    for p in poms:
        gid, aid, deps = _pom_info(_run(path, "show", f"{ref}:{p}"))
        mod = str(Path(p).parent) if "/" in p else ""
        if gid and aid:
            ctx["artifacts"].append({"groupId": gid, "artifactId": aid, "pom": p, "moduleDir": mod})
        for d in deps:
            ctx["dependencies"].setdefault(d, []).append(p)

    # git grep -E is POSIX ERE: no \s, \w or \b
    for f, _, text in _grep(path, ref, ["-E", r"^[[:space:]]*package[[:space:]]+[a-zA-Z_][a-zA-Z0-9_.]*[[:space:]]*;"], ["*.java"]):
        if _TEST_PATH.search(f):
            continue
        if m := _PKG.match(text):
            ctx["types"][f"{m.group(1)}.{Path(f).stem}"] = f

    for f, line, text in _grep(path, ref, ["-E", r"^[[:space:]]*import[[:space:]]+"], ["*.java", "*.kt"]):
        m = _IMPORT.match(text)
        if not m or _JDK_IMPORT.match(m.group(2)):
            continue
        static, name, star = m.groups()
        parts = name.split(".")
        # top-level type = first capitalised segment; `import a.b.*;` keeps the package wildcard
        idx = next((i for i, p in enumerate(parts) if p[:1].isupper()), None)
        key = ".".join(parts[: idx + 1]) if idx is not None else name + (".*" if star else "")
        lst = ctx["imports"].setdefault(key, [])
        if len(lst) < 60:
            lst.append([f, line])

    listed = _run(path, "grep", "-l", "-I", "-E", r"@(Path|RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping)([^A-Za-z0-9_]|$)",
                  ref, "--", "*.java", timeout=300)
    resource_files = sorted({ln.split(":", 1)[1] if ln.startswith(ref + ":") else ln for ln in listed.splitlines() if ln})
    ctx["endpoints"] = _endpoints(path, ref, [f for f in resource_files if not _TEST_PATH.search(f)])
    annotated = {(e["method"], e["path"]) for e in ctx["endpoints"]}
    ctx["endpoints"] += [e for e in _openapi_endpoints(path, ref, tree, names, _handlers(path, ref))
                         if (e["method"], e["path"]) not in annotated]

    seen = set()
    for f, line, text in _grep(path, ref, ["-o", "-i", "-E", r"https?://[a-z0-9][a-z0-9.-]*\.ebay\.com(/[a-zA-Z0-9/{}._~%-]*)?"],
                               [":!*.md", ":!*.html", ":!*.lock", ":!**/package-lock.json"]):
        m = _URL.match(text)
        if not m:
            continue
        host, base = m.group(1).lower(), (m.group(2) or "")
        app = app_from_host(host)
        if not app or app in names:
            continue
        k = (app, host, base, f)
        if k in seen:
            continue
        seen.add(k)
        ctx["outbound"].append({"app": app, "host": host, "basePath": base, "file": f, "line": line,
                                "isTest": bool(_TEST_PATH.search(f))})
        if len(ctx["outbound"]) >= 3000:
            break
    ctx["stats"] = {"files": len(tree), "poms": len(poms), "types": len(ctx["types"]),
                    "importedTypes": len(ctx["imports"]), "endpoints": len(ctx["endpoints"]),
                    "outbound": len(ctx["outbound"])}
    return ctx


class ContextIndex:
    """All repo contexts, keyed by lower-case ``org/repo``."""

    def __init__(self, cfg: Config = CONFIG, rs: RepoSource | None = None):
        self.cfg = cfg
        self.rs = rs or RepoSource(cfg)
        self.dir = cfg.cache_dir / "context"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.repos: dict[str, dict] = {}
        self.errors: dict[str, str] = {}

    # ---- discovery --------------------------------------------------------
    def clone_roots(self) -> dict[str, Path]:
        """Local clones plus the ones shockwave cloned itself (item 8: unknown repos)."""
        idx = dict(self.rs.local_index())
        extra = self.cfg.cache_dir / "clones"
        if extra.exists():
            for org_dir in extra.iterdir():
                for d in org_dir.iterdir() if org_dir.is_dir() else []:
                    if (d / ".git").exists() or (d / "HEAD").exists():
                        idx.setdefault(f"{org_dir.name}/{d.name}".lower(), d)
        return idx

    def ensure_local(self, org: str, repo: str) -> Path:
        """Return a full clone of org/repo, cloning it into the shockwave cache if it isn't local."""
        key = f"{org}/{repo}".lower()
        p = self.clone_roots().get(key)
        if p is None:
            p = self.cfg.cache_dir / "clones" / org / repo
            p.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["git", "clone", "--no-checkout", "--quiet", self.rs.remote_url(org, repo), str(p)],
                               capture_output=True, text=True, timeout=1800, env=_GIT_ENV)
            if r.returncode != 0:
                raise RuntimeError(f"clone of {org}/{repo} failed: {r.stderr.strip()[:300]}")
            self.rs.local_index.cache_clear()
        elif _run(p, "config", "--get", "remote.origin.promisor").strip() == "true":
            # a blob-less clone (Story 2's cache) would fetch every blob one by one under git grep
            _run(p, "config", "--unset", "remote.origin.partialclonefilter")
            subprocess.run(["git", "-C", str(p), "fetch", "--refetch", "--quiet", "origin"],
                           capture_output=True, text=True, timeout=1800, env=_GIT_ENV)
            _run(p, "config", "remote.origin.promisor", "false")
        return p

    # ---- build / load -----------------------------------------------------
    def _cache_path(self, key: str) -> Path:
        return self.dir / (key.replace("/", "__") + ".json")

    def load_or_build(self, key: str, path: Path, fetch: bool = False, force: bool = False) -> dict:
        try:
            real = parse_remote(git(path, "remote", "get-url", "origin")) or tuple(key.split("/", 1))
        except Exception:
            real = tuple(key.split("/", 1))
        ref = default_ref(path)
        fetch_err = refresh(path, ref) if fetch else None
        commit = _run(path, "rev-parse", ref + "^{commit}").strip()
        cp = self._cache_path(key)
        if not force and cp.exists():
            try:
                old = json.loads(cp.read_text())
                if old.get("commit") == commit and old.get("version") == CONTEXT_VERSION:
                    if fetch_err:
                        old["fetchError"] = fetch_err
                    self.repos[key] = old
                    return old
            except Exception:
                pass
        ctx = build(path, real[0], real[1], ref)
        if fetch_err:
            ctx["fetchError"] = fetch_err
        cp.write_text(json.dumps(ctx))
        self.repos[key] = ctx
        return ctx

    def build_all(self, fetch: bool = False, force: bool = False, workers: int = 6, only: list[str] | None = None) -> dict[str, dict]:
        roots = self.clone_roots()
        if only:
            keep = {o.lower() for o in only}
            roots = {k: v for k, v in roots.items() if k in keep}

        def one(item):
            k, p = item
            try:
                self.load_or_build(k, p, fetch=fetch, force=force)
            except Exception as e:  # one broken clone must not hide the others
                self.errors[k] = str(e)[:300]

        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(one, sorted(roots.items())))
        return self.repos

    def load_cached(self) -> dict[str, dict]:
        """Load whatever is on disk without touching git (fast path for the portal / tests)."""
        for cp in self.dir.glob("*.json"):
            try:
                ctx = json.loads(cp.read_text())
                if ctx.get("version") == CONTEXT_VERSION:
                    self.repos[f"{ctx['org']}/{ctx['repo']}".lower()] = ctx
            except Exception:
                continue
        return self.repos

    def get(self, org: str, repo: str) -> dict | None:
        return self.repos.get(f"{org}/{repo}".lower())

    def summary(self) -> list[dict]:
        return [{"repo": f"{c['org']}/{c['repo']}", "ref": c["ref"], "commit": c["commit"][:12],
                 "appNames": c["appNames"], **(c.get("stats") or {}), "fetchError": c.get("fetchError")}
                for _, c in sorted(self.repos.items())]
