#!/usr/bin/env bash
# Optional: run inside the guest to inspect/control the SAME desktop the agent sees.
set -euo pipefail
mkdir -p "$HOME/.vnc" "$HOME/.config/systemd/user"
chmod 700 "$HOME/.vnc"
if [[ ! -f "$HOME/.vnc/passwd" ]]; then
    x11vnc -storepasswd "$HOME/.vnc/passwd"
fi
cat > "$HOME/.config/systemd/user/treic-vnc.service" <<'UNIT'
[Unit]
Description=Loopback VNC viewer for the Treic agent X11 desktop
Requires=treic-desktop.service
After=treic-desktop.service

[Service]
ExecStart=/usr/bin/x11vnc -display :99 -auth %h/.Xauthority -localhost -rfbport 5901 -forever -shared -rfbauth %h/.vnc/passwd
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now treic-vnc.service
echo 'VNC listens on guest loopback port 5901. See docs/SETUP.md for the two-hop SSH tunnel.'
