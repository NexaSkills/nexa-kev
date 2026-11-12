"""advisory — pre_llm_call auto-routing for nexa-kev (no user mention needed).

Fires once per distinct user message per session: asks kev two `choice`
questions in ONE gateway request (best skill to load, best first tool),
then injects a one-line advisory into the turn context. Fail-open
everywhere: any error, timeout, or kev outage yields a silent None and the
turn proceeds untouched.

Conventions borrowed from kev-router (structure, not code): once-per-
message dedup, compact state, echo marker first, never-raise hooks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from typing import Any, Dict, Optional

try:
    from . import config as _config
    from . import kev_decide as _kd
except ImportError:  # pragma: no cover - flat dev loading
    import config as _config  # type: ignore
    import kev_decide as _kd  # type: ignore

logger = logging.getLogger("hermes.plugins.nexa-kev")

# session_key -> {msg_hash: True}; bounded.
_routed: Dict[str, Dict[str, bool]] = {}


def _load_skills() -> Dict[str, str]:
    """skill_name -> short description (host prompt builder first, dir scan fallback)."""
    skills: Dict[str, str] = {}
    try:
        from agent.prompt_builder import build_skills_system_prompt  # type: ignore
        prompt = build_skills_system_prompt()
        if prompt:
            for line in prompt.splitlines():
                m = re.match(r"^\s*(?:-\s+|\d+\.\s*)([A-Za-z0-9_\-]+):\s*(.+)$", line)
                if m:
                    skills[m.group(1)] = m.group(2).strip()[:110]
            if skills:
                return skills
    except Exception:
        pass
    # Fallback: scan this profile's skills dirs for SKILL.md frontmatter names.
    try:
        from pathlib import Path
        roots = []
        try:
            from hermes_constants import get_hermes_home
            roots.append(Path(get_hermes_home()) / "skills")
        except Exception:
            pass
        import os
        roots.append(Path(os.path.expanduser("~/.hermes/skills")))
        seen = set()
        for root in roots:
            if not root.is_dir() or str(root) in seen:
                continue
            seen.add(str(root))
            for md in root.glob("*/SKILL.md"):
                try:
                    text = md.read_text(encoding="utf-8", errors="replace")[:1500]
                    nm = re.search(r"^name:\s*(.+)$", text, re.M)
                    ds = re.search(r"^description:\s*(.+)$", text, re.M)
                    if nm:
                        skills[nm.group(1).strip()] = (ds.group(1).strip() if ds else "")[:110]
                except Exception:
                    continue
    except Exception:
        pass
    return skills


def _load_tools() -> Dict[str, str]:
    """tool_name -> short description, from the live registry (union-safe: an
    advisory naming an unavailable tool is simply ignored by the model)."""
    tools: Dict[str, str] = {}
    try:
        from tools.registry import registry
        for d in registry.get_definitions(registry.get_all_tool_names(), quiet=True):
            fn = d.get("function") or {}
            name = fn.get("name")
            if name:
                tools[name] = (fn.get("description") or "").strip().replace("\n", " ")[:110]
    except Exception:
        pass
    return tools


def _hint(prefix: str, probs: Dict[str, float], cfg) -> str:
    ranked = sorted(((k, v) for k, v in (probs or {}).items()
                     if k != "none" and isinstance(v, (int, float))
                     and v >= cfg.advisory_min),
                    key=lambda kv: -kv[1])[:cfg.advisory_max_items]
    if not ranked:
        return ""
    return prefix + ", ".join(f"{k} ({v:.2f})" for k, v in ranked)


def _hint_rel(prefix: str, probs: Dict[str, float], cfg) -> str:
    """Relative-confidence fallback: with N options the uniform baseline is
    1/N, so a top pick >= advisory_rel_min x uniform IS informative even when
    the absolute prob looks small (softmax dilution over large catalogs)."""
    vals = [(k, v) for k, v in (probs or {}).items()
            if k != "none" and isinstance(v, (int, float))]
    if not vals:
        return ""
    n = len(vals) + 1
    top_k, top_v = max(vals, key=lambda kv: kv[1])
    if top_v * n < cfg.advisory_rel_min:
        return ""
    ranked = sorted(vals, key=lambda kv: -kv[1])[:cfg.advisory_max_items]
    return prefix + ", ".join(
        f"{k} ({v:.2f})" for k, v in ranked if v * n >= cfg.advisory_rel_min or k == top_k)


_STOP = {"the", "with", "from", "into", "that", "this", "your", "have", "what",
         "which", "when", "then", "them", "there", "these", "those", "using",
         "would", "should", "could", "about", "after", "before", "please", "and",
         "for", "are", "was", "were", "been", "being", "its", "you", "can",
         "will", "not", "but", "all", "any", "get", "let", "put", "out", "how"}


def _prefilter(text: str, items: Dict[str, str], cap: int) -> Dict[str, str]:
    """Cap the criteria set for kev's per-question token limit: rank items by
    crude lexical overlap with the task (name+description vs message words),
    keep top `cap`. Deterministic: score desc, then name asc. 'none' is added
    by the caller, not here."""
    if len(items) <= cap:
        return items
    words = {w for w in "".join(c if c.isalnum() else " " for c in text.lower()).split()
             if len(w) >= 3 and w not in _STOP}
    scored = []
    for name, desc in items.items():
        hay = (name + " " + (desc or "")).lower().replace("_", " ").replace("-", " ")
        toks = {w for w in "".join(c if c.isalnum() else " " for c in hay).split()}
        score = len(words & toks)
        scored.append((-score, name))
    scored.sort()
    return {name: items[name] for _, name in scored[:cap]}


def _log_decision(cfg: Any, **entry: Any) -> None:
    try:
        from pathlib import Path
        path = cfg.decision_log
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        entry["ts"] = time.time()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---- outcome tracking -------------------------------------------------
# After an advisory fires we watch the SAME session for proof the agent
# took (or ignored) the advice: a skill_view of a suggested skill counts
# as used=1; any tool error before expiry counts as later_error=1. One
# "advisory_outcome" row per fired advisory = ranking-quality curve for
# free (and feeds the usage prior into later asks).
_PENDING: Dict[str, Dict[str, Any]] = {}
_PENDING_MAX = 200
_PENDING_MAX_TURNS = 8
_PENDING_MAX_AGE = 900.0
_ERR_RE = re.compile(
    r"(command not found|No such file|Permission denied|Traceback|"
    r"exit code [1-9]\d*|error:|failed)", re.I)


def _flush_all() -> None:
    """Emit pending outcomes at process exit (one-shot -z sessions never
    reach the 8-turn/900s expiry ticks)."""
    try:
        cfg = _config.NexaKevConfig.load()
    except Exception:
        cfg = None
    for key in list(_PENDING):
        pend = _PENDING.pop(key)
        try:
            _log_decision(cfg, kind="advisory_outcome",
                          session=pend.get("session"),
                          suggested=sorted(pend["skills"])[:3],
                          used=pend.get("used") or 0,
                          tools=sorted(pend.get("tools") or [])[:3],
                          used_tool=pend.get("used_tool") or 0,
                          later_error=1 if pend.get("err") else 0,
                          turns=pend.get("turns", 0), exit_flush=True)
        except Exception:
            pass


import atexit as _atexit
_atexit.register(_flush_all)


def _emit(cfg: Any, key: str, pend: Dict[str, Any]) -> None:
    try:
        _log_decision(cfg, kind="advisory_outcome", session=pend.get("session"),
                      suggested=sorted(pend["skills"])[:3],
                      used=pend.get("used") or 0,
                      tools=sorted(pend.get("tools") or [])[:3],
                      used_tool=pend.get("used_tool") or 0,
                      later_error=1 if pend.get("err") else 0,
                      turns=pend.get("turns", 0),
                      age_s=round(time.time() - pend["ts"], 1))
    except Exception:
        pass
    _PENDING.pop(key, None)


def _tick(session_key: str, cfg: Any) -> None:
    pend = _PENDING.get(session_key[-64:])
    if not pend:
        return
    pend["turns"] = pend.get("turns", 0) + 1
    if (pend["turns"] > _PENDING_MAX_TURNS
            or time.time() - pend["ts"] > _PENDING_MAX_AGE):
        _emit(cfg, session_key[-64:], pend)


def note_tool_call(session_key: str, tool_name: str, args_text: str,
                   result_text: str) -> None:
    """Called (fail-open) from memrecall's transform for EVERY tool result."""
    try:
        pend = _PENDING.get((session_key or "default")[-64:])
        if not pend:
            return
        pend["notes"] = pend.get("notes", 0) + 1
        # Tool-lane adoption: the agent called a tool the advisory NAMED,
        # under its exact registry name. (Bridge describe can render plugin
        # tool schemas empty, so an unnamed call is invisible to the model;
        # a call of a named tool is proof the steering worked.)
        if tool_name and pend.get("tools") and tool_name in pend["tools"]:
            pend["used_tool"] = tool_name
            _emit(_config.NexaKevConfig.load(),
                  (session_key or "default")[-64:], pend)
            return
        low = ((args_text or "") + "\n" + (result_text or ""))[:1200].lower()
        for cand in pend["skills"]:
            if cand and re.search(r"\b" + re.escape(cand.lower()) + r"\b", low):
                pend["used"] = cand
                _emit(_config.NexaKevConfig.load(),
                      (session_key or "default")[-64:], pend)
                return
        if _ERR_RE.search((result_text or "")[:2000]):
            pend["err"] = 1
        # hermes one-shots may os._exit() past atexit: flush from inside
        # the turn once the session has clearly moved past the advice.
        if pend["notes"] >= 3 or (pend["notes"] >= 2
                                  and time.time() - pend["ts"] > 20):
            _emit(_config.NexaKevConfig.load(),
                  (session_key or "default")[-64:], pend)
    except Exception:
        pass



def _flag(key: str, default: str = "1") -> bool:
    try:
        v = str(_config._pick(key, default) or default).strip().lower()
    except Exception:
        v = default
    return v not in ("0", "false", "off", "no")


def _priors_digest(cfg: Any) -> str:
    """Deterministic usage prior from recent advisory_outcome rows:
    skills demonstrably used (u=) vs ignored (i=) after advisories.
    Dark behind NEXA_KEV_PRIORS (default off)."""
    try:
        path = str(cfg.decision_log)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(max(0, __import__("os").stat(path).st_size - 65536))
            lines = fh.read().splitlines()[-400:]
        used: Dict[str, int] = {}
        ign: Dict[str, int] = {}
        rows = 0
        for ln in lines:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("kind") != "advisory_outcome":
                continue
            rows += 1
            u = r.get("used") or 0
            for cand in (r.get("suggested") or []):
                if u == cand:
                    used[cand] = used.get(cand, 0) + 1
                else:
                    ign[cand] = ign.get(cand, 0) + 1
        if not rows:
            return ""
        names = sorted(set(used) | set(ign))[:8]
        parts = ["{} was suggested and taken {} of {} times".format(
                     n, used.get(n, 0), used.get(n, 0) + ign.get(n, 0))
                 for n in names]
        return ("historical outcomes: " + "; ".join(parts) + ".\n")
    except Exception:
        return ""


def pre_llm_call(**kwargs) -> Optional[Dict[str, str]]:
    """pre_llm_call hook. Returns {'context': ...} to advise, else None.
    NEVER raises — a kev outage must never surface in the turn."""
    try:
        cfg = _config.NexaKevConfig.load()
        if not cfg.hook_enabled:
            return None
        user_message = (kwargs.get("user_message") or "").strip()
        if not user_message:
            return None
        session_key = kwargs.get("session_id") or "default"
        msg_hash = hashlib.sha1(user_message.encode("utf-8")).hexdigest()[:16]
        routed = _routed.setdefault(session_key[-64:], {})
        if routed.get(msg_hash):
            return None
        if len(routed) > 200:
            routed.clear()
        routed[msg_hash] = True
        _tick(session_key, cfg)
        _log_decision(cfg, kind="advisory", outcome="fired",
                      msg=user_message[:60], session=session_key[-24:])

        skills = _load_skills()
        tools = _load_tools()
        cap = max(8, cfg.advisory_max_criteria)
        skills = {k: v[:60] for k, v in _prefilter(user_message, skills, cap).items()}
        tools = {k: v[:60] for k, v in _prefilter(user_message, tools, cap).items()}
        questions: Dict[str, Dict[str, Any]] = {}
        if skills:
            questions["skill"] = {
                "type": "choice",
                "instructions": ("Which skill should the agent load (if any) for this "
                                 "task? Choose 'none' when no skill clearly applies."),
                "criteria": {**skills, "none": "No skill applies to this task."},
            }
        if tools:
            questions["tool"] = {
                "type": "choice",
                "instructions": ("Which tool is the best FIRST instrument for this task? "
                                 "Choose 'none' when no specialized tool clearly applies."),
                "criteria": {**tools, "none": "No specialized tool applies; answer directly."},
            }
        if not questions:
            return None

        state = user_message[: cfg.max_state_chars]
        if _flag("PRIORS", "0"):
            state = (_priors_digest(cfg) + state)[: cfg.max_state_chars * 2]
        ans = _kd.raw_request(state, questions, cfg)
        if not ans or ans.get("status") != "ok":
            return None

        s_ans = (ans["answers"].get("skill") or {})
        t_ans = (ans["answers"].get("tool") or {})
        s_probs = s_ans.get("probabilities") or {}
        t_probs = t_ans.get("probabilities") or {}

        _log_decision(cfg, kind="advisory", task=state[:200],
                      skill_top=s_ans.get("choice"), tool_top=t_ans.get("choice"),
                      skill_probs={k: round(v, 3) for k, v in list(s_probs.items())[:10]},
                      tool_probs={k: round(v, 3) for k, v in list(t_probs.items())[:10]},
                      endpoint=ans.get("endpoint"), latency_ms=ans.get("latency_ms"),
                      session_id=kwargs.get("session_id") or "")

        try:
            if len(_PENDING) > _PENDING_MAX:
                _PENDING.clear()
            _PENDING[session_key[-64:]] = {
                "session": session_key[-24:],
                "skills": set(list(s_probs)[:3]),
                "tools": set(list(t_probs)[:3]),
                "ts": time.time(), "turns": 0}
        except Exception:
            pass
        skill_hint = _hint("skill: ", s_probs, cfg) or _hint_rel("skill: ", s_probs, cfg)
        tool_hint = _hint("tool: ", t_probs, cfg) or _hint_rel("tool: ", t_probs, cfg)
        if not (skill_hint or tool_hint):
            return None
        marker = f"\u26a1 [nexa-kev] {' \u00b7 '.join(p for p in (skill_hint, tool_hint) if p)}"
        context = (
            marker + "\n(kev's ranked suggestions for THIS task — echo the \u26a1 line "
            "verbatim at the top of your reply; prefer the named skill/tool when "
            "reasonable; ignore if clearly wrong. "
            f"{ans.get('latency_ms')}ms.)"
        )
        # The tool_search bridge renders ctx-registered plugin tools with an
        # EMPTY schema (host-core deferral layer), so kev_decide's args are
        # invisible in tool_describe. Spell the call shape inline whenever
        # kev_decide is ranked — a zero-knowledge agent can then call it
        # correctly without guessing (empty-args guesses fail as no_questions).
        if "kev_decide" in (tool_hint or ""):
            context += ("\nkev_decide args (bridge may show its schema as empty): "
                        'state=<string: current factual situation>, '
                        'questions=<list of strings>. Call it only when a decision '
                        "is genuinely at stake; skip it for routine steps.")
        return {"context": context}
    except Exception as e:
        logger.debug("nexa-kev advisory fail-open: %s", e)
        return None
