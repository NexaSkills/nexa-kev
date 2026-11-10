#!/usr/bin/env bash
# nexa-kev self-learning canary — proves the KEV DECISIONS fire in real
# agent sessions, verified OUTSIDE the agent via decisions.jsonl trail rows
# (the lane ledger) + one direct engine probe. No "save this" prompts:
# every memory action must be kev-decision-driven.
#
# Usage (on the target host):  bash canary_kev.sh [profile]
# Prints a verdict table; exit 0 = KEV_CANARY: PASS. Self-cleaning of any
# rows its anchors touch (own hard-delete; agent saves quarantine+expire).
set -uo pipefail
PROF=${1:-}
HC=(hermes); [ -n "$PROF" ] && HC=(hermes -p "$PROF")
AGENT=${PROF:-default}
TRAIL="${NK_TRAIL:-$( [ -z "$PROF" ] && echo "$HOME/.hermes/plugins-data/nexa-kev/decisions.jsonl" || echo "$HOME/.hermes/profiles/$PROF/plugins-data/nexa-kev/decisions.jsonl" )}"
PSQL="$HOME/lcm-pg/pg/bin/psql -h $HOME/lcm2-mnemo2-pg/pg/run -p 54331 -U mnemo201 -d mnemo2db"
SCHEMA="mnemo2_$AGENT"
R=$(openssl rand -hex 3)
K1="thimblewood-$R"     # private-rule anchor
K2="grantsmere-$R"      # fleet-doctrine anchor (should route SHARED)
LOG=/tmp/canary-kev-$R.log
fails=0
say(){ printf '  %-24s %s\n' "$1" "$2"; }
fail(){ say "$1" "FAIL$2"; fails=$((fails+1)); }
turn(){ { [ -z "${MARKLOCK:-}" ] && mark; }; OUT=$(timeout 200 env -u HERMES_HOME "${HC[@]}" --pass-session-id -z "$1" 2>/dev/null)
  SID=$(echo "$OUT" | grep -oE '[0-9a-f]{24,36}' | tail -1)
  echo "===== SID=$SID PROMPT=$1" >> "$LOG"; echo "$OUT" >> "$LOG"; }
mark(){ T0=$(date +%s 2>/dev/null); }
wait_trail(){ # wait_trail <kind> -> row of kind written since mark() (poll)
  local end=$((SECONDS+${2:-30}))
  until ROW=$(grep -h "\"kind\": \"$1\"" "$TRAIL" 2>/dev/null | sed -E 's/.*"ts": ([0-9]+).*/\1|&/' | awk -F'|' -v s="$T0" '$1+0>s+0 {print substr($0,index($0,"{"))}' | tail -1) [ -n "$ROW" ] || [ $SECONDS -ge $end ]; do sleep 2; done
  echo "${ROW:-}"; }

echo "== nexa-kev decision canary (agent=$AGENT anchors=$R)"
[ -f "$TRAIL" ] || { say trail_present "FAIL ($TRAIL missing)"; fails=$((fails+1)); }

# 1. advisory lane alive: any kev advisory row for the session.
turn "quick one: what is 12 times 7?"
ROW=$(wait_trail advisory 20)
[ -n "$ROW" ] && say advisory_lane OK || fail advisory_lane ""

# 2. L1 learning-capture decision fires on a natural learning moment.
turn "perfect, that's exactly how to do it â from now on, when you write canary notes for this machine always end them with the audit tag $K1 so they can be traced"
ROW=$(wait_trail memlearn)
if echo "$ROW" | grep -q '"decision": "fired"'; then say l1_fired "OK ($(echo "$ROW" | grep -oE '"p_save": [0-9.]+' | head -1))"
else fail l1_fired " (${ROW:-no trail row})"; fi
echo "$ROW" | grep -q '"status": "ok"' && say engine_verdicts OK || fail engine_verdicts ""

# 3. L3 bank routing: doctrine phrasing must produce a SHARED decision.
turn "policy for everyone on every host from now on: the drill marker is $K2"
ROW=$(wait_trail memlearn)
if echo "$ROW" | grep -q '"decision": "fired"'; then
  echo "$ROW" | grep -q '"bank": "shared"' && say l3_bank_shared "OK ($(echo "$ROW" | grep -oE '"p_shared": [0-9.]+' | head -1))" \
    || fail l3_bank_shared " (fired but bank=private: $(echo "$ROW" | grep -oE '"p_shared": [0-9.]+' | head -1))"
else fail l3_fired " (${ROW:-no trail row})"; fi

# 4. L2 error-recall: same hard error in two fresh sessions; second run
#    must auto-recall the first failure's experience (trail row fired).
BAD="drill-dr-$R"
MARKLOCK=1; mark
for n in 1 2 3; do
  turn "use the terminal tool to run the command $BAD once in the FOREGROUND (no backgrounding, no sleep) and quote its raw output"
  ROW=$(wait_trail memrecall 20); [ -n "$ROW" ] && break
done
unset MARKLOCK
ROW=$(wait_trail memrecall 40)
echo "$ROW" | grep -q '"decision": "fired"' && say l2_error_recall "OK ($(echo "$ROW" | grep -oE '"ms": [0-9.]+' | head -1))" || fail l2_error_recall " (${ROW:-NO ROW — advisory rows exist but no memrecall rows = transform dispatch died on a hot-reload; bounce gateway})"

# 5. direct engine probe (no session): decide reachable with verdict+spread.
PBIN=$(ls "$HOME"/.hermes/installs/*/environments/*/venv/bin/python3 2>/dev/null | head -1)
DP=$("$PBIN" - "$HOME/.hermes/plugins/nexa-kev" <<'PY' 2>/dev/null
import importlib.util,sys
from pathlib import Path
h=Path(sys.argv[1]); s=importlib.util.spec_from_file_location("nk",str(h/"__init__.py"),submodule_search_locations=[str(h)])
m=importlib.util.module_from_spec(s); sys.modules["nk"]=m; s.loader.exec_module(m)
import logging; logging.disable(logging.WARNING)
r=m.decide("Canary direct probe. Is this decision path healthy?",["Is this healthy?"])
d=(r.get("decisions") or {}).get("q1",{})
print(r.get("status"), d.get("noul"), d.get("spread"))
PY
)
case "$DP" in ok*) say direct_engine "OK ($DP)";; *) fail direct_engine " ($DP)";; esac

# ---- cleanup: quarantine agent-saved rows touching our anchors ----
$PSQL -q -c "update $SCHEMA.working_memory set metadata = coalesce(metadata,'{}'::jsonb) || jsonb_build_object('canary_quarantined','$R'), expired_at=now(), valid_until=now() where content like '%$R%' and expired_at is null" 2>/dev/null
$PSQL -q -c "delete from shared.memory_embeddings where memory_id in (select id from shared.working_memory where content like '%$R%')" 2>/dev/null
$PSQL -q -c "delete from shared.working_memory where content like '%$R%'" 2>/dev/null
say cleanup "done (agent saves quarantined+expired; shared purged)"

echo
[ "$fails" -eq 0 ] && { echo "KEV_CANARY: PASS"; exit 0; } || { echo "KEV_CANARY: FAIL ($fails)"; exit 1; }
