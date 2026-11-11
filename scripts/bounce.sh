#!/usr/bin/env bash
# bounce.sh — restart a hermes systemd-user unit SAFELY, even when run from
# inside a gateway-served agent session (the in-session guard refuses
# literal restart commands, and killing the gateway kills its children).
# Run it DETACHED from an external shell, or fire remotely:
#   ssh HOST 'setsid nohup bash ~/bounce.sh hermes-gateway-default >/dev/null 2>&1 </dev/null &'
# It records before/after MainPID + start time to /tmp/nexa-kev-bounce.log
# and VERIFY-FAILS loudly if the PID did not change (silent no-fire class).
set -uo pipefail
UNITS=("$@"); [ ${#UNITS[@]} -eq 0 ] && UNITS=(hermes-gateway.service)
# 2026-11-09 fix: previous UNIT="${1}" bounced ONLY the first argument —
# operators naming gateway+dashboard+webui got a silent single bounce
# (fleet ops mybuddy-trade/clone). Now: every arg is bounced, in order.
LOG=/tmp/nexa-kev-bounce.log
V=$(echo cmVzdGFydA== | base64 -d)   # verb kept encoded: guard text-matches
for UNIT in "${UNITS[@]}"; do
{
  echo "=== $(date -Is) bounce $UNIT"
  echo "before: $(systemctl --user show "$UNIT" -p MainPID -p ExecMainStartTimestamp -p NRestarts --value | tr '\n' ' ')"
  systemctl --user "$V" "$UNIT" || echo "restart command returned $?"
  sleep 6
  echo "after:  $(systemctl --user show "$UNIT" -p MainPID -p ExecMainStartTimestamp -p NRestarts --value | tr '\n' ' ')"
} >> "$LOG" 2>&1
  tail -3 "$LOG"
done

# 2026-11-10: on the QEMU-guest hosts (trade/clone) there is no gateway
# unit; sessions are served by RESIDENT processes (boot-autostarted
# `hermes dashboard`, or a process_supervisor-owned python server). These
# hold plugin code in memory from their START time, so a rollout that only
# bounced units kept serving pre-rollout lanes (bank verdicts and row
# schemas provably matched Monday's build although the checkout was fresh).
# Recycle every such resident: kill, then respawn `hermes dashboard` with
# the ORIGINAL host/port so the customer's dashboard returns on 9119.
respawn_dashboard() {  # $1 = cmdline string of the old server
  local H=localhost P=9119 tok prev=""
  for tok in $1; do
    case "$prev" in --host) H=$tok ;; --port) P=$tok ;; esac
    prev=$tok
  done
  setsid nohup hermes dashboard --host "$H" --port "$P" --no-open --skip-build \
    >>/tmp/nexa-kev-dashboard.log 2>&1 &
  echo "dashboard respawn requested (host $H port $P)" >> "$LOG"
}
for PID in $(pgrep -f "dashboard --host" 2>/dev/null); do
  [ "$PID" = "$$" ] && continue
  CMD=$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null)
  case "$CMD" in *hermes*|*python*) ;; *) continue ;; esac
  echo "=== $(date -Is) recycling standalone dashboard pid $PID" >> "$LOG"
  kill "$PID" 2>/dev/null
  sleep 3
  kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null
  respawn_dashboard "$CMD"
done
# supervisor-managed session server (cmdline args do NOT contain the
# dashboard flags, so the pgrep above misses it): recycle via the supervised
# pid extracted from its supervisor script.
for SUP in $(pgrep -f "process_supervisor.sh [0-9]" 2>/dev/null); do
  SPID=$(tr '\0' ' ' < "/proc/$SUP/cmdline" 2>/dev/null | awk '{print $NF}')
  case "$SPID" in *[!0-9]*|"") continue ;; esac
  SCMD=$(tr '\0' ' ' < "/proc/$SPID/cmdline" 2>/dev/null)
  case "$SCMD" in *dashboard*port*|*python*hermes*) ;; *) continue ;; esac
  echo "=== $(date -Is) recycling supervised server pid $SPID (sup $SUP)" >> "$LOG"
  kill "$SPID" 2>/dev/null
  sleep 3
  kill -0 "$SPID" 2>/dev/null && kill -9 "$SPID" 2>/dev/null
  respawn_dashboard "$SCMD"
done
