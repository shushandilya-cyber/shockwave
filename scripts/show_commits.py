"""Show details for candidate incident commits: message body, parents, stat, and what a revert reverts."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path.home() / "Documents" / "projects"


def g(repo, *a):
    return subprocess.run(["git", "-C", str(ROOT / repo), *a], capture_output=True, text=True).stdout


for spec in sys.argv[1:]:
    repo, sha = spec.split("@")
    print("=" * 100)
    print(f"{repo}@{sha}")
    print(g(repo, "show", "-s", "--format=%H%n%ad %an%nparents: %P%n%B", "--date=iso", sha)[:1400])
    print(g(repo, "show", "--stat", "--format=", sha)[-1500:])
    m = re.search(r"This reverts commit ([0-9a-f]{7,40})", g(repo, "show", "-s", "--format=%B", sha))
    if m:
        print(f"--> reverts {m.group(1)}:", g(repo, "show", "-s", "--format=%h %ad %an | %s", "--date=short", m.group(1)).strip())
    br = g(repo, "branch", "-r", "--contains", sha).split()
    print("on branches:", [b for b in br if b.endswith(("/master", "/main", "/Raptor2Migration"))] or br[:3])

