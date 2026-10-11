#!/usr/bin/env bash
set -euo pipefail
treic_repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
treic_state_dir="$HOME/.config/treics-agent-vm"
treic_image_dir='/var/lib/libvirt/images'
treic_disk="$treic_image_dir/treic-agent.qcow2"
treic_seed="$treic_image_dir/treic-agent-seed.iso"
if [[ "$EUID" -eq 0 ]]; then
    echo 'Run as your normal server user; the script calls sudo where needed.' >&2
    exit 1
fi
if command -v virsh >/dev/null && sudo virsh -c qemu:///system dominfo treic-agent >/dev/null 2>&1; then
    echo 'A VM named treic-agent already exists. It has not been changed. Use scripts/ssh-vm.sh.' >&2
    exit 1
fi
if [[ -e "$treic_disk" || -e "$treic_seed" ]]; then
    echo 'Existing treic-agent disk or seed found. Move it aside before creating a new VM.' >&2
    exit 1
fi
bash "$treic_repo_dir/scripts/setup-host.sh" "${1:-}"
mkdir -p "$treic_state_dir" "$HOME/.ssh"
chmod 700 "$treic_state_dir" "$HOME/.ssh"
if [[ ! -f "$HOME/.ssh/treic-agent-vm" ]]; then
    ssh-keygen -t ed25519 -N '' -f "$HOME/.ssh/treic-agent-vm" -C 'treic-agent-vm-host-access'
fi
cat > "$treic_state_dir/network.xml" <<'XML'
<network>
  <name>treic-agent</name>
  <forward mode='nat'/>
  <bridge name='virbr-treic' stp='on' delay='0'/>
  <ip address='192.168.152.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='192.168.152.10' end='192.168.152.10'/>
      <host mac='52:54:00:74:72:01' name='treic-agent' ip='192.168.152.10'/>
    </dhcp>
  </ip>
</network>
XML
if sudo virsh -c qemu:///system net-info treic-agent >/dev/null 2>&1; then
    sudo virsh -c qemu:///system net-dumpxml treic-agent > "$treic_state_dir/existing-network.xml"
    python3 - "$treic_state_dir/existing-network.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
assert root.find('bridge').get('name') == 'virbr-treic', 'Existing network bridge differs'
assert root.find('ip').get('address') == '192.168.152.1', 'Existing network subnet differs'
assert root.find('forward').get('mode') == 'nat', 'Existing network must use NAT'
PY
else
    python3 - <<'PY'
import ipaddress
import json
import subprocess
network = ipaddress.ip_network('192.168.152.0/24')
for route in json.loads(subprocess.check_output(['ip', '-j', '-4', 'route'])):
    destination = route.get('dst', 'default')
    if destination == 'default' or route.get('dev') == 'virbr-treic':
        continue
    if network.overlaps(ipaddress.ip_network(destination, strict=False)):
        raise SystemExit('192.168.152.0/24 overlaps an existing route. Adjust the documented network addresses first.')
PY
    sudo virsh -c qemu:///system net-define "$treic_state_dir/network.xml"
fi
sudo virsh -c qemu:///system net-autostart treic-agent
if ! sudo virsh -c qemu:///system net-list --name | grep -qx treic-agent; then
    sudo virsh -c qemu:///system net-start treic-agent
fi
sudo systemctl restart treic-agent-firewall.service
sudo systemctl enable --now treic-model-proxy.service
sudo python3 "$treic_repo_dir/scripts/vm-config.py" --output "$treic_state_dir" \
    --ssh-public-key "$HOME/.ssh/treic-agent-vm.pub" --proxy-env /etc/treics-agent/model-proxy.env
treic_download_dir="$(mktemp -d)"
trap 'rm -rf "$treic_download_dir"' EXIT
curl -fL --retry 3 https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img \
    -o "$treic_download_dir/noble-server-cloudimg-amd64.img"
curl -fL --retry 3 https://cloud-images.ubuntu.com/noble/current/SHA256SUMS \
    -o "$treic_download_dir/SHA256SUMS"
python3 - "$treic_download_dir" <<'PY'
import hashlib
import sys
from pathlib import Path
folder = Path(sys.argv[1])
name = 'noble-server-cloudimg-amd64.img'
entries = {line.split()[1].lstrip('*'): line.split()[0] for line in (folder / 'SHA256SUMS').read_text().splitlines() if line.strip()}
digest = hashlib.sha256()
with (folder / name).open('rb') as handle:
    while block := handle.read(1024 * 1024):
        digest.update(block)
if digest.hexdigest() != entries.get(name):
    raise SystemExit('Cloud-image checksum mismatch. Retry the download.')
PY
sudo install -d -m 755 "$treic_image_dir"
sudo qemu-img convert -f qcow2 -O qcow2 "$treic_download_dir/noble-server-cloudimg-amd64.img" "$treic_disk"
sudo qemu-img resize "$treic_disk" 60G
sudo cloud-localds --network-config="$treic_state_dir/network-config" "$treic_seed" \
    "$treic_state_dir/user-data" "$treic_state_dir/meta-data"
sudo chown libvirt-qemu:kvm "$treic_disk" "$treic_seed"
sudo chmod 600 "$treic_disk" "$treic_seed"
sudo virt-install --connect qemu:///system --name treic-agent --memory 8192 --vcpus 6 \
    --cpu host-passthrough --os-variant ubuntu22.04 --import \
    --disk "path=$treic_disk,format=qcow2,bus=virtio" \
    --disk "path=$treic_seed,device=cdrom" \
    --network network=treic-agent,model=virtio,mac=52:54:00:74:72:01 \
    --channel unix,target.type=virtio,target.name=org.qemu.guest_agent.0 \
    --graphics none --noautoconsole
sudo virsh -c qemu:///system autostart treic-agent
echo 'VM created: treic-agent, 192.168.152.10, 8 GB RAM / 6 vCPU / 60 GB disk.'
echo 'Guest provisioning takes several minutes. Once SSH is ready, run:'
echo "  bash $treic_repo_dir/scripts/ssh-vm.sh 'cloud-init status --wait'"
echo "  bash $treic_repo_dir/scripts/ssh-vm.sh '~/treics-agent/.venv/bin/treics-agent doctor --test-model'"
echo "  bash $treic_repo_dir/scripts/ssh-vm.sh '~/treics-agent/.venv/bin/treics-agent token'"
echo 'On your laptop: ssh -N -L 8010:192.168.152.10:8010 treic@YOUR_SERVER_IP'
echo 'Then open http://localhost:8010 and paste the dashboard token.'
