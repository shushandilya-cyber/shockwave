"""§1 Changes Summary — deterministic classification of what changed and why it matters.

First principles:
  A human asks: "Do I need to act? What exactly changed, and what does that mean for callers?"
  Raw symbol names don't answer this. Groups + risk classes do.

Classification is fully deterministic (rule-based). An LLM is used only to write the
``intent`` sentence from the commit message; it always cites the evidence it used.

Groups
------
contractChanges  Endpoint added/removed/changed, public method signature changed,
                 DTO field added/removed/renamed, serialization or enum changes.
behaviorChanges  Logic change inside an existing public/exposed path.
internalOnly     Private helpers, logging, metrics, tests, build files.
configDataOnly   JSON/YAML/properties/XML data-only changes (no source symbols).

Risk classes
------------
BREAKING    Existing callers WILL break without a code change (removed endpoint,
            removed method, changed signature, removed required field).
BEHAVIORAL  Callers may see different behaviour but their code still compiles
            (logic change in exposed path, new endpoint/method, new optional field).
SAFE        No externally observable change (internal refactor, config value added,
            logging change, test-only change).
"""
from __future__ import annotations

import re
from pathlib import Path


# ── pattern helpers ─────────────────────────────────────────────────────────────

# Classes whose public methods form a contract (JAX-RS resources, Spring controllers)
_RESOURCE_CLS = re.compile(
    r"(Resource|Controller|Endpoint|RestController|Service)$", re.I
)
# Classes whose fields/getters are serialised (DTOs, request/response models)
_DTO_CLS = re.compile(
    r"(DTO|Request|Response|Model|Payload|Entity|Vo|View|Result|Info|Data|Config)$",
    re.I,
)
# Source-level paths that indicate internal/test code
_TEST_PATH = re.compile(r"(^|/)(src/test|src/it|test|tests|it)/", re.I)
_BUILD_PATH = re.compile(r"\.(xml|gradle|gradle\.kts|properties|toml)$", re.I)
_CONFIG_EXT = re.compile(r"\.(json|yaml|yml|properties|xml|conf|ini|toml)$", re.I)
_ENUM_CLS = re.compile(r"(Enum|Type|Kind|Status|State)$", re.I)

_GETTER_SETTER = re.compile(r"^(get|set|is)[A-Z]")

# An API spec IS the contract, whatever its file extension.
_API_SPEC = re.compile(r"(openapi|swagger)[\w.-]*\.(ya?ml|json)$|/api/openapi\.", re.I)
# Data the service loads at runtime and serves to callers. Changing it changes what
# consumers observe, with no code change anywhere — the INC-002 shape.
_RUNTIME_DATA = re.compile(r"(^|/)src/main/resources/", re.I)
# Build descriptors: they affect how the artifact is produced, not what callers see.
_BUILD_FILE = re.compile(r"(^|/)(pom\.xml|build\.gradle(\.kts)?|settings\.gradle(\.kts)?|"
                         r"package\.json|package-lock\.json)$", re.I)


def classify_config_file(path: str, repo: str | None = None) -> tuple[str, str, str]:
    """Classify a non-source file. Order matters; first match wins.

    Not every config file is inert. Test fixtures are; an OpenAPI spec is the published
    contract; and data under src/main/resources is served to callers, so editing it
    changes behaviour without touching a line of code. Treating all three as SAFE is what
    let INC-002 (a Hubs.json edit that caused a live incident) score as a no-op.
    """
    name = Path(path).name
    if _TEST_PATH.search(path):
        return "internalOnly", "SAFE", f"test fixture or resource: {name}"
    if _API_SPEC.search(path):
        return ("contractChanges", "BEHAVIORAL",
                f"API specification changed: {name} — this file IS the published contract; "
                f"review the diff for removed or renamed operations, which would be BREAKING")
    if _BUILD_FILE.search(path):
        return "internalOnly", "SAFE", f"build descriptor: {name}"
    is_cfg_repo = bool(repo) and repo.lower().endswith("cfg")
    if _RUNTIME_DATA.search(path) or is_cfg_repo:
        where = "a config repo" if is_cfg_repo else "src/main/resources"
        return ("configDataOnly", "BEHAVIORAL",
                f"runtime data changed: {name} (in {where}) — the service loads this and "
                f"serves it to callers, so consumers see different results with no code change")
    return "configDataOnly", "SAFE", f"data/config file change: {name}"


def _cls_tail(fqn: str) -> str:
    """'com.acme.foo.MyResource' -> 'MyResource'"""
    return fqn.rsplit(".", 1)[-1]


def _classify_symbol(sym: dict, resource_classes: set[str], repo: str | None = None) -> tuple[str, str, str]:
    """Return (group, riskClass, reason) for one changed symbol.

    group     : contractChanges | behaviorChanges | internalOnly | configDataOnly
    riskClass : BREAKING | BEHAVIORAL | SAFE
    reason    : one-line plain-English justification (cited evidence)
    """
    kind = sym["kind"]
    change = sym["changeType"]
    file_path = sym.get("file", "")
    cls = sym.get("className") or ""
    cls_tail = _cls_tail(cls) if cls else ""
    member = sym.get("member") or ""

    # ── non-source files (config, data, specs, build) ────────────────────────────
    if _CONFIG_EXT.search(file_path):
        return classify_config_file(file_path, repo)

    # ── test / build files ────────────────────────────────────────────────────────
    if _TEST_PATH.search(file_path) or _BUILD_PATH.search(file_path):
        return "internalOnly", "SAFE", f"test or build file: {Path(file_path).name}"

    # ── endpoint changes (already extracted by s2_diff) ──────────────────────────
    if kind == "endpoint":
        if change == "removed":
            return (
                "contractChanges",
                "BREAKING",
                f"endpoint removed: {sym.get('name')}",
            )
        if change == "added":
            return (
                "contractChanges",
                "BEHAVIORAL",
                f"new endpoint added: {sym.get('name')}",
            )
        return (
            "contractChanges",
            "BREAKING",
            f"endpoint signature changed: {sym.get('name')}",
        )

    # ── enum / type changes ────────────────────────────────────────────────────────
    if kind == "class" and _ENUM_CLS.search(cls_tail):
        if change == "removed":
            return (
                "contractChanges",
                "BREAKING",
                f"enum class removed: {cls_tail}",
            )
        return (
            "contractChanges",
            "BEHAVIORAL",
            f"enum class {change}: {cls_tail} (new values do not break existing callers; removed values would)",
        )

    # ── class-level changes ────────────────────────────────────────────────────────
    if kind == "class":
        if change == "removed":
            return (
                "contractChanges",
                "BREAKING",
                f"class removed: {sym.get('name')} (all callers break)",
            )
        if change == "added":
            return (
                "contractChanges",
                "BEHAVIORAL",
                f"new class added: {sym.get('name')}",
            )
        return (
            "internalOnly",
            "SAFE",
            f"class structure modified (e.g. annotations, inheritance): {cls_tail}",
        )

    # ── method changes ─────────────────────────────────────────────────────────────
    if kind == "method":
        in_resource = cls_tail in resource_classes or bool(_RESOURCE_CLS.search(cls_tail))
        in_dto = bool(_DTO_CLS.search(cls_tail))
        is_getter_setter = bool(_GETTER_SETTER.match(member))

        if in_resource:
            # Methods in resource/controller classes form the external API
            if change == "removed":
                return (
                    "contractChanges",
                    "BREAKING",
                    f"{cls_tail}.{member} removed from API resource class",
                )
            if change == "added":
                return (
                    "contractChanges",
                    "BEHAVIORAL",
                    f"new method {member} added to API resource class {cls_tail}",
                )
            return (
                "behaviorChanges",
                "BEHAVIORAL",
                f"logic change inside existing API method {cls_tail}.{member}",
            )

        if in_dto and is_getter_setter:
            field = member[3:] if len(member) > 3 else member  # strip get/set/is
            if change == "removed":
                return (
                    "contractChanges",
                    "BREAKING",
                    f"DTO {cls_tail}.{field}: accessor removed (serialization/deserialization break)",
                )
            if change == "added":
                return (
                    "contractChanges",
                    "BEHAVIORAL",
                    f"DTO {cls_tail}.{field}: new accessor added (additive, old consumers unaffected)",
                )
            return (
                "contractChanges",
                "BEHAVIORAL",
                f"DTO {cls_tail}.{field}: accessor modified (serialization may change)",
            )

        # Generic method: treat removed as contract change (callers may exist),
        # modified/added as behavior.
        if change == "removed":
            return (
                "contractChanges",
                "BREAKING",
                f"{cls_tail}.{member} removed (may break callers outside this repo)",
            )
        return (
            "behaviorChanges",
            "BEHAVIORAL",
            f"logic change in {cls_tail}.{member}",
        )

    # ── field changes ──────────────────────────────────────────────────────────────
    if kind == "field":
        in_dto = bool(_DTO_CLS.search(cls_tail))
        if in_dto:
            if change == "removed":
                return (
                    "contractChanges",
                    "BREAKING",
                    f"DTO field {cls_tail}.{member} removed (JSON/XML serialization break)",
                )
            if change == "added":
                return (
                    "contractChanges",
                    "BEHAVIORAL",
                    f"DTO field {cls_tail}.{member} added (additive JSON/XML change)",
                )
            return (
                "contractChanges",
                "BEHAVIORAL",
                f"DTO field {cls_tail}.{member} modified (serialization behaviour may change)",
            )
        # non-DTO field: internal state
        if change == "removed":
            return (
                "internalOnly",
                "SAFE",
                f"internal field {cls_tail}.{member} removed",
            )
        return (
            "internalOnly",
            "SAFE",
            f"internal field {cls_tail}.{member} {change}",
        )

    return "internalOnly", "SAFE", f"unknown kind {kind!r}"


def _worst_risk(classes: list[str]) -> str:
    for r in ("BREAKING", "BEHAVIORAL", "SAFE"):
        if r in classes:
            return r
    return "SAFE"


def classify_changes(changed: dict, commit_msg: str | None = None) -> dict:
    """Classify ChangedSymbols into a changesSummary block.

    Parameters
    ----------
    changed:    The output dict from Story 2 (ChangedSymbols).
    commit_msg: First-line commit message (used only for the intent sentence).

    Returns
    -------
    changesSummary dict with:
      intent           str  — one sentence from the commit message.
      groups           dict — keyed by group name.
        .<group>:
          riskClass    BREAKING | BEHAVIORAL | SAFE
          reason       str  — one-line justification.
          symbols      list — symbol names in this group.
      overallRiskClass str  — worst risk class across all groups.
      exposedVia       list — endpoints (method+path) the change reaches.
      rawSymbolsRef    str  — "see changedSymbols in the parent document"
    """
    symbols = changed.get("changedSymbols", [])
    config_files = [
        f for f in changed.get("changedFiles", [])
        if _CONFIG_EXT.search(f) and not any(s["file"] == f for s in symbols)
    ]
    eps = changed.get("changedApiEndpoints", [])

    # ── Collect the simple class names of classes that appear in the API endpoints.
    # These are used so that methods inside a *Resource class that is annotated with
    # @Path are classified as contract changes even if the class name doesn't end with
    # "Resource".  (Heuristic: if a class has a changed endpoint, its methods are API.)
    resource_classes: set[str] = set()
    for s in symbols:
        if s.get("kind") == "class" or s.get("kind") == "method":
            # Check if this class's file also contains a changed endpoint
            cls_tail = _cls_tail(s.get("className") or "")
            if eps and cls_tail:
                resource_classes.add(cls_tail)

    groups: dict[str, dict] = {
        "contractChanges": {"riskClass": None, "reasons": [], "symbols": [], "riskClasses": []},
        "behaviorChanges": {"riskClass": None, "reasons": [], "symbols": [], "riskClasses": []},
        "internalOnly": {"riskClass": None, "reasons": [], "symbols": [], "riskClasses": []},
        "configDataOnly": {"riskClass": None, "reasons": [], "symbols": [], "riskClasses": []},
    }

    repo_name = changed.get("repo")
    for sym in symbols:
        group, risk, reason = _classify_symbol(sym, resource_classes, repo_name)
        g = groups[group]
        g["symbols"].append(sym["name"])
        g["riskClasses"].append(risk)
        if reason not in g["reasons"]:
            g["reasons"].append(reason)

    # Synthetic entries for changed non-source files, which yield no symbols to classify
    for cf in config_files:
        group, risk, reason = classify_config_file(cf, repo_name)
        g = groups[group]
        g["symbols"].append(f"<data:{Path(cf).name}>")
        g["riskClasses"].append(risk)
        if reason not in g["reasons"]:
            g["reasons"].append(reason)

    # Endpoint changes (summary)
    for ep in eps:
        change_ep = "added" if ep.get("changeType") == "added" else "modified"
        g = groups["contractChanges"]
        name = f"{ep['method']} {ep['path']}"
        if name not in g["symbols"]:
            g["symbols"].append(name)
            g["riskClasses"].append("BREAKING" if change_ep == "removed" else "BEHAVIORAL")
            reason = f"endpoint {change_ep}: {name}"
            if reason not in g["reasons"]:
                g["reasons"].append(reason)

    # Collapse each group
    result_groups: dict[str, dict] = {}
    for gname, gdata in groups.items():
        if not gdata["symbols"] and not gdata["riskClasses"]:
            continue
        worst = _worst_risk(gdata["riskClasses"])
        result_groups[gname] = {
            "riskClass": worst,
            "reason": "; ".join(gdata["reasons"][:5]),
            "symbols": gdata["symbols"],
        }

    overall = _worst_risk(
        [g["riskClass"] for g in result_groups.values() if g["riskClass"]]
    ) if result_groups else "SAFE"

    # Intent: first non-empty line of commit message (not an LLM call).
    # The caller is responsible for passing the commit message. We strip ticket
    # prefixes so the sentence reads naturally.
    intent = _intent_from_msg(commit_msg, changed)

    return {
        "intent": intent,
        "jiraKeys": jira_keys(commit_msg),
        "groups": result_groups,
        "overallRiskClass": overall,
        "exposedVia": [
            {"method": e["method"], "path": e["path"],
             "operationId": e.get("operationId")}
            for e in eps
        ],
        "rawSymbolsRef": "see changedSymbols[] in this document",
        "_evidence": {
            "symbolCount": len(symbols),
            "configFileCount": len(config_files),
            "endpointCount": len(eps),
            "org": changed.get("org"),
            "repo": changed.get("repo"),
            "commit": (changed.get("commit") or "")[:8],
        },
    }


def enrich_exposed_via(changes_summary: dict, blast: dict) -> dict:
    """Merge Story 3's reachability walk into the summary's ``exposedVia`` (§1).

    Story 2 alone can only report endpoints whose own handler was edited. The case that
    matters for callers is the opposite one: an endpoint nobody touched that now reaches
    changed code through a chain of internal calls. Only Story 3 can see that, so the
    summary is backfilled once the blast radius has been computed.

    Mutates and returns ``changes_summary``. Safe to call more than once.
    """
    if not changes_summary:
        return changes_summary
    existing = {(e.get("method"), e.get("path")): e for e in changes_summary.get("exposedVia") or []}
    for e in blast.get("exposedVia") or []:
        key = (e.get("method"), e.get("path"))
        if key in existing:
            existing[key].update({k: v for k, v in e.items() if v is not None})
        else:
            existing[key] = dict(e)
    changes_summary["exposedVia"] = sorted(
        existing.values(), key=lambda e: (e.get("path") or "", e.get("method") or "")
    )
    return changes_summary


_TICKET_PREFIX = re.compile(
    r"^\s*[\[\(]?[A-Z][A-Z0-9]+-\d+[\]\)]?\s*[:\-–]?\s*", re.I
)
# Jira keys anywhere in the message. Upper-case only: lower-case matches turn ordinary
# hyphenated words into fake ticket keys.
_TICKET_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
_MERGE_PREFIX = re.compile(r"^(Merge)\s+", re.I)
# A PR suffix like "(#1159)" is noise in a one-line intent.
_PR_SUFFIX = re.compile(r"\s*\(#\d+\)\s*$")


def jira_keys(msg: str | None) -> list[str]:
    """Jira keys referenced by the commit message, in first-seen order."""
    if not msg:
        return []
    seen: list[str] = []
    for k in _TICKET_KEY.findall(msg):
        if k not in seen:
            seen.append(k)
    return seen


def _intent_from_msg(msg: str | None, changed: dict) -> str:
    """Extract a 1–2 sentence intent from the commit message.

    Does NOT call an LLM. Returns a sentence that cites its evidence: the Jira key(s)
    it read the intent from, plus the repo and commit.
    """
    org = changed.get("org", "?")
    repo = changed.get("repo", "?")
    commit = (changed.get("commit") or "")[:8]
    keys = jira_keys(msg)
    citation = f"[{', '.join(keys) + ' · ' if keys else ''}{org}/{repo}@{commit}]"

    if msg:
        lines = [ln.strip() for ln in msg.splitlines() if ln.strip()]
        if lines:
            # A revert names what it undoes, which is the intent — keep it, just tidy it.
            # Quotes are dropped rather than balanced: the subject of a revert is quoted,
            # and stripping one end leaves a stray mark mid-sentence.
            first = lines[0].replace('"', "")
            first = _PR_SUFFIX.sub("", first).strip()
            first = _TICKET_PREFIX.sub("", first).strip()
            if first and not _MERGE_PREFIX.match(first):
                if len(first) > 200:
                    first = first[:197] + "…"
                return f"{first}. {citation}"

    # Fallback: describe the change structurally
    n_syms = len(changed.get("changedSymbols", []))
    n_eps = len(changed.get("changedApiEndpoints", []))
    parts = []
    if n_syms:
        parts.append(f"{n_syms} symbol(s) changed")
    if n_eps:
        parts.append(f"{n_eps} API endpoint(s) affected")
    config_files = [
        f for f in changed.get("changedFiles", []) if _CONFIG_EXT.search(f)
    ]
    if config_files:
        parts.append(f"config files: {', '.join(Path(f).name for f in config_files[:3])}")
    desc = "; ".join(parts) if parts else "no symbols or endpoints detected"
    return f"Commit {citation}: {desc}. (No commit message provided.)"
