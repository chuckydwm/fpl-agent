#!/usr/bin/env bash
# Installs the FPL launchd jobs so they run on a timer with no terminal open.
#
# The plists in this directory are templates carrying a placeholder home path;
# this script renders them with the real location of this checkout and copies
# them into ~/Library/LaunchAgents. Re-run it after moving the repo.
#
# Adapted from crypto-trading-agent/launchd/install.sh, which learned the hard
# way (2026-08-18, 2026-09-28, 2026-10-22) about three launchd traps that all
# read as success from `launchctl list`:
#
#   1. A plist whose entrypoint does not exist crash-loops forever and is
#      reported as "loaded". So every job's interpreter and module are checked
#      before anything is installed, and missing ones are skipped.
#   2. macOS tags files under protected folders (Desktop, Documents, Downloads)
#      with a com.apple.macl xattr naming the process that created them. A log
#      file created by a manual run is then refused to the launchd job, which
#      fails with EX_CONFIG (78) before exec'ing anything. So existing logs are
#      rotated aside and launchd creates fresh ones. This repo lives outside
#      those folders for the same reason — the previous FPL jobs ran /bin/bash
#      from ~/Desktop and exited 126 on every run for seven weeks.
#   3. `launchctl bootout` returns before the job is gone; a bootstrap in that
#      window fails. So the script waits until launchd forgets the label.
#
# usage: launchd/install.sh [--dry-run] [--only <job>]
#   --dry-run   report what would be installed and touch nothing
#   --only      install one job and leave the rest alone (label with or
#               without the agency.prokonsalt.fpl- prefix / .plist suffix)
set -euo pipefail

DRY_RUN=0
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --only)
      [ -n "${2:-}" ] || { echo "--only needs a job label" >&2; exit 2; }
      ONLY="${2%.plist}"; ONLY="${ONLY#agency.prokonsalt.fpl-}"; shift ;;
    *) echo "unknown option: $1" >&2
       echo "usage: install.sh [--dry-run] [--only <job>]" >&2; exit 2 ;;
  esac
  shift
done

# `pwd -P`: the PHYSICAL path. Run through a symlink, plain `pwd` would bake
# the symlink's path into every plist.
PLIST_DIR="$(cd "$(dirname "$0")" && pwd -P)"
REPO_DIR="$(cd "$PLIST_DIR/.." && pwd -P)"
TARGET_DIR="$HOME/Library/LaunchAgents"
PLACEHOLDER="/Users/YOUR_USERNAME/fpl-agent"
PREFIX="agency.prokonsalt.fpl-"
# Where fpl_agent.ops.common keeps success markers (platformdirs user_state_dir).
MARKER_DIR="$HOME/Library/Application Support/fpl-agent/markers"

case "$REPO_DIR" in
  "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*)
    echo "refusing: $REPO_DIR is under a macOS-protected folder; launchd jobs will be denied there (see header)" >&2
    exit 2 ;;
esac

[ "$DRY_RUN" = "1" ] || mkdir -p "$TARGET_DIR" "$REPO_DIR/logs" "$MARKER_DIR"

installed=0; skipped=0; failed=0
BOOTOUT_WAIT_SEC=60

for plist in "$PLIST_DIR"/${PREFIX}*.plist; do
  name=$(basename "$plist")
  if [ -n "$ONLY" ] && [ "$name" != "${PREFIX}${ONLY}.plist" ]; then
    continue
  fi
  rendered=$(sed "s|$PLACEHOLDER|$REPO_DIR|g" "$plist")

  args=$(printf '%s\n' "$rendered" | awk '
    /<key>ProgramArguments<\/key>/ { inblock = 1 }
    inblock && /<string>/ { gsub(/.*<string>|<\/string>.*/, ""); print }
    inblock && /<\/array>/ { exit }
  ')
  interpreter=$(printf '%s\n' "$args" | head -1)
  module=$(printf '%s\n' "$args" | awk 'prev == "-m" { print; exit } { prev = $0 }')

  problem=""
  if [ ! -x "$interpreter" ]; then
    problem="interpreter $interpreter (run: python3 -m venv .venv && .venv/bin/pip install -e .)"
  elif [ -n "$module" ]; then
    if ! (cd "$REPO_DIR" && "$interpreter" -c "
import importlib.util, sys
sys.exit(0 if importlib.util.find_spec('$module') else 1)" 2>/dev/null); then
      problem="module $module (not importable from $REPO_DIR)"
    fi
  fi
  if [ -n "$problem" ]; then
    echo "  skip     $name"
    echo "           missing $problem"
    skipped=$((skipped + 1))
    continue
  fi
  if [ "$DRY_RUN" = "1" ]; then
    echo "  would install  $name"
    installed=$((installed + 1))
    continue
  fi

  echo "  install  $name"
  printf '%s\n' "$rendered" > "$TARGET_DIR/$name"

  # Rotate existing logs so launchd creates the files itself (trap 2 above).
  # `while read`, not `for ... in $(...)`: paths may contain spaces.
  printf '%s\n' "$rendered" | awk '
      /<key>Standard(Out|Error)Path<\/key>/ { getline; gsub(/.*<string>|<\/string>.*/, ""); print }
  ' | sort -u | while IFS= read -r logfile; do
    [ -n "$logfile" ] && [ -f "$logfile" ] || continue
    mv "$logfile" "$logfile.$(date +%Y%m%d-%H%M%S).bak"
    echo "           rotated existing $(basename "$logfile")"
  done

  service="gui/$(id -u)/${name%.plist}"
  launchctl bootout "$service" 2>/dev/null || true
  waited=0
  while launchctl print "$service" >/dev/null 2>&1; do
    if [ "$waited" -ge "$BOOTOUT_WAIT_SEC" ]; then
      echo "           still shutting down after ${BOOTOUT_WAIT_SEC}s"; break
    fi
    sleep 1; waited=$((waited + 1))
  done
  if launchctl bootstrap "gui/$(id -u)" "$TARGET_DIR/$name" 2>/dev/null \
    || { sleep 2; launchctl bootstrap "gui/$(id -u)" "$TARGET_DIR/$name"; }; then
    installed=$((installed + 1))
  else
    echo "  FAILED   $name did not load; retry with:"
    echo "           launchctl bootstrap gui/$(id -u) \"$TARGET_DIR/$name\""
    failed=$((failed + 1))
  fi
done

if [ "$DRY_RUN" != "1" ] && [ "$installed" -gt 0 ]; then
  # The dead-man's switch treats a never-run job as stale only once the install
  # is older than that job's limit. Stamp the install.
  date +%s > "$MARKER_DIR/installed"
fi

echo "installed $installed, skipped $skipped, failed $failed"
[ "$failed" -eq 0 ]
