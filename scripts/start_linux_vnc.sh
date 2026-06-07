#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DISPLAY_NUM="${DISPLAY_NUM:-1}"
DISPLAY_VALUE=":${DISPLAY_NUM}"
VNC_PORT="${VNC_PORT:-5901}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
VNC_RESOLUTION="${VNC_RESOLUTION:-1440x900x24}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing command: $1" >&2
    return 1
  fi
}

require_cmd Xvfb
require_cmd x11vnc
require_cmd python3

export DISPLAY="${BROWSER_DISPLAY:-$DISPLAY_VALUE}"
export BROWSER_DISPLAY="$DISPLAY"
DISPLAY_ID="${DISPLAY#:}"

if ! pgrep -f "Xvfb ${DISPLAY}" >/dev/null 2>&1; then
  rm -f "/tmp/.X${DISPLAY_ID}-lock" "/tmp/.X11-unix/X${DISPLAY_ID}"
  Xvfb "$DISPLAY" -screen 0 "$VNC_RESOLUTION" -ac +extension GLX +render -noreset \
    >/tmp/fuckoai_xvfb.log 2>&1 &
  sleep 1
fi

if command -v openbox >/dev/null 2>&1 && ! pgrep -f "openbox.*${DISPLAY}" >/dev/null 2>&1; then
  DISPLAY="$DISPLAY" openbox >/tmp/fuckoai_openbox.log 2>&1 &
elif command -v fluxbox >/dev/null 2>&1 && ! pgrep -f "fluxbox.*${DISPLAY}" >/dev/null 2>&1; then
  DISPLAY="$DISPLAY" fluxbox >/tmp/fuckoai_fluxbox.log 2>&1 &
fi

if ! pgrep -f "x11vnc.*-rfbport ${VNC_PORT}" >/dev/null 2>&1; then
  x11vnc -display "$DISPLAY" -rfbport "$VNC_PORT" -forever -shared -nopw -bg \
    -o /tmp/fuckoai_x11vnc.log
fi

NOVNC_WEB_ROOT=""
for candidate in /usr/share/novnc /usr/share/novnc/html /opt/novnc; do
  if [ -d "$candidate" ]; then
    NOVNC_WEB_ROOT="$candidate"
    break
  fi
done

if command -v websockify >/dev/null 2>&1 && [ -n "$NOVNC_WEB_ROOT" ]; then
  if ! pgrep -f "websockify.*${NOVNC_PORT}.*${VNC_PORT}" >/dev/null 2>&1; then
    websockify --web="$NOVNC_WEB_ROOT" "$NOVNC_PORT" "127.0.0.1:${VNC_PORT}" \
      >/tmp/fuckoai_novnc.log 2>&1 &
  fi
  export VNC_WEB_URL="${VNC_WEB_URL:-http://127.0.0.1:${NOVNC_PORT}/vnc.html?autoconnect=1&resize=remote}"
fi

echo "DISPLAY=$DISPLAY"
echo "VNC: 127.0.0.1:${VNC_PORT}"
if [ -n "${VNC_WEB_URL:-}" ]; then
  echo "noVNC: ${VNC_WEB_URL}"
else
  echo "noVNC not started. Install novnc and websockify if browser embedding is needed."
fi

cd "$ROOT_DIR"
exec python3 server.py
