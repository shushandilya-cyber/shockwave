"""Runtime configuration. Everything is overridable via env vars.

Credentials are never printed or written anywhere. If GITHUB_TOKEN / JIRA_PAT are
not in the environment, they are read (in-memory only) from the git-server /
jira-server entries in ~/.claude.json.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _claude_env(server: str) -> dict:
    try:
        cfg = json.loads((Path.home() / ".claude.json").read_text())
        return cfg.get("mcpServers", {}).get(server, {}).get("env", {}) or {}
    except Exception:
        return {}


@dataclass
class Config:
    knowledge_base: str = os.getenv("SHOCKWAVE_KNOWLEDGE_BASE", "https://knowledgehub.vip.qa.ebay.com/knowledgegw")
    # "http" = call Code Knowledge REST directly; "mcp" = go through hosted codemcp (what Obsidian uses)
    knowledge_backend: str = os.getenv("SHOCKWAVE_KNOWLEDGE_BACKEND", "http")
    codemcp_url: str = os.getenv("SHOCKWAVE_CODEMCP_URL", "https://codemcp2.vip.qa.ebay.com/mcp")
    github_web: str = os.getenv("SHOCKWAVE_GITHUB_WEB", "https://github.corp.ebay.com")
    github_api: str = os.getenv("GITHUB_API_URL", "") or _claude_env("git-server").get("GITHUB_API_URL", "https://github.corp.ebay.com/api/v3")
    github_token: str = field(default_factory=lambda: os.getenv("GITHUB_TOKEN") or _claude_env("git-server").get("GITHUB_TOKEN", ""), repr=False)
    jira_base: str = os.getenv("JIRA_HOME", "") or _claude_env("jira-server").get("JIRA_HOME", "https://jirap.corp.ebay.com")
    jira_pat: str = field(default_factory=lambda: os.getenv("JIRA_PAT") or _claude_env("jira-server").get("JIRA_PAT", ""), repr=False)
    local_repos_root: Path = Path(os.getenv("SHOCKWAVE_LOCAL_REPOS", str(Path.home() / "Documents" / "projects")))
    cache_dir: Path = Path(os.getenv("SHOCKWAVE_CACHE", str(Path.home() / ".cache" / "shockwave")))
    cache_ttl_seconds: int = int(os.getenv("SHOCKWAVE_CACHE_TTL", str(6 * 3600)))
    http_timeout: int = int(os.getenv("SHOCKWAVE_HTTP_TIMEOUT", "240"))
    # blast radius tuning
    transitive_depth: int = int(os.getenv("SHOCKWAVE_TRANSITIVE_DEPTH", "6"))
    transitive_limit: int = int(os.getenv("SHOCKWAVE_TRANSITIVE_LIMIT", "500"))
    vector_min_score: float = float(os.getenv("SHOCKWAVE_VECTOR_MIN_SCORE", "0.35"))
    max_symbols: int = int(os.getenv("SHOCKWAVE_MAX_SYMBOLS", "60"))
    # test runner
    test_timeout_seconds: int = int(os.getenv("SHOCKWAVE_TEST_TIMEOUT", "600"))
    test_concurrency: int = int(os.getenv("SHOCKWAVE_TEST_CONCURRENCY", "4"))
    # jira
    jira_triage_project: str = os.getenv("SHOCKWAVE_JIRA_TRIAGE_PROJECT", "")
    jira_mapping_file: Path = Path(os.getenv("SHOCKWAVE_JIRA_MAPPING", str(Path(__file__).resolve().parents[2] / "jira-team-mapping.yaml")))


CONFIG = Config()

