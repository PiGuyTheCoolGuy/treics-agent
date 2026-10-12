# Environment setup

## 1. Host: Dell R620, Ubuntu 22.04

The host runs KVM/libvirt and Ollama. The VM runs Ubuntu 24.04, which provides Python 3.12 for the controller. You do not need a GPU or a desktop on the host. Keep your existing services running; the installer uses a separate guest network and does not replace Docker's firewall rules.

From your normal server account:

```bash
git clone https://github.com/PiGuyTheCoolGuy/treics-agent.git
cd treics-agent
bash scripts/create-vm.sh
```

The model download is about 20 GB for the default quantized tag. Qwen3-VL requires Ollama 0.12.7 or newer. If your existing installation is older, update it before provisioning:

```bash
ollama --version
curl -fsSL https://ollama.com/install.sh | sh
```

The gateway expects your existing Ollama API on `127.0.0.1:11434`. It leaves Ollama's bind settings and model storage unchanged. If you previously changed that address, make sure localhost works or adjust `upstream` in `/opt/treics-agent-host/model-proxy.py` and restart `treic-model-proxy`.

The installer uses the `ubuntu22.04` libosinfo profile for compatibility with the host's older virt-install database; the downloaded and provisioned guest OS is Ubuntu **24.04**.

| Component | Location |
| --- | --- |
| Guest VM | `treic-agent` |
| Guest address | `192.168.152.10` |
| Dedicated bridge | `virbr-treic`, gateway `192.168.152.1` |
| Guest disk | `/var/lib/libvirt/images/treic-agent.qcow2` |
| Cloud-init seed | `/var/lib/libvirt/images/treic-agent-seed.iso` |
| Model-only gateway | `192.168.152.1:11435` |
| Existing host Ollama | `127.0.0.1:11434` |
| Guest dashboard | `192.168.152.10:8010`, authenticated |
| Host's guest SSH private key | `~/.ssh/treic-agent-vm` |
| Generated SSH trust and boot files | `~/.config/treics-agent-vm/` |

The generated boot files and seed contain the guest's model/dashboard tokens. Keep them private. Your host's guest-login **private** key is never copied into the guest.

If `192.168.152.0/24` overlaps an existing route, provisioning stops. To choose another subnet, update the network addresses consistently in `create-vm.sh`, `vm-config.py`, `host-firewall.sh`, `model-proxy.py`, `setup-host.sh`, and `ssh-vm.sh` before running it. Do not reuse a bridge already used by another VM.

## 2. Wait for guest installation and check it

From the checked-out repository on the host:

```bash
bash scripts/ssh-vm.sh 'cloud-init status --wait'
bash scripts/ssh-vm.sh '~/treics-agent/.venv/bin/treics-agent doctor --test-model'
bash scripts/ssh-vm.sh '~/treics-agent/.venv/bin/treics-agent token'
```

First boot installs packages, Firefox, the Python environment, and the services. It can take several minutes. If SSH refuses the connection, retry after the guest has booted. The first command must finish without provisioning errors before proceeding. `doctor --test-model` checks X11 screenshot capture, model connectivity, vision capability, and a real image plus JSON response.

The `agent` guest user has no password login. Use the generated SSH key via `ssh-vm.sh`. For an interactive terminal:

```bash
bash scripts/ssh-vm.sh
```

## 3. Open the dashboard from your laptop

```bash
ssh -N -L 8010:192.168.152.10:8010 treic@YOUR_SERVER_IP
```

Open <http://localhost:8010> and paste the dashboard token. Use the address you already use to SSH into your R620, including a Tailscale address if applicable. If laptop port 8010 is occupied, use `-L 8012:192.168.152.10:8010` and open `http://localhost:8012`.

Start with the README example. Then try file generation, coding and verification tasks before longer GUI workflows. The dashboard shows the model wait time, chosen tools, outputs, run summaries, and the latest screen. Model-call time includes prompt processing and generation. Screenshots displayed during terminal-only work are explicitly the most recent capture, not a video feed.

## 4. Configure or switch models

Inside the VM, edit:

```bash
nano ~/.config/treics-agent/config.toml
systemctl --user restart treics-agent
```

Start with `num_thread = 16`, `num_ctx = 8192`, and 1024 × 768 images. Benchmark 12, 16 and 20 threads rather than assuming all 40 logical threads are faster. The VM's vCPU allocation does not pin or reserve particular host cores. Avoid running other CPU-heavy jobs during comparison.

To compare the smaller model, pull it **on the host**:

```bash
ollama pull qwen3-vl:8b
```

Change the guest configuration to `model = "qwen3-vl:8b"`, restart the guest controller, and run `doctor --test-model` again. Both tags are in the gateway allowlist. To use another local vision model, add its exact tag to `MODEL_PROXY_MODELS` in `/etc/treics-agent/model-proxy.env` on the host, restart `treic-model-proxy`, and set the same tag in the guest config. A text-only Qwen model cannot inspect screenshots.

The gateway limits context to 16,384 tokens, output to 4,096 tokens, threads to 20, and concurrent requests to one. Larger configuration values require adjusting those gateway limits too. Longer task budgets are set with `max_steps` and `max_run_minutes`; pause time counts against the elapsed-time budget. A model call can run until its request timeout, but no next action executes after the elapsed budget has expired.

### Optional task checklist experiment

`task_checklist = false` is the default, including configurations created before this option existed. To test the progress aid, stop the current dashboard task and run **inside the guest**:

```bash
cd ~/treics-agent
.venv/bin/treics-agent checklist on
systemctl --user restart treics-agent
```

Use `checklist off` and restart again for a comparison without the aid. The command changes only that configuration setting; it preserves tokens and other settings. The dashboard displays the configured mode and each selected run's own checklist. New runs record their mode so earlier experiments remain identifiable.

Enabled runs use one initial planning action to select up to eight concise requirements with explicit checks. The original task remains visible, and the model can revise, reorder, block, or reopen checks. The controller records pending, attempted, verified, and blocked states and updates matching checks automatically from actual tool results, without extra model calls for those updates. These checks persist beyond the recent-history window and include database event IDs for their evidence. Repeated identical plan submissions are treated as no progress.

`verified` means the stated criterion passed, not that the whole task is correct. A `file_written` check confirms a write, a `file_read` check confirms an untruncated read from offset zero, and `directory_listed` confirms a file-tool listing. `command_succeeded` confirms exit code zero, not semantic correctness. `output_equals` and `output_contains` compare the observed shell output or file content against an explicit expected value. A model-chosen expected value can itself be wrong; independent correctness checks and human review are still needed.

Optional progress updates can accompany an ordinary action, avoiding a separate action for each status change. The controller rejects nonexistent, future, foreign-run, or non-tool evidence and rejects verification based on failures, stale file reads, or newer contradicting observations before executing the proposed action. Recorded file writes reopen affected file-content/listing checks. Arbitrary shell side effects and semantic dependencies cannot be inferred automatically; the model must reopen checks when appropriate. Exiting with unverified checks remains allowed, and the dashboard summary lists them.

For comparison, keep the original prompt, model options, starting files and step budget the same. Use separate output folders for each trial. The extra planning step and bounded checklist context are part of the experiment; disabling the mode removes its tools, progress fields and instructions from model requests. This feature is a planning aid, not an approval gate or a security boundary.

## 5. Stop, restart, update, and keep a baseline

**Dashboard Stop** prevents the next action, cancels the model request, and kills the current shell process group. Pause waits for the current tool to return before stopping at the next action boundary. Stop does not undo earlier changes or kill background processes intentionally detached by previous commands.

Inside the VM:

```bash
systemctl --user stop treics-agent
systemctl --user start treics-agent
journalctl --user -u treics-agent -u treic-desktop -n 100 --no-pager
```

To update the controller, inside the guest:

```bash
systemctl --user stop treics-agent
cd ~/treics-agent
git pull --ff-only
bash scripts/install-guest.sh --skip-apt
```

From **treicserver**, the equivalent update is:

```bash
cd ~/treics-agent
git pull --ff-only
bash scripts/ssh-vm.sh 'systemctl --user stop treics-agent && cd ~/treics-agent && git pull --ff-only && bash scripts/install-guest.sh --skip-apt && systemctl --user restart treics-agent'
```

The installer refreshes Python dependencies (including python-xlib for the live last-click marker) and preserves the existing configuration. No desktop rebuild or model download is needed for the zoom/marker update. Refresh the dashboard after restarting. The full-desktop preview shows the last click in red even while the model examines a zoomed crop; a caption reports that crop's location and dimensions. If the live marker cannot connect to X11, the preview still marks clicks and the dashboard reports the live-marker error.

For an immediate halt of **all** guest activity, power off the VM from the host. A normal shutdown is preferable when it is responsive:

```bash
sudo virsh -c qemu:///system shutdown treic-agent
# Emergency power-off if needed; like pulling power and may lose unwritten data:
sudo virsh -c qemu:///system destroy treic-agent
```

`destroy` turns off the VM; it does not delete its disk. Start it again with:

```bash
sudo virsh -c qemu:///system start treic-agent
```

After installation and the smoke test, shut down the guest and create an offline baseline:

```bash
sudo virsh -c qemu:///system shutdown treic-agent
# Wait until this reports "shut off":
sudo virsh -c qemu:///system domstate treic-agent
sudo virsh -c qemu:///system snapshot-create-as treic-agent clean-install \
  --description 'Desktop and agent installed, before task work'
sudo virsh -c qemu:///system start treic-agent
```

Restoring a baseline discards later guest changes, including agent history and files. Export work you want to keep before restoring it.

## Optional: view/control the same desktop over VNC

The desktop is an Xvfb X11 session, so the VM's emulated console is not the agent's desktop. Use the dashboard for screenshots, or install the provided loopback-only VNC service **inside the guest**:

```bash
bash ~/treics-agent/scripts/install-vnc.sh
```

Choose a VNC password when prompted. On the **host**, forward the guest's loopback VNC port:

```bash
bash scripts/ssh-vm.sh -N -L 5901:127.0.0.1:5901
```

On the **laptop**, add the second hop:

```bash
ssh -N -L 5901:127.0.0.1:5901 treic@YOUR_SERVER_IP
```

Connect your VNC client to `localhost:5901`. Pause the agent before you manually operate its desktop so your input does not race its actions.

## Install in an existing VM instead

Use an Ubuntu 24.04 guest with Python 3.11+; XFCE/X11 is supplied by the script. On the guest, as the user who should own the desktop:

```bash
git clone https://github.com/PiGuyTheCoolGuy/treics-agent.git
cd treics-agent
bash scripts/install-guest.sh
nano ~/.config/treics-agent/config.toml
systemctl --user restart treics-agent
.venv/bin/treics-agent doctor --test-model
```

Set `ollama_url` to an endpoint reachable from that guest. If you use this project's model gateway, set its `ollama_token` too. The default manual installation binds the dashboard to `127.0.0.1`, so forward it from your SSH client with `-L 8010:127.0.0.1:8010`. The existing VM needs its own host/network isolation; the guest installer alone does not add the host-side firewall or gateway.

This controller can access everything that guest user can access. Install it in a VM intended for the agent. The CLI refuses to run the controller outside a detected VM unless you explicitly supply `--allow-non-vm`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `/dev/kvm` is missing | Enable Intel VT-x in BIOS; check KVM kernel modules. |
| Guest SSH refuses connections | `sudo virsh domstate treic-agent`, `sudo virsh net-dhcp-leases treic-agent`; allow first boot to finish. |
| Cloud-init reports an error | In guest: `sudo tail -n 100 /var/log/cloud-init-output.log`. Retry `bash ~/treics-agent/scripts/install-guest.sh` after correcting package/network errors. |
| Ollama connection fails | Host: `curl http://127.0.0.1:11434/api/version`; `sudo systemctl status treic-model-proxy`; `sudo journalctl -u treic-model-proxy -n 60 --no-pager`. |
| Gateway HTTP 401 | Guest `ollama_token` must match host `MODEL_PROXY_TOKEN`; dashboard token is separate. |
| Gateway HTTP 403 | Model tag must match the allowlist exactly; model-management endpoints are intentionally unavailable. |
| Gateway HTTP 429 | A previous model request is still draining; wait briefly. Only one inference is admitted. |
| No screenshot / X11 error | In guest: `systemctl --user status treic-desktop`, `DISPLAY=:99 XAUTHORITY=~/.Xauthority xdpyinfo`. |
| Task stops after five errors | Inspect the run log for the failing tool/model response; fix the underlying problem before a new task. |
| Model emits truncated JSON | Keep actions smaller; raise `num_predict` up to gateway limit 4096 if required. |
| Dashboard is unreachable | Keep the laptop SSH tunnel open; check guest `systemctl --user status treics-agent`. |
| Another controller is running | Use the existing dashboard. Stop the service before using the standalone `run` command. |

Host firewall hooks reapply the rules when the dedicated network or VM starts. If another tool such as UFW flushes/reorders firewall rules, run `sudo systemctl restart treic-agent-firewall`. The rules allow DNS/DHCP and port 11435 to the gateway, block other guest-to-host services, and block forwarding to private networks. Private-address lab targets need a deliberate, narrow allow rule on the host above the private-network rejects.

## Primary references

- [Qwen3-VL model tags and requirements](https://ollama.com/library/qwen3-vl:30b-a3b-instruct)
- [Ollama chat API](https://docs.ollama.com/api/chat) and [vision with structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
- [Ubuntu Noble cloud images](https://cloud-images.ubuntu.com/noble/current/)
- [cloud-init module reference](https://cloudinit.readthedocs.io/en/latest/reference/modules.html)
- [libvirt networks](https://libvirt.org/formatnetwork.html) and [hook lifecycle](https://libvirt.org/hooks.html)
