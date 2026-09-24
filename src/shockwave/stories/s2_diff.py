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


def _sym(pkg: str, d: javaparse.Decl, path: str, change: str) -> dict:
    if d.kind == "class":
        fq = javaparse.class_fqn(pkg, d.cls, d.name)
        return {"file": path, "kind": "class", "name": fq, "changeType": change,
                "package": pkg, "className": (f"{d.cls}.{d.name}" if d.cls else d.name), "member": None, "params": None}
    fq_cls = javaparse.class_fqn(pkg, "", d.cls) if d.cls else pkg
    sig = f"{d.name}({d.params})" if d.kind == "method" else d.name
    return {"file": path, "kind": d.kind, "name": f"{fq_cls}#{sig}", "changeType": change,
            "package": pkg, "className": d.cls, "member": d.name, "params": d.params if d.kind == "method" else None}


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

    if f["status"] in ("added", "removed"):
        decls, pkg, change = (new_decls, new_pkg, "added") if f["status"] == "added" else (old_decls, old_pkg, "removed")
        for d in decls:
            if d.kind == "field":
                continue
            s = _sym(pkg, d, path, change); syms[s["name"]] = s
        if f["status"] == "added":
            for d in new_decls:
                if d.kind == "method":
                    eps += javaparse.endpoints_for(d, cls_by_path.get(d.cls))
        return list(syms.values()), eps

    for key, d in _touched(new_decls, f["new_lines"]).items():
        change = "modified" if key in old_keys else "added"
        s = _sym(new_pkg, d, path, change); syms[s["name"]] = s
        if d.kind == "method":
            eps += javaparse.endpoints_for(d, cls_by_path.get(d.cls))
    for key, d in _touched(old_decls, f["old_lines"]).items():
        if key not in new_keys:
            s = _sym(old_pkg, d, path, "removed"); syms.setdefault(s["name"], s)
            if d.kind == "method":
                old_cls = {(x.cls + "." + x.name if x.cls else x.name): x for x in old_decls if x.kind == "class"}
                eps += javaparse.endpoints_for(d, old_cls.get(d.cls))
    return list(syms.values()), eps


def _openapi_endpoints(gitdir: Path, commit: str, f: dict) -> list[dict]:
    src = _show(gitdir, commit, f["new_path"]) or ""
    lines = src.splitlines()
    out = []
    for ln in sorted(f["new_lines"]):
        path, method = None, None
        for k in range(min(ln, len(lines)) - 1, -1, -1):
            t = lines[k]
            if method is None and (m := _OPENAPI_METHOD.match(t)):
                method = m.group(1).upper()
            if (m := _OPENAPI_PATH.match(t)):
                path = m.group(1); break
        if path:
            out.append({"method": method or "ANY", "path": path, "operationId": None})
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
    notes: list[str] = []
    for path, f in files.items():
        ext = Path(path).suffix.lower()
        is_test = bool(_TEST_PATH.search(path))
        if ext == ".java" and (include_tests or not is_test):
            s, e = _java_symbols(gitdir, parent, commit, f)
            symbols += s
            endpoints += e
        elif ext in SOURCE_EXT and not is_test:
            notes.append(f"{path}: {ext} symbol extraction not implemented in v1 (file-level only)")
        elif ext in (".yaml", ".yml", ".json") and re.search(r"openapi|swagger", path, re.I):
            endpoints += _openapi_endpoints(gitdir, commit, f)

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
        "notes": notes,
    }
    # classify_changes never raises — if it fails we attach the error as a note
    try:
        out["changesSummary"] = classify_changes(out, commit_msg)
    except Exception as exc:
        notes.append(f"changesSummary classification failed: {exc}")
        out["notes"] = notes

    return validate_changed_symbols(out)

