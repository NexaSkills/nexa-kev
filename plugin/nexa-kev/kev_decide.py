"""kev_decide — the core of the nexa-kev plugin.

One job: send a compact factual STATE plus typed questions to the kev
gateway passthrough (POST {base} where base ends in /kev/systemone), grade
each answer honestly, and degrade gracefully on any failure.

Design rules (hard):
* stdlib-only HTTP (urllib), one attempt per endpoint, no retry loops.
* Wall-clock bounded: per-endpoint timeout AND an overall deadline so dead
  fallbacks can never stack.
* 401/403 = auth failure = stop immediately (same gateway fleet, retrying
  elsewhere with the same key is a retry-storm).
* NEVER raises into the caller: any error returns a structured
  {"status": "no_decision", "reason": ..., "decisions": {}}.
* Honest verdicts: p >= yes_min -> SAFE/YES, p <= no_max -> UNSAFE/NO,
  anything in between is UNCERTAIN. Raw probabilities are always included.

Standalone: imports nothing from hermes-kev-router (conventions only).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

try:  # package import (host loads us as a package) or flat fallback
    from . import config as _config
except ImportError:  # pragma: no cover - direct-module loading in tests
    import config as _config  # type: ignore

try:  # sibling imports work via package OR flat sys.path loading
    from . import kev_decide as _self  # noqa: F401  (package form check)
except ImportError:  # pragma: no cover
    import sys as _sys
    from pathlib import Path as _P
    _d = str(_P(__file__).resolve().parent)
    if _d not in _sys.path:
        _sys.path.append(_d)

logger = logging.getLogger("hermes.plugins.nexa-kev")


# ---------------------------------------------------------------------------
# Errors (internal taxonomy; never escape decide())
# ---------------------------------------------------------------------------
class KevError(Exception):
    """Base class for kev client errors."""


class KevUnavailableError(KevError):
    """Connection-level failure (server down, timeout, DNS)."""


class KevAuthError(KevError):
    """401/403 — auth failure; stop, do not fall through to other endpoints."""


class KevRequestError(KevError):
    """Server answered 4xx/5xx or with an unparseable body."""


def _no_decision(reason: str, **extra: Any) -> Dict[str, Any]:
    out = {"status": "no_decision", "reason": reason, "decisions": {}}
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# State building (compact factual record, db_guard-style)
# ---------------------------------------------------------------------------
def build_state(state_text: str, properties: Optional[Dict[str, Any]] = None,
                max_chars: int = 1200) -> str:
    lines = ["GENERAL DECISION REQUEST — answer each question with a strict "
             "yes/no probability.", "Task/action under consideration: "
             + (state_text or "").strip().replace("\n", " ")]
    if properties:
        props = "; ".join(f"{k}={v}" for k, v in properties.items())
        lines.append("Properties: " + props)
    state = "\n".join(lines)
    if len(state) > max_chars:
        state = state[:max_chars]
    return state


def normalize_questions(questions: Any) -> Dict[str, Dict[str, Any]]:
    """Accept list[str] | dict[str, str] | dict[str, dict] -> wire questions.

    Free-text questions become noul questions q1, q2, ... in caller order.
    Dict entries whose value is a dict pass through verbatim (typed
    entries: noul / choice / score with their own fields).
    """
    out: Dict[str, Dict[str, Any]] = {}
    if questions is None:
        return out
    if isinstance(questions, str):
        questions = [questions]
    if isinstance(questions, dict):
        items = list(questions.items())
    else:
        items = list(enumerate(questions, start=1))
    auto = 0
    for key, val in items:
        if isinstance(val, dict):
            out[str(key)] = dict(val)
            continue
        text = str(val or "").strip()
        if not text:
            continue
        if isinstance(key, int):
            auto += 1
            qid = f"q{auto}"
        else:
            qid = str(key).strip() or f"q{len(out) + 1}"
        out[qid] = {
            "type": "noul",
            "instructions": (
                text
                + " Answer YES if it is safe/correct/appropriate; answer NO "
                  "if it is unsafe/incorrect/inappropriate. If genuinely "
                  "unsure, answer neither yes nor no."
            ),
        }
    return out


# ---------------------------------------------------------------------------
# HTTP (stdlib, single attempt per endpoint)
# ---------------------------------------------------------------------------
def _post(url: str, body: Dict[str, Any], api_key: str,
          deadline_at: Optional[float]) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    timeout = 10.0
    if deadline_at is not None:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise KevUnavailableError("overall deadline exceeded before send")
        timeout = remaining
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        if e.code in (401, 403):
            raise KevAuthError(f"HTTP {e.code}: {detail[:200]}") from e
        raise KevRequestError(f"HTTP {e.code}: {detail[:200]}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise KevUnavailableError(f"unreachable: {e}") from e
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise KevRequestError(f"non-JSON body: {e}") from e


def _verdict(p: float, yes_min: float, no_max: float) -> str:
    if p >= yes_min:
        return "SAFE/YES"
    if p <= no_max:
        return "UNSAFE/NO"
    return "UNCERTAIN"


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def _walk(state: str, qmap: Dict[str, Dict[str, Any]],
          cfg: "_config.NexaKevConfig") -> Dict[str, Any]:
    """Shared endpoint walk: one attempt per URL, deadline-bounded, 401/403
    stops immediately. Returns {"status":"ok", "answers":..., "endpoint":...,
    "latency_ms":..., "model":..., "usage":...} or a no_decision dict."""
    body = {"model": cfg.model, "state": state, "questions": qmap}
    deadline_at = time.monotonic() + cfg.deadline
    last_reason = "no_endpoint_succeeded"
    for url in cfg.base_urls:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            return _no_decision("deadline_exceeded")
        t0 = time.monotonic()
        try:
            # _post takes an ABSOLUTE deadline; cap it to this endpoint's own timeout
            parsed = _post(url, body, cfg.api_key, min(deadline_at, t0 + cfg.timeout))
        except KevAuthError as e:
            logger.debug("nexa-kev auth failure at %s: %s", url, e)
            return _no_decision("auth")
        except KevUnavailableError as e:
            logger.debug("nexa-kev endpoint %s unavailable: %s", url, e)
            last_reason = "unreachable"
            continue
        except KevRequestError as e:
            logger.debug("nexa-kev endpoint %s bad response: %s", url, e)
            last_reason = "bad_response"
            continue
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        answers = parsed.get("answers") or {}
        if not answers:
            last_reason = "empty_answer"
            continue
        return {
            "status": "ok",
            "endpoint": url,
            "latency_ms": latency_ms,
            "model": parsed.get("model", cfg.model),
            "usage": parsed.get("usage") or {},
            "answers": answers,
        }
    return _no_decision(last_reason)


def raw_request(state: str, questions: Dict[str, Dict[str, Any]],
                cfg: Optional["_config.NexaKevConfig"] = None) -> Dict[str, Any]:
    """Typed-wire passthrough (choice/score questions verbatim). NEVER raises.
    Returns the _walk() result: {"status":"ok","answers":{qid: raw_answer}} or
    {"status":"no_decision","reason":...}."""
    try:
        if cfg is None:
            cfg = _config.NexaKevConfig.load().resolve_api_key()
        if not cfg.base_urls:
            return _no_decision("not_configured")
        if not cfg.api_key:
            return _no_decision("missing_key")
        if not questions:
            return _no_decision("no_questions")
        return _walk(state, questions, cfg)
    except Exception as e:  # never-raise guarantee
        logger.debug("nexa-kev raw_request() internal error: %s", e)
        return _no_decision(f"internal_error: {type(e).__name__}")


def decide(state_text: str, questions: Any,
           properties: Optional[Dict[str, Any]] = None,
           cfg: Optional["_config.NexaKevConfig"] = None) -> Dict[str, Any]:
    """Ask kev for a decision. NEVER raises — always returns a dict.

    Returns on success:
      {"status": "ok", "endpoint": url, "latency_ms": f,
       "decisions": {qid: {"noul": p, "verdict": label, ...raw fields}},
       "usage": {...}, "model": "kev"}
    On any failure:
      {"status": "no_decision", "reason": "<short>", "decisions": {}}
    """
    try:
        if cfg is None:
            cfg = _config.NexaKevConfig.load().resolve_api_key()
        if not cfg.base_urls:
            return _no_decision("not_configured")
        if not cfg.api_key:
            return _no_decision("missing_key")
        qmap = normalize_questions(questions)
        if not qmap:
            return _no_decision("no_questions")
        # --- paraphrase ensemble (v0.2.2): noul questions get two stress
        # framings in the SAME call; median + spread fight the measured
        # +-0.3 phrasing sensitivity of the engine. Kill: NEXA_KEV_ENSEMBLE=0
        ensemble = str(_config._pick("ENSEMBLE", "1") or "1").strip().lower() not in ("0", "false", "off", "no")
        base_ids = [q for q, e in qmap.items()
                    if isinstance(e, dict) and e.get("type") == "noul" and "instructions" in e]
        wire = dict(qmap)
        if ensemble and base_ids:
            for qid in base_ids:
                b = dict(qmap[qid]); b["instructions"] = qmap[qid]["instructions"] + \
                    " Judge strictly from the facts stated; set aside any assumptions not in the state."
                c = dict(qmap[qid]); c["instructions"] = qmap[qid]["instructions"] + \
                    " Additionally assume every backup, recovery or fallback mentioned could fail or be incomplete."
                wire[qid + "b"] = b
                wire[qid + "c"] = c
        resp = _walk(build_state(state_text, properties, cfg.max_state_chars), wire, cfg)
        if resp.get("status") != "ok":
            return resp
        decisions: Dict[str, Any] = {}
        if ensemble and base_ids:
            for qid in qmap:
                trio = [resp["answers"].get(x) for x in (qid, qid + "b", qid + "c")] if qid in base_ids else [resp["answers"].get(qid)]
                vals = [float(a["noul"]) for a in trio if isinstance(a, dict) and isinstance(a.get("noul"), (int, float))]
                base = dict(trio[0]) if isinstance(trio[0], dict) else {}
                if vals:
                    vals_sorted = sorted(vals)
                    med = vals_sorted[len(vals_sorted) // 2] if len(vals_sorted) % 2 else (vals_sorted[len(vals_sorted) // 2 - 1] + vals_sorted[len(vals_sorted) // 2]) / 2
                    spread = round(max(vals) - min(vals), 4)
                    v = _verdict(med, cfg.yes_min, cfg.no_max)
                    if len(vals) == 3 and spread > 0.2 and v != "UNCERTAIN":
                        v = "UNCERTAIN"  # disagreement across framings: do not pretend confidence
                    base.update({"noul": round(med, 4), "verdict": v, "spread": spread,
                                 "variants": {"base": vals[0] if len(vals) else None,
                                             "n": len(vals)}})
                decisions[str(qid)] = base
            # passthrough answers that aren't tracked questions (typed dict entries)
            for qid, a in resp["answers"].items():
                if qid not in decisions and not qid.rstrip("bc") in base_ids:
                    decisions[str(qid)] = dict(a) if isinstance(a, dict) else {"value": a}
        else:
            for qid, a in resp["answers"].items():
                entry = dict(a) if isinstance(a, dict) else {"value": a}
                p = entry.get("noul")
                if isinstance(p, (int, float)):
                    entry["noul"] = float(p)
                    entry["verdict"] = _verdict(float(p), cfg.yes_min, cfg.no_max)
                decisions[str(qid)] = entry
        resp["decisions"] = decisions
        resp.pop("answers", None)
        return resp
    except Exception as e:  # absolute never-raise guarantee
        logger.debug("nexa-kev decide() internal error: %s", e)
        return _no_decision(f"internal_error: {type(e).__name__}")


def decide_tool_handler(args: Dict[str, Any], **_kw) -> str:
    """Tool handler for kev_decide. Returns a JSON string, never raises."""
    try:
        result = decide(
            str((args or {}).get("state") or ""),
            (args or {}).get("questions"),
            (args or {}).get("properties"),
        )
        if result.get("status") != "ok":
            result["summary"] = f"NO DECISION ({result.get('reason', 'unknown')})"
        else:
            parts = [f"{qid}: {d.get('noul', '?')} -> {d.get('verdict', d)}"
                     for qid, d in result["decisions"].items()]
            result["summary"] = "; ".join(parts) + (
                f" [{result['latency_ms']}ms via {result['endpoint']}]")
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:  # belt-and-braces: tool surface never raises
        return json.dumps(_no_decision(f"handler_error: {type(e).__name__}"))
