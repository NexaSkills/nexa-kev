#!/usr/bin/env bash
# install_nexa_kev.sh — idempotent installer for the nexa-kev Hermes plugin.
# Safe to re-run (upgrade = git pull + re-run). Does NOT restart services and
# does NOT touch the LiteLLM gateway DB; it prints the verified commands for
# those steps instead. Never writes secrets anywhere.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
# NB: do NOT trust an inherited HERMES_HOME env here — inside gateway-served
# shells it points at a PROFILE home, not the host root. Host root is
# overridable explicitly via NK_HOST_HOME.
HOSTROOT="${NK_HOST_HOME:-$HOME/.hermes}"
PROFILES_STR="${NEXA_KEV_PROFILES:-}"   # space-separated; default: all profiles

echo "== nexa-kev installer (repo: $REPO)"

# 1. Plugin code -> host plugins dir
#    Fleet layout (2026-11-10 rule): if the plugin dir already resolves
#    inside this repo checkout (rollout_update.sh shape: plugins/nexa-kev
#    -> <repo>/plugin/nexa-kev symlink), the code is ALREADY deployed —
#    skip copying entirely (git pull is the deployment).
mkdir -p "$HOSTROOT/plugins"
if [ "$(readlink -f "$HOSTROOT/plugins/nexa-kev" 2>/dev/null)" = "$(readlink -f "$REPO/plugin/nexa-kev")" ]; then
  echo "  plugin live via repo checkout symlink (git-pull layout) — copy skipped"
elif command -v rsync >/dev/null; then
  rsync -a --delete --exclude __pycache__ "$REPO/plugin/nexa-kev/" "$HOSTROOT/plugins/nexa-kev/"
else
  rm -rf "$HOSTROOT/plugins/nexa-kev"
  cp -r "$REPO/plugin/nexa-kev" "$HOSTROOT/plugins/nexa-kev"
fi
echo "  plugin installed -> $HOSTROOT/plugins/nexa-kev"

# 2. Symlink into every profile's plugins dir (idempotent)
#    DRIFT BUG FIX (2026-11-10, found on buddyos-nexa): `ln -sfn target
#    existing-dir` does NOT replace the dir — it creates a link INSIDE it,
#    silently leaving a stale CODE COPY live for profile sessions. So an
#    existing real dir is first moved aside (backup, not deleted).
PROFILES=()
if [ -n "$PROFILES_STR" ]; then read -r -a PROFILES <<< "$PROFILES_STR"; fi
if [ ${#PROFILES[@]} -eq 0 ] && [ -d "$HOSTROOT/profiles" ]; then
  mapfile -t PROFILES < <(ls "$HOSTROOT/profiles")
fi
for p in "${PROFILES[@]}"; do
  [ -d "$HOSTROOT/profiles/$p" ] || continue
  case "$p" in profiles|*) ;; esac   # skip recursive nonsense dir name
  [ "$p" = "profiles" ] && continue  # ghost dir ~/.hermes/profiles/profiles
  mkdir -p "$HOSTROOT/profiles/$p/plugins"
  link="$HOSTROOT/profiles/$p/plugins/nexa-kev"
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    mv "$link" "$link.stale-dir.$(date +%s)"
    echo "  moved stale dir aside: $link.stale-dir.*"
  fi
  ln -sfn "$HOSTROOT/plugins/nexa-kev" "$link"
  [ "$(readlink -f "$link")" = "$(readlink -f "$HOSTROOT/plugins/nexa-kev")" ] \
    || echo "  WARN: symlink verify failed for $link"
done
echo "  symlinked into profiles: ${PROFILES[*]:-<none>}"

# 3. Enable in Hermes config via the sanctioned CLI (never edit yaml by hand)
enable() {  # $1 = extra hermes args (empty or -p NAME)
  local cur
  cur=$(env -u HERMES_HOME -u NK_HOST_HOME hermes $1 config get plugins.enabled 2>/dev/null | tr -d '\r' | tail -1) || true
  case "$cur" in *nexa-kev*) echo "  enabled already: hermes $1"; return;; esac
  if [ -z "$cur" ] || [ "$cur" = "None" ]; then
    env -u HERMES_HOME -u NK_HOST_HOME hermes $1 config set plugins.enabled '["nexa-kev"]' >/dev/null 2>&1 || true
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
    env -u HERMES_HOME -u NK_HOST_HOME hermes $1 config set plugins.enabled "$new" >/dev/null 2>&1 || true
  fi
  echo "  enabled: hermes $1"
}
enable ""
for p in "${PROFILES[@]}"; do [ -d "$HOSTROOT/profiles/$p" ] && enable "-p $p"; done

# 4. Key presence (names only; values never printed)
key_ok=0
for f in "$HOSTROOT/.env" "$HOSTROOT/profiles"/*/.env; do
  [ -f "$f" ] || continue
  if grep -qE "^(NEXA_KEV_API_KEY|HERMES_CUSTOM_AI_NEXASKILLS_CO_ZA_API_KEY)=" "$f"; then key_ok=1; fi
done
[ "$key_ok" = 1 ] && echo "  LLM key: found in host/profile .env" \
  || echo "  WARN: no NEXA_KEV_API_KEY or HERMES_CUSTOM_AI_NEXASKILLS_CO_ZA_API_KEY in .env files; kev_decide will answer NO DECISION (missing_key) until a LiteLLM virtual key for this host's team is set (gateway base ai.nexaskills.co.za)."

# 5. Direct probe (data layer, no session involved)
PYBIN="${PYTHON:-}"
for cand in "$HOSTROOT"/installs/*/environments/*/venv/bin/python3 "$(command -v python3)"; do
  [ -x "$cand" ] || continue
  if "$cand" -c "import hermes_cli" 2>/dev/null; then PYBIN="$cand"; break; fi
done
[ -n "$PYBIN" ] || PYBIN=$(command -v python3)
echo "  probe python: $PYBIN"
if HERMES_HOME="${NK_PROBE_PROFILE_HOME:-$HOSTROOT}" "$PYBIN" - "$HOSTROOT" <<'PY'
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
