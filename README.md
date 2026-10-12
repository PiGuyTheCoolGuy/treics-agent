# Treic's Agent

A local desktop agent for the Dell R620: **Ollama runs the model on the host; the controller and all of its tools run inside a KVM VM.** Give it a task through the dashboard, watch its live desktop and action log, and pause or stop it when needed.

The default model is **`qwen3-vl:30b-a3b-instruct`**, the quantized vision model we discussed. This is a working controller, not a claim that the model will complete every task reliably. It uses the model to choose actions, executes them, and returns actual results to the next model call.

## What it can do

- See the desktop, click, double-click, right-click, drag, scroll, paste Unicode text, and use keyboard shortcuts.
- Inspect small targets with `zoom_screenshot`; clicks in the crop map back to the desktop automatically.
- See a red last-click crosshair on the live desktop/VNC and dashboard preview, with coordinates in the log.
- Stream the desktop and mouse pointer to the dashboard while the model is thinking, paused, or idle.
- Open Firefox, run Bash commands, read/write files, install guest software with `sudo`, and verify its work.
- Keep a working summary within a run and persistent notes between runs.
- Save run history and tool results in SQLite, with the most recent screenshot saved per run.
- Start, pause, resume, and stop a task through an authenticated dashboard on port **8010**.

It acts as the guest's `agent` user, with passwordless sudo **inside the guest**. The model never receives a host SSH key or a host shell tool. The dedicated network permits public Internet access and the host model gateway, and blocks other host services, private networks, and IPv6. This is practical isolation for a desktop assistant; it is not a hardened malware-analysis lab.

## Set it up on treicserver

Run these commands **on the Ubuntu server**, as your normal `treic` user:

```bash
git clone https://github.com/PiGuyTheCoolGuy/treics-agent.git
cd treics-agent
bash scripts/create-vm.sh
```

The script installs KVM/libvirt, reuses or installs Ollama, downloads the model, and creates an **Ubuntu 24.04 VM with 8 GB RAM, 6 vCPU and a 60 GB disk**. It verifies the Ubuntu image checksum, configures its network, and installs the desktop and agent using cloud-init. BIOS virtualization must be enabled. Allow roughly **100 GB of free host disk space** for the VM's possible growth, model, download, and headroom.

If the model is already downloaded, `bash scripts/create-vm.sh --skip-model-pull` skips that step. Existing VMs or disks named `treic-agent` are never overwritten by this script. See [the setup guide](docs/SETUP.md) for existing-VM installation, logs, updates, and desktop viewing.

Once SSH is ready, verify the guest and get its dashboard token:

```bash
# If SSH says "Connection refused", give first boot a little longer and retry.
bash scripts/ssh-vm.sh 'cloud-init status --wait'
bash scripts/ssh-vm.sh '~/treics-agent/.venv/bin/treics-agent doctor --test-model'
bash scripts/ssh-vm.sh '~/treics-agent/.venv/bin/treics-agent token'
```

The vision test actually sends a red image to your model and checks its structured response. It reports the elapsed time, including a model load if needed. The first call can take longer than later calls on your CPU.

On **your laptop**, substitute your server's reachable IP or hostname:

```bash
ssh -N -L 8010:192.168.152.10:8010 treic@YOUR_SERVER_IP
```

Leave that connection open. Visit **[http://localhost:8010](http://localhost:8010)**, paste the token, and start a task. The port is forwarded over SSH; no Boingo port forwarding is required if you can already SSH to the server.

Try this first:

> Create `hello.txt` in your workspace containing “Hello Owen” and “你好”. Read it back to verify it. Open Firefox to https://example.com, inspect the screenshot, then finish with a summary.

The desktop starts automatically even though nobody is logged into a physical display. The VM and both guest services start at boot. A controller restart marks an unfinished task interrupted; it does **not** silently resume actions.

## Defaults for your CPU-only server

| Setting | Default |
| --- | --- |
| Vision model | `qwen3-vl:30b-a3b-instruct` |
| Ollama worker threads | 16, a starting point for your dual E5-2660 v2 |
| Context window | 8,192 tokens |
| Model request timeout | 20 minutes |
| Model keep-alive | 30 minutes |
| Desktop and maximum screenshot | 1024 × 768 |
| Live dashboard feed | 5 FPS target, JPEG quality 70; configurable |
| Task budget | 100 steps, 4 hours |
| Shell command timeout | 120 seconds; up to 600 per command |

Only the latest screenshot is sent to the model. Terminal/file steps use text results and omit old images. The agent can request a new screenshot when it returns to GUI work. Actual latency needs to be measured on your server; this repository contains no R620 benchmark results. An 8B model is also supported if you want to compare it later.

For a small target, the model can request `zoom_screenshot` with `x`, `y`, `width`, and `height` in the **current image's pixels**. A fresh native-resolution crop is enlarged (default 3×, optional `scale` 2–6) within the same screenshot size limit. Click, move, drag, and scroll then use local crop coordinates. The controller applies the crop offset and scaling; the next ordinary desktop action restores the full view. Zooming makes existing pixels easier to inspect; it cannot recover detail absent from the native desktop.

The last successful `click` leaves a hollow red crosshair on the actual desktop and the dashboard's full-desktop preview. It passes mouse input through and never takes keyboard focus. The marker stays until the next click or controller restart; pointer movement and scrolling do not move it. Model images exclude the marker. Each click result records the source image coordinates, mapped desktop coordinates, and pointer coordinates sampled immediately before sending the click. The dashboard also reports when the model is inspecting a crop. To close the active window, the model is instructed to prefer `Alt+F4`. For mouse-only closing, it is instructed to dismiss obscuring menus with Escape and locate the X on the window's actual title bar, rather than XFCE's top-panel account menu.

The dashboard starts a live desktop stream when you connect. It keeps showing desktop changes during long model calls, terminal work, pause, and idle time. **Pause live view** freezes the feed while the task keeps running; **Resume live view** reconnects. A hidden browser tab suspends its stream until visible again. Automatic reconnection handles a dropped connection. The stream uses the same dashboard token, port, and SSH tunnel, and shows the full desktop even while the model inspects a zoomed crop.

One separate capture worker serves all viewers (up to four) and stops when the last viewer disconnects. Only the latest JPEG is retained in memory; live frames are not added to the model context or saved as a recording. Set `stream_fps = 5` (1–15) and `stream_quality = 70` (30–95) in the guest config to adjust smoothness/bandwidth, then restart the controller. The worker uses Pillow's native X11 capture and includes the hardware pointer through XFIXES when available. No extra viewer port or video service is required.

Dashboard status and cached screenshot reads remain available while desktop actions run. Marker X11 work is isolated in a helper with a three-second response deadline; a stuck helper is killed and the dashboard preview keeps its annotation. Streaming has separate worker/read/write deadlines and cannot acquire the desktop action lock. Status and Pause/Stop controls remain independent of viewers and the model's screenshot schedule.

## Verification

```bash
# Python 3.11+ with project dependencies installed; no model needed for these regression tests.
python3 -m unittest discover -s tests -v
```

The regression suite exercises real shell/file operations, cancellation, pause/resume, persistence, HTTP authentication, zoom and nested-crop coordinate mapping, click-marker separation from model images, streaming worker sharing/cleanup, authenticated multipart delivery, controls during streaming, the Ollama request format and image handling, the gateway's model/API limits, and disconnect cancellation using a local simulated Ollama endpoint. An optional Xvfb test checks real marker behavior and live changing pixels/pointer capture without altering model frames. Node tests exercise the actual dashboard parser, fragmented frames, rendering, pause/reconnect, and request deadlines. It does not test Qwen's task accuracy or boot a KVM guest. Run `doctor --test-model` and the example task after provisioning to verify the real desktop/model integration.

More detail: [setup and troubleshooting](docs/SETUP.md) · [architecture and operation](docs/ARCHITECTURE.md).
