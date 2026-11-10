#!/usr/bin/env bash
# rollout_update.sh — fleet-host update for the nexa-kev plugin (Jan's
# 2026-11-10 layout rule): the git checkout LIVES IN THE PLUGIN LOCATION
# (~/.hermes/plugins/nexa-kev-src) and the plugin dir is a symlink into
# it, so a plain `git pull` IS the deployment. No ~/ops, no projects dir,
# no rsync copies. Idempotent / self-healing: safe to re-run blindly; a
# missing checkout gets cloned, a missing/foreign plugin-dir symlink is
# repaired (old real dir kept as .pre-git.<ts> backup).
#
# Usage (on the target host, or via ssh):  bash rollout_update.sh
# Then: bounce the gateway from an OUTSIDE shell (scripts/bounce.sh) and
# run check_nexa_kev.sh.
set -euo pipefail
REPO_URL="${NEXA_KEV_REPO_URL:-https://github.com/NexaSkills/nexa-kev}"
BRANCH="${NEXA_KEV_BRANCH:-main}"
HOSTROOT="${NK_HOST_HOME:-$HOME/.hermes}"
SRC="$HOSTROOT/plugins/nexa-kev-src"

echo "== nexa-kev rollout_update (host root: $HOSTROOT)"
mkdir -p "$HOSTROOT/plugins"
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" remote set-url origin "$REPO_URL"
  git -C "$SRC" fetch origin "$BRANCH" || { echo "FAIL: fetch"; exit 1; }
  git -C "$SRC" checkout "$BRANCH" 2>/dev/null || git -C "$SRC" checkout -B "$BRANCH" "origin/$BRANCH"
  git -C "$SRC" reset --hard "origin/$BRANCH"
  echo "  pulled: $(git -C "$SRC" log -1 --format='%h %s' | cut -c1-72)"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$SRC" || { echo "FAIL: clone"; exit 1; }
  echo "  cloned: $(git -C "$SRC" log -1 --format='%h %s' | cut -c1-72)"
fi

# plugin dir = symlink into the checkout (self-heal)
want="$SRC/plugin/nexa-kev"
cur="$HOSTROOT/plugins/nexa-kev"
if [ "$(readlink -f "$cur" 2>/dev/null)" != "$(readlink -f "$want")" ]; then
  if [ -e "$cur" ] && [ ! -L "$cur" ]; then
    bak="$cur.pre-git.$(date +%s)"
    mv "$cur" "$bak"; echo "  old dir kept: $bak"
  fi
  ln -sfn "$want" "$cur"
  echo "  wired: plugins/nexa-kev -> $want"
fi

# profile symlinks + enable-list leg via the sanctioned installer (its
# copy step self-skips now that the plugin dir resolves inside the repo)
bash "$SRC/scripts/install_nexa_kev.sh"
echo "ROLLOUT_UPDATE: DONE — next: bounce gateway (scripts/bounce.sh) + check_nexa_kev.sh"
