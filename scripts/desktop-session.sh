#!/usr/bin/env bash
set -euo pipefail
export DISPLAY="${DISPLAY:-:99}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"
mkdir -p "$(dirname "$XAUTHORITY")"
touch "$XAUTHORITY"
chmod 600 "$XAUTHORITY"
xauth add "$DISPLAY" . "$(openssl rand -hex 16)"
Xvfb "$DISPLAY" -screen 0 1024x768x24 -nolisten tcp -auth "$XAUTHORITY" &
treic_xvfb_pid=$!
trap 'kill "$treic_xvfb_pid" 2>/dev/null || true' EXIT
for treic_attempt in {1..100}; do
    if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then break; fi
    if ! kill -0 "$treic_xvfb_pid" 2>/dev/null; then
        echo 'Xvfb exited. Inspect journalctl --user -u treic-desktop.' >&2
        exit 1
    fi
    sleep 0.1
done
xdpyinfo -display "$DISPLAY" >/dev/null
xset s off
xset s noblank
xset -dpms 2>/dev/null || true
unset SESSION_MANAGER DBUS_SESSION_BUS_ADDRESS
dbus-run-session -- bash -c '
    xfconf-query -c xfce4-power-manager -p /xfce4-power-manager/dpms-enabled -n -t bool -s false
    xfconf-query -c xfce4-session -p /general/SaveOnExit -n -t bool -s false
    startxfce4
'
