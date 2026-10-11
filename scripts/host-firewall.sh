#!/usr/bin/env bash
# Scoped to this guest bridge. Do not flush Docker, host, or other libvirt rules.
set -euo pipefail
treic_bridge='virbr-treic'
treic_gateway='192.168.152.1'
for treic_chain in TREIC_AGENT_IN TREIC_AGENT_FWD; do
    iptables -w -N "$treic_chain" 2>/dev/null || true
    iptables -w -F "$treic_chain"
done
iptables -w -A TREIC_AGENT_IN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -w -A TREIC_AGENT_IN -d "$treic_gateway" -p udp --dport 53 -j ACCEPT
iptables -w -A TREIC_AGENT_IN -d "$treic_gateway" -p tcp --dport 53 -j ACCEPT
iptables -w -A TREIC_AGENT_IN -p udp --dport 67 -j ACCEPT
iptables -w -A TREIC_AGENT_IN -d "$treic_gateway" -p tcp --dport 11435 -j ACCEPT
iptables -w -A TREIC_AGENT_IN -j REJECT
for treic_subnet in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 \
    172.16.0.0/12 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4; do
    iptables -w -A TREIC_AGENT_FWD -d "$treic_subnet" -j REJECT
done
iptables -w -A TREIC_AGENT_FWD -j RETURN
# Reinsert at the start so libvirt NAT accepts cannot bypass the private-network rules.
while iptables -w -C INPUT -i "$treic_bridge" -j TREIC_AGENT_IN 2>/dev/null; do
    iptables -w -D INPUT -i "$treic_bridge" -j TREIC_AGENT_IN
done
while iptables -w -C FORWARD -i "$treic_bridge" -j TREIC_AGENT_FWD 2>/dev/null; do
    iptables -w -D FORWARD -i "$treic_bridge" -j TREIC_AGENT_FWD
done
iptables -w -I INPUT 1 -i "$treic_bridge" -j TREIC_AGENT_IN
iptables -w -I FORWARD 1 -i "$treic_bridge" -j TREIC_AGENT_FWD
for treic_table_chain in INPUT FORWARD; do
    while ip6tables -w -C "$treic_table_chain" -i "$treic_bridge" -j REJECT 2>/dev/null; do
        ip6tables -w -D "$treic_table_chain" -i "$treic_bridge" -j REJECT
    done
    ip6tables -w -I "$treic_table_chain" 1 -i "$treic_bridge" -j REJECT
done
