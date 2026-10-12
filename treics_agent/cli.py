from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .agent import Agent
from .config import Config, config_path, initialize_config, load_config, set_task_checklist
from .model import ModelClient
from .server import AgentHTTPServer
from .storage import Store
from .tools import Desktop


def in_vm() -> bool:
    command = shutil.which("systemd-detect-virt")
    return bool(command and subprocess.run([command, "--vm", "--quiet"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0)


def doctor(cfg: Config, test_model: bool = False) -> int:
    problems = []
    print(f"Configuration: {config_path()}\nModel: {cfg.model}\nOllama: {cfg.ollama_url}\nDesktop: {cfg.display}")
    print("Virtual machine:", "yes" if in_vm() else "not detected")
    for command in ("xdotool", "scrot", "xclip", "Xvfb", "dbus-run-session", cfg.browser):
        found = shutil.which(command)
        print(f"{command}: {found or 'MISSING'}")
        if not found:
            problems.append(f"Missing {command}")
    try:
        screen = Desktop(cfg).capture()
        print(f"Screen: {screen['desktop_width']}×{screen['desktop_height']} "
              f"(model sees {screen['width']}×{screen['height']})")
    except Exception as exc:
        problems.append(f"Desktop: {exc}")
    try:
        client = ModelClient(cfg)
        version = client.request("GET", "/api/version")
        print("Ollama version:", version.get("version", "unknown"))
        details = client.request("POST", "/api/show", {"model": cfg.model})
        capabilities = details.get("capabilities", [])
        print("Model capabilities:", ", ".join(capabilities) or "not reported")
        if capabilities and "vision" not in capabilities:
            problems.append("Selected model does not support images. Use Qwen3-VL, not text-only Qwen3.")
        if test_model:
            from PIL import Image
            import base64
            import io
            import time

            output = io.BytesIO()
            Image.new("RGB", (128, 128), "red").save(output, format="PNG")
            start = time.monotonic()
            reply = client.request("POST", "/api/chat", {
                "model": cfg.model, "stream": False, "keep_alive": cfg.keep_alive,
                "format": {"type": "object", "properties": {"color": {"type": "string"}}, "required": ["color"]},
                "messages": [{"role": "user", "content": "Return the image color as JSON, e.g. {\"color\":\"red\"}.",
                              "images": [base64.b64encode(output.getvalue()).decode()]}],
                "options": {"num_ctx": cfg.num_ctx, "num_thread": cfg.num_thread, "num_predict": 100, "temperature": 0}})
            answer = json.loads(reply["message"]["content"])
            print(f"Vision + JSON test: {answer}; {time.monotonic() - start:.1f}s (includes cold load if needed)")
            if "red" not in str(answer.get("color", "")).lower():
                problems.append("Vision smoke test did not identify the red image.")
    except Exception as exc:
        problems.append(f"Model: {exc}")
    for problem in problems:
        print("FAIL:", problem)
    if not problems:
        print("Ready.")
    return bool(problems)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Treic's Agent — run the controller inside your desktop VM.")
    parser.add_argument("--config", type=Path, help="configuration file")
    parser.add_argument("--allow-non-vm", action="store_true", help="explicitly allow controller execution outside a VM")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create a local config with a random dashboard token")
    check = commands.add_parser("doctor", help="check desktop and Ollama connectivity")
    check.add_argument("--test-model", action="store_true", help="also run a real vision + structured JSON request")
    commands.add_parser("token", help="print this guest's dashboard access token")
    commands.add_parser("serve", help="serve the control dashboard")
    run_parser = commands.add_parser("run", help="run a task directly in the VM terminal")
    run_parser.add_argument("task")
    run_parser.add_argument("--max-steps", type=int)
    commands.add_parser("memory", help="print saved cross-run notes")
    checklist_parser = commands.add_parser("checklist", help="enable/disable the optional task checklist")
    checklist_parser.add_argument("mode", choices=("on", "off"))
    args = parser.parse_args(argv)
    try:
        if args.config:
            os.environ["TREICS_AGENT_CONFIG"] = str(args.config.expanduser())
        if args.command == "init":
            print(initialize_config(args.config))
            return 0
        if args.command == "checklist":
            path = set_task_checklist(args.mode == "on", args.config)
            print(f"Task checklist {args.mode}: {path}. Restart the controller to apply.")
            return 0
        cfg = load_config(args.config)
        if args.command == "token":
            if not cfg.dashboard_token:
                raise ValueError("No token configured; run treics-agent init first.")
            print(cfg.dashboard_token)
            return 0
        if args.command == "doctor":
            return doctor(cfg, args.test_model)
        if args.command in ("run", "serve") and not args.allow_non_vm and not in_vm():
            raise ValueError("No VM detected. Run this controller INSIDE the guest. "
                             "Use --allow-non-vm only if you intentionally want access to this machine.")
        controller_handle = None
        if args.command in ("run", "serve"):
            cfg.data_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            controller_handle = (cfg.data_path / "controller.lock").open("a")
            try:
                fcntl.flock(controller_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                controller_handle.close()
                raise ValueError("Another controller is running. Use its dashboard or stop its service first.") from exc
        store = Store(cfg.data_path)
        if args.command == "memory":
            print(store.memory())
            store.close()
            return 0
        agent = Agent(cfg, store)
        if args.command == "run":
            run_id = agent.start(args.task, args.max_steps)
            print(f"Run {run_id}. Logs: {cfg.data_path / 'agent.db'}", flush=True)
            try:
                while agent.thread.is_alive():
                    agent.thread.join(timeout=0.5)
            except KeyboardInterrupt:
                agent.shutdown()
            result = store.run(run_id)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            store.close()
            return 0 if result["status"] == "completed" else 1
        server = AgentHTTPServer((cfg.bind, cfg.port), agent)

        def shutdown(signum, frame):
            agent.stop()
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        print(f"Dashboard listening on http://{cfg.bind}:{cfg.port}; authentication required.", flush=True)
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            agent.shutdown()
            server.server_close()
            if not agent.thread or not agent.thread.is_alive():
                store.close()
        return 0
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
