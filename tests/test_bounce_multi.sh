#!/usr/bin/env bash
# bounce.sh multi-unit reproducer (fleet op 2026-11-09: 3 units named, only
# the FIRST was bounced — UNIT="${1}" dropped the rest, silently).
# Runs fully offline: a stub systemctl records what bounce.sh would do.
set -u
HERE=$(dirname "$0"); SCRIPT="$HERE/../scripts/bounce.sh"
STUB=$(mktemp -d); LOGF="$STUB/calls"; trap 'rm -rf "$STUB"' EXIT
cat > "$STUB/systemctl" <<STUBEOF
#!/usr/bin/env bash
echo "\$@" >> "$LOGF"
if [ "\$1" = "--user" ] && [ "\$3" = "show" ]; then echo "123 0 now"; exit 0; fi
exit 0
STUBEOF
chmod +x "$STUB/systemctl"
PATH="$STUB:$PATH" bash "$SCRIPT" unit-a.service unit-b.service unit-c.service >/dev/null 2>&1

for u in unit-a.service unit-b.service unit-c.service; do
  if ! grep -q "restart $u" "$LOGF" 2>/dev/null; then
    echo "FAIL: $u never restarted (calls: $(tr '\n' '|' < "$LOGF"))"; exit 1
  fi
done
echo "PASS: all 3 units restarted"
