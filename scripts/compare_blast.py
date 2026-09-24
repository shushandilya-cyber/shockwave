"""Compare two BlastRadius JSONs (e.g. http vs mcp backend, or local engine vs Obsidian node output).

Exit 0 if the impacted-repo set and confidences match, 1 otherwise.
  python scripts/compare_blast.py a.json b.json
"""
import json
import sys

a, b = (json.load(open(p)) for p in sys.argv[1:3])


def idx(d):
    return {f"{r['org']}/{r['repo']}".lower(): r["confidence"] for r in d.get("impactedRepos", [])}


ia, ib = idx(a), idx(b)
ok = True
for k in sorted(set(ia) | set(ib)):
    if ia.get(k) != ib.get(k):
        ok = False
        print(f"DIFF {k}: {ia.get(k)} vs {ib.get(k)}")
for f in ("notIndexed", "unresolvedSymbols"):
    if a.get(f) != b.get(f):
        ok = False
        print(f"DIFF {f}: {a.get(f)} vs {b.get(f)}")
print(f"{'MATCH' if ok else 'MISMATCH'}: {len(ia)} vs {len(ib)} repos "
      f"({sum(v == 'high' for v in ia.values())} vs {sum(v == 'high' for v in ib.values())} high)")
sys.exit(0 if ok else 1)

