"""Config loading: a YAML file first, environment variables override it."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

PLACEHOLDER_KEY = "PASTE_ADMIN_API_KEY_HERE"

TEMPLATE = f"""# Jellyfin Collection Manager config
# Keep this file private (chmod 600): the API key is admin-level.
jellyfin:
  url: "https://jellyfin.example.com"
  # Keep the quotes. An all-digit key would otherwise be read as a number.
  api_key: "{PLACEHOLDER_KEY}"
  verify_tls: true
  # Only set this if the self-check shows the API key can't list items
  # without a user. Leave empty otherwise.
  user_id: ""

app:
  # How long the in-memory snapshot is reused before re-reading Jellyfin.
  cache_seconds: 30
"""


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    url: str
    api_key: str
    verify_tls: bool
    user_id: str
    cache_seconds: int


def _as_bool(value, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg_path = Path(path or os.environ.get("CONFIG_PATH", "/config/config.yml"))
    data: dict = {}

    if cfg_path.exists():
        try:
            data = yaml.safe_load(cfg_path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{cfg_path} is not valid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{cfg_path} must contain a YAML mapping at the top level.")
        try:
            if cfg_path.stat().st_mode & 0o077:
                print(
                    f"WARNING: {cfg_path} is readable by other users; "
                    "run chmod 600 on it (it holds an admin API key).",
                    file=sys.stderr,
                )
        except OSError:
            pass
    elif not (os.environ.get("JELLYFIN_URL") and os.environ.get("JELLYFIN_API_KEY")):
        try:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(TEMPLATE)
            cfg_path.chmod(0o600)
        except OSError as exc:
            raise ConfigError(
                f"No config found at {cfg_path} and a template could not be written ({exc}). "
                "Mount a writable /config, or set JELLYFIN_URL and JELLYFIN_API_KEY."
            ) from exc
        raise ConfigError(
            f"No config found, so a template was written to {cfg_path}. "
            "Edit it with your Jellyfin URL and API key, then start the app again."
        )

    jf = data.get("jellyfin") or {}
    app = data.get("app") or {}

    raw_key = os.environ.get("JELLYFIN_API_KEY")
    if raw_key is None:
        raw_key = jf.get("api_key", "")
    if raw_key is not None and not isinstance(raw_key, str):
        raise ConfigError(
            "jellyfin.api_key must be a quoted string in config.yml "
            "(YAML read it as a number, which can silently change the key)."
        )
    api_key = (raw_key or "").strip()
    url = str(os.environ.get("JELLYFIN_URL") or jf.get("url") or "").strip().rstrip("/")

    if not url.startswith(("http://", "https://")) or "example.com" in url:
        raise ConfigError(f"jellyfin.url is not set to a real address (got {url!r}).")
    if not api_key or api_key == PLACEHOLDER_KEY:
        raise ConfigError("jellyfin.api_key is not set.")

    try:
        cache_seconds = int(os.environ.get("CACHE_SECONDS") or app.get("cache_seconds") or 30)
    except (TypeError, ValueError) as exc:
        raise ConfigError("app.cache_seconds must be a whole number.") from exc

    return Config(
        url=url,
        api_key=api_key,
        verify_tls=_as_bool(os.environ.get("JELLYFIN_VERIFY_TLS", jf.get("verify_tls")), True),
        user_id=str(os.environ.get("JELLYFIN_USER_ID") or jf.get("user_id") or "").strip(),
        cache_seconds=max(0, cache_seconds),
    )
