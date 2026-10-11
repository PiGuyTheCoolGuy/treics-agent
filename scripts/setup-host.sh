#!/usr/bin/env bash
# Run on the Ubuntu R620 host. The agent controller is installed only in the VM.
set -euo pipefail
treic_repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients virtinst cloud-image-utils \
    curl git openssh-client python3 iptables
if [[ ! -c /dev/kvm ]]; then
    echo 'KVM is unavailable. Enable Intel VT-x in BIOS and check ls -l /dev/kvm.' >&2
    exit 1
fi
if ! command -v ollama >/dev/null; then
    curl -fsSL https://ollama.com/install.sh | sh
fi
sudo systemctl enable --now ollama libvirtd
sudo install -d -m 755 /opt/treics-agent-host /etc/treics-agent /etc/libvirt/hooks/network.d /etc/libvirt/hooks/qemu.d
sudo install -m 755 "$treic_repo_dir/scripts/host-firewall.sh" /opt/treics-agent-host/firewall.sh
sudo install -m 644 "$treic_repo_dir/scripts/model-proxy.py" /opt/treics-agent-host/model-proxy.py
sudo python3 - <<'PY'
import os
import secrets
from pathlib import Path

path = Path('/etc/treics-agent/model-proxy.env')
if not path.exists():
    with path.open('x') as file:
        os.chmod(path, 0o600)
        file.write('MODEL_PROXY_TOKEN=' + secrets.token_urlsafe(32) + '\n')
        file.write('MODEL_PROXY_BIND=192.168.152.1\n')
        file.write('MODEL_PROXY_MODELS=qwen3-vl:30b-a3b-instruct,qwen3-vl:8b\n')
network_hook = Path('/etc/libvirt/hooks/network.d/90-treic-agent-firewall')
network_hook.write_text('''#!/bin/bash
set -euo pipefail
if [[ "$1" == 'treic-agent' && "$2" == 'started' ]]; then
    /opt/treics-agent-host/firewall.sh
fi
''')
network_hook.chmod(0o755)
qemu_hook = Path('/etc/libvirt/hooks/qemu.d/90-treic-agent-firewall')
qemu_hook.write_text('''#!/bin/bash
set -euo pipefail
if [[ "$1" == 'treic-agent' && "$2" == 'prepare' ]]; then
    /opt/treics-agent-host/firewall.sh
fi
''')
qemu_hook.chmod(0o755)
Path('/etc/systemd/system/treic-agent-firewall.service').write_text('''[Unit]
Description=Network boundary for the Treic agent VM
After=libvirtd.service
PartOf=libvirtd.service

[Service]
Type=oneshot
ExecStart=/opt/treics-agent-host/firewall.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
''')
Path('/etc/systemd/system/treic-model-proxy.service').write_text('''[Unit]
Description=Authenticated model-only gateway for the Treic agent VM
After=network-online.target libvirtd.service treic-agent-firewall.service ollama.service
Wants=network-online.target
Requires=treic-agent-firewall.service

[Service]
DynamicUser=yes
EnvironmentFile=/etc/treics-agent/model-proxy.env
ExecStart=/usr/bin/python3 /opt/treics-agent-host/model-proxy.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_UNIX

[Install]
WantedBy=multi-user.target
''')
PY
sudo systemctl daemon-reload
# Restarting libvirtd loads the new hooks; already-running guests continue running.
sudo systemctl restart libvirtd
sudo systemctl enable --now treic-agent-firewall.service
if [[ "${1:-}" != '--skip-model-pull' ]]; then
    ollama pull qwen3-vl:30b-a3b-instruct
fi
echo 'Host prepared. create-vm.sh starts the gateway after its bridge exists.'
