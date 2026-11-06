"""memroute — kev-gated shared/private memory routing (v0.3.0).

pre_tool_call policy hook for mnemo-pg2 memory SAVES:
  * shared-bank saves (mnemosyne_shared_remember[_canonical],
    mnemosyne_remember_canonical) get two gates:
      1. hard secret-pattern gate (no kev, never optional):
         secrets/keys never belong in the fleet-wide shared bank.
      2. kev ensemble question "should this stay PRIVATE?" (3 framings,
         median verdict via kev_decide). Confident YES + agreement ->
         BLOCK with an actionable message telling the agent to re-save
         into its private bank.
  * private saves are never touched (private is the safe default).
Fail-open on ANY error/uncertainty: the save proceeds unchanged.
Kill-switch: NEXA_KEV_MEMROUTE=0. Recall-side hinting is documented
convention for now (post_tool_call contract not yet verified).
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from . import config as _config
    from . import kev_decide as _kd
except ImportError:  # pragma: no cover - flat dev loading
    import config as _config  # type: ignore
    import kev_decide as _kd  # type: ignore

logger = logging.getLogger("hermes.plugins.nexa-kev")

SHARED_TOOLS = {
    "mnemosyne_shared_remember",
    "mnemosyne_shared_remember_canonical",
    "mnemosyne_remember_canonical",
}

_SECRET_RE = re.compile(
    r"(BEGIN [A-Z ]*PRIVATE KEY|sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}"
    r"|gho_[A-Za-z0-9]{20,}|xox[bap]-[A-Za-z0-9-]{10,}"
    r"|password\s*[=:]|api[_-]?key\s*[=:]|Bearer\s+[A-Za-z0-9_.\-]{20,})",
    re.IGNORECASE,
)

# agent-scoped markers: content talking about ONE agent's own habits/history
_AGENT_SCOPED_HINT = (
    "The fact below was proposed for the SHARED (all-agents) memory bank. "
    "Should it instead stay in the saving agent's PRIVATE bank? "
    "(Shared is only for facts that are true and useful for every agent: "
    "fleet conventions, shared infrastructure, cross-agent proofs. Facts "
    "about one agent's own habits, history, preferences, tasks or "
    "conclusions belong in private.)\nContent: "
)


def _log(profile_home: Path, record: Dict[str, Any]) -> None:
    try:
        d = profile_home / "plugins-data" / "nexa-kev"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "decisions.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _enabled() -> bool:
    try:
        v = str(_config._pick("MEMROUTE", "1") or "1").strip().lower()
    except Exception:
        v = "1"
    return v not in ("0", "false", "off", "no")


def pre_tool_call(tool_name: str = "", args: Optional[Dict[str, Any]] = None, **_kw):
    """Return None (proceed) or {'action':'block','message':...}. Never raises."""
    try:
        if not tool_name or tool_name not in SHARED_TOOLS:
            return None
        if not _enabled():
            return None
        args = args if isinstance(args, dict) else {}
        content = str(args.get("content") or args.get("body") or "")
        if not content.strip():
            return None
        t0 = time.monotonic()
        # gate 1: hard secret scan
        if _SECRET_RE.search(content):
            msg = ("BLOCKED by nexa-kev memory-router: content matches a "
                   "secret/credential pattern; the shared bank is fleet-wide. "
                   "Re-save privately (mnemosyne_remember) or strip the secret.")
            _log(Path(_config._pick("PROFILE_HOME", "") or Path.home() / ".hermes"), {
                "kind": "memroute", "tool": tool_name, "decision": "block_secret",
                "ms": round((time.monotonic() - t0) * 1000, 1), "ts": time.time()})
            return {"action": "block", "message": msg}
        # gate 2: kev ensemble — should this stay private?
        r = _kd.decide(_AGENT_SCOPED_HINT + content[:900],
                       ["Should this memory stay in the agent's PRIVATE bank "
                        "rather than the shared bank?"])
        d = (r.get("decisions") or {}).get("q1") or {}
        rec = {"kind": "memroute", "tool": tool_name, "ts": time.time(),
               "noul": d.get("noul"), "spread": d.get("spread"),
               "ms": round((time.monotonic() - t0) * 1000, 1)}
        if r.get("status") == "ok" and d.get("verdict") == "SAFE/YES" and float(d.get("spread") or 1) <= 0.2:
            rec["decision"] = "block_private"
            _log(Path(_config._pick("PROFILE_HOME", "") or Path.home() / ".hermes"), rec)
            return {"action": "block", "message": (
                "BLOCKED by nexa-kev memory-router (ensemble agreement): this "
                "fact reads as agent-scoped, not fleet-wide. Re-save into your "
                "PRIVATE bank (mnemosyne_remember). If it truly generalizes to "
                "every agent, rewrite the content to state the general rule "
                "and retry.")}
        rec["decision"] = "allow" if r.get("status") == "ok" else f"allow_{r.get('reason')}"
        _log(Path(_config._pick("PROFILE_HOME", "") or Path.home() / ".hermes"), rec)
        return None
    except Exception as e:  # never block due to our own fault
        logger.debug("nexa-kev memroute error (fail-open): %s", e)
        return None
