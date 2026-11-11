#!/usr/bin/env bash
# rollout_update.sh — fleet-host update for the nexa-kev plugin (Jan's
# 2026-11-10 layout rules): the git checkout is self-contained under
# ~/.hermes but OUTSIDE any plugin scan path (~/.hermes/checkout/
# nexa-kev), and the plugin dir is a symlink into it, so a plain
# `git pull` IS the deployment. No ~/ops, no Projects dir, no rsync
# copies. The former ~/.hermes/plugins/nexa-kev-src location is migrated
# automatically (loader-inert there, but it LOOKED like a competing
# plugin dir — Jan 2026-11-10; a dir named nexa-kev* inside a scanned
# plugins/ is a confusion magnet regardless of manifest validity).
# Idempotent / self-healing: safe to re-run blindly; a missing checkout
# gets cloned, a missing/foreign plugin-dir symlink is repaired (old
# real dir kept as .pre-git.<ts> backup, swept outside the scan path).
#
# Usage (on the target host, or via ssh):  bash rollout_update.sh
# Then: bounce the gateway from an OUTSIDE shell (scripts/bounce.sh) and
# run check_nexa_kev.sh.
set -euo pipefail
REPO_URL="${NEXA_KEV_REPO_URL:-https://github.com/NexaSkills/nexa-kev}"
BRANCH="${NEXA_KEV_BRANCH:-main}"
HOSTROOT="${NK_HOST_HOME:-$HOME/.hermes}"
SRC="$HOSTROOT/checkout/nexa-kev"
OLD_SRC="$HOSTROOT/plugins/nexa-kev-src"

# one-time migration from the in-plugins checkout location
if [ -d "$OLD_SRC/.git" ] && [ ! -e "$SRC" ]; then
  mkdir -p "$HOSTROOT/checkout"
  mv "$OLD_SRC" "$SRC" && echo "  migrated checkout: $OLD_SRC -> $SRC"
fi

echo "== nexa-kev rollout_update (host root: $HOSTROOT)"
mkdir -p "$HOSTROOT/plugins" "$HOSTROOT/checkout"
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

# plugin dir = REAL DIR in the hub (~/.hermes/plugins/nexa-kev),
# materialised from the checkout (placement standard 2026-11-11: all
# plugins RESIDE in the hub; profiles link to the hub; the checkout is
# the single write-source, never a load path). Self-heal, fail-closed.
want="$HOSTROOT/plugins/nexa-kev"
src="$SRC/plugin/nexa-kev"
MARK=".nk-hub-sync"
mkdir -p "$HOSTROOT/backup"
# BUG CLASS 2026-11-10: a backup dir left INSIDE a plugins scan path is
# discovered as a second same-named plugin manifest and can shadow the
# live one (template booted 0.3.0 off nexa-kev.pre-git.*). Sweep any
# leftover backup dirs out of every scanned plugins dir.
for dpath in "$HOSTROOT/plugins" "$HOSTROOT"/profiles/*/plugins; do
  for junk in "$dpath"/nexa-kev.pre-git.* "$dpath"/nexa-kev.stale-dir.*; do
    [ -d "$junk" ] && { mv "$junk" "$HOSTROOT/backup/" && echo "  swept out of scan path: $(basename "$junk")"; }
  done
done
if [ -L "$want" ]; then
  # legacy shape (git-pull-era symlink into the checkout): retire, materialise
  mv "$want" "$HOSTROOT/backup/nexa-kev.pre-hub.$(date +%s)"
  echo "  retired legacy checkout-symlink to backup (outside scan path)"
elif [ -e "$want" ] && [ ! -d "$want" ]; then
  echo "FAIL: plugins/nexa-kev exists but is not a dir/symlink — refusing"; exit 1
elif [ -d "$want" ] && [ ! -f "$want/$MARK" ] \
     && ! diff -rq -x __pycache__ -x "$MARK" "$src" "$want" >/dev/null 2>&1; then
  # marker-less real dir whose content MATCHES the checkout is our own
  # pre-marker era (installer rsync predates the marker) — proceed and
  # re-stamp; a DIFFERENT content real dir is vendor, refuse
  echo "FAIL: plugins/nexa-kev is a real dir NOT owned by this script (no $MARK, content differs) — refusing"; exit 1
fi
mkdir -p "$want"
rsync -a -c --delete --exclude __pycache__ --exclude "$MARK" "$src/" "$want/" \
  || { echo "FAIL: materialise $src -> $want"; exit 1; }
echo "$SRC" > "$want/$MARK"
echo "  hub materialised: plugins/nexa-kev (from checkout $(git -C "$SRC" rev-parse --short HEAD))"
# legacy PROFILE links pointing into the checkout (or dangling at the old
# in-plugins checkout) -> repoint at the hub; profiles must link to HUB only
for l in "$HOSTROOT"/profiles/*/plugins/nexa-kev; do
  [ -e "$l" ] || [ -L "$l" ] || continue
  if [ -L "$l" ]; then
    case "$(readlink "$l")" in
      "$want") : ;;
      "$SRC"/*|*nexa-kev-src*) ln -sfn "$want" "$l" && echo "  profile link repointed to hub: $l" ;;
    esac
  else
    # REAL DIR profile surface = the drift bug class of 2026-11-10 (booted
    # a stale version off it): back it up OUTSIDE the scan path, link to hub
    mv "$l" "$HOSTROOT/backup/$(basename "$(dirname "$(dirname "$l")")").nexa-kev.stale-dir.$(date +%s)" \
      && ln -sfn "$want" "$l" && echo "  profile real dir -> hub link: $l"
  fi
done

# profile symlinks + enable-list leg via the sanctioned installer (its
# copy step self-skips now that the plugin dir resolves inside the repo)
bash "$SRC/scripts/install_nexa_kev.sh"
echo "ROLLOUT_UPDATE: DONE — next: bounce gateway (scripts/bounce.sh) + check_nexa_kev.sh"
