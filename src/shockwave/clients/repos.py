"""Repo access: local clones (default) with optional GitHub Enterprise REST fallback.

Why local-first: the GHE PAT in the MCP config can expire (it did during build),
while ``git`` over HTTPS works via the macOS keychain. Obsidian nodes will use the
GitHub MCP instead — same contract, different transport.
"""
from __future__ import annotations

import base64
import json
import ssl
import subprocess
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

from ..config import CONFIG, Config

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def git(path: Path | str, *args: str, check: bool = True, timeout: int = 120) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout,
                       env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(Path.home())})
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {path}: {r.stderr.strip()[:300]}")
    return r.stdout


def parse_remote(url: str) -> tuple[str, str] | None:
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@"):
        url = url.split(":", 1)[1]
    parts = url.split("/")
    return (parts[-2], parts[-1]) if len(parts) >= 2 else None


class RepoSource:
    def __init__(self, cfg: Config = CONFIG):
        self.cfg = cfg
        self._gh_ok: bool | None = None

    # ---- local clone index ----------------------------------------------
    @lru_cache(maxsize=1)
    def local_index(self) -> dict[str, Path]:
        idx: dict[str, Path] = {}
        root = self.cfg.local_repos_root
        if not root.exists():
            return idx
        for d in sorted(root.iterdir()):
            if not (d / ".git").exists():
                continue
            try:
                pr = parse_remote(git(d, "remote", "get-url", "origin"))
            except Exception:
                continue
            if pr:
                idx[f"{pr[0]}/{pr[1]}".lower()] = d
        return idx

    def local_path(self, org: str, repo: str) -> Path | None:
        return self.local_index().get(f"{org}/{repo}".lower())

    def remote_url(self, org: str, repo: str) -> str:
        return f"{self.cfg.github_web}/{org}/{repo}.git"

    # ---- GitHub REST (optional) -----------------------------------------
    def _gh(self, path: str):
        if not self.cfg.github_token or self._gh_ok is False:
            return None
        rq = urllib.request.Request(self.cfg.github_api.rstrip("/") + path,
                                    headers={"Authorization": f"token {self.cfg.github_token}", "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(rq, context=_CTX, timeout=30) as r:
                self._gh_ok = True
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self._gh_ok = False  # stop trying for this process
            return None
        except Exception:
            return None

    # ---- high-level ops (local first, then GH API) -----------------------
    def default_branch(self, org: str, repo: str) -> str | None:
        p = self.local_path(org, repo)
        if p:
            ref = git(p, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False).strip()
            if ref:
                return ref.rsplit("/", 1)[-1]
        d = self._gh(f"/repos/{org}/{repo}")
        if d:
            return d.get("default_branch")
        if p:
            for b in ("main", "master"):
                if git(p, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{b}", check=False).strip():
                    return b
            cur = git(p, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
            if cur and cur != "HEAD" and not git(p, "for-each-ref", "refs/remotes/origin", check=False).strip():
                return cur
        # last resort: ask the remote
        try:
            out = subprocess.run(["git", "ls-remote", "--symref", self.remote_url(org, repo), "HEAD"], capture_output=True,
                                 text=True, timeout=30, env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(Path.home())}).stdout
            for ln in out.splitlines():
                if ln.startswith("ref:"):
                    return ln.split()[1].rsplit("/", 1)[-1]
        except Exception:
            pass
        return None

    def read_file(self, org: str, repo: str, path: str, ref: str | None = None) -> str | None:
        p = self.local_path(org, repo)
        if p:
            if ref:
                out = subprocess.run(["git", "-C", str(p), "show", f"{ref}:{path}"], capture_output=True, text=True)
                if out.returncode == 0:
                    return out.stdout
            fp = p / path
            if fp.is_file():
                return fp.read_text(errors="replace")
            return None
        d = self._gh(f"/repos/{org}/{repo}/contents/{path}" + (f"?ref={ref}" if ref else ""))
        if d and d.get("encoding") == "base64":
            return base64.b64decode(d["content"]).decode(errors="replace")
        return None

    def exists(self, org: str, repo: str, path: str, ref: str | None = None) -> bool:
        p = self.local_path(org, repo)
        if p and not ref:
            return (p / path).exists()
        return self.read_file(org, repo, path, ref) is not None

    def available(self, org: str, repo: str) -> str:
        """'local' | 'github' | 'none'"""
        if self.local_path(org, repo):
            return "local"
        if self._gh(f"/repos/{org}/{repo}"):
            return "github"
        return "none"

