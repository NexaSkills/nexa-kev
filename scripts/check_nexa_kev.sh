#!/usr/bin/env bash
# check_nexa_kev.sh — READ-ONLY end-to-end verification. Exit 0 = healthy.
set -uo pipefail
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
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

echo; [ "$fail" = 0 ] && echo "CHECK: PASS" || echo "CHECK: FAIL"
exit $fail
