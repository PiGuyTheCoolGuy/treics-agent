#!/usr/bin/env bash
# Run on the Ubuntu R620 host. The agent controller is installed only in the VM.
set -euo pipefail
treic_repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients virtinst cloud-image-utils \
    curl git openssh-client python3 iptables zstd
if [[ ! -c /dev/kvm ]]; then
    echo 'KVM is unavailable. Enable Intel VT-x in BIOS and check ls -l /dev/kvm.' >&2
    exit 1
fi
if ! command -v ollama >/dev/null || ! ollama --version >/dev/null 2>&1 || ! sudo systemctl cat ollama.service >/dev/null 2>&1; then
    # Download a fixed release to disk; never pipe a partial download into tar.
    treic_version="$(curl --fail --silent --show-error --location --retry 5 \
        https://api.github.com/repos/ollama/ollama/releases/latest | \
        python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')"
    if [[ ! "$treic_version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "Unexpected Ollama release: $treic_version" >&2
        exit 1
    fi
    treic_cache="${XDG_CACHE_HOME:-$HOME/.cache}/treics-agent"
    mkdir -p "$treic_cache"
    treic_archive="$treic_cache/ollama-linux-amd64-$treic_version.tar.zst"
    treic_url="https://github.com/ollama/ollama/releases/download/$treic_version/ollama-linux-amd64.tar.zst"
    treic_downloaded=false
    for treic_attempt in {1..20}; do
        if [[ -s "$treic_archive" ]] && tar --zstd -tf "$treic_archive" >/dev/null 2>&1; then
            treic_downloaded=true
            break
        fi
        echo "Downloading Ollama $treic_version (attempt $treic_attempt/20; resuming saved bytes)..."
        if curl --http1.1 --fail --location --show-error --connect-timeout 30 \
            --speed-limit 1024 --speed-time 120 --continue-at - \
            --output "$treic_archive" "$treic_url"; then
            if tar --zstd -tf "$treic_archive" >/dev/null 2>&1; then
                treic_downloaded=true
                break
            fi
            echo 'Downloaded archive is invalid; discarding it before retrying.' >&2
            rm -f "$treic_archive"
        else
            treic_curl_status=$?
            # A server that refuses ranges requires a fresh download.
            if [[ "$treic_curl_status" == 33 ]]; then
                rm -f "$treic_archive"
            fi
        fi
        if [[ "$treic_attempt" -lt 20 ]]; then sleep 5; fi
    done
    if [[ "$treic_downloaded" != true ]]; then
        echo "Download interrupted. Saved bytes remain at $treic_archive; rerun setup to resume." >&2
        exit 1
    fi
    sudo systemctl stop ollama.service 2>/dev/null || true
    sudo rm -rf /usr/local/lib/ollama
    sudo tar --zstd -xf "$treic_archive" -C /usr/local
    if ! id ollama >/dev/null 2>&1; then
        sudo useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama
    fi
    sudo tee /etc/systemd/system/ollama.service >/dev/null <<'UNIT'
[Unit]
Description=Ollama Service
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/ollama serve
User=ollama
Group=ollama
Restart=always
RestartSec=3
Environment="OLLAMA_HOST=127.0.0.1:11434"

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
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
    treic_model_ready=false
    for treic_attempt in {1..20}; do
        if ollama pull qwen3-vl:30b-a3b-instruct; then
            treic_model_ready=true
            break
        fi
        if [[ "$treic_attempt" -lt 20 ]]; then
            echo "Model pull failed; retrying in 10 seconds ($treic_attempt/20)." >&2
            sleep 10
        fi
    done
    if [[ "$treic_model_ready" != true ]]; then
        echo 'Model pull failed after 20 attempts. Rerun create-vm.sh to try again.' >&2
        exit 1
    fi
fi
echo 'Host prepared. create-vm.sh starts the gateway after its bridge exists.'
