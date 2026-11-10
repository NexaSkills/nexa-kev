"""Trail rows must land in the ACTIVE profile's plugins-data, not root.

Bug (fleet canary 2026-11-10): memlearn/memrecall resolved the trail path to
~/.hermes at write time via a static fallback, so rows produced while serving
another profile's sessions were attributed to the ROOT trail (memroute/advisory
were correct). The trail writer must honour get_hermes_home() dynamically.
"""
import importlib
import json
import pathlib
import sys
import types

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "nexa-kev"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import memlearn  # noqa: E402
import memrecall  # noqa: E402


def _profile_home(tmp_path, monkeypatch, module):
    home = tmp_path / "profiles" / "canary"
    monkeypatch.delenv("NEXA_KEV_PROFILE_HOME", raising=False)
    stub = types.ModuleType("hermes_constants")
    stub.get_hermes_home = lambda: home
    monkeypatch.setitem(sys.modules, "hermes_constants", stub)
    return home


def _row_path(home):
    return home / "plugins-data" / "nexa-kev" / "decisions.jsonl"


def test_memlearn_trail_follows_active_profile(tmp_path, monkeypatch):
    home = _profile_home(tmp_path, monkeypatch, memlearn)
    memlearn._log({"kind": "memlearn", "decision": "fired"})
    p = _row_path(home)
    assert p.exists(), f"row written outside active profile home: {p}"
    assert json.loads(p.read_text().splitlines()[-1])["kind"] == "memlearn"


def test_memrecall_trail_follows_active_profile(tmp_path, monkeypatch):
    home = _profile_home(tmp_path, monkeypatch, memrecall)
    memrecall._log({"kind": "memrecall", "decision": "fired"})
    p = _row_path(home)
    assert p.exists(), f"row written outside active profile home: {p}"
    assert json.loads(p.read_text().splitlines()[-1])["kind"] == "memrecall"


def test_profile_home_env_override_still_wins(tmp_path, monkeypatch):
    forced = tmp_path / "forced"
    monkeypatch.setenv("NEXA_KEV_PROFILE_HOME", str(forced))
    stub = types.ModuleType("hermes_constants")
    stub.get_hermes_home = lambda: tmp_path / "should-not-be-used"
    monkeypatch.setitem(sys.modules, "hermes_constants", stub)
    memlearn._log({"kind": "memlearn", "decision": "fired"})
    assert _row_path(forced).exists()
