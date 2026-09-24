import json
import subprocess
from pathlib import Path

import pytest

from shockwave.config import Config

FIX = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.cache_dir = tmp_path / "cache"
    c.local_repos_root = tmp_path / "projects"
    c.local_repos_root.mkdir()
    c.jira_mapping_file = tmp_path / "mapping.yaml"
    return c


def load(rel: str):
    return json.loads((FIX / rel).read_text())


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True).stdout


def make_repo(root: Path, name: str, org: str = "TestOrg") -> Path:
    p = root / name
    p.mkdir(parents=True)
    git(p, "init", "-q", "-b", "master")
    git(p, "config", "user.email", "t@t"); git(p, "config", "user.name", "t")
    git(p, "remote", "add", "origin", f"https://github.corp.ebay.com/{org}/{name}.git")
    return p


def commit_all(p: Path, msg: str) -> str:
    git(p, "add", "-A"); git(p, "commit", "-q", "-m", msg)
    return git(p, "rev-parse", "HEAD").strip()

