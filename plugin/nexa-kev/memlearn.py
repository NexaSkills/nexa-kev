"""memlearn — kev-gated learning-moment capture (v0.4.0, lane L1).

pre_llm_call policy/injection hook. When the user makes a DURABLE
statement — a new rule, a correction, an approval of the form
"yes, that was good, do it like that from now on" — kev decides, per
message, whether it is worth persisting across sessions (Q1) and whether
it belongs in the shared (fleet-wide) or the agent's private bank (Q2,
same criteria text as memroute). Confident YES injects a SAVE DIRECTIVE
into the user message context; the AGENT performs the write (tool
surface keeps audit_log + P3L binding + memroute's shared gates intact).

Cost model: a local lexical prefilter (0 ms, no gateway) decides whether
the message even looks like a learning moment; misses are fully silent.
Candidates pay one kev ensemble decide() call (3 framings, median
verdict; spread > 0.2 demotes to UNCERTAIN = silent).

Log rows: kind=memlearn (decision: fired | fired_shadow | silent_no |
silent_uncertain | silent_gate | capped). Shadow mode
(NEXA_KEV_MEMLEARN_SHADOW=1) runs the full decision chain, logs it, and
injects nothing — the rollout soak mode. Kill: NEXA_KEV_MEMLEARN=0.

Vendor-neutrality: public plugin hook API only; every host field is read
defensively (.get with fallbacks); never raises into the turn loop.
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

# ---------------------------------------------------------------------------
# Speech-act prefilter (over-inclusive by design: kev gates the verdict).

_LEARN_RE = re.compile(
    r"""\b(from\s+now\s+on|going\s+forward|from\s+here\s+(in|on)|next\s+time|
        forever\s+more|in\s+all\s+future\s+sessions|
        that\s+was\s+(good|right|correct|perfect|great|nice)|
        well\s+done|exactly\s+right|nail(ed)?\s+it|
        do(n'?t|not)\s+do\s+it\s+like|stop\s+doing|stop\s+using|
        always\s+(remember|use|prefer|do|check|save|run|ask|reply|respond)|
        never\s+(again\s+)?(do|use|say|run|call|save|reply|trust)|
        remember\s+(that|this|to|from|these|these\s+rules?)|
        keep\s+(this|these|it)\s+(in\s+mind|saved)|
        make\s+(it\s+)?a\s+rule|rule\s+(of\s+thumb|is|[:\-])|
        prefer\s+(this|that|it|the|using|to\s+use)|
        that'?s\s+(the\s+)?(right|correct|way\s+to\s+do\s+it)|
        the\s+(right|correct|proper)\s+way\s+is|
        i\s+(like|prefer|want|need)\s+you\s+to|
        in\s+future\s+(sessions?|conversations?)|
        whenever\s+you\s+(see|get|hit|encounter))\b""",
    re.IGNORECASE | re.VERBOSE,
)

_Q_SAVE = (
    "Does this user message state (or approve) a DURABLE rule, preference, "
    "correction or convention the agent should follow in FUTURE sessions, "
    "not just today? Approvals like 'yes, that was good, do it like that "
    "from now on' count. One-off requests, today-only facts, chit-chat and "
    "question-answering do not."
)
try:
    _BANK_CRITERIA = _config.BANK_CRITERIA
except AttributeError:  # pragma: no cover - older config
    _BANK_CRITERIA = ("Shared is only for facts that are true and useful "
                      "for every agent; agent-specific facts are private.")
_Q_BANK = (
    "Would the rule or fact in this message be true and useful for EVERY "
    "agent and machine in the fleet (shared bank), or is it specific to "
    "this one agent (private bank)? " + _BANK_CRITERIA
)

# In-process dedup/budget state (thread safety: dict ops we use are atomic
# under the GIL; a rare lost/duplicate directive is harmless by design).
_SEEN: Dict[str, float] = {}          # message-hash -> ts
_FIRED: Dict[str, int] = {}            # session_key  -> directives fired
_MAX_SEEN = 4096


def _flag(key: str, default: str = "1") -> bool:
    try:
        v = str(_config._pick(key, default) or default).strip().lower()
    except Exception:
        v = default
    return v not in ("0", "false", "off", "no")


def _pick_int(key: str, default: int) -> int:
    try:
        return int(float(str(_config._pick(key, str(default)) or default)))
    except Exception:
        return default


def _log(rec: Dict[str, Any]) -> None:
    try:
        d = Path(_config._pick("PROFILE_HOME", "") or Path.home() / ".hermes")
        d = d / "plugins-data" / "nexa-kev"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "decisions.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _flatten(msg: Any) -> str:
    """Best-effort text of a message payload (str | list-of-parts | dict)."""
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        out = []
        for p in msg:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict):
                t = p.get("text") or p.get("content")
                if isinstance(t, str):
                    out.append(t)
        return " ".join(out)
    if isinstance(msg, dict):
        return _flatten(msg.get("content"))
    return ""


def _last_assistant_tail(history: Any, limit: int = 500) -> str:
    if not isinstance(history, list):
        return ""
    for m in reversed(history[-8:]):
        if isinstance(m, dict) and m.get("role") == "assistant":
            t = _flatten(m.get("content")).strip()
            if t:
                return t[-limit:]
    return ""


def _directive(p_save: float, shared: bool, user_msg: str) -> str:
    bank = "SHARED (all agents)" if shared else "PRIVATE"
    tool = "mnemosyne_shared_remember" if shared else "mnemosyne_remember"
    rule = " ".join(user_msg.split())[:220]
    return (
        f"🧠 [nexa-kev memlearn] LEARNING MOMENT detected "
        f"(kev p={p_save:.2f}, bank={bank}). Before continuing, persist "
        f"this:\n"
        f"1) mnemosyne_recall for an existing similar memory — if one "
        f"exists, mnemosyne_update it instead of adding a duplicate.\n"
        f"2) Otherwise save ONE declarative rule sentence (state the rule, "
        f"not the incident) with {tool} "
        f"(importance 0.7, scope persistent).\n"
        f"Rule as stated: \"{rule}\""
    )


def _agree(d: Dict[str, Any], max_spread: float = 0.2) -> bool:
    """Ensemble agreement check. spread==0.0 means PERFECT agreement (must
    not be confused with a missing spread — `d.get('spread') or 1` is a
    classic trap: 0.0 or 1 == 1). Missing spread (ENSEMBLE=0, single
    framing) is treated as agreement; confidence then rests on the verdict
    alone."""
    s = d.get("spread")
    try:
        return s is None or float(s) <= max_spread
    except (TypeError, ValueError):
        return False


def pre_llm_call(**kwargs: Any) -> Optional[Dict[str, str]]:
    """Return {'context': directive} or None. NEVER raises."""
    try:
        if not _flag("MEMLEARN"):
            return None
        user_msg = (kwargs.get("user_message")
                    or kwargs.get("message") or "")
        if not isinstance(user_msg, str):
            user_msg = _flatten(user_msg)
        user_msg = user_msg.strip()
        if len(user_msg) < 12:
            return None
        import hashlib
        session_key = str(kwargs.get("session_id") or "")[-24:]
        h = hashlib.sha1(user_msg.encode("utf-8", "replace")).hexdigest()[:16]
        if h in _SEEN:
            return None
        if len(_SEEN) > _MAX_SEEN:
            _SEEN.clear()
        _SEEN[h] = time.time()
        if not _LEARN_RE.search(user_msg):
            return None            # prefilter miss: zero gateway cost
        # budget: directives per session (learning moments are rare)
        cap = _pick_int("MEMLEARN_MAX_SESSION", 3)
        if _FIRED.get(session_key, 0) >= cap:
            _log({"kind": "memlearn", "decision": "capped",
                  "session": session_key, "ts": time.time()})
            return None
        state = (f"User message:\n{user_msg[:900]}"
                 + (f"\n\nAssistant message just before it (context for "
                    f"what the approval/correction refers to):\n"
                    f"{_last_assistant_tail(kwargs.get('conversation_history'))}"
                    if isinstance(kwargs.get("conversation_history"), list)
                    else ""))
        r = _kd.decide(state, [_Q_SAVE, _Q_BANK])
        ds = r.get("decisions") or {}
        d1 = ds.get("q1") or {}
        d2 = ds.get("q2") or {}
        t0 = time.monotonic()
        rec: Dict[str, Any] = {
            "kind": "memlearn", "ts": time.time(),
            "session": session_key, "msg": user_msg[:80],
            "p_save": d1.get("noul"), "spread": d1.get("spread"),
            "p_shared": d2.get("noul"), "spread_shared": d2.get("spread"),
            "status": r.get("status"),
        }
        if r.get("status") != "ok":
            rec["decision"] = f"silent_gate_{r.get('reason')}"
            _log(rec)
            return None
        if d1.get("verdict") != "SAFE/YES" or not _agree(d1):
            rec["decision"] = ("silent_uncertain" if d1.get("verdict") == "UNCERTAIN"
                               else "silent_no")
            _log(rec)
            return None
        shared = d2.get("verdict") == "SAFE/YES" and _agree(d2)   # unsure -> private
        _FIRED[session_key] = _FIRED.get(session_key, 0) + 1
        if len(_FIRED) > 1000:
            _FIRED.clear()
        rec["bank"] = "shared" if shared else "private"
        rec["ms"] = round((time.monotonic() - t0) * 1000, 1)
        if _flag("MEMLEARN_SHADOW", "0"):
            rec["decision"] = "fired_shadow"
            _log(rec)
            return None
        rec["decision"] = "fired"
        _log(rec)
        return {"context": _directive(float(d1.get("noul") or 0), shared, user_msg)}
    except Exception as e:  # never break a turn over a maybe-memory
        logger.debug("nexa-kev memlearn error (fail-open): %s", e)
        try:
            _log({"kind": "memlearn", "decision": f"error:{type(e).__name__}",
                  "err": str(e)[:160], "ts": time.time()})
        except Exception:
            pass
        return None
