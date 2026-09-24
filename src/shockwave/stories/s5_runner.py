"""Story 5 — Downstream Test Execution Runner: ImpactedRepoManifest entry -> TestResult.

§3 requirements:
  - No silent SKIPPED. SKIPPED is only returned when the caller explicitly passes a skip
    reason (--skip-tests). Every other non-execution is NOT_RUN with a specific,
    actionable reason string (e.g. "gradle toolchain missing", "clone 403").
  - Tri-state live signal: LIVE | MOCKED | NONE (replaces the legacy bool).
  - Runners: Maven, Gradle, npm (detected by build tool).
  - Integration-test repo discovery: scans local repos for references to the source
    service's staging hosts/endpoints; discovered repos are reported in
    ``integrationTestRepos`` and run as the live signal.
  - Backward compat: ``hadLiveIntegrationSignal`` bool is kept, derived from liveSignal.
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

_ENV_BASE = {
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": str(Path.home()),
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
}

_IT_NAME = re.compile(
    r"(IT|Integration\w*|E2E\w*|Smoke\w*|Functional\w*|Live\w*)Test[s]?\.java$|IT\.java$", re.I
)
# @Mock and @MockBean start with '@' (non-word char), so \b before the group would
# fail to match them; detect them separately without the leading \b.
_MOCK = re.compile(r"(?:\b(Mockito|WireMock|MockServer)|@(?:Mock|MockBean)\b|mock\()", re.I)
_HOST_RX_TEMPLATE = r"({apps})[\w.-]*\.vip\.(?:qa\.)?ebay\.com"
_NAME_RX_TEMPLATE = r"\b({apps})\b"


# ── Public entry point ────────────────────────────────────────────────────────────

def _result(entry: dict, **kw) -> dict:
    base = {
        "org": entry["org"], "repo": entry["repo"],
        "attempted": False,
        "status": "NOT_RUN",
        "skippedReason": None,
        "notRunReason": None,
        "durationSeconds": 0,
        "failedTests": [],
        "logExcerpt": "",
        # §3 tri-state
        "liveSignal": "NONE",
        # backward-compat bool derived from liveSignal
        "hadLiveIntegrationSignal": False,
        "liveIntegrationNote": None,
        "integrationTestRepos": [],
        "exitCode": None,
        "testsRun": None,
        # set for repos found by host-scan rather than by Story 3 (see run_many)
        "discoveredAs": entry.get("discoveredAs"),
        "discoveryEvidence": entry.get("discoveryEvidence") or [],
        # test classes that mention the changed service; used by the targeted run
        "targetedTestClasses": [],
    }
    base.update(kw)
    # keep backward-compat bool in sync
    base["hadLiveIntegrationSignal"] = base.get("liveSignal") == "LIVE"
    return validate_test_result(base)


# ── §3 Tri-state live integration scan ───────────────────────────────────────────

def live_integration_scan(root: Path, source_apps: list[str]) -> tuple[str, str]:
    """Scan a cloned repo for tests that reference the changed service.

    Returns (liveSignal, note) where liveSignal ∈ {"LIVE", "MOCKED", "NONE"}.

    LIVE   = a file in a test path contains the service's staging hostname.
    MOCKED = integration-style test mentions the service name but mocks it.
    NONE   = no relevant tests found.
    """
    apps = sorted({a.lower() for a in source_apps if a and len(a) >= 4})
    if not apps:
        return "NONE", "source service app names unknown — this run is a generic sanity check only"

    esc = "|".join(map(re.escape, apps))
    host_rx = re.compile(_HOST_RX_TEMPLATE.format(apps=esc), re.I)
    name_rx = re.compile(_NAME_RX_TEMPLATE.format(apps=esc), re.I)

    live_hits, mocked_hits = [], []
    for p in root.rglob("*"):
        if not p.is_file() or "/target/" in str(p) or "/.git/" in str(p):
            continue
        rel = str(p.relative_to(root))
        in_test_dir = "src/test" in rel or "src/it" in rel or "integration" in rel.lower()
        if not in_test_dir:
            continue
        if p.suffix not in (".java", ".kt", ".properties", ".yaml", ".yml", ".json", ".xml"):
            continue
        try:
            txt = p.read_text(errors="ignore")
        except Exception:
            continue
        if host_rx.search(txt):
            live_hits.append(rel)
        elif p.suffix in (".java", ".kt") and _IT_NAME.search(p.name) and name_rx.search(txt):
            (mocked_hits if _MOCK.search(txt) else live_hits).append(rel)

    if live_hits:
        return "LIVE", f"tests reference the changed service live: {', '.join(sorted(live_hits)[:5])}"
    if mocked_hits:
        return "MOCKED", (
            f"integration-style tests mention the service but mock it "
            f"({', '.join(sorted(mocked_hits)[:3])}) — contract shape only"
        )
    return "NONE", "no tests referencing the changed service were found — generic sanity check only"


def targeted_test_classes(root: Path, source_apps: list[str], max_classes: int = 25) -> list[str]:
    """Test classes that mention the changed service, for a targeted ``-Dtest=`` run.

    Running only these makes the result *relevant* (it exercises the change) and fast,
    instead of burning minutes on a suite where nothing touches the source service.
    Returns simple class names, which is what Surefire's ``-Dtest`` expects.
    """
    apps = [a.lower() for a in (source_apps or []) if a and len(a) >= 4]
    if not apps:
        return []
    name_rx = re.compile(_NAME_RX_TEMPLATE.format(apps="|".join(map(re.escape, apps))), re.I)
    hits: set[str] = set()
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in (".java", ".kt"):
            continue
        rel = str(p.relative_to(root))
        if "src/test" not in rel and "src/it" not in rel:
            continue
        if not re.search(r"(Test|Tests|IT)\.(java|kt)$", p.name):
            continue
        try:
            if name_rx.search(p.read_text(errors="ignore")):
                hits.add(p.stem)
        except Exception:
            continue
    return sorted(hits)[:max_classes]


# ── §3 Integration-test repo discovery ───────────────────────────────────────────

def discover_integration_test_repos(
    rs: RepoSource,
    source_apps: list[str],
    exclude: set[str] | None = None,
) -> list[dict]:
    """Scan local clones for repos that reference the source service's staging hosts.

    These are dedicated integration-test repos (e.g. Ship-AST/PudoIntegrationTests) that
    exercise the source service over the network but are not linked to it by any call
    edge, so Story 3 never finds them.

    ``exclude`` holds keys already covered by the manifest plus the source repo itself.
    Returns [{key, org, repo, evidenceFiles}] so the report can cite why each was picked.
    """
    apps = [a.lower() for a in (source_apps or []) if a and len(a) >= 4]
    if not apps:
        return []
    esc = "|".join(map(re.escape, apps))
    exclude = {e.lower() for e in (exclude or set())}
    found: list[dict] = []
    for key, path in rs.local_index().items():
        if key in exclude:
            continue
        try:
            # POSIX ERE — \w is not portable across git grep builds; spell the class out.
            r = subprocess.run(
                ["git", "-C", str(path), "grep", "-I", "-l", "--extended-regexp",
                 esc + r"[a-zA-Z0-9_.-]*\.vip\.(qa\.)?ebay\.com"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:
            continue
        if r.returncode != 0 or not r.stdout.strip():
            continue
        files = [f for f in r.stdout.strip().splitlines() if f][:10]
        org, repo = key.split("/", 1)
        found.append({"key": key, "org": org, "repo": repo, "evidenceFiles": files})
    return sorted(found, key=lambda d: d["key"])


def integration_test_entries(
    discovered: list[dict], rs: RepoSource, cfg: Config = CONFIG
) -> list[dict]:
    """Turn discovered integration-test repos into runnable manifest entries.

    They carry ``discoveredAs`` so the report can distinguish them from repos that
    Story 3 put in the blast radius.
    """
    from .s4_resolve import detect_build

    out = []
    for d in discovered:
        bt, cmd, why = detect_build(rs, d["org"], d["repo"])
        out.append({
            "org": d["org"], "repo": d["repo"],
            "confidence": "medium",
            "team": None,
            "buildTool": bt,
            "testCommand": cmd,
            "runnable": bool(cmd) and bt != "unknown",
            "notRunnableReason": why or (None if cmd else f"no test command for build tool {bt}"),
            "defaultBranch": rs.default_branch(d["org"], d["repo"]),
            "discoveredAs": "integration-test-repo",
            "discoveryEvidence": d["evidenceFiles"],
        })
    return out


# ── Clone helper ──────────────────────────────────────────────────────────────────

def _clone(entry: dict, dest: Path, rs: RepoSource) -> str:
    org, repo, br = entry["org"], entry["repo"], entry.get("defaultBranch")
    args = ["git", "clone", "--depth", "1", "--quiet"] + (["--branch", br] if br else [])
    r = subprocess.run(
        args + [rs.remote_url(org, repo), str(dest)],
        capture_output=True, text=True, timeout=600, env=_ENV_BASE,
    )
    if r.returncode == 0:
        return "remote"
    # Fall back to local clone
    local = rs.local_path(org, repo)
    if local:
        shutil.rmtree(dest, ignore_errors=True)
        ref = f"origin/{br}" if br else "HEAD"
        r2 = subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", "--shared", str(local), str(dest)],
            capture_output=True, text=True, timeout=600, env=_ENV_BASE,
        )
        if r2.returncode == 0:
            subprocess.run(
                ["git", "-C", str(dest), "checkout", "--quiet", "--detach", ref],
                capture_output=True, text=True,
            )
            return f"local ({ref}, may be stale — remote clone failed: {r.stderr.strip()[:120]})"
    raise RuntimeError(f"{_classify_clone_error(r.stderr)}: {r.stderr.strip()[:300]}")


def _classify_clone_error(stderr: str) -> str:
    """Map raw git stderr to a specific, actionable reason (§3: never a vague 'skipped').

    Ordered most-specific first. GHE returns "Repository not found" both for a repo
    that does not exist and for one the runner identity cannot see, so that case names
    both possibilities rather than asserting an auth failure.
    """
    s = stderr.strip()
    low = s.lower()
    if "could not read username" in low or "terminal prompt" in low:
        return "clone needs credentials: no git identity available to the runner (set GITHUB_TOKEN or a credential helper)"
    if "authentication failed" in low or "403" in low or "permission denied" in low:
        return "clone 403/auth error: runner identity is not authorised for this repo"
    if "remote branch" in low:
        return "clone failed: requested branch does not exist on the remote"
    if "repository not found" in low or "not found" in low:
        return "repo not found or not visible to the runner identity"
    if "timed out" in low or "timeout" in low:
        return "clone timed out"
    if "could not resolve host" in low or "network is unreachable" in low:
        return "clone failed: host unreachable (VPN down?)"
    return "clone failed"


# ── Maven helpers ─────────────────────────────────────────────────────────────────

def parse_surefire(root: Path) -> tuple[list[str], int]:
    failed, run = [], 0
    for x in (
        list(root.rglob("target/surefire-reports/TEST-*.xml"))
        + list(root.rglob("target/failsafe-reports/TEST-*.xml"))
    ):
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
    m = re.search(
        r"<(?:java\.version|maven\.compiler\.release|maven\.compiler\.source)>\s*(?:1\.)?(\d+)", pom
    )
    if not m:
        return None
    r = subprocess.run(["/usr/libexec/java_home", "-v", m.group(1)], capture_output=True, text=True)
    return (r.stdout.strip() or None) if r.returncode == 0 else None


# ── Gradle helpers ────────────────────────────────────────────────────────────────

def parse_gradle_results(root: Path) -> tuple[list[str], int]:
    """Parse Gradle XML test results from build/test-results/."""
    failed, run = [], 0
    for x in root.rglob("build/test-results/**/TEST-*.xml"):
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


def _check_gradle_wrapper(root: Path) -> str | None:
    """Returns None if gradlew is usable, else a NOT_RUN reason string."""
    gw = root / "gradlew"
    if not gw.exists():
        return "gradlew script not found in repo root (non-standard Gradle project)"
    if not os.access(gw, os.X_OK):
        try:
            gw.chmod(0o755)
        except Exception:
            return "gradlew exists but is not executable and chmod failed"
    # Quick version probe to catch missing JDK / toolchain
    r = subprocess.run(
        ["./gradlew", "--version", "--no-daemon"],
        cwd=root, capture_output=True, text=True, timeout=60, env=_ENV_BASE,
    )
    if r.returncode != 0:
        err = (r.stdout + r.stderr).strip()[:300]
        if "toolchain" in err.lower() or "jdk" in err.lower() or "java" in err.lower():
            return f"gradle toolchain/JDK missing on runner: {err[:200]}"
        return f"gradlew --version failed (env issue): {err[:200]}"
    return None


# ── npm helpers ───────────────────────────────────────────────────────────────────

def parse_npm_results(log: str) -> tuple[list[str], int]:
    """Heuristic parse of npm test output for failures."""
    failed = []
    run = 0
    # jest-style: "Tests: N failed, M passed"
    m = re.search(r"Tests:\s+(?:(\d+)\s+failed)?[,\s]*(?:(\d+)\s+passed)?", log, re.I)
    if m:
        run = int(m.group(1) or 0) + int(m.group(2) or 0)
    # mocha-style failure lines
    for line in log.splitlines():
        if re.match(r"\s+\d+\) ", line):
            failed.append(line.strip()[:120])
    return failed[:50], run


# ── Build-failure classification ──────────────────────────────────────────────────

# An environment problem means we learned nothing (NOT_RUN). A compile failure means the
# build genuinely broke, which is a real signal about the consumer (ERROR → NeedsReview).
_ENV_FAILURE_PATTERNS = [
    (r"Could not resolve dependencies|Non-resolvable|Could not find artifact|Failed to read artifact descriptor",
     "dependency resolution failed on the runner (artifact repo unreachable or credentials missing)"),
    (r"Could not resolve all files for configuration|Could not download",
     "gradle dependency download failed on the runner"),
    (r"No compiler is provided|Unsupported class file major version|invalid target release|error: invalid source release",
     "wrong JDK on the runner for this repo's target release"),
    (r"Missing script: |npm ERR! missing script",
     "package.json has no 'test' script"),
    (r"Cannot find module|npm ERR! code E404|ERR_MODULE_NOT_FOUND",
     "node dependencies are not installed on the runner (npm ci was not run)"),
    (r"npm ERR! code EACCES|EACCES: permission denied",
     "permission denied on the runner"),
    (r"toolchain|No matching toolchains",
     "gradle toolchain missing on the runner"),
]
_COMPILE_FAILURE = re.compile(
    r"COMPILATION ERROR|Compilation failure|error: cannot find symbol|"
    r"Execution failed for task '.*:compile|TS\d{4}:",
    re.I,
)


def classify_build_failure(log: str, tool: str, code: int) -> tuple[str, str]:
    """Return (status, reason) for a build that exited non-zero with no test failures.

    NOT_RUN = the runner could not execute the tests, so the result carries no signal.
    ERROR   = the build ran but broke (usually compilation), which IS a signal.
    """
    for pattern, reason in _ENV_FAILURE_PATTERNS:
        if re.search(pattern, log, re.I):
            return "NOT_RUN", f"{tool} could not run tests: {reason}"
    if _COMPILE_FAILURE.search(log):
        return "ERROR", f"{tool} build failed to compile (exit {code}) — see logExcerpt"
    return "NOT_RUN", (
        f"{tool} exited {code} with no parsed test results and no recognised cause — "
        f"see logExcerpt for the raw tail"
    )


# ── Core runner ───────────────────────────────────────────────────────────────────

def run_one(
    entry: dict,
    source_apps: list[str],
    rs: RepoSource | None = None,
    cfg: Config = CONFIG,
    keep: bool = False,
    it_repos: list[str] | None = None,
) -> dict:
    rs = rs or RepoSource(cfg)
    bt = entry.get("buildTool", "unknown")
    it_repos = it_repos or []

    # §3: SKIPPED is reserved for explicit user opt-out; use NOT_RUN for everything else
    if not entry.get("runnable"):
        reason = entry.get("notRunnableReason") or "not runnable (no test command / unknown build tool)"
        return _result(entry, status="NOT_RUN", notRunReason=reason, integrationTestRepos=it_repos)

    if bt not in ("maven", "gradle", "npm"):
        return _result(
            entry, status="NOT_RUN",
            notRunReason=f"build tool {bt!r} not yet supported by the runner (only maven/gradle/npm)",
            integrationTestRepos=it_repos,
        )

    work = Path(tempfile.mkdtemp(prefix=f"shockwave-{entry['repo']}-"))
    dest = work / entry["repo"]
    t0 = time.time()

    try:
        # ── clone ────────────────────────────────────────────────────────────────
        try:
            how = _clone(entry, dest, rs)
        except Exception as e:
            return _result(
                entry, attempted=True, status="NOT_RUN",
                notRunReason=str(e)[:400],
                logExcerpt=str(e)[:4000],
                durationSeconds=round(time.time() - t0, 1),
                integrationTestRepos=it_repos,
            )

        # ── live signal scan ─────────────────────────────────────────────────────
        live_signal, live_note = live_integration_scan(dest, source_apps)
        targeted = targeted_test_classes(dest, source_apps)

        # ── environment ──────────────────────────────────────────────────────────
        env = dict(os.environ)

        # ── Maven ────────────────────────────────────────────────────────────────
        if bt == "maven":
            jh = _java_home(dest)
            if jh:
                env["JAVA_HOME"] = jh
            cmd = (entry.get("testCommand") or "mvn -B test").split()
            if cfg.targeted_tests and targeted:
                cmd += [f"-Dtest={','.join(targeted)}", "-DfailIfNoTests=false"]
            failed, run, status, timed_out, code, log = _run_cmd(
                cmd, dest, work, env, cfg.test_timeout_seconds
            )
            if timed_out:
                status = "NOT_RUN"
                nr = f"mvn timed out after {cfg.test_timeout_seconds}s"
            else:
                failed, run = parse_surefire(dest)
                nr = None
                if failed:
                    status = "FAIL"
                elif code == 0:
                    status = "PASS"
                else:
                    status, nr = classify_build_failure(log, "mvn", code)
            return _result(
                entry, attempted=True, status=status,
                notRunReason=nr,
                durationSeconds=round(time.time() - t0, 1),
                failedTests=failed, logExcerpt=log,
                liveSignal=live_signal, liveIntegrationNote=live_note,
                integrationTestRepos=it_repos, targetedTestClasses=targeted,
                exitCode=code, testsRun=run, clonedFrom=how,
                javaHome=jh if bt == "maven" else None,
                command=" ".join(cmd),
            )

        # ── Gradle ───────────────────────────────────────────────────────────────
        if bt == "gradle":
            check_err = _check_gradle_wrapper(dest)
            if check_err:
                return _result(
                    entry, attempted=True, status="NOT_RUN",
                    notRunReason=check_err,
                    durationSeconds=round(time.time() - t0, 1),
                    liveSignal=live_signal, liveIntegrationNote=live_note,
                    integrationTestRepos=it_repos, clonedFrom=how,
                )
            cmd = (entry.get("testCommand") or "./gradlew test --no-daemon").split()
            failed, run, status, timed_out, code, log = _run_cmd(
                cmd, dest, work, env, cfg.test_timeout_seconds
            )
            if timed_out:
                return _result(
                    entry, attempted=True, status="NOT_RUN",
                    notRunReason=f"gradle timed out after {cfg.test_timeout_seconds}s",
                    durationSeconds=round(time.time() - t0, 1),
                    logExcerpt=log, liveSignal=live_signal, liveIntegrationNote=live_note,
                    integrationTestRepos=it_repos, exitCode=code, clonedFrom=how,
                )
            gradle_failed, gradle_run = parse_gradle_results(dest)
            if gradle_run:
                failed, run = gradle_failed, gradle_run
            nr = None
            if failed:
                final_status = "FAIL"
            elif code == 0:
                final_status = "PASS"
            else:
                final_status, nr = classify_build_failure(log, "gradle", code)
            return _result(
                entry, attempted=True, status=final_status,
                notRunReason=nr,
                durationSeconds=round(time.time() - t0, 1),
                failedTests=failed, logExcerpt=log,
                liveSignal=live_signal, liveIntegrationNote=live_note,
                integrationTestRepos=it_repos, targetedTestClasses=targeted,
                exitCode=code, testsRun=run, clonedFrom=how, command=" ".join(cmd),
            )

        # ── npm ───────────────────────────────────────────────────────────────────
        if bt == "npm":
            cmd = (entry.get("testCommand") or "npm test").split()
            failed, run, status, timed_out, code, log = _run_cmd(
                cmd, dest, work, env, cfg.test_timeout_seconds
            )
            if timed_out:
                return _result(
                    entry, attempted=True, status="NOT_RUN",
                    notRunReason=f"npm timed out after {cfg.test_timeout_seconds}s",
                    durationSeconds=round(time.time() - t0, 1),
                    logExcerpt=log, liveSignal=live_signal, liveIntegrationNote=live_note,
                    integrationTestRepos=it_repos, exitCode=code, clonedFrom=how,
                )
            npm_failed, npm_run = parse_npm_results(log)
            nr = None
            if npm_failed:
                final_status = "FAIL"
            elif code == 0:
                final_status = "PASS"
            else:
                # A non-zero npm exit with no parsed failures is usually a missing test
                # script or uninstalled dependencies, not a real test failure.
                final_status, nr = classify_build_failure(log, "npm", code)
            return _result(
                entry, attempted=True, status=final_status,
                notRunReason=nr,
                durationSeconds=round(time.time() - t0, 1),
                failedTests=npm_failed, logExcerpt=log[-4000:],
                liveSignal=live_signal, liveIntegrationNote=live_note,
                integrationTestRepos=it_repos, targetedTestClasses=targeted,
                exitCode=code, testsRun=npm_run, clonedFrom=how, command=" ".join(cmd),
            )

    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)

    # Should never reach here
    return _result(entry, status="NOT_RUN", notRunReason="internal runner error (unknown build tool)")


def _run_cmd(
    cmd: list[str],
    cwd: Path,
    work: Path,
    env: dict,
    timeout: int,
) -> tuple[list[str], int, str, bool, int, str]:
    """Run cmd, return (failed, run, status, timed_out, code, log)."""
    log_path = work / "test.log"
    with open(log_path, "w") as lf:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=lf, stderr=subprocess.STDOUT, env=env, start_new_session=True
        )
        try:
            code = proc.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            code, timed_out = -9, True
    log = log_path.read_text(errors="ignore")
    if timed_out:
        log = f"[TIMEOUT after {timeout}s]\n" + log[-3900:]
    else:
        log = log[-4000:]
    return [], 0, "PASS", timed_out, code, log


def _static_live_signal(rs: RepoSource, entry: dict, source_apps: list[str]) -> dict:
    """Live signal computed from the local clone, with no test execution (§3).

    Used when tests are skipped or cannot run, so the report can still say whether a
    live signal *exists* for this consumer.
    """
    p = rs.local_path(entry["org"], entry["repo"])
    if not p:
        return {
            "liveSignal": "NONE",
            "liveIntegrationNote": "no local clone available — live signal could not be determined",
        }
    signal, note = live_integration_scan(p, source_apps)
    return {
        "liveSignal": signal,
        "liveIntegrationNote": f"{note} (static scan of local clone; tests were not run)",
        "targetedTestClasses": targeted_test_classes(p, source_apps),
    }


# ── Batch runner ──────────────────────────────────────────────────────────────────

def run_many(
    entries: list[dict],
    source_apps: list[str],
    cfg: Config = CONFIG,
    skip: str | None = None,
    source_repo: str | None = None,
) -> list[dict]:
    """Run tests for a list of manifest entries, plus any discovered integration-test repos.

    ``skip`` is ONLY accepted when the user explicitly passed --skip-tests. In that case
    all entries get status=SKIPPED with the skip reason. Otherwise every entry is
    attempted and any non-execution is NOT_RUN with a specific reason.

    Integration-test repos (§3) are discovered once here rather than per entry: the scan
    covers every local clone and its answer does not depend on which consumer is running.
    """
    rs = RepoSource(cfg)
    manifest_keys = {f"{e['org']}/{e['repo']}".lower() for e in entries}
    exclude = set(manifest_keys)
    if source_repo:
        exclude.add(source_repo.lower())
    discovered = discover_integration_test_repos(rs, source_apps, exclude)
    it_keys = [d["key"] for d in discovered]

    if skip:
        # §3: SKIPPED only on explicit user request, and prominently labelled. The live
        # signal is still computed from the local clone — "we didn't run the tests" is no
        # reason to also throw away the cheap, static answer to "would they have told us
        # anything?". Without this the whole run reports NONE and looks like real evidence.
        return [
            _result(e, status="SKIPPED", skippedReason=f"[USER REQUESTED SKIP] {skip}",
                    integrationTestRepos=it_keys, **_static_live_signal(rs, e, source_apps))
            for e in entries + integration_test_entries(discovered, rs, cfg)
        ]

    # §3: discovered integration-test repos ARE run — they are usually the only live signal
    # for consumers that reach the source over HTTP rather than through a shared library.
    all_entries = entries + integration_test_entries(discovered, rs, cfg)
    with ThreadPoolExecutor(max(1, cfg.test_concurrency)) as ex:
        return list(ex.map(
            lambda e: run_one(e, source_apps, rs, cfg, it_repos=it_keys), all_entries
        ))
