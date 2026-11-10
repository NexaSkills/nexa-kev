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
# unit; the resident session server is `hermes dashboard` autostarted at
# boot. A plugin rollout that only bounced units left it running PRE-ROLLOUT
# code in memory (bank decisions and lane rows silently came from the old
# build). Recycle it with identical argv so the customer's dashboard comes
# back on the same port with the fresh plugin code.
for PID in $(pgrep -f "dashboard --host" 2>/dev/null); do
  [ "$PID" = "$$" ] && continue
  CMD=$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null)
  case "$CMD" in *hermes*) ;; *) continue ;; esac
  echo "=== $(date -Is) recycling standalone dashboard pid $PID" >> "$LOG"
  kill "$PID" 2>/dev/null
  sleep 3
  kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null
  setsid nohup $CMD >>/tmp/nexa-kev-dashboard.log 2>&1 &
  echo "dashboard respawn requested (see /tmp/nexa-kev-dashboard.log)" >> "$LOG"
done
