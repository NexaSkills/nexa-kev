"""Offline contract tests for the v0.4.0 memlearn lane (lane L1).

Run with the agent venv python; the suite is fully offline (kev decide is
monkeypatched, logs land in a tmp profile). Harness note (nexa-kev-plugin
skill): use the agent venv python + HERMES_HOME pointing at a tmp home so
the dotenv resolver sees NOTHING and our NEXA_KEV_* env values win.

    <venv>/bin/python3 -m pytest tests/test_memlearn.py -q
"""
import importlib
import json
import pathlib
import sys
import time

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "nexa-kev"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


@pytest.fixture()
def lane(tmp_path, monkeypatch):
    """Fresh memlearn module with hermetic env + tmp log home."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("NEXA_KEV_PROFILE_HOME", str(tmp_path))
    monkeypatch.delenv("NEXA_KEV_MEMLEARN", raising=False)
    monkeypatch.delenv("NEXA_KEV_MEMLEARN_SHADOW", raising=False)
    monkeypatch.delenv("NEXA_KEV_MEMLEARN_MAX_SESSION", raising=False)
    mod = importlib.import_module("memlearn")
    mod._SEEN.clear()
    mod._FIRED.clear()
    return mod


def _stub_decide(monkeypatch, mod, q1, q2, status="ok", spread1=0.0, spread2=0.0):
    calls = {"n": 0, "state": None, "questions": None}

    def fake(state, questions, properties=None, cfg=None):
        calls["n"] += 1
        calls["state"] = state
        calls["questions"] = questions
        if status != "ok":
            return {"status": "no_decision", "reason": "stub", "decisions": {}}
        return {"status": "ok",
                "decisions": {"q1": {"noul": q1, "verdict": q1 and "SAFE/YES"
                                     if q1 >= 0.7 else "UNCERTAIN",
                                     "spread": spread1},
                              "q2": {"noul": q2, "verdict": "SAFE/YES"
                                     if q2 >= 0.7 else "NO",
                                     "spread": spread2}}}
    monkeypatch.setattr(mod._kd, "decide", fake)
    return calls


def _stub_verdicts(monkeypatch, mod, v1, v2, p1=0.9, p2=0.9,
                   s1=0.0, s2=0.0, status="ok"):
    """Finer control: explicit verdict labels per question."""
    def fake(state, questions, properties=None, cfg=None):
        if status != "ok":
            return {"status": "no_decision", "reason": "stub", "decisions": {}}
        return {"status": "ok", "decisions": {
            "q1": {"noul": p1, "verdict": v1, "spread": s1},
            "q2": {"noul": p2, "verdict": v2, "spread": s2}}}
    monkeypatch.setattr(mod._kd, "decide", fake)


def _rows(tmp_path):
    p = tmp_path / "plugins-data" / "nexa-kev" / "decisions.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


POSITIVE = [
    "yes, that was good, do it like that from now on",
    "from now on always run the checker before you commit",
    "going forward, prefer tailnet endpoints over LAN",
    "that was correct, keep doing it like that",
    "don't do it like that anymore, use the update script",
    "remember that trade uses hermes-gateway-default",
    "make it a rule: never edit a fleet host directly",
    "I want you to always quote checker output verbatim",
    "whenever you see a 403 on /kev, check the team grant first",
    "stop using the inline key, use the dedicated kev key",
    "in future sessions, reply in Chinese for this project",
    "next time, bounce webui and dashboard too",
]
NEGATIVE = [
    "what's the weather like in Munich today?",
    "list the files in ~/Projects please",
    "run the test suite and show me the failures",
    "hi there, how are you",
    "can you explain what a postgres vacuum does and why it runs",
    "translate this sentence into French: good morning everyone",
    "rename the branch to feature/retry-backoff",
    "what version of the plugin is deployed on template",
    "summarize this article about sea temperature anomalies",
    "open the file and fix the typo on line 12",
    "thanks! that resolved it",
    "did the cron job finish yet",
]


@pytest.mark.parametrize("msg", POSITIVE)
def test_prefilter_matches_learning_phrases(lane, monkeypatch, msg):
    calls = _stub_decide(monkeypatch, lane, 0.9, 0.2)
    out = lane.pre_llm_call(user_message=msg, session_id="s1",
                            conversation_history=[])
    assert calls["n"] == 1, f"prefilter missed: {msg!r}"


@pytest.mark.parametrize("msg", NEGATIVE)
def test_prefilter_silent_on_non_learning(lane, monkeypatch, msg):
    def boom(*a, **k):
        raise AssertionError("gateway must not be called on prefilter miss")
    monkeypatch.setattr(lane._kd, "decide", boom)
    assert lane.pre_llm_call(user_message=msg, session_id="s1") is None


def test_directive_fires_private_by_default(lane, monkeypatch, tmp_path):
    _stub_decide(monkeypatch, lane, 0.9, 0.2)
    out = lane.pre_llm_call(user_message="from now on always run pytest first",
                            session_id="s1", conversation_history=[])
    assert out and "LEARNING MOMENT" in out["context"]
    assert "PRIVATE" in out["context"]
    assert "mnemosyne_remember" in out["context"]
    assert "mnemosyne_update" in out["context"]  # dedup discipline mandated
    assert _rows(tmp_path)[-1]["decision"] == "fired"


def test_shared_bank_recommendation(lane, monkeypatch):
    _stub_decide(monkeypatch, lane, 0.9, 0.9)
    out = lane.pre_llm_call(user_message="from now on all agents must use tailnet",
                            session_id="s1")
    assert "SHARED" in out["context"] and "mnemosyne_shared_remember" in out["context"]


def test_uncertain_bank_defaults_private(lane, monkeypatch):
    _stub_verdicts(monkeypatch, lane, "SAFE/YES", "UNCERTAIN", s2=0.31)
    out = lane.pre_llm_call(user_message="from now on skip the legacy migrations",
                            session_id="s1")
    assert "PRIVATE" in out["context"]


def test_uncertain_save_is_silent(lane, monkeypatch, tmp_path):
    _stub_verdicts(monkeypatch, lane, "UNCERTAIN", "NO", p1=0.6)
    out = lane.pre_llm_call(user_message="from now on maybe run the checker",
                            session_id="s1")
    assert out is None
    assert _rows(tmp_path)[-1]["decision"] == "silent_uncertain"


def test_spread_demotes_confident_save_verdict(lane, monkeypatch):
    # verdict YES but ensemble disagreement (spread > 0.2) -> silent
    _stub_verdicts(monkeypatch, lane, "SAFE/YES", "NO", s1=0.31)
    out = lane.pre_llm_call(user_message="from now on always quote checker output",
                            session_id="s1")
    assert out is None


def test_no_decision_fail_open_silent(lane, monkeypatch, tmp_path):
    _stub_verdicts(monkeypatch, lane, "SAFE/YES", "NO", status="fail")
    out = lane.pre_llm_call(user_message="from now on always bounce webui too",
                            session_id="s1")
    assert out is None
    assert _rows(tmp_path)[-1]["decision"].startswith("silent_gate")


def test_shadow_mode_logs_without_injecting(lane, monkeypatch, tmp_path):
    monkeypatch.setenv("NEXA_KEV_MEMLEARN_SHADOW", "1")
    _stub_decide(monkeypatch, lane, 0.9, 0.2)
    out = lane.pre_llm_call(user_message="from now on use the bounce script",
                            session_id="s1")
    assert out is None
    assert _rows(tmp_path)[-1]["decision"] == "fired_shadow"


def test_kill_switch_no_gateway_call(lane, monkeypatch):
    monkeypatch.setenv("NEXA_KEV_MEMLEARN", "0")
    def boom(*a, **k):
        raise AssertionError("kill switch must short-circuit before kev")
    monkeypatch.setattr(lane._kd, "decide", boom)
    assert lane.pre_llm_call(user_message="from now on always run the checker",
                             session_id="s1") is None


def test_session_directive_cap(lane, monkeypatch, tmp_path):
    monkeypatch.setenv("NEXA_KEV_MEMLEARN_MAX_SESSION", "2")
    _stub_decide(monkeypatch, lane, 0.9, 0.2)
    for i in range(3):
        out = lane.pre_llm_call(
            user_message=f"from now on rule number {i} applies everywhere",
            session_id="s-cap")
        assert (out is not None) == (i < 2)
    assert _rows(tmp_path)[-1]["decision"] == "capped"


def test_message_hash_dedup(lane, monkeypatch):
    calls = _stub_decide(monkeypatch, lane, 0.9, 0.2)
    m = "from now on always quote the checker PASS line"
    assert lane.pre_llm_call(user_message=m, session_id="s1") is not None
    assert lane.pre_llm_call(user_message=m, session_id="s2") is None
    assert calls["n"] == 1


def test_defensive_kwargs_variants(lane, monkeypatch):
    _stub_decide(monkeypatch, lane, 0.9, 0.2)
    # list-part content, dict message, missing history, extra fields
    assert lane.pre_llm_call(
        user_message=[{"text": "from now on"}, {"text": " always run pytest"}],
        session_id="s1") is not None
    assert lane.pre_llm_call(
        user_message={"content": "from now on always log the verdict"},
        session_id="s1x", whatever_new_host_field=42) is not None
    assert lane.pre_llm_call(
        message="from now on check the team grant on 403",
        conversation_history="not-a-list") is not None


def test_history_context_reaches_state(lane, monkeypatch):
    calls = _stub_decide(monkeypatch, lane, 0.9, 0.2)
    lane.pre_llm_call(
        user_message="yes that was good, do it like that from now on",
        session_id="s1",
        conversation_history=[{"role": "assistant",
                               "content": [{"text": "I edited deploy.sh"}]}])
    assert "deploy.sh" in calls["state"]


def test_internal_error_is_logged_not_swallowed_silently(lane, monkeypatch, tmp_path):
    def boom(state, questions, properties=None, cfg=None):
        raise RuntimeError("stubbed decide explosion")
    monkeypatch.setattr(lane._kd, "decide", boom)
    assert lane.pre_llm_call(user_message="from now on always quote checker output",
                             session_id="s-err") is None
    assert _rows(tmp_path)[-1]["decision"].startswith("error:")


def test_short_and_empty_messages_inert(lane, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no gateway call for trivial messages")
    monkeypatch.setattr(lane._kd, "decide", boom)
    assert lane.pre_llm_call(user_message="", session_id="s") is None
    assert lane.pre_llm_call(user_message="ok", session_id="s") is None
    assert lane.pre_llm_call(session_id="s") is None
