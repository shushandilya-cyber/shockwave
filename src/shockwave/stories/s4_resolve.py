"""Story 4 — Impacted Repo & Team Resolution: BlastRadius -> ImpactedRepoManifest."""
from __future__ import annotations

import json
import re

from ..clients.repos import RepoSource
from ..config import CONFIG, Config
from ..contracts import validate_manifest

CODEOWNERS_PATHS = ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"]


def parse_codeowners(text: str) -> list[tuple[str, list[str]]]:
    rules = []
    for ln in text.splitlines():
        ln = ln.split("#", 1)[0].strip()
        if not ln:
            continue
        parts = ln.split()
        owners = [p for p in parts[1:] if p.startswith("@") or "@" in p]
        if owners:
            rules.append((parts[0], owners))
    return rules


def _glob_rx(p: str) -> str:
    out, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            out += "(?:.*/)?"; i += 3
        elif p.startswith("**", i):
            out += ".*"; i += 2
        elif p[i] == "*":
            out += "[^/]*"; i += 1
        elif p[i] == "?":
            out += "[^/]"; i += 1
        else:
            out += re.escape(p[i]); i += 1
    return out


def _pattern_matches(pattern: str, path: str) -> bool:
    """GitHub CODEOWNERS semantics (subset): '/' anchors to root, trailing '/' = directory,
    patterns without an inner '/' match at any depth, a match on a directory covers its contents."""
    path = path.lstrip("/")
    anchored = pattern.startswith("/")
    p = pattern.strip("/")
    if p in ("*", "**"):
        return True
    prefix = "" if (anchored or "/" in p) else "(?:.*/)?"
    return re.match("^" + prefix + _glob_rx(p) + "(?:/.*)?$", path) is not None


def owners_for(rules: list[tuple[str, list[str]]], paths: list[str]) -> tuple[list[str], str | None]:
    """Prefer the rule matching an evidence path; else the '*' default. Returns (owners, matched_pattern)."""
    best = None
    for path in paths:
        for pat, owners in rules:  # last match wins
            if pat not in ("*", "**") and _pattern_matches(pat, path):
                best = (owners, pat)
    if best:
        return best
    default = None
    for pat, owners in rules:
        if pat in ("*", "**", "/*", "/**"):
            default = (owners, pat)
    return default or ([], None)


def team_name(owners: list[str]) -> str | None:
    """'@CoreShipping/shipping-orbital' -> 'CoreShipping/shipping-orbital'; prefer team handles over users."""
    teams = [o.lstrip("@") for o in owners if "/" in o]
    if teams:
        return teams[0]
    users = [o.lstrip("@") for o in owners]
    return ",".join(users) if users else None


def detect_build(rs: RepoSource, org: str, repo: str) -> tuple[str, str | None, str | None]:
    if rs.exists(org, repo, "pom.xml"):
        return "maven", "mvn -B test", None
    if rs.exists(org, repo, "build.gradle") or rs.exists(org, repo, "build.gradle.kts"):
        return "gradle", "./gradlew test", None
    if rs.exists(org, repo, "package.json"):
        try:
            pkg = json.loads(rs.read_file(org, repo, "package.json") or "{}")
            t = (pkg.get("scripts") or {}).get("test")
        except Exception:
            t = None
        if t and "no test specified" not in t:
            return "npm", "npm test", None
        return "npm", None, "package.json has no usable scripts.test"
    return "unknown", None, "no pom.xml / build.gradle / package.json at repo root"


def _source_entry(blast: dict) -> dict | None:
    """The changed repo itself, as a manifest entry.

    Story 3 only looks downstream, so without this the repo that actually changed is the
    one repo whose tests never run. Two of the five backtested incidents (INC-004,
    INC-005) were exactly that: the culprit broke itself and was reverted, and a
    downstream-only blast radius could not have said anything.
    """
    src = blast.get("sourceRepo")
    if not src or "/" not in src:
        return None
    org, repo = src.split("/", 1)
    return {
        "org": org, "repo": repo, "confidence": "high", "isSourceRepo": True, "commit": blast.get("commit"),
        "evidence": [{"source": "changeEvent", "detail": "the change was made in this repo",
                      "matchedChangedCode": True}],
    }


def resolve(blast: dict, rs: RepoSource | None = None, cfg: Config = CONFIG) -> dict:
    rs = rs or RepoSource(cfg)
    entries = []
    targets = list(blast.get("impactedRepos", []))
    src_entry = _source_entry(blast)
    if src_entry and not any(
        f"{r['org']}/{r['repo']}".lower() == f"{src_entry['org']}/{src_entry['repo']}".lower()
        for r in targets
    ):
        targets.insert(0, src_entry)
    for r in targets:
        org, repo = r["org"], r["repo"]
        avail = rs.available(org, repo)
        ev_paths = [f for e in r.get("evidence", []) for f in (e.get("files") or [])]
        e = {"org": org, "repo": repo, "confidence": r["confidence"], "team": None, "teamSource": "unassigned",
             "owners": [], "buildTool": "unknown", "testCommand": None, "runnable": False, "defaultBranch": None,
             "notRunnableReason": None, "repoAccess": avail, "isSourceRepo": bool(r.get("isSourceRepo")),
             # the changed repo is tested AT the change; consumers are tested at their default branch
             "commit": r.get("commit") if r.get("isSourceRepo") else None}
        if avail == "none":
            e["notRunnableReason"] = "repo not cloned locally and GitHub API unavailable — cannot inspect CODEOWNERS/build"
            entries.append(e)
            continue
        for p in CODEOWNERS_PATHS:
            txt = rs.read_file(org, repo, p)
            if txt:
                owners, pat = owners_for(parse_codeowners(txt), ev_paths)
                if owners:
                    e.update(team=team_name(owners), owners=owners, teamSource="codeowners", codeownersFile=p, codeownersRule=pat)
                break
        bt, cmd, why = detect_build(rs, org, repo)
        e.update(buildTool=bt, testCommand=cmd, defaultBranch=rs.default_branch(org, repo))
        e["runnable"] = bool(cmd) and bt != "unknown"
        if not e["runnable"]:
            e["notRunnableReason"] = why or f"no test command for build tool {bt}"
        entries.append(e)
    return validate_manifest({"sourceRepo": blast.get("sourceRepo"), "commit": blast.get("commit"), "entries": entries})


