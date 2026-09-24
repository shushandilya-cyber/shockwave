"""Mine local git history for incident-shaped commits (revert / hotfix / rollback / incident / fix prod)."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path.home() / "Documents" / "projects"
REPOS = sys.argv[1:] or ["PUDO", "pudef", "pudocfg", "PickUpSvc", "PickupEligibilityService", "PickupEligibilityCfg",
                         "PickupEvalBatch", "PickupNotificationService", "LocBridge", "waypointservice", "PudoIntegrationTests",
                         "StorePickerExperienceService", "locnapi", "PudoTools", "PudoMonitor"]
PAT = r"revert|hotfix|hot fix|rollback|roll back|incident|prod issue|production issue|sev[ -]?[0-9]|outage|urgent|emergency|npe|nullpointer"
for r in REPOS:
    p = ROOT / r
    if not (p / ".git").exists():
        continue
    out = subprocess.run(["git", "-C", str(p), "log", "--all", "-i", "-E", f"--grep={PAT}", "--since=2019-01-01",
                          "--format=%h|%ad|%an|%s", "--date=short"], capture_output=True, text=True).stdout
    lines = [l for l in out.splitlines() if l]
    print(f"=== {r}: {len(lines)}")
    for l in lines[:40]:
        print("  ", l[:170])

