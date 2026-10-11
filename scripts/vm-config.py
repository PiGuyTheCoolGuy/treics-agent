#!/usr/bin/env python3
"""Generate cloud-init input locally; compatible with the host's Python 3.10."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import uuid
from pathlib import Path


MAC = "52:54:00:74:72:01"
GUEST_IP = "192.168.152.10"
PACKAGES = ["python3-venv", "python3-pip", "git", "curl", "xfce4", "xfce4-terminal", "xvfb",
            "dbus-x11", "xdotool", "scrot", "xclip", "xauth", "x11-utils", "x11-xserver-utils",
            "openssl", "firefox", "fonts-noto-cjk", "qemu-guest-agent", "x11vnc"]


def cloud_config(public_key: str, model_token: str, dashboard_token: str, host_private: str,
                 host_public: str, model: str = "qwen3-vl:30b-a3b-instruct") -> dict:
    config = {
        "ollama_url": "http://192.168.152.1:11435", "ollama_token": model_token, "model": model,
        "dashboard_token": dashboard_token, "bind": "0.0.0.0", "port": 8010,
        "num_thread": 16, "num_ctx": 8192, "num_predict": 1600,
        "request_timeout": 1200, "display": ":99", "xauthority": "~/.Xauthority",
        "work_dir": "~/agent-workspace", "max_steps": 100,
    }
    config_text = "\n".join(f"{key} = {json.dumps(value)}" for key, value in config.items()) + "\n"
    return {
        "hostname": "treic-agent", "manage_etc_hosts": True,
        "users": [{"name": "agent", "shell": "/bin/bash", "groups": ["sudo"],
                   "sudo": ["ALL=(ALL) NOPASSWD:ALL"], "lock_passwd": True,
                   "ssh_authorized_keys": [public_key.strip()]}],
        "disable_root": True, "ssh_pwauth": False,
        "ssh_keys": {"ed25519_private": host_private, "ed25519_public": host_public},
        "package_update": True, "packages": PACKAGES,
        "write_files": [{"path": "/home/agent/.config/treics-agent/config.toml", "owner": "agent:agent",
                         "permissions": "0600", "defer": True, "content": config_text}],
        "runcmd": [
            ["chown", "-R", "agent:agent", "/home/agent/.config"],
            ["bash", "-c", "loginctl enable-linger agent && systemctl start user@$(id -u agent).service"],
            ["su", "-", "agent", "-c", "git clone https://github.com/PiGuyTheCoolGuy/treics-agent.git /home/agent/treics-agent"],
            ["su", "-", "agent", "-c", "bash /home/agent/treics-agent/scripts/install-guest.sh --skip-apt"],
        ],
        "final_message": "Treic agent guest provisioning finished after $UPTIME seconds. Check cloud-init status for errors.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ssh-public-key", type=Path, required=True)
    parser.add_argument("--proxy-env", type=Path, required=True)
    args = parser.parse_args()
    output = args.output
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    variables = dict(line.split("=", 1) for line in args.proxy_env.read_text().splitlines() if "=" in line)
    credential_file = output / "credentials.json"
    credentials = json.loads(credential_file.read_text()) if credential_file.exists() else {
        "dashboard_token": secrets.token_urlsafe(32)}
    host_key = output / "ssh_host_ed25519_key"
    if not host_key.exists():
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(host_key)],
                       check=True, stdout=subprocess.DEVNULL)
    host_public = host_key.with_suffix(".pub").read_text()
    cloud = cloud_config(args.ssh_public_key.read_text(), variables["MODEL_PROXY_TOKEN"],
                         credentials["dashboard_token"], host_key.read_text(), host_public)
    files = {
        "user-data": "#cloud-config\n" + json.dumps(cloud, indent=2) + "\n",
        "meta-data": json.dumps({"instance-id": "treic-agent-" + uuid.uuid4().hex, "local-hostname": "treic-agent"}),
        "network-config": json.dumps({"version": 2, "ethernets": {"vm": {
            "match": {"macaddress": MAC}, "set-name": "ens3", "dhcp4": True, "dhcp6": False}}}),
        "known_hosts": GUEST_IP + " " + " ".join(host_public.split()[:2]) + "\n",
        "credentials.json": json.dumps(credentials, indent=2) + "\n",
    }
    uid, gid = int(os.environ.get("SUDO_UID", os.getuid())), int(os.environ.get("SUDO_GID", os.getgid()))
    for name, content in files.items():
        path = output / name
        path.write_text(content)
        path.chmod(0o600)
    if os.geteuid() == 0:
        os.chown(output, uid, gid)
        for path in output.iterdir():
            if path.is_file():
                os.chown(path, uid, gid)


if __name__ == "__main__":
    main()
