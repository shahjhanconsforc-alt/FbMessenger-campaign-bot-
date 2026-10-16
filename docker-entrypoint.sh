#!/bin/sh
# Container entrypoint.
#
# Headed mode (XBOT_HEADLESS=0, the default): the Chrome windows are shown on
# the X display named by $DISPLAY, which the VM deployment shares in from the
# web desktop container through /tmp/.X11-unix. Wait for that socket so the
# first "Open" does not race the desktop coming up, and give Xlib an Xauthority
# file (it refuses to start without one, even when the server needs no cookie).
#
# Headless mode (XBOT_HEADLESS=1): no display needed, start straight away.
set -eu

cd /app

is_headless() {
  case "$(printf '%s' "${XBOT_HEADLESS:-0}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

if ! is_headless; then
  DISPLAY="${DISPLAY:-:1}"
  export DISPLAY
  NUM="${DISPLAY#:}"; NUM="${NUM%%.*}"
  SOCK="/tmp/.X11-unix/X${NUM}"
  WAIT="${XBOT_DISPLAY_WAIT:-120}"
  i=0
  while [ ! -S "$SOCK" ] && [ "$i" -lt "$WAIT" ]; do
    [ "$i" -eq 0 ] && echo "messenger-bot: waiting for display $DISPLAY ($SOCK)…"
    i=$((i + 1)); sleep 1
  done
  if [ -S "$SOCK" ]; then
    echo "messenger-bot: display $DISPLAY is up — Chrome windows will show on the web desktop"
  else
    echo "messenger-bot: display $DISPLAY not found after ${WAIT}s — windows will fail to open until it is (the API still serves)"
  fi
  export XAUTHORITY="${XAUTHORITY:-/tmp/.Xauthority}"
  [ -f "$XAUTHORITY" ] || : > "$XAUTHORITY"
fi

# The data directory holds the live sessions; make sure it exists on a fresh
# bind mount before the server starts writing into it.
mkdir -p /app/backend/data

exec python run.py
