"""
OAuth provider configuration loader.

Reads oauth_config.yaml from (in order):
  1. Path in env var OAUTH_CONFIG_FILE
  2. ~/.config/mcp-itop/oauth_config.yaml
  3. ./oauth_config.yaml

Exposes a singleton OAuthConfig instance: oauth_cfg
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class OAuthConfig:
    issuer_url: str
    audience: str
    upn_claim: str = "preferred_username"
    jwks_cache_ttl: int = 300
    verify_ssl: bool = True
    token_store_path: str = "token_store.yaml"


def _find_config_file() -> Path:
    env_path = os.environ.get("OAUTH_CONFIG_FILE")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
        raise FileNotFoundError(f"OAUTH_CONFIG_FILE set but not found: {env_path}")

    candidates = [
        Path.home() / ".config" / "mcp-itop" / "oauth_config.yaml",
        Path("oauth_config.yaml"),
    ]
    for c in candidates:
        if c.is_file():
            return c

    raise FileNotFoundError(
        "oauth_config.yaml not found. Create one from oauth_config.yaml.example. "
        "Search paths: OAUTH_CONFIG_FILE env var, "
        "~/.config/mcp-itop/oauth_config.yaml, ./oauth_config.yaml"
    )


def load_oauth_config() -> OAuthConfig:
    config_path = _find_config_file()
    with open(config_path) as fh:
        raw = yaml.safe_load(fh)

    oa = raw.get("oauth") or {}

    issuer_url = oa.get("issuer_url", "").strip().rstrip("/")
    if not issuer_url:
        raise ValueError("oauth.issuer_url is required in oauth_config.yaml")

    audience = oa.get("audience", "").strip()
    if not audience:
        raise ValueError("oauth.audience is required in oauth_config.yaml")

    token_store_path = raw.get("token_store_path", "token_store.yaml")
    # Resolve relative paths against the directory of the config file
    ts_path = Path(token_store_path)
    if not ts_path.is_absolute():
        ts_path = config_path.parent / ts_path
    token_store_path = str(ts_path)

    return OAuthConfig(
        issuer_url=issuer_url,
        audience=audience,
        upn_claim=oa.get("upn_claim", "preferred_username"),
        jwks_cache_ttl=int(oa.get("jwks_cache_ttl", 300)),
        verify_ssl=bool(oa.get("verify_ssl", True)),
        token_store_path=token_store_path,
    )


# Singleton loaded at import time. Server startup fails fast if config is missing.
try:
    oauth_cfg: OAuthConfig = load_oauth_config()
except FileNotFoundError:
    # Allow import in environments where OAuth is not yet configured.
    # auth.py will raise a clear error at token-verification time.
    oauth_cfg = None  # type: ignore[assignment]
