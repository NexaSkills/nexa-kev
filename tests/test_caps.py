"""Session fire-cap defaults (Jan 2026-11-11): the canary's l2_error_recall
leg FAILed on busy hosts (nexa/trade) with decision=silent_budget — the
per-SESSION trailer cap (old default 2) was exhausted by earlier legs in
the same long-lived session, while quiet tutor passed. Fleet consistency:
raise the defaults; env still overrides per NEXA_KEV_<KEY>. Dedup
(identical-query) and the kill switch are untouched — the caps only bound
per-session volume.
"""
import os
import pathlib
import sys

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "nexa-kev"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import memrecall  # noqa: E402
import memlearn   # noqa: E402


def _env_clean(monkeypatch):
    for k in list(os.environ):
        if k.startswith("NEXA_KEV_"):
            monkeypatch.delenv(k, raising=False)


def test_recall_cap_default_8(monkeypatch):
    _env_clean(monkeypatch)
    assert memrecall._pick_int("MEMRECALL_MAX_SESSION",
                               memrecall._MAX_SESSION_DEFAULT) == 8


def test_learn_cap_default_6(monkeypatch):
    _env_clean(monkeypatch)
    assert memlearn._pick_int("MEMLEARN_MAX_SESSION",
                              memlearn._MAX_SESSION_DEFAULT) == 6


def test_env_override_still_wins(monkeypatch):
    monkeypatch.setenv("NEXA_KEV_MEMRECALL_MAX_SESSION", "3")
    assert memrecall._pick_int("MEMRECALL_MAX_SESSION",
                               memrecall._MAX_SESSION_DEFAULT) == 3
