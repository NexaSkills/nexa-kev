"""memrecall — mid-task memory recall on error tool results (v0.4.0, lane L2).

transform_tool_result hook. When a tool call FAILS (or matches an
error/surprise pattern), recall this agent's stored memories for that
failure — private bank first, shared bank on a private miss — and append
a compact "Related memory" trailer to the tool result so the model sees
what it already knows about this exact problem, mid-task, without having
to think to query its own memory.

Design stance:
  * Recall executes through the mnemo-pg2 plugin's OWN dispatch
    (capability probe via sys.modules — try handle_tool_call, then
    _dispatch, on a module anchored in a mnemo-pg2 plugin dir). nexa-kev
    never opens a PG connection of its own: schema/dsn resolution stays
    in mnemo-pg2's single lane (P3L discipline). Sibling missing/erroring
    => lane INERT, one log line.
  * The error lane needs no kev (an error is an unambiguous recall
    trigger). For pattern-only ("ambiguous") results, set
    NEXA_KEV_MEMRECALL_KEVGATE=1 to let a kev ensemble question decide.
  * Budget: NEXA_KEV_MEMRECALL_MAX_SESSION trailers/session (default 2),
    identical-query dedup. Kill: NEXA_KEV_MEMRECALL=0.

Log rows: kind=memrecall (decision: fired | no_hits | silent_budget |
silent_gate | silent_nosibling | silent_query).
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    from . import config as _config
    from . import kev_decide as _kd
except ImportError:  # pragma: no cover - flat dev loading
    import config as _config  # type: ignore
    import kev_decide as _kd  # type: ignore

logger = logging.getLogger("hermes.plugins.nexa-kev")

_ERROR_TEXT_RE = re.compile(
    r"(Traceback \(most recent call last\)"
    r"|\"exit_code\"\s*:\s*[1-9]\d*"
    r"|exit code [1-9]\d*"
    r"|command not found"
    r"|Permission denied"
    r"|No such file or directory"
    r"|ModuleNotFoundError|ImportError"
    r"|HTTP Error 4\d\d|HTTP Error 5\d\d"
    r"|Connection (refused|reset|timed out)"
    r"|Operation timed out"
    r"|command timed out"
    r"|Segmentation fault"
    r"|FAIL(ED|URE)?\b"
    r"|Traceback\b)",
    re.IGNORECASE,
)

_Q_RECALL = (
    "Given this failed/unexpected tool result, is it LIKELY the agent has "
    "stored memories or learned rules relevant to handling THIS kind of "
    "failure (an environment quirk, a known pitfall, a convention about "
    "this tool)? Answer NO for ordinary one-off errors with no plausible "
    "stored knowledge."
)

_FIRED: Dict[str, int] = {}
_QUERY_SEEN: Dict[str, float] = {}
_MAX_Q = 4096


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


# ---------------------------------------------------------------------------
def _sibling_recall():
    """Capability-probe mnemo-pg2's in-process dispatch. Returns a callable
    (tool_name, args) -> json str, or None. Never raises."""
    try:
        for m in list(sys.modules.values()):
            try:
                f = getattr(m, "__file__", None)
                if not isinstance(f, str) or "mnemo-pg2" not in f:
                    continue
                fn = getattr(m, "handle_tool_call", None)
                if callable(fn):
                    def _call(t, a, _f=fn):
                        return _f(t, dict(a))
                    return _call
                fn = getattr(m, "_dispatch", None)
                if callable(fn):
                    def _call(t, a, _f=fn):
                        return _f(t, dict(a))
                    return _call
            except Exception:
                continue
    except Exception:
        pass
    return None


def _recall_hits(call, query: str, bank_args: Dict[str, Any]) -> Tuple[list, bool]:
    """Returns (lines, ok). ok=False means the call itself failed."""
    try:
        raw = call("mnemosyne_recall", {"query": query, "limit": 3, **bank_args})
        data = raw if isinstance(raw, dict) else json.loads(str(raw))
        if not isinstance(data, dict):
            return [], False
        mems = data.get("memories") or data.get("results") or data.get("data") or []
        lines = []
        for mm in mems if isinstance(mems, list) else []:
            body = (mm.get("content") or mm.get("body") or "") if isinstance(mm, dict) else str(mm)
            body = " ".join(str(body).split())
            if body:
                lines.append(body[:240])
        return lines[:3], True
    except Exception:
        return [], False


def _err_line(result_text: str, error_message: str) -> str:
    src = (error_message or result_text or "").strip()
    for line in src.splitlines():
        s = line.strip()
        if s and (_ERROR_TEXT_RE.search(s) or "error" in s.lower()):
            return s[:200]
    return src.splitlines()[0][:200] if src else ""


def transform_tool_result(**kwargs: Any) -> Optional[str]:
    """Return a REPLACEMENT result string (result + trailer) or None.
    NEVER raises; returns None on every skip path."""
    try:
        if not _flag("MEMRECALL"):
            return None
        tool_name = str(kwargs.get("tool_name") or "")
        status = str(kwargs.get("status") or "")
        error_message = str(kwargs.get("error_message") or "")
        result = kwargs.get("result")
        if isinstance(result, str):
            result_text = result
        elif result is None:
            result_text = ""
        else:
            try:
                result_text = json.dumps(result, ensure_ascii=False, default=str)
            except Exception:
                result_text = str(result)
        hard_error = status.lower() in ("error", "failed", "fail") or bool(error_message)
        if not hard_error and not _ERROR_TEXT_RE.search(result_text):
            return None
        session_key = str(kwargs.get("session_id") or "")[-24:]
        import hashlib
        err = _err_line(result_text, error_message)
        qhash = hashlib.sha1(f"{tool_name}|{err}".encode("utf-8", "replace")).hexdigest()[:16]
        if qhash in _QUERY_SEEN:
            return None
        if len(_QUERY_SEEN) > _MAX_Q:
            _QUERY_SEEN.clear()
        _QUERY_SEEN[qhash] = time.time()
        cap = _pick_int("MEMRECALL_MAX_SESSION", 2)
        if _FIRED.get(session_key, 0) >= cap:
            _log({"kind": "memrecall", "decision": "silent_budget",
                  "tool": tool_name, "session": session_key, "ts": time.time()})
            return None
        # optional kev gate for pattern-only (non-hard) matches
        if not hard_error and _flag("MEMRECALL_KEVGATE", "0"):
            r = _kd.decide(
                f"Tool {tool_name or '?'} returned an unusual result: {err}",
                [_Q_RECALL])
            d = (r.get("decisions") or {}).get("q1") or {}
            _s = d.get("spread")
            if not (r.get("status") == "ok" and d.get("verdict") == "SAFE/YES"
                    and (_s is None or float(_s) <= 0.2)):
                _log({"kind": "memrecall", "decision": "silent_gate",
                      "tool": tool_name, "p": d.get("noul"), "ts": time.time()})
                return None
        call = _sibling_recall()
        if call is None:
            _log({"kind": "memrecall", "decision": "silent_nosibling",
                  "tool": tool_name, "ts": time.time()})
            return None
        t0 = time.monotonic()
        query = f"{tool_name} {err}".strip()
        lines, ok = _recall_hits(call, query, {})
        if ok and not lines:
            lines, ok = _recall_hits(call, query, {"bank": "surface"})
        rec: Dict[str, Any] = {
            "kind": "memrecall", "tool": tool_name, "session": session_key,
            "query": query[:120], "hits": len(lines), "hard_error": hard_error,
            "ms": round((time.monotonic() - t0) * 1000, 1), "ts": time.time(),
        }
        if not ok:
            rec["decision"] = "silent_nosibling"
            _log(rec)
            return None
        if not lines:
            rec["decision"] = "no_hits"
            _log(rec)
            return None
        _FIRED[session_key] = _FIRED.get(session_key, 0) + 1
        if len(_FIRED) > 1000:
            _FIRED.clear()
        rec["decision"] = "fired"
        _log(rec)
        trailer = ("\n\n--- Related memory (nexa-kev recall) ---\n"
                   + "\n".join(f"- {ln}" for ln in lines))
        return result_text + trailer
    except Exception as e:
        logger.debug("nexa-kev memrecall error (fail-open): %s", e)
        return None
