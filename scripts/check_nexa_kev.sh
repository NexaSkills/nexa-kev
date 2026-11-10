#!/usr/bin/env bash
# check_nexa_kev.sh — READ-ONLY end-to-end verification. Exit 0 = healthy.
set -uo pipefail
# NB: ignore an inherited HERMES_HOME (may be a profile home inside a
# gateway session); host root is overridable via NK_HOST_HOME only.
unset HERMES_HOME
HERMES_HOME="${NK_HOST_HOME:-$HOME/.hermes}"
fail=0
note() { printf '%s\n' "$*"; }
ok()   { printf 'PASS  %s\n' "$*"; }
bad()  { printf 'FAIL  %s\n' "$*"; fail=1; }

[ -f "$HERMES_HOME/plugins/nexa-kev/plugin.yaml" ] && ok "plugin files present" || bad "plugin files missing under $HERMES_HOME/plugins/nexa-kev"
VER=$(grep -m1 '^version' "$HERMES_HOME/plugins/nexa-kev/plugin.yaml" 2>/dev/null | tr -d '"' | awk '{print $2}')
note "      plugin version: ${VER:-?}"

linked=$(find "$HERMES_HOME/profiles" -maxdepth 3 -name nexa-kev -type l 2>/dev/null | wc -l)
[ "${linked:-0}" -ge 1 ] && ok "profile symlink(s): $linked" || bad "no profile symlink"

enabled=$(hermes config get plugins.enabled 2>/dev/null | grep -c nexa-kev)
[ "${enabled:-0}" -ge 1 ] && ok "enabled (root config)" || note "WARN  not in root plugins.enabled (profile-level may still serve it)"

PYBIN=""
for cand in "$HERMES_HOME"/installs/*/environments/*/venv/bin/python3 "$(command -v python3)"; do
  [ -x "$cand" ] || continue
  if "$cand" -c "import hermes_cli" 2>/dev/null; then PYBIN="$cand"; break; fi
done
[ -n "$PYBIN" ] || { bad "no python that can import hermes_cli (key resolution degraded)"; }

for PH in "$HERMES_HOME" "$HERMES_HOME"/profiles/*; do
  [ -d "$PH" ] || continue
  out=$(HERMES_HOME="$PH" "${PYBIN:-python3}" - "$HERMES_HOME" <<'PY'
import importlib.util, sys
from pathlib import Path
h = Path(sys.argv[1]) / "plugins" / "nexa-kev"
s = importlib.util.spec_from_file_location("nk", str(h/"__init__.py"), submodule_search_locations=[str(h)])
m = importlib.util.module_from_spec(s); sys.modules["nk"] = m; s.loader.exec_module(m)
import logging; logging.disable(logging.WARNING)
r = m.decide("Checker probe. No task running.", ["Is it safe to reply?"])
d = (r.get("decisions") or {}).get("q1", {})
print(f"{r.get('status')} {r.get('reason','')} {d.get('noul')} {d.get('spread')}")
PY
) 2>/dev/null
  case "$out" in
    ok*0.7*|ok*0.8*|ok*0.9*) ok "decide probe [$PH] -> $out" ;;
    ok*) note "WARN  decide probe [$PH] -> $out (ok but low noul)" ;;
    *)   bad "decide probe [$PH] -> $out" ;;
  esac
  break   # one probe is enough for data-layer; envs share config
done

trail=$(find "$HERMES_HOME/profiles" -path "*plugins-data/nexa-kev/decisions.jsonl" 2>/dev/null | head -1)
[ -n "$trail" ] && ok "audit trail exists: $trail" || note "INFO  no decisions.jsonl yet (fills after first session use)"

# ---- v0.4.0 self-learning lanes: static + offline contract probes ----
PDIR="$HERMES_HOME/plugins/nexa-kev"
grep -q "transform_tool_result" "$PDIR/plugin.yaml" 2>/dev/null \
  && ok "manifest lists transform_tool_result hook" || bad "manifest missing transform_tool_result (pre-0.4.0 install?)"
for ln in memlearn memrecall; do
  grep -q "register_hook.*pre_llm_call\|register_hook.*transform_tool_result" "$PDIR/__init__.py" 2>/dev/null && break
done
grep -q "memlearn" "$PDIR/__init__.py" 2>/dev/null && grep -q "memrecall" "$PDIR/__init__.py" 2>/dev/null \
  && ok "lane hooks present in __init__" || bad "memlearn/memrecall not registered in installed __init__"

# sibling contract (L2 recall rides mnemo-pg2's entrypoints): static check
if [ -d "$HERMES_HOME/plugins/mnemo-pg2" ]; then
  grep -lq "def handle_tool_call\|def _dispatch" "$HERMES_HOME"/plugins/mnemo-pg2/*.py 2>/dev/null \
    && ok "mnemo-pg2 recall entrypoints present (probe contract)" \
    || bad "mnemo-pg2 installed but no handle_tool_call/_dispatch — L2 would run inert"
else
  note "INFO  mnemo-pg2 not in $HERMES_HOME/plugins (L2 inert by design)"
fi

# offline firing probe of both lanes (decide stubbed; no gateway needed)
LANE_OUT=$(HERMES_HOME="${HERMES_HOME}" "${PYBIN:-python3}" - "$PDIR" <<'PY' 2>/dev/null
import importlib.util, json, sys, types
from pathlib import Path
h = Path(sys.argv[1])
s = importlib.util.spec_from_file_location("nkc", str(h/"__init__.py"), submodule_search_locations=[str(h)])
m = importlib.util.module_from_spec(s); sys.modules["nkc"] = m; s.loader.exec_module(m)
import logging; logging.disable(logging.WARNING)
import importlib
ml = importlib.import_module("nkc.memlearn"); mr = importlib.import_module("nkc.memrecall")
ok_parts = []
# force lane logic (not rollout flags) under test: kill=on, shadow/gate=off
ml._flag = lambda k, d="1": (False if k == "MEMLEARN_SHADOW" else True)
mr._flag = lambda k, d="1": (True if k == "MEMRECALL" else False)
# L1: stub YES/NO verdicts -> must fire a PRIVATE directive
ml._kd = types.SimpleNamespace(decide=lambda *a, **k: {"status":"ok","decisions":{
    "q1":{"noul":0.9,"verdict":"SAFE/YES","spread":0.0},
    "q2":{"noul":0.1,"verdict":"NO","spread":0.0}}})
r = ml.pre_llm_call(user_message="from now on the checker must always pass",
                    session_id="nk-checker", conversation_history=[])
ok_parts.append("L1_fire=" + str(bool(isinstance(r, dict) and "LEARNING MOMENT" in (r.get("context") or ""))))
# L2: pattern match with a forced-miss recall -> must be silent, not crash
mr._sibling_recall = lambda: (lambda t, a: json.dumps({"memories": []}))
r2 = mr.transform_tool_result(tool_name="checker", status="error",
                              error_message="checker probe failure",
                              result="checker probe failure", session_id="nk-checker")
ok_parts.append("L2_silent_on_miss=" + str(r2 is None))
print(" ".join(ok_parts))
PY
)
case "$LANE_OUT" in
  *L1_fire=True*L2_silent_on_miss=True*) ok "lane firing probes ($LANE_OUT)" ;;
  *) bad "lane firing probes -> '${LANE_OUT:-no-output}'" ;;
esac

echo; [ "$fail" = 0 ] && echo "CHECK: PASS" || echo "CHECK: FAIL"
exit $fail
