"""Scan local repos for service-identity references (hostnames / service-client names) -> who references whom."""
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path.home() / "Documents" / "projects"
EXT = {".properties", ".yaml", ".yml", ".json", ".java", ".xml", ".kt"}
HOST = re.compile(r"\b([a-z0-9][a-z0-9-]{2,})\.(?:vip\.)?(?:qa\.|stratus\.[a-z.]*qa\.)?ebay\.com\b", re.I)
KEYS = ["pudo", "pickup", "spesvc", "locn", "waypoint", "upcs", "puelig", "pudef", "ppsvc", "pudoapi"]
refs = defaultdict(Counter)
for repo in sorted(p for p in ROOT.iterdir() if (p / ".git").exists()):
    for f in repo.rglob("*"):
        if f.suffix not in EXT or "/target/" in str(f) or "/.git/" in str(f) or "node_modules" in str(f):
            continue
        try:
            t = f.read_text(errors="ignore")
        except Exception:
            continue
        for m in HOST.finditer(t):
            h = m.group(1).lower()
            if any(k in h for k in KEYS):
                refs[h][repo.name] += 1
for h, c in sorted(refs.items(), key=lambda x: -sum(x[1].values()))[:30]:
    print(f"{h:28} {dict(c.most_common(8))}")

