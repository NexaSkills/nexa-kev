"""askctx: flags gate context, filters are hard, failures are invisible."""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "plugin" / "nexa-kev"))
import askctx  # noqa: E402


def test_signals_detect_fleet_and_policy():
    s = askctx.signals("from now on all agents on all machines run the checker")
    assert "scope-tokens=fleet-wide" in s
    assert "speech-act=standing-policy" in s


def test_signals_detect_host_specific_tokens():
    s = askctx.signals("from now on this host uses psql port 54331")
    assert "host-specific-tokens=present" in s
    assert "host-specific-tokens=present" in askctx.signals("peer at 10.0.0.5 replies")


def test_precedents_off_by_default(monkeypatch):
    monkeypatch.delenv("NEXA_KEV_EXP", raising=False)
    assert askctx.precedents("x", call=lambda t, a: '{"memories": []}') == ""


def test_precedents_filter_low_importance_and_quarantined(monkeypatch):
    monkeypatch.setenv("NEXA_KEV_EXP", "1")
    askctx._EXPMEMO.clear()

    def call(tool, args):
        return json.dumps({"memories": [
            {"content": "a proven shared rule", "importance": 0.8},
            {"content": "ignored-low-importance", "importance": 0.2},
            {"content": "ignored-quarantined", "importance": 0.9,
             "quarantined": True},
        ]})
    out = askctx.precedents("unique-text-%s" % os.getpid(), call=call)
    assert "a proven shared rule" in out
    assert "ignored-low-importance" not in out
    assert "ignored-quarantined" not in out
    assert "tie-breaker" in out  # engine told precedent cannot override signals


def test_precedents_fail_open(monkeypatch):
    monkeypatch.setenv("NEXA_KEV_EXP", "1")
    askctx._EXPMEMO.clear()

    def boom(tool, args):
        raise RuntimeError("db down")
    assert askctx.precedents("other-unique-%s" % os.getpid(), call=boom) == ""


def test_preamble_dark_by_default(monkeypatch):
    monkeypatch.delenv("NEXA_KEV_SIGNALS", raising=False)
    monkeypatch.delenv("NEXA_KEV_EXP", raising=False)
    assert askctx.preamble("any text at all", call=lambda t, a: "{}") == ""


def test_preamble_signals_flag(monkeypatch):
    monkeypatch.setenv("NEXA_KEV_SIGNALS", "1")
    monkeypatch.delenv("NEXA_KEV_EXP", raising=False)
    out = askctx.preamble("from now on every host must run the checker")
    assert out.startswith("signals:")
