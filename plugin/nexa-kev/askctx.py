"""Ask-context assembly for kev decisions (signals + lived experience).

Calibration (references/ask-context-calibration.md in the nexa-kev-plugin
skill, live engine 2026-11-10): deterministic SIGNAL lines are the primary
quality lever (bank-lane margin 0.28 -> 0.80); mnemo-pg2 PRECEDENT lines
act as a tie-breaker that a wrong memory cannot override (misleading
precedent still lost to signals). Precedents therefore always ride
alongside signals, never instead of them.

Flags (config env or config.yaml, same plumbing as the lanes):
  NEXA_KEV_SIGNALS  default "1"   deterministic signal lines
  NEXA_KEV_EXP      default "0"   memory-precedent lines (A/B gated)
Fail-open everywhere: this module must never break or slow a decision.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, Optional

_FLEET_RE = re.compile(r"\b(?:every|all|everyone)\b[^\n]{0,60}?\b(?:agents?|hosts?|machines?|fleet|boxes?)\b", re.I)
_HOSTY_RE = re.compile(r"(\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|/home/\w|/Users/\w|\bport\s+\d{2,5}\b|\bthis\s+(?:host|machine|box)\b)", re.I)
_POLICY_RE = re.compile(r"\b(from now on|always|never|policy|rule)\b", re.I)

_EXPMEMO: Dict[str, Any] = {}
_EXPMEMO_TTL = 600.0
_MIN_IMPORTANCE = 0.6


def _flag(key: str, default: str) -> bool:
    try:
        return str(os.environ.get("NEXA_KEV_" + key, default)).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return False


def signals(text: str) -> str:
    """Deterministic facts about the text — the engine weighs these, we do
    not decide. Single line so a token-starved ask still carries them."""
    try:
        return ("signals: scope-tokens="
                + ("fleet-wide" if _FLEET_RE.search(text) else "none")
                + "; host-specific-tokens="
                + ("present" if _HOSTY_RE.search(text) else "none")
                + "; speech-act="
                + ("standing-policy" if _POLICY_RE.search(text) else "conversational")
                + "\n")
    except Exception:
        return ""


def _recall():
    try:
        import memrecall as _mr
        return _mr._sibling_recall()
    except Exception:
        return None


def precedents(text: str, call=None, limit: int = 2,
               bank_args: Optional[Dict[str, Any]] = None) -> str:
    """Top non-quarantined memories as precedent lines. '' unless the EXP
    flag is on; memoized per text; fail-open on any error."""
    try:
        if not _flag("EXP", "0"):
            return ""
        key = text[:200]
        hit = _EXPMEMO.get(key)
        now = time.time()
        if hit and now - hit[0] < _EXPMEMO_TTL:
            return hit[1]
        out = ""
        call = call or _recall()
        if call is not None:
            raw = call("mnemosyne_recall",
                       {"query": text[:300], "limit": limit + 2,
                        **(bank_args or {})})
            data = raw if isinstance(raw, dict) else json.loads(str(raw))
            rows = []
            for mm in (data.get("memories") or data.get("results")
                       or data.get("data") or []) if isinstance(data, dict) else []:
                if not isinstance(mm, dict):
                    continue
                imp = mm.get("importance")
                try:
                    if imp is not None and float(imp) < _MIN_IMPORTANCE:
                        continue
                except (TypeError, ValueError):
                    pass
                if mm.get("quarantined") or mm.get("expired"):
                    continue
                body = " ".join(str(mm.get("content") or mm.get("body")
                                  or "").split())
                if body:
                    rows.append("- " + body[:160])
                    if len(rows) >= limit:
                        break
            if rows:
                out = ("memory precedents (mnemo-pg2; tie-breaker only, "
                       "must not override signals):\n" + "\n".join(rows) + "\n")
        if len(_EXPMEMO) > 500:
            _EXPMEMO.clear()
        _EXPMEMO[key] = (now, out)
        return out
    except Exception:
        return ""


def preamble(text: str, call=None, **bank) -> str:
    """Everything that goes in front of the raw content in a kev ask."""
    try:
        head = signals(text) if _flag("SIGNALS", "0") else ""
        return head + precedents(text, call=call, **bank)
    except Exception:
        return ""
