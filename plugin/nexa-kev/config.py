"""Configuration for the nexa-kev plugin.

Resolution order for every setting (same pattern as kev-router's config,
independent implementation — no imports from that plugin):
  1. ``NEXA_KEV_*`` environment variables (root/profile ``~/.hermes/.env``
     are loaded into the process env at startup),
  2. ``plugins.entries.nexa-kev.<key>`` in config.yaml,
  3. built-in defaults.

The plugin is inert-but-answerable when unconfigured: defaults point at the
verified gateway endpoints and the API key resolves from the host's existing
nexa-litellm provider key env var, so no configuration is required to use
the tool; a missing key simply yields ``NO DECISION (auth)``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes.plugins.nexa-kev")

PLUGIN_KEY = "nexa-kev"       # manifest name == plugins.entries key
ENV_PREFIX = "NEXA_KEV_"

# Verified 2026-11-06 from this host (HTTP 200 on the health probe).
# Order: LAN gateway -> tailnet gateway -> public HTTPS gateway.
# NOTE: plain HTTP on :4000 and HTTPS on 443 work; HTTPS on :4000 does NOT
# (nothing speaks TLS there — SSL WRONG_VERSION_NUMBER).
DEFAULT_BASE_URLS = (
    "http://192.168.1.10:4000/kev/systemone,"
    "http://100.89.135.14:4000/kev/systemone,"
    "https://ai.nexaskills.co.za/kev/systemone"
)
DEFAULT_KEY_ENV = "HERMES_CUSTOM_AI_NEXASKILLS_CO_ZA_API_KEY"
LEGACY_KEY_ENV = "KEV_ROUTER_API_KEY"  # honoured only if it is actually set


def _cfg_section() -> Dict[str, Any]:
    """Return plugins.entries.nexa-kev ({} when unavailable)."""
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
        cfg = load_config_readonly()
        entries = cfg_get(cfg, "plugins", "entries", default={}) or {}
        sec = entries.get(PLUGIN_KEY)
        return sec if isinstance(sec, dict) else {}
    except Exception:
        return {}


def _env(key: str) -> Optional[str]:
    # dotenv-aware: .env files are NOT loaded into os.environ by disk-lane
    # sessions; consult Hermes' resolver first, then the process env.
    try:
        from hermes_cli.config import get_env_value_prefer_dotenv as _g  # type: ignore
        v = _g("NEXA_KEV_" + key)
        if v and str(v).strip():
            return str(v).strip()
    except Exception:
        pass
    v = os.environ.get("NEXA_KEV_" + key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return v.strip()


def _dotenv_aware(name: str) -> str:
    """Resolve an env var via Hermes' dotenv-aware helper when available
    (disk-lane sessions do NOT load ~/.hermes/.env into os.environ), then
    fall back to the plain process environment."""
    try:
        from hermes_cli.config import get_env_value_prefer_dotenv as _g  # type: ignore
        v = _g(name)
        if v and str(v).strip():
            return str(v).strip()
    except Exception:
        pass
    return (os.environ.get(name) or "").strip()


def _pick(key: str, default: Optional[str] = None) -> Optional[str]:
    """env (uppercase key) -> config entry -> default."""
    v = _env(key)
    if v is not None:
        return v
    sec = _cfg_section()
    if key.lower() in sec and sec[key.lower()] is not None:
        val = sec[key.lower()]
        return str(val) if str(val).strip() != "" else None
    return default


def _as_bool(v: Optional[str], default: bool) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _as_float(v: Optional[str], default: float) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _default_decision_log() -> str:
    try:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    except Exception:
        import os
        from pathlib import Path
        home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    from pathlib import Path
    return str(Path(str(home)) / "plugins-data" / "nexa-kev" / "decisions.jsonl")


def _as_int(v: Optional[str], default: int) -> int:
    try:
        return int(float(v))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


@dataclass
class NexaKevConfig:
    base_urls: List[str]
    api_key: str
    key_source: str          # env-var name the key came from ("" = none)
    model: str
    timeout: float           # per-endpoint HTTP timeout (s)
    deadline: float          # overall wall-clock budget across endpoints (s)
    yes_min: float           # p >= yes_min  -> SAFE/YES
    no_max: float            # p <= no_max   -> UNSAFE/NO
    max_state_chars: int
    hook_enabled: bool       # pre_llm_call advisory on/off (NEXA_KEV_HOOK)
    advisory_min: float      # only list options with prob >= this
    advisory_max_items: int  # max items per hint line
    advisory_max_criteria: int  # max options per choice question (kev token cap)
    advisory_rel_min: float  # relative floor: top_prob x n_options >= this
    decision_log: str        # JSONL advisory log path

    @staticmethod
    def load() -> "NexaKevConfig":
        raw_urls = _pick("BASE_URLS", DEFAULT_BASE_URLS) or DEFAULT_BASE_URLS
        urls = [u.strip() for u in raw_urls.split(",") if u.strip()]
        key_env = _pick("KEY_ENV", DEFAULT_KEY_ENV) or DEFAULT_KEY_ENV
        api_key = _dotenv_aware(key_env)
        key_source = key_env if api_key else ""
        if not api_key:
            legacy = _dotenv_aware(LEGACY_KEY_ENV)
            if legacy:
                api_key = legacy
                key_source = LEGACY_KEY_ENV
        cfg = NexaKevConfig(
            base_urls=urls,
            api_key=api_key,
            key_source=key_source,
            model=_pick("MODEL", "kev") or "kev",
            timeout=_as_float(_pick("TIMEOUT"), 10.0),
            deadline=0.0,  # resolved below
            yes_min=_as_float(_pick("YES_MIN"), 0.7),
            no_max=_as_float(_pick("NO_MAX"), 0.3),
            max_state_chars=_as_int(_pick("MAX_STATE_CHARS"), 1200),
            hook_enabled=_as_bool(_pick("HOOK"), True),
            advisory_min=_as_float(_pick("ADVISORY_MIN"), 0.35),
            advisory_max_items=_as_int(_pick("ADVISORY_MAX_ITEMS"), 5),
            advisory_max_criteria=_as_int(_pick("ADVISORY_MAX_CRITERIA"), 64),
            advisory_rel_min=_as_float(_pick("ADVISORY_REL_MIN"), 4.0),
            decision_log=_pick("DECISION_LOG") or _default_decision_log(),
        )
        # Overall deadline: ~12 s by default; never below a single endpoint's
        # timeout, and never so high that dead fallbacks can stack forever.
        cfg.deadline = _as_float(_pick("DEADLINE"), max(12.0, cfg.timeout + 2.0))
        if cfg.deadline < cfg.timeout:
            cfg.deadline = cfg.timeout
        return cfg

    def resolve_api_key(self) -> "NexaKevConfig":
        """Re-read the key from the env (config object may outlive env setup)."""
        key_env = _pick("KEY_ENV", DEFAULT_KEY_ENV) or DEFAULT_KEY_ENV
        api_key = _dotenv_aware(key_env)
        if api_key:
            self.api_key, self.key_source = api_key, key_env
        else:
            legacy = _dotenv_aware(LEGACY_KEY_ENV)
            if legacy:
                self.api_key, self.key_source = legacy, LEGACY_KEY_ENV
            else:
                self.api_key, self.key_source = "", ""
        return self
