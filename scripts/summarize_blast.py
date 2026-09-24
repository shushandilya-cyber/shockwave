"""Quick summary printer for a BlastRadius JSON (dev helper)."""
import json
import sys

d = json.load(open(sys.argv[1]))
print("indexed", d.get("indexedRef"), "notIndexed", d["notIndexed"], "transitive", d.get("transitiveCallerCount"),
      "toolCalls", d.get("toolCalls"), "backend", d.get("backend"))
print("resolved", len(d.get("resolvedSymbols", [])), "unresolved", d["unresolvedSymbols"])
print("newSymbols", len(d.get("newSymbols", [])))
u = d.get("unmappedApiCallers", [])
print("unmappedApi", len(u), "matched", sum(1 for x in u if x["matchedChangedCode"]))
for r in d["impactedRepos"]:
    srcs = sorted({e["source"] for e in r["evidence"]})
    best = next((e for e in r["evidence"] if e.get("matchedChangedCode") or e["source"] == "graphSearch"), r["evidence"][0])
    print(f"  {r['confidence']:6} {r['org'] + '/' + r['repo']:50} {srcs} | {best['detail'][:120]}")
print("notes", d.get("notes"), "hints", d.get("notIndexedHints"))

