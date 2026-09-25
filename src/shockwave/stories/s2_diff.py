"""Story 2 — Diff Extraction: ChangeEvent -> ChangedSymbols.

Local-first: uses a local clone (``~/Documents/projects``) or a blob-less cache clone,
fetches the commit if missing, diffs against the first parent (or PR merge-base),
and maps hunks to Java class/method/field declarations + REST endpoints.

§1 extension: calls s2_changes_summary.classify_changes() to add a ``changesSummary``
block to the output.  This is deterministic (no LLM) except for the single ``intent``
sentence which is derived from the commit message.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..clients.repos import RepoSource, git
from ..config import CONFIG, Config
from ..contracts import validate_changed_symbols
from .. import javaparse
from .s2_changes_summary import classify_changes

SOURCE_EXT = {".java", ".ts", ".js", ".py", ".go", ".kt"}
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_OPENAPI_PATH = re.compile(r"^\s{0,4}(/[^\s:]*)\s*:\s*$")
_OPENAPI_METHOD = re.compile(r"^\s+(get|post|put|delete|patch|head|options)\s*:\s*$", re.I)
_TEST_PATH = re.compile(r"(^|/)(src/test|test|tests|it)/", re.I)


def ensure_repo(org: str, repo: str, commit: str, rs: RepoSource, cfg: Config = CONFIG) -> Path:
    """Return a git dir that contains ``commit`` (fetching if needed)."""
    p = rs.local_path(org, repo)
    if p is None:
        p = cfg.cache_dir / "clones" / org / repo
        if not (p / ".git").exists() and not (p / "HEAD").exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout", rs.remote_url(org, repo), str(p)],
                           check=True, capture_output=True, text=True, timeout=600,
                           env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(Path.home())})
    if subprocess.run(["git", "-C", str(p), "cat-file", "-e", f"{commit}^{{commit}}"], capture_output=True).returncode != 0:
        git(p, "fetch", "--quiet", "origin", commit, check=False, timeout=600)
        if subprocess.run(["git", "-C", str(p), "cat-file", "-e", f"{commit}^{{commit}}"], capture_output=True).returncode != 0:
            git(p, "fetch", "--quiet", "origin", timeout=900)
    return p


def _parse_diff(diff: str) -> dict[str, dict]:
    """-> {path: {status, old_path, old_lines:set, new_lines:set}}"""
    files: dict[str, dict] = {}
    cur = None
    for ln in diff.splitlines():
        if ln.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+) b/(.+)$", ln)
            cur = {"status": "modified", "old_path": m.group(1), "new_path": m.group(2), "old_lines": set(), "new_lines": set()} if m else None
            if cur:
                files[cur["new_path"]] = cur
        elif cur is None:
            continue
        elif ln.startswith("new file mode"):
            cur["status"] = "added"
        elif ln.startswith("deleted file mode"):
            cur["status"] = "removed"
        elif ln.startswith("rename from "):
            cur["old_path"] = ln[len("rename from "):]
        elif (m := _HUNK.match(ln)):
            o, oc, n, nc = int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1)
            cur["old_lines"].update(range(o, o + oc) if oc else [o])
            cur["new_lines"].update(range(n, n + nc) if nc else [n])
    return files


def _show(gitdir: Path, ref: str, path: str) -> str | None:
    r = subprocess.run(["git", "-C", str(gitdir), "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def _touched(decls: list, lines: set[int]) -> dict[tuple, javaparse.Decl]:
    """Innermost member (method/field) or class touched by changed lines."""
    hit: dict[tuple, javaparse.Decl] = {}
    members = [d for d in decls if d.kind in ("method", "field")]
    classes = [d for d in decls if d.kind == "class"]
    for ln in lines:
        m = [d for d in members if d.start <= ln <= d.end]
        if m:
            d = min(m, key=lambda x: x.end - x.start)
        else:
            c = [d for d in classes if d.start <= ln <= d.end]
            if not c:
                continue  # imports / package / whitespace outside types
            d = min(c, key=lambda x: x.end - x.start)
        hit[(d.kind, d.cls, d.name, d.params)] = d
    return hit


_VISIBILITY = re.compile(r"\b(public|protected|private)\b")


def _head(d: javaparse.Decl) -> str:
    """Declaration header without annotations: '@X(a = 1) public static Foo bar' -> 'public static Foo bar'."""
    h = javaparse._ANNOT_ARGS.sub("", d.annotations or "")
    h = re.sub(r"@[\w.$]+", " ", h)
    return h.split("(", 1)[0] if d.kind == "method" else h


def class_kind(d: javaparse.Decl | None) -> str | None:
    if d is None:
        return None
    h = javaparse.sanitize(d.annotations or "")
    if re.search(r"@interface\s+" + re.escape(d.name) + r"\b", h):
        return "annotation"
    m = re.search(r"\b(class|interface|enum|record)\s+" + re.escape(d.name) + r"\b", h)
    return m.group(1) if m else None


def _visibility(d: javaparse.Decl, owner_kind: str | None = None) -> str | None:
    """Declared visibility, or None when it can't be read (fields carry no header text)."""
    head = _head(d)
    if not head.strip():
        return None
    m = _VISIBILITY.search(head)
    if m:
        return m.group(1)
    # interface and annotation members are implicitly public
    return "public" if owner_kind in ("interface", "annotation") else "package-private"


def _is_abstract(d: javaparse.Decl, owner_kind: str | None) -> bool:
    head = _head(d)
    if owner_kind == "interface":
        return not re.search(r"\b(default|static|private)\b", head)
    return bool(re.search(r"\babstract\b", head))


def _sym(pkg: str, d: javaparse.Decl, path: str, change: str, owners: dict | None = None) -> dict:
    owners = owners or {}
    if d.kind == "class":
        fq = javaparse.class_fqn(pkg, d.cls, d.name)
        return {"file": path, "kind": "class", "name": fq, "changeType": change,
                "package": pkg, "className": (f"{d.cls}.{d.name}" if d.cls else d.name), "member": None, "params": None,
                "visibility": _visibility(d, class_kind(owners.get(d.cls))), "classKind": class_kind(d), "line": d.start}
    owner_kind = class_kind(owners.get(d.cls))
    fq_cls = javaparse.class_fqn(pkg, "", d.cls) if d.cls else pkg
    sig = f"{d.name}({d.params})" if d.kind == "method" else d.name
    s = {"file": path, "kind": d.kind, "name": f"{fq_cls}#{sig}", "changeType": change,
         "package": pkg, "className": d.cls, "member": d.name, "params": d.params if d.kind == "method" else None,
         "visibility": _visibility(d, owner_kind), "classKind": owner_kind, "line": d.start}
    if d.kind == "method":
        s["abstract"] = _is_abstract(d, owner_kind)
    return s


def _pom_versions(text: str | None) -> dict[str, str]:
    """{'parent g:a' | 'dep g:a' | 'property name': version} from a pom."""
    import xml.etree.ElementTree as ET
    if not text:
        return {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    tag = lambda el: re.sub(r"^\{[^}]+\}", "", el.tag)
    kid = lambda el, n: next((c for c in list(el) if tag(c) == n), None)
    val = lambda el, n: ((kid(el, n).text or "").strip() if el is not None and kid(el, n) is not None else "")
    out = {}
    par = kid(root, "parent")
    if par is not None:
        out[f"parent {val(par, 'groupId')}:{val(par, 'artifactId')}"] = val(par, "version")
    props = kid(root, "properties")
    for p in list(props) if props is not None else []:
        if "version" in tag(p).lower():
            out[f"property {tag(p)}"] = (p.text or "").strip()
    for el in root.iter():
        if tag(el) == "dependency" and val(el, "version"):
            out[f"dependency {val(el, 'groupId')}:{val(el, 'artifactId')}"] = val(el, "version")
    return out


def _build_changes(gitdir: Path, parent: str | None, commit: str, f: dict) -> list[dict]:
    if Path(f["new_path"]).name != "pom.xml":
        return []
    old = _pom_versions(_show(gitdir, parent, f["old_path"])) if parent else {}
    new = _pom_versions(_show(gitdir, commit, f["new_path"]))
    out = []
    for k in sorted(set(old) | set(new)):
        if old.get(k) != new.get(k):
            kind, name = k.split(" ", 1)
            out.append({"file": f["new_path"], "kind": kind, "name": name, "from": old.get(k), "to": new.get(k)})
    return out


_LEGACY_ENV = re.compile(r"^(.*?)src/main/(?:webapp|resources)/META-INF/configuration/([^/]+)/")
_PROFILE = re.compile(r"^(.*?)src/main/resources/application-([^/.]+)\.(?:properties|ya?ml)$")
_ENV_TOUCH = re.compile(r"(^|/)pom\.xml$|application[-.][^/]*\.(properties|ya?ml)$|META-INF/configuration/")


def _env_state(gitdir: Path, rev: str) -> dict[str, dict]:
    """{module: {'legacy': {env: example path}, 'profiles': {env}}} for one tree."""
    mods: dict[str, dict] = {}
    for p in git(gitdir, "ls-tree", "-r", "--name-only", rev, timeout=120).splitlines():
        if m := _LEGACY_ENV.match(p):
            if m.group(2).lower() != "common":
                mods.setdefault(m.group(1), {"legacy": {}, "profiles": set()})["legacy"].setdefault(m.group(2), p)
        elif m := _PROFILE.match(p):
            mods.setdefault(m.group(1), {"legacy": {}, "profiles": set()})["profiles"].add(m.group(2).lower())
    return mods


def _gaps(state: dict[str, dict]) -> dict[tuple[str, str], dict]:
    out = {}
    for mod, s in state.items():
        if not s["profiles"]:
            continue  # legacy-only module: environments are still driven by META-INF/configuration
        for env, example in s["legacy"].items():
            if env.lower() not in s["profiles"]:
                out[(mod, env.lower())] = {"module": mod.rstrip("/") or ".", "environment": env, "legacyConfig": example,
                                           "profilesPresent": sorted(s["profiles"])}
    return out


def environment_gaps(gitdir: Path, parent: str | None, commit: str, files: dict) -> list[dict]:
    """Environments that lost their runtime config in this commit.

    A module that ships ``application-<Env>.properties`` profiles starts each environment from its
    profile. If an environment still has legacy ``META-INF/configuration/<Env>`` config but no
    profile, a deploy there boots without it. Only gaps this commit introduced are reported.
    """
    if not any(_ENV_TOUCH.search(p) for p in files):
        return []
    new = _gaps(_env_state(gitdir, commit))
    old = _gaps(_env_state(gitdir, parent)) if parent else {}
    return [g for k, g in sorted(new.items()) if k not in old]


def _eps(d: javaparse.Decl, cls: javaparse.Decl | None, change: str) -> list[dict]:
    return [{**e, "changeType": change} for e in javaparse.endpoints_for(d, cls)]


_LOG_CALL = re.compile(r"\b(?:log|logger|LOG|LOGGER|Logger|CalEventHelper|CalLogger|System\s*\.\s*(?:out|err))"
                       r"\s*\.\s*\w+\s*\(")


def _strip_log_calls(src: str) -> str:
    """``src`` with every ``log.x(...);``-style statement removed (parens and string literals balanced)."""
    out, i = [], 0
    while (m := _LOG_CALL.search(src, i)):
        out.append(src[i:m.start()])
        j, depth, quote = m.end(), 1, None
        while j < len(src) and depth:
            c = src[j]
            if quote:
                if c == "\\":
                    j += 1
                elif c == quote:
                    quote = None
            elif c in "\"'":
                quote = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            j += 1
        k = j
        while k < len(src) and src[k].isspace():
            k += 1
        if depth or k >= len(src) or src[k] != ";":
            out.append(src[m.start():j])  # a log call used as an expression is not a pure log statement
            i = j
            continue
        i = k + 1
    return "".join(out) + src[i:]


def _logging_only(old_src: str, old: javaparse.Decl, new_src: str, new: javaparse.Decl) -> bool:
    body = lambda s, d: re.sub(r"\s+", "", _strip_log_calls("\n".join(s.splitlines()[d.start - 1:d.end])))
    return body(old_src, old) == body(new_src, new) and old_src.splitlines()[old.start - 1:old.end] != new_src.splitlines()[new.start - 1:new.end]


def _java_symbols(gitdir: Path, parent: str | None, commit: str, f: dict) -> tuple[list[dict], list[dict]]:
    path = f["new_path"]
    new_src = _show(gitdir, commit, path) if f["status"] != "removed" else None
    old_src = _show(gitdir, parent, f["old_path"]) if parent and f["status"] != "added" else None
    syms: dict[str, dict] = {}
    eps: list[dict] = []
    new_pkg, new_decls = javaparse.outline(new_src) if new_src else ("", [])
    old_pkg, old_decls = javaparse.outline(old_src) if old_src else ("", [])
    old_keys = {(d.kind, d.cls, d.name, d.params) for d in old_decls}
    new_keys = {(d.kind, d.cls, d.name, d.params) for d in new_decls}
    cls_by_path = {(d.cls + "." + d.name if d.cls else d.name): d for d in new_decls if d.kind == "class"}
    old_cls = {(x.cls + "." + x.name if x.cls else x.name): x for x in old_decls if x.kind == "class"}

    if f["status"] in ("added", "removed"):
        decls, pkg, change = (new_decls, new_pkg, "added") if f["status"] == "added" else (old_decls, old_pkg, "removed")
        by_path = cls_by_path if f["status"] == "added" else old_cls
        for d in decls:
            if d.kind == "field":
                continue
            s = _sym(pkg, d, path, change, by_path); syms[s["name"]] = s
        for d in decls:
            if d.kind == "method":
                eps += _eps(d, by_path.get(d.cls), change)
        return list(syms.values()), eps

    old_by_key = {(d.kind, d.cls, d.name, d.params): d for d in old_decls}
    for key, d in _touched(new_decls, f["new_lines"]).items():
        change = "modified" if key in old_keys else "added"
        s = _sym(new_pkg, d, path, change, cls_by_path); syms[s["name"]] = s
        if change == "modified" and d.kind == "method" and _logging_only(old_src, old_by_key[key], new_src, d):
            s["loggingOnly"] = True
        if d.kind == "method":
            eps += _eps(d, cls_by_path.get(d.cls), change)
    new_eps = {(e["method"], e["path"]) for d in new_decls if d.kind == "method" for e in javaparse.endpoints_for(d, cls_by_path.get(d.cls))}
    for key, d in _touched(old_decls, f["old_lines"]).items():
        if key not in new_keys:
            s = _sym(old_pkg, d, path, "removed", old_cls); syms.setdefault(s["name"], s)
            if d.kind == "method":
                # a handler whose signature changed still serves the same route: that is a modification
                eps += [{**e, "changeType": "modified" if (e["method"], e["path"]) in new_eps else "removed"}
                        for e in javaparse.endpoints_for(d, old_cls.get(d.cls))]
    return list(syms.values()), eps


def _spec_ops(src: str | None) -> dict[tuple[str, str], str | None] | None:
    if not src:
        return None
    try:
        import yaml
        doc = yaml.safe_load(src)
    except Exception:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
        return None
    return {(verb.upper(), str(p)): (op.get("operationId") if isinstance(op, dict) else None)
            for p, item in doc["paths"].items() if isinstance(item, dict)
            for verb, op in item.items() if verb.lower() in ("get", "post", "put", "delete", "patch", "head", "options")}


def _openapi_endpoints(gitdir: Path, commit: str, f: dict, parent: str | None = None) -> list[dict]:
    src = _show(gitdir, commit, f["new_path"]) or ""
    old = _spec_ops(_show(gitdir, parent, f["old_path"])) if parent and f["status"] != "added" else {}
    new = _spec_ops(src) if f["status"] != "removed" else {}
    out = []
    # operations that disappeared or appeared are contract changes a line-level scan cannot see
    if old is not None and new is not None:
        out += [{"method": m, "path": p, "operationId": op, "changeType": "removed"} for (m, p), op in old.items() if (m, p) not in new]
        out += [{"method": m, "path": p, "operationId": op, "changeType": "added"} for (m, p), op in new.items() if (m, p) not in old]
    lines = src.splitlines()
    for ln in sorted(f["new_lines"]):
        path, method = None, None
        for k in range(min(ln, len(lines)) - 1, -1, -1):
            t = lines[k]
            if method is None and (m := _OPENAPI_METHOD.match(t)):
                method = m.group(1).upper()
            if (m := _OPENAPI_PATH.match(t)):
                path = m.group(1); break
        if path:
            op = (new or {}).get((method or "", path))
            out.append({"method": method or "ANY", "path": path, "operationId": op, "changeType": "modified"})
    return out


def extract(event: dict, rs: RepoSource | None = None, cfg: Config = CONFIG, include_tests: bool = False) -> dict:
    rs = rs or RepoSource(cfg)
    org, repo, commit = event["org"], event["repo"], event["commit"]
    gitdir = ensure_repo(org, repo, commit, rs, cfg)
    commit = git(gitdir, "rev-parse", commit).strip()
    parents = git(gitdir, "rev-list", "--parents", "-n", "1", commit).split()[1:]
    parent = parents[0] if parents else None
    base = parent or "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # empty tree for root commits
    diff = git(gitdir, "diff", "-U0", "--no-color", "-M", base, commit, timeout=300)
    files = _parse_diff(diff)

    changed_files = sorted(files)
    symbols: list[dict] = []
    endpoints: list[dict] = []
    build_changes: list[dict] = []
    notes: list[str] = []
    same_tree = False
    if not files:
        same_tree = parent and git(gitdir, "rev-parse", f"{commit}^{{tree}}").strip() == git(gitdir, "rev-parse", f"{parent}^{{tree}}").strip()
        notes.append(f"empty commit: tree is identical to parent {parent[:8]}, so nothing was altered" if same_tree
                     else "no file changes against the first parent")
    for path, f in files.items():
        ext = Path(path).suffix.lower()
        is_test = bool(_TEST_PATH.search(path))
        build_changes += _build_changes(gitdir, parent, commit, f)
        if ext == ".java" and (include_tests or not is_test):
            s, e = _java_symbols(gitdir, parent, commit, f)
            symbols += s
            endpoints += e
        elif ext in SOURCE_EXT and not is_test:
            notes.append(f"{path}: {ext} symbol extraction not implemented in v1 (file-level only)")
        elif ext in (".yaml", ".yml", ".json") and re.search(r"openapi|swagger", path, re.I):
            endpoints += _openapi_endpoints(gitdir, commit, f, parent)

    # dedupe endpoints
    seen, eps = set(), []
    for e in endpoints:
        k = (e["method"], e["path"])
        if k not in seen:
            seen.add(k); eps.append(e)
    if len(symbols) > cfg.max_symbols:
        notes.append(f"{len(symbols)} symbols changed; truncated to {cfg.max_symbols} (methods first)")
        symbols = sorted(symbols, key=lambda s: {"method": 0, "field": 1, "class": 2}[s["kind"]])[:cfg.max_symbols]

    # §1: build changesSummary before validate so the full symbol list is available
    # Commit message: read from git log (first line of the subject)
    try:
        commit_msg = git(gitdir, "log", "-1", "--pretty=%B", commit).strip()
    except Exception:
        commit_msg = None

    out = {
        "org": org, "repo": repo, "commit": commit, "parent": parent, "branch": event.get("branch"),
        "changedFiles": changed_files,
        "changedSymbols": symbols,
        "changedApiEndpoints": eps,
        "buildChanges": build_changes,
        "environmentGaps": environment_gaps(gitdir, parent, commit, files),
        "emptyCommit": parent if same_tree else None,
        "notes": notes,
    }
    # classify_changes never raises — if it fails we attach the error as a note
    try:
        out["changesSummary"] = classify_changes(out, commit_msg)
    except Exception as exc:
        notes.append(f"changesSummary classification failed: {exc}")
        out["notes"] = notes

    return validate_changed_symbols(out)

