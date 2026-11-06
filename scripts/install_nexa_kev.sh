#!/usr/bin/env bash
# install_nexa_kev.sh — idempotent installer for the nexa-kev Hermes plugin.
# Safe to re-run (upgrade = git pull + re-run). Does NOT restart services and
# does NOT touch the LiteLLM gateway DB; it prints the verified commands for
# those steps instead. Never writes secrets anywhere.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PROFILES="${NEXA_KEV_PROFILES:-}"   # space-separated; default: all profiles

echo "== nexa-kev installer (repo: $REPO)"

# 1. Plugin code -> host plugins dir
mkdir -p "$HERMES_HOME/plugins"
if command -v rsync >/dev/null; then
  rsync -a --delete --exclude __pycache__ "$REPO/plugin/nexa-kev/" "$HERMES_HOME/plugins/nexa-kev/"
else
  rm -rf "$HERMES_HOME/plugins/nexa-kev"
  cp -r "$REPO/plugin/nexa-kev" "$HERMES_HOME/plugins/nexa-kev"
fi
echo "  plugin installed -> $HERMES_HOME/plugins/nexa-kev"

# 2. Symlink into every profile's plugins dir (idempotent)
mapfile -p PROFILES <<< "$PROFILES"
if [ ${#PROFILES[@]} -eq 0 ] && [ -d "$HERMES_HOME/profiles" ]; then
  mapfile -t PROFILES < <(ls "$HERMES_HOME/profiles")
fi
for p in "${PROFILES[@]}"; do
  [ -d "$HERMES_HOME/profiles/$p" ] || continue
  mkdir -p "$HERMES_HOME/profiles/$p/plugins"
  ln -sfn "$HERMES_HOME/plugins/nexa-kev" "$HERMES_HOME/profiles/$p/plugins/nexa-kev"
done
echo "  symlinked into profiles: ${PROFILES[*]:-<none>}"

# 3. Enable in Hermes config via the sanctioned CLI (never edit yaml by hand)
enable() {  # $1 = extra hermes args (empty or -p NAME)
  local cur
  cur=$(hermes $1 config get plugins.enabled 2>/dev/null | tr -d '\r' | tail -1) || true
  case "$cur" in *nexa-kev*) echo "  enabled already: hermes $1"; return;; esac
  if [ -z "$cur" ] || [ "$cur" = "None" ]; then
    hermes $1 config set plugins.enabled '["nexa-kev"]' >/dev/null 2>&1 || true
  else
    local new
    new=$(python3 - "$cur" <<'PY'
import json,sys
cur=sys.argv[1].strip()
try: lst=json.loads(cur)
except Exception: sys.exit(1)
if not isinstance(lst,list): sys.exit(1)
if "nexa-kev" not in lst: lst.append("nexa-kev")
print(json.dumps(lst))
PY
) || { echo "  WARN: could not parse plugins.enabled ($cur) for 'hermes $1'; set it manually"; return; }
    hermes $1 config set plugins.enabled "$new" >/dev/null 2>&1 || true
  fi
  echo "  enabled: hermes $1"
}
enable ""
for p in "${PROFILES[@]}"; do [ -d "$HERMES_HOME/profiles/$p" ] && enable "-p $p"; done

# 4. Key presence (names only; values never printed)
key_ok=0
for f in "$HERMES_HOME/.env" "$HERMES_HOME/profiles"/*/.env; do
  [ -f "$f" ] || continue
  if grep -qE "^(NEXA_KEV_API_KEY|HERMES_CUSTOM_AI_NEXASKILLS_CO_ZA_API_KEY)=" "$f"; then key_ok=1; fi
done
[ "$key_ok" = 1 ] && echo "  LLM key: found in host/profile .env" \
  || echo "  WARN: no NEXA_KEV_API_KEY or HERMES_CUSTOM_AI_NEXASKILLS_CO_ZA_API_KEY in .env files; kev_decide will answer NO DECISION (missing_key) until a LiteLLM virtual key for this host's team is set (gateway base ai.nexaskills.co.za)."

# 5. Direct probe (data layer, no session involved)
PYBIN="${PYTHON:-}"
for cand in "$HERMES_HOME"/installs/*/environments/*/venv/bin/python3 "$(command -v python3)"; do
  [ -x "$cand" ] || continue
  if "$cand" -c "import hermes_cli" 2>/dev/null; then PYBIN="$cand"; break; fi
done
[ -n "$PYBIN" ] || PYBIN=$(command -v python3)
echo "  probe python: $PYBIN"
if HERMES_HOME="${HERMES_PROFILE_HOME:-$HERMES_HOME}" "$PYBIN" - "$HERMES_HOME" <<'PY'
import importlib.util, sys, json
from pathlib import Path
h = Path(sys.argv[1]) / "plugins" / "nexa-kev"
s = importlib.util.spec_from_file_location("nk", str(h/"__init__.py"), submodule_search_locations=[str(h)])
m = importlib.util.module_from_spec(s); sys.modules["nk"] = m; s.loader.exec_module(m)
import logging; logging.disable(logging.WARNING)
r = m.decide("Installer health probe. No task running.", ["Is it safe to reply?"])
d = (r.get("decisions") or {}).get("q1", {})
print(f"  probe: {r.get('status')} {r.get('reason','')} noul={d.get('noul')} spread={d.get('spread')}")
sys.exit(0 if r.get("status") == "ok" else 2)
PY
then echo "  probe OK (kev reachable from this host)"; else echo "  NOTE: probe not ok yet — usually missing team grant or not-yet-resolvable key; see README (gateway setup + P0)."; fi

echo
echo "== NEXT STEPS (read-only installer does not do these itself):"
cat <<'EOF'
  1. Gateway pass-through grant (LiteLLM gateway admin, ONE TIME per team):
       psql "$LITELLM_DB" -c "UPDATE \"local_\".\"LiteLLM_TeamTable\"
         SET metadata = jsonb_set(coalesce(metadata::jsonb,'{}'::jsonb),
         '{allowed_passthrough_routes}', '[\"/kev\"]', true)
         WHERE team_alias='<your-team>';"
     (Admin runs this; README has a no-DB route probe to check first.)
  2. Restart the serving surfaces FROM A SHELL OUTSIDE ANY AGENT SESSION:
       systemctl --user list-units --type=service | grep -iE "gateway|webui|dashboard"
       systemctl --user restart <hermes-gateway-unit> hermes-webui hermes-dashboard
  3. Verify end to end with README 'End-to-end verification prompts' (P0-P2).
EOF
