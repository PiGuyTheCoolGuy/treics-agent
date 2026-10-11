#!/usr/bin/env bash
set -euo pipefail
treic_repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$EUID" -eq 0 ]]; then
    echo 'Run this installer as the guest desktop user (agent), not root.' >&2
    exit 1
fi
if [[ "${1:-}" != '--skip-apt' ]]; then
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
        python3-venv python3-pip git curl xfce4 xfce4-terminal xvfb dbus-x11 \
        xdotool scrot xclip xauth x11-utils x11-xserver-utils openssl \
        firefox fonts-noto-cjk qemu-guest-agent x11vnc
fi
python3 -m venv "$treic_repo_dir/.venv"
"$treic_repo_dir/.venv/bin/python" -m pip install --upgrade pip
"$treic_repo_dir/.venv/bin/python" -m pip install -e "$treic_repo_dir"
"$treic_repo_dir/.venv/bin/treics-agent" init
mkdir -p "$HOME/agent-workspace" "$HOME/.config/systemd/user"
chmod 700 "$HOME/.config/treics-agent"
"$treic_repo_dir/.venv/bin/python" - "$treic_repo_dir" <<'PY'
import os
import sys
from pathlib import Path

repo = str(Path(sys.argv[1]).resolve())
def quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
quoted = quote(repo)
directory = Path.home() / '.config/systemd/user'
(directory / 'treic-desktop.service').write_text(f'''[Unit]
Description=Treic agent X11 desktop (XFCE on Xvfb)

[Service]
Type=simple
Environment=DISPLAY=:99
Environment=XAUTHORITY=%h/.Xauthority
ExecStart=/usr/bin/bash {quote(repo + '/scripts/desktop-session.sh')}
Restart=on-failure
RestartSec=3
KillMode=control-group

[Install]
WantedBy=default.target
''')
(directory / 'treics-agent.service').write_text(f'''[Unit]
Description=Treic desktop agent dashboard
Requires=treic-desktop.service
After=treic-desktop.service

[Service]
Type=simple
Environment=DISPLAY=:99
Environment=XAUTHORITY=%h/.Xauthority
WorkingDirectory={repo.replace('%', '%%')}
ExecStart={quote(repo + '/.venv/bin/treics-agent')} serve
Restart=on-failure
RestartSec=3
TimeoutStopSec=20
KillMode=control-group

[Install]
WantedBy=default.target
''')
PY
sudo loginctl enable-linger "$USER"
if [[ -e /dev/virtio-ports/org.qemu.guest_agent.0 ]]; then
    sudo systemctl enable --now qemu-guest-agent
fi
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user daemon-reload
systemctl --user enable --now treic-desktop.service treics-agent.service
echo "Guest installed. Check: $treic_repo_dir/.venv/bin/treics-agent doctor --test-model"
echo "Dashboard token: $treic_repo_dir/.venv/bin/treics-agent token"
