"""advisory outcome tracking: used / later_error / expiry rows."""
import json
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "plugin" / "nexa-kev"))
import advisory  # noqa: E402


class _Cfg:
    decision_log = "/tmp/nk-outcome-test.jsonl"


def _capture(monkeypatch):
    rows = []
    monkeypatch.setattr(advisory, "_log_decision",
                        lambda cfg, **e: rows.append(e))
    monkeypatch.setattr(advisory._config, "NexaKevConfig",
                        type("X", (), {"load": staticmethod(lambda: _Cfg())}))
    return rows


def _arm(session="sess-abc", skills=("grounded-citations", "pdf"), ts=None):
    advisory._PENDING[session[-64:]] = {
        "session": session[-24:], "skills": set(skills),
        "ts": ts or time.time(), "turns": 0}


def test_used_emits_immediately(monkeypatch):
    rows = _capture(monkeypatch)
    advisory._PENDING.clear()
    _arm()
    advisory.note_tool_call("sess-abc", "skill_view", "{}",
                            '{"success": true, "name": "pdf", "x": 1}')
    assert len(rows) == 1 and rows[0]["kind"] == "advisory_outcome"
    assert rows[0]["used"] == "pdf"
    assert "sess-abc"[-64:] not in advisory._PENDING


def test_error_marks_later_error_on_expiry_emit(monkeypatch):
    rows = _capture(monkeypatch)
    advisory._PENDING.clear()
    _arm(skills=("spike",))
    advisory.note_tool_call("sess-abc", "terminal", "{}",
                            "bash: zk: command not found")
    assert not rows                       # not emitted yet
    advisory._tick("sess-abc", _Cfg())    # no use -> no emit yet
    assert not rows
    # expire by age
    advisory._PENDING["sess-abc"]["ts"] = time.time() - 10_000
    advisory._tick("sess-abc", _Cfg())
    assert rows and rows[0]["used"] == 0 and rows[0]["later_error"] == 1


def test_no_pending_is_noop(monkeypatch):
    rows = _capture(monkeypatch)
    advisory._PENDING.clear()
    advisory.note_tool_call("nobody", "terminal", "", "boom failed")
    advisory._tick("nobody", _Cfg())
    assert rows == []


def test_unrelated_skill_view_counts_as_not_used(monkeypatch):
    rows = _capture(monkeypatch)
    advisory._PENDING.clear()
    _arm(skills=("arxiv",))
    advisory.note_tool_call("sess-abc", "skill_view", "{}",
                            '{"name": "manim-video"}')
    assert not rows
    advisory._PENDING["sess-abc"]["turns"] = advisory._PENDING_MAX_TURNS + 1
    advisory._tick("sess-abc", _Cfg())
    assert rows[0]["used"] == 0


def test_exit_flush_emits_outcome(monkeypatch):
    rows = _capture(monkeypatch)
    advisory._PENDING.clear()
    _arm(skills=("arxiv",))
    advisory._flush_all()
    assert rows and rows[0]["kind"] == "advisory_outcome"
    assert rows[0]["exit_flush"] is True and rows[0]["used"] == 0
    assert advisory._PENDING == {}


def test_notes_flush_for_oneshots(monkeypatch):
    rows = _capture(monkeypatch)
    advisory._PENDING.clear()
    _arm(skills=("arxiv",))
    advisory._PENDING["sess-abc"]["ts"] -= 30   # 30s into the session
    advisory.note_tool_call("sess-abc", "terminal", "", "ok files listed")
    assert not rows                              # first note: not yet
    advisory.note_tool_call("sess-abc", "web_search", "", "results")
    assert rows and rows[0]["used"] == 0         # second note flushes
    assert "sess-abc"[-64:] not in advisory._PENDING


def test_three_notes_flush_fast(monkeypatch):
    rows = _capture(monkeypatch)
    advisory._PENDING.clear()
    _arm(skills=("arxiv",))          # fresh ts: <20s
    for i in range(3):
        advisory.note_tool_call("sess-abc", "terminal", "", "fine")
    assert rows and rows[0]["used"] == 0


class _CfgLog:
    def __init__(self, p):
        self.decision_log = str(p)


def test_priors_digest_counts(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text(
        '{"kind": "advisory_outcome", "suggested": ["pdf", "arxiv", "spike"], "used": "pdf"}\n'
        '{"kind": "advisory_outcome", "suggested": ["arxiv", "pdf", "x"], "used": 0}\n'
        '{"kind": "advisory", "noise": 1}\n')
    d = advisory._priors_digest(_CfgLog(p))
    assert "pdf was suggested and taken 1 of 2 times" in d
    assert "arxiv was suggested and taken 0 of 2 times" in d
    assert d.endswith("\n")


def test_priors_digest_empty_and_missing(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text('{"kind": "advisory"}\n')
    assert advisory._priors_digest(_CfgLog(p)) == ""
    assert advisory._priors_digest(_CfgLog(tmp_path / "nope.jsonl")) == ""


def _arm_tools(session="sess-abc", tools=("kev_decide", "terminal"), ts=None):
    advisory._PENDING[session[-64:]] = {
        "session": session[-24:], "skills": set(),
        "tools": set(tools), "ts": ts or time.time(), "turns": 0}


def test_used_tool_emits_immediately(monkeypatch):
    rows = _capture(monkeypatch)
    advisory._PENDING.clear()
    _arm_tools()
    advisory.note_tool_call("sess-abc", "kev_decide", '{"state": "x"}', "{}")
    assert len(rows) == 1 and rows[0]["kind"] == "advisory_outcome"
    assert rows[0]["used_tool"] == "kev_decide"
    assert "kev_decide" in rows[0]["tools"]
    assert "sess-abc"[-64:] not in advisory._PENDING


def test_unnamed_tool_expiry_row_lists_candidates(monkeypatch):
    rows = _capture(monkeypatch)
    advisory._PENDING.clear()
    _arm_tools(ts=time.time() - 10_000)
    advisory.note_tool_call("sess-abc", "read_file", "{}", "ok")  # not a candidate
    advisory._tick("sess-abc", _Cfg())
    assert rows and rows[0]["used_tool"] == 0
    assert rows[0]["tools"] == ["kev_decide", "terminal"]


def test_no_tools_pending_still_works(monkeypatch):
    rows = _capture(monkeypatch)
    advisory._PENDING.clear()
    _arm(skills=("pdf",))                     # old-shape row: no tools key
    advisory.note_tool_call("sess-abc", "terminal", "{}", "boom failed")
    advisory._PENDING["sess-abc"]["ts"] = time.time() - 10_000
    advisory._tick("sess-abc", _Cfg())
    assert rows and rows[0]["used_tool"] == 0 and rows[0]["tools"] == []


def test_bridge_call_counts_as_use(monkeypatch):
    rows = _capture(monkeypatch)
    advisory._PENDING.clear()
    _arm_tools()
    advisory.note_tool_call("sess-abc", "tool_call",
                            '{"name": "kev_decide", "arguments": {}}', "{}")
    assert len(rows) == 1 and rows[0]["used_tool"] == "kev_decide"


def test_describe_mention_is_not_a_use(monkeypatch):
    rows = _capture(monkeypatch)
    advisory._PENDING.clear()
    _arm_tools(ts=time.time() - 10_000)
    advisory.note_tool_call("sess-abc", "tool_describe",
                            '{"name": "kev_decide"}', '{"description": ""}')
    advisory._tick("sess-abc", _Cfg())
    assert rows and rows[0]["used_tool"] == 0
