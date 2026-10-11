#!/usr/bin/env bash
set -euo pipefail
exec ssh -i "$HOME/.ssh/treic-agent-vm" \
    -o "UserKnownHostsFile=$HOME/.config/treics-agent-vm/known_hosts" \
    -o StrictHostKeyChecking=yes agent@192.168.152.10 "$@"
