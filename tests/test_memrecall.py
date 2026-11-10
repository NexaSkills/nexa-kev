"""Offline contract tests for the v0.4.0 memrecall lane (lane L2).

Fully offline: the sibling recall probe and the kev decide call are
monkeypatched; logs land in a tmp profile.

    <venv>/bin/python3 -m pytest tests/test_memrecall.py -q
"""
import importlib
import json
import pathlib
import sys

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "nexa-kev"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


@pytest.fixture()
def lane(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("NEXA_KEV_PROFILE_HOME", str(tmp_path))
    for k in ("NEXA_KEV_MEMRECALL", "NEXA_KEV_MEMRECALL_MAX_SESSION",
              "NEXA_KEV_MEMRECALL_KEVGATE"):
        monkeypatch.delenv(k, raising=False)
    mod = importlib.import_module("memrecall")
    mod._FIRED.clear()
    mod._QUERY_SEEN.clear()
    return mod


def _stub_sibling(monkeypatch, mod, private_lines, shared_lines=None, ok=True):
    """Pretend mnemo-pg2 dispatch: private bank -> private_lines,
    bank=surface -> shared_lines. Records calls."""
    calls = []

    def call(tool, args):
        calls.append((tool, dict(args)))
        if not ok:
            return json.dumps({"error": "boom"})
        if args.get("bank") == "surface":
            return json.dumps({"memories": [{"content": c} for c in shared_lines or []]})
        return json.dumps({"memories": [{"content": c} for c in private_lines or []]})

    monkeypatch.setattr(mod, "_sibling_recall", lambda: call)
    return calls


def _rows(tmp_path):
    p = tmp_path / "plugins-data" / "nexa-kev" / "decisions.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_hard_error_appends_trailer(lane, monkeypatch, tmp_path):
    calls = _stub_sibling(monkeypatch, lane, ["terminal: use ssh mybuddy@host, not scp"])
    out = lane.transform_tool_result(
        tool_name="terminal", status="error",
        error_message="Permission denied (publickey)",
        result=json.dumps({"output": "Permission denied", "exit_code": 255}),
        session_id="s1")
    assert out and "--- Related memory (nexa-kev recall) ---" in out
    assert "ssh mybuddy@host" in out
    assert out.startswith('{"output"')          # original result preserved
    assert calls[0][1]["query"].startswith("terminal")
    assert _rows(tmp_path)[-1]["decision"] == "fired"


def test_shared_fallback_on_private_miss(lane, monkeypatch, tmp_path):
    calls = _stub_sibling(monkeypatch, lane, [], ["fleet rule: bounce webui too"])
    out = lane.transform_tool_result(
        tool_name="terminal", status="error", error_message="command not found: x",
        result="command not found: x", session_id="s1")
    assert out and "bounce webui too" in out
    assert ("mnemosyne_recall", {"query": calls[1][1]["query"],
                                "limit": 3, "bank": "surface"})[0] == "mnemosyne_recall"
    assert any(a.get("bank") == "surface" for _, a in calls)


def test_text_pattern_without_status(lane, monkeypatch):
    _stub_sibling(monkeypatch, lane, ["known pitfall: exit 127 = wrong venv"])
    out = lane.transform_tool_result(
        tool_name="terminal", result='{"exit_code": 127, "output": "boom"}',
        session_id="s1")
    assert out and "wrong venv" in out


def test_clean_result_untouched_no_recall(lane, monkeypatch):
    calls = _stub_sibling(monkeypatch, lane, ["irrelevant"])
    assert lane.transform_tool_result(
        tool_name="read_file", status="ok",
        result='{"content": "all fine", "exit_code": 0}', session_id="s1") is None
    assert calls == []


def test_no_hits_silent(lane, monkeypatch, tmp_path):
    _stub_sibling(monkeypatch, lane, [], [])
    out = lane.transform_tool_result(
        tool_name="terminal", status="error", error_message="weird failure here",
        result="weird failure here", session_id="s1")
    assert out is None
    assert _rows(tmp_path)[-1]["decision"] == "no_hits"


def test_sibling_missing_inert(lane, monkeypatch, tmp_path):
    monkeypatch.setattr(lane, "_sibling_recall", lambda: None)
    out = lane.transform_tool_result(
        tool_name="terminal", status="error", error_message="boom failed",
        result="boom failed", session_id="s1")
    assert out is None
    assert _rows(tmp_path)[-1]["decision"] == "silent_nosibling"


def test_budget_cap(lane, monkeypatch, tmp_path):
    monkeypatch.setenv("NEXA_KEV_MEMRECALL_MAX_SESSION", "2")
    _stub_sibling(monkeypatch, lane, ["a stored rule"])
    for i in range(3):
        out = lane.transform_tool_result(
            tool_name="terminal", status="error",
            error_message=f"failure variant {i} unique here",
            result=f"failure variant {i} unique here", session_id="s-b")
        assert (out is not None) == (i < 2)
    assert _rows(tmp_path)[-1]["decision"] == "silent_budget"


def test_query_dedup(lane, monkeypatch):
    calls = _stub_sibling(monkeypatch, lane, ["rule"])
    kw = dict(tool_name="terminal", status="error",
              error_message="same failure again", result="same failure again",
              session_id="s1")
    assert lane.transform_tool_result(**kw) is not None
    assert lane.transform_tool_result(**kw) is None
    assert len(calls) == 1


def test_kill_switch(lane, monkeypatch):
    monkeypatch.setenv("NEXA_KEV_MEMRECALL", "0")
    def boom():
        raise AssertionError("kill switch must short-circuit the probe")
    monkeypatch.setattr(lane, "_sibling_recall", boom)
    assert lane.transform_tool_result(
        tool_name="terminal", status="error", error_message="x broke",
        result="x broke", session_id="s1") is None


def test_kevgate_blocks_ambiguous_only(lane, monkeypatch, tmp_path):
    monkeypatch.setenv("NEXA_KEV_MEMRECALL_KEVGATE", "1")
    _stub_sibling(monkeypatch, lane, ["rule"])

    def no(state, questions, properties=None, cfg=None):
        return {"status": "ok", "decisions": {"q1": {"noul": 0.1,
                "verdict": "NO", "spread": 0.0}}}
    monkeypatch.setattr(lane._kd, "decide", no)
    # pattern-only (no status/error_message) -> gated off
    assert lane.transform_tool_result(
        tool_name="terminal", result='{"exit_code": 3, "output": "odd"}',
        session_id="s1") is None
    assert _rows(tmp_path)[-1]["decision"] == "silent_gate"
    # hard error -> gate does NOT apply (errors always recall)
    assert lane.transform_tool_result(
        tool_name="terminal", status="error", error_message="hard crash here",
        result="hard crash here", session_id="s1") is not None


def test_dict_and_weird_results_defended(lane, monkeypatch):
    _stub_sibling(monkeypatch, lane, ["rule"])
    out = lane.transform_tool_result(
        tool_name="x", status="error", error_message="boom",
        result={"output": "boom", "nested": {"code": 1}}, session_id="s1")
    assert out is not None and out.startswith("{")
    assert lane.transform_tool_result(
        tool_name="x", status="error", error_message="Traceback (most recent call last)",
        result=None, session_id="s1").endswith("rule")


def test_bad_recall_payload_fail_open(lane, monkeypatch):
    monkeypatch.setattr(lane, "_sibling_recall", lambda: (lambda t, a: "not-json{{"))
    assert lane.transform_tool_result(
        tool_name="terminal", status="error", error_message="some failure",
        result="some failure", session_id="s1") is None
