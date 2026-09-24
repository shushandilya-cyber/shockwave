"""Story 1 — Trigger & Event Ingestion: raw custom-event payload -> ChangeEvent."""
from __future__ import annotations

from datetime import datetime, timezone

from ..contracts import ContractError, validate_change_event

REQUIRED = ["org", "repo", "branch", "commit", "environment"]


def ingest(raw: dict, now: datetime | None = None) -> dict:
    """Accepts either the full Obsidian trigger body ({eventContext:{payload}}),
    an ``eventContext`` object, or the bare payload."""
    payload = raw
    if isinstance(raw.get("eventContext"), dict):
        payload = raw["eventContext"].get("payload") or {}
    elif isinstance(raw.get("payload"), dict) and "org" not in raw:
        payload = raw["payload"]
    if not isinstance(payload, dict):
        raise ContractError("ChangeEvent: payload must be an object")

    missing = [k for k in REQUIRED if not str(payload.get(k) or "").strip()]
    if missing:
        raise ContractError(f"ChangeEvent: missing required field(s): {', '.join(missing)}")

    commit = str(payload["commit"]).strip()
    if not all(c in "0123456789abcdefABCDEF" for c in commit) or len(commit) < 7:
        raise ContractError(f"ChangeEvent: commit {commit!r} is not a git SHA")

    pr = payload.get("prNumber")
    pr = str(pr).strip() if pr not in (None, "", "null") else None
    ts = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return validate_change_event({
        "org": str(payload["org"]).strip(),
        "repo": str(payload["repo"]).strip(),
        "branch": str(payload["branch"]).strip(),
        "commit": commit,
        "environment": str(payload["environment"]).strip(),
        "prNumber": pr,
        "triggeredAt": ts,
    })

