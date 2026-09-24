"""Story 5 — Downstream Test Execution Runner: ImpactedRepoManifest entry -> TestResult.

v1 scope: Maven only. Clones shallow at defaultBranch into a temp dir, scans tests for a
*live integration signal* (tests that talk to the changed service's staging endpoint),
runs ``mvn -B test`` with a hard timeout, parses Surefire/Failsafe XML.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..clients.repos import RepoSource
from ..config import CONFIG, Config
from ..contracts import validate_test_result

_ENV_BASE = {"GIT_TERMINAL_PROMPT": "0", "HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"}
_IT_NAME = re.compile(r"(IT|Integration\w*|E2E\w*|Smoke\w*|Functional\w*|Live\w*)Test[s]?\.java$|IT\.java$", re.I)
_MOCK = re.compile(r"\b(Mockito|WireMock|@Mock\b|@MockBean|MockServer|mock\()", re.I)


def _result(entry: dict, **kw) -> dict:
    base = {"org": entry["org"], "repo": entry["repo"], "attempted": False, "status": "SKIPPED", "skippedReason": None,
            "durationSeconds": 0, "failedTests": [], "logExcerpt": "", "hadLiveIntegrationSignal": False,
            "liveIntegrationNote": None, "exitCode": None, "testsRun": None}
    base.update(kw)
    return validate_test_result(base)


def _clone(entry: dict, dest: Path, rs: RepoSource) -> str:
    org, repo, br = entry["org"], entry["repo"], entry.get("defaultBranch")
    args = ["git", "clone", "--depth", "1", "--quiet"] + (["--branch", br] if br else [])
    r = subprocess.run(args + [rs.remote_url(org, repo), str(dest)], capture_output=True, text=True, timeout=600, env=_ENV_BASE)
    if r.returncode == 0:
        return "remote"
    local = rs.local_path(org, repo)
    if local:
        shutil.rmtree(dest, ignore_errors=True)
        ref = f"origin/{br}" if br else "HEAD"
        r2 = subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", "--shared", str(local), str(dest)], capture_output=True, text=True, timeout=600, env=_ENV_BASE)
        if r2.returncode == 0:
            subprocess.run(["git", "-C", str(dest), "checkout", "--quiet", "--detach", ref], capture_output=True, text=True)
            return f"local ({ref}, may be stale — remote clone failed: {r.stderr.strip()[:120]})"
    raise RuntimeError(f"clone failed: {r.stderr.strip()[:300]}")


def live_integration_scan(root: Path, source_apps: list[str]) -> tuple[bool, str]:
    apps = sorted({a.lower() for a in source_apps if a and len(a) >= 4})
    if not apps:
        return False, "source service app names unknown — this run is a generic sanity check only"
    host_rx = re.compile(r"(" + "|".join(map(re.escape, apps)) + r")[\w.-]*\.vip\.(?:qa\.)?ebay\.com", re.I)
    name_rx = re.compile(r"\b(" + "|".join(map(re.escape, apps)) + r")\b", re.I)
    hits, mocked = [], []
    for p in root.rglob("*"):
        if not p.is_file() or "/target/" in str(p) or "/.git/" in str(p):
            continue
        rel = str(p.relative_to(root))
        if "src/test" not in rel and "src/it" not in rel and "integration" not in rel.lower():
            continue
        if p.suffix not in (".java", ".kt", ".properties", ".yaml", ".yml", ".json", ".xml"):
            continue
        try:
            txt = p.read_text(errors="ignore")
        except Exception:
            continue
        if host_rx.search(txt):
            hits.append(rel)
        elif p.suffix in (".java", ".kt") and _IT_NAME.search(p.name) and name_rx.search(txt):
            (mocked if _MOCK.search(txt) else hits).append(rel)
    if hits:
        return True, f"tests reference the changed service live: {', '.join(sorted(hits)[:5])}"
    if mocked:
        return False, f"integration-style tests mention the service but mock it ({', '.join(sorted(mocked)[:3])}) — generic sanity check only"
    return False, "no tests referencing the changed service were found — this run is a generic sanity check only"


def parse_surefire(root: Path) -> tuple[list[str], int]:
    failed, run = [], 0
    for x in list(root.rglob("target/surefire-reports/TEST-*.xml")) + list(root.rglob("target/failsafe-reports/TEST-*.xml")):
        try:
            t = ET.parse(x).getroot()
        except ET.ParseError:
            continue
        suites = [t] if t.tag == "testsuite" else t.findall("testsuite")
        for s in suites:
            run += int(s.get("tests", 0) or 0)
            for tc in s.findall("testcase"):
                if tc.find("failure") is not None or tc.find("error") is not None:
                    failed.append(f"{tc.get('classname')}#{tc.get('name')}")
    return sorted(set(failed)), run


def _java_home(root: Path) -> str | None:
    try:
        pom = (root / "pom.xml").read_text(errors="ignore")
    except Exception:
        return None
    m = re.search(r"<(?:java\.version|maven\.compiler\.release|maven\.compiler\.source)>\s*(?:1\.)?(\d+)", pom)
    if not m:
        return None
    r = subprocess.run(["/usr/libexec/java_home", "-v", m.group(1)], capture_output=True, text=True)
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def run_one(entry: dict, source_apps: list[str], rs: RepoSource | None = None, cfg: Config = CONFIG, keep: bool = False) -> dict:
    rs = rs or RepoSource(cfg)
    if not entry.get("runnable"):
        return _result(entry, skippedReason=entry.get("notRunnableReason") or "not runnable (no test command / unknown build tool)")
    if entry.get("buildTool") != "maven":
        return _result(entry, skippedReason=f"build tool {entry.get('buildTool')} not yet supported by the runner")

    work = Path(tempfile.mkdtemp(prefix=f"shockwave-{entry['repo']}-"))
    dest = work / entry["repo"]
    t0 = time.time()
    try:
        try:
            how = _clone(entry, dest, rs)
        except Exception as e:
            return _result(entry, attempted=True, status="ERROR", skippedReason=None, logExcerpt=str(e)[:4000],
                           durationSeconds=round(time.time() - t0, 1))
        live, note = live_integration_scan(dest, source_apps)
        env = dict(os.environ)
        jh = _java_home(dest)
        if jh:
            env["JAVA_HOME"] = jh
        cmd = (entry.get("testCommand") or "mvn -B test").split()
        log_path = work / "test.log"
        with open(log_path, "w") as lf:
            proc = subprocess.Popen(cmd, cwd=dest, stdout=lf, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            try:
                code = proc.wait(timeout=cfg.test_timeout_seconds)
                timed_out = False
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                code, timed_out = -9, True
        log = log_path.read_text(errors="ignore")
        failed, run = parse_surefire(dest)
        if timed_out:
            status = "ERROR"
        elif code == 0 and not failed:
            status = "PASS"
        elif failed:
            status = "FAIL"
        else:
            status = "ERROR"
        excerpt = log[-4000:]
        if timed_out:
            excerpt = f"[TIMEOUT after {cfg.test_timeout_seconds}s]\n" + excerpt
        return _result(entry, attempted=True, status=status, durationSeconds=round(time.time() - t0, 1),
                       failedTests=failed, logExcerpt=excerpt, hadLiveIntegrationSignal=live, liveIntegrationNote=note,
                       exitCode=code, testsRun=run, clonedFrom=how, javaHome=jh, command=" ".join(cmd))
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def run_many(entries: list[dict], source_apps: list[str], cfg: Config = CONFIG, skip: str | None = None) -> list[dict]:
    if skip:
        return [_result(e, skippedReason=skip) for e in entries]
    rs = RepoSource(cfg)
    with ThreadPoolExecutor(max(1, cfg.test_concurrency)) as ex:
        return list(ex.map(lambda e: run_one(e, source_apps, rs, cfg), entries))

