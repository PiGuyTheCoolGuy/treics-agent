from __future__ import annotations

import copy
import dataclasses
import json
import threading
import time
from pathlib import Path

from .config import Config, config_path, set_model, set_stall_attempts, validate_model_name, validate_stall_attempts
from .checklist import Checklist
from .model import Cancelled, ModelClient, ModelError
from .progress import ProgressGuard
from .storage import Store
from .tools import ToolError, Tools


TERMINAL_STATES = {"completed", "failed", "stopped", "limit_reached", "interrupted"}


class Agent:
    def __init__(self, config: Config, store: Store, tools: Tools | None = None,
                 model: ModelClient | None = None, config_file: Path | None = None):
        self.config = config
        self.config_file = (config_file or config_path()).expanduser()
        self.store = store
        self.tools = tools or Tools(config, store)
        self.model = model or ModelClient(config)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.stop_event = threading.Event()
        self.paused = False
        self.pause_reason: str | None = None
        self.thread: threading.Thread | None = None
        self.current: str | None = None
        self.previewing = False
        self.model_switching = False
        self.stall_setting_saving = False
        self.stall_policy_version = 0
        self.phase = "idle"
        self.last_model_seconds: float | None = None
        self.phase_started = time.time()
        self.store.recover()

    def set_phase(self, phase: str) -> None:
        with self.lock:
            self.phase = phase
            self.phase_started = time.time()

    def snapshot(self) -> dict:
        with self.lock:
            result = {"current": self.store.run(self.current) if self.current else None,
                    "phase": self.phase, "phase_started": self.phase_started,
                    "last_model_seconds": self.last_model_seconds,
                    "pause_reason": self.pause_reason,
                    "task_checklist": self.config.task_checklist,
                    "model": self.config.model, "ollama_url": self.config.ollama_url,
                    "workspace": str(self.config.work_path), "runs": self.store.runs(),
                    "previewing": self.previewing, "model_switching": self.model_switching,
                    "stall_attempts": self.config.stall_attempts, "stall_setting_saving": self.stall_setting_saving,
                    "task_active": bool(self.thread and self.thread.is_alive())}
        # Never hold the control lock while consulting desktop state.
        return {**result, **self.tools.desktop.snapshot()}

    def available_models(self) -> dict:
        with self.lock:
            config = self.config
        # Discovery must never overwrite the inference client's cancellable socket.
        names = ModelClient(config).available_models()
        with self.lock:
            return {"model": self.config.model, "models": names}

    def switch_model(self, name: str) -> dict:
        validate_model_name(name)
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("Finish or stop the current task before switching models.")
            if self.model_switching or self.stall_setting_saving:
                raise ValueError("A settings change is already in progress. Try again shortly.")
            self.model_switching = True
            config = self.config
        try:
            # Network checks and disk writes run without holding the dashboard control lock.
            ModelClient(config).check_model(name)
            selected = dataclasses.replace(config, model=name)
            set_model(name, self.config_file)
            with self.lock:
                self.config = selected
                self.model = ModelClient(selected)
                self.last_model_seconds = None
            return {"model": name}
        finally:
            with self.lock:
                self.model_switching = False

    def capture_preview(self) -> dict:
        with self.lock:
            if self.previewing or (self.thread and self.thread.is_alive()):
                raise ValueError("Desktop capture is already active or updates automatically during a run.")
            self.previewing = True
        try:
            return self.tools.desktop.capture()
        finally:
            with self.lock:
                self.previewing = False

    def set_stall_control(self, attempts: int) -> dict:
        validate_stall_attempts(attempts)
        with self.lock:
            if self.model_switching or self.stall_setting_saving:
                raise ValueError("A settings change is already in progress. Try again shortly.")
            self.stall_setting_saving = True
        try:
            set_stall_attempts(attempts, self.config_file)
            with self.lock:
                if self.config.stall_attempts != attempts:
                    self.stall_policy_version += 1
                self.config = dataclasses.replace(self.config, stall_attempts=attempts)
                if self.thread and self.thread.is_alive():
                    step = self.store.run(self.current)["step"]
                    self.store.event(self.current, step, "stall_control", {
                        "stall_attempts": attempts,
                        "message": (f"User enabled automatic pause after {attempts} matching attempts."
                                    if attempts else "User selected manual stall control; Pause/Stop remain available.")})
            return {"stall_attempts": attempts}
        finally:
            with self.lock:
                self.stall_setting_saving = False

    def start(self, task: str, max_steps: int | None = None) -> str:
        if not isinstance(task, str) or not task.strip() or len(task) > 12000:
            raise ValueError("Task must contain 1–12000 characters.")
        max_steps = self.config.max_steps if max_steps is None else max_steps
        if type(max_steps) is not int or not 1 <= max_steps <= 10000:
            raise ValueError("max_steps must be an integer from 1 to 10000.")
        with self.lock:
            if self.model_switching:
                raise ValueError("A model change is being checked. Try starting the task again shortly.")
            if self.previewing:
                raise ValueError("A desktop preview is being captured. Try starting the task again shortly.")
            if self.thread and self.thread.is_alive():
                raise ValueError("A task is already active. Stop it before starting another.")
            self.stop_event.clear()
            self.paused = False
            self.pause_reason = None
            self.current = self.store.create_run(task.strip(), max_steps, self.config.model, self.config.task_checklist)
            self.tools.run_id = self.current
            self.tools.desktop.run_directory = self.config.data_path / "runs" / self.current
            self.set_phase("starting")
            self.thread = threading.Thread(target=self._run, args=(self.current,), daemon=True,
                                           name="treic-agent-run")
            self.thread.start()
            return self.current

    def pause(self) -> None:
        with self.condition:
            if not self.thread or not self.thread.is_alive() or self.stop_event.is_set():
                raise ValueError("No running task to pause.")
            self.paused = True
            self.store.update(self.current, status="pausing")
            self.condition.notify_all()

    def resume(self) -> None:
        with self.condition:
            if not self.paused or not self.thread or not self.thread.is_alive() or self.stop_event.is_set():
                raise ValueError("No paused task to resume.")
            self.paused = False
            if self.pause_reason:
                self.store.event(self.current, self.store.run(self.current)["step"], "resumed",
                                 {"message": "Resumed after a detected loop. Choose a different approach "
                                             "or finish with the blocker; do not repeat the stalled sequence."})
                self.pause_reason = None
                self.store.update(self.current, status="running", summary="")
            else:
                self.store.update(self.current, status="running")
            self.condition.notify_all()

    def stop(self) -> None:
        with self.condition:
            if not self.thread or not self.thread.is_alive():
                return
            self.stop_event.set()
            self.store.update(self.current, status="stopping")
            self.condition.notify_all()
        self.model.cancel()
        self.tools.cancel()

    def _gate(self, deadline: float) -> None:
        with self.condition:
            while self.paused and not self.stop_event.is_set():
                self.store.update(self.current, status="paused")
                self.set_phase("paused")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Run time limit reached while paused.")
                self.condition.wait(timeout=0.5)
            if self.stop_event.is_set():
                raise Cancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError("Run time limit reached.")

    def _context_events(self, run_id: str) -> list[dict]:
        multiplier = 4 if self.config.task_checklist else 3
        events = [e for e in self.store.events(run_id, self.config.history_steps * multiplier)
                  if e['kind'] not in {'action_started', 'checklist_updated'}][-self.config.history_steps:]
        events = copy.deepcopy(events)
        for event in events:
            # Keep logs complete on disk, but avoid re-sending giant scripts/output every step.
            data = event["data"]
            if "decision" in data:
                args = data["decision"].get("arguments", {})
                for key, value in args.items():
                    if isinstance(value, str) and len(value) > 1600:
                        args[key] = value[:1600] + "\n[context truncated; full action is in the run log]"
            if "result" in data:
                encoded = json.dumps(data["result"], ensure_ascii=False)
                if len(encoded) > 3500:
                    data["result"] = {"ok": data["result"].get("ok"),
                                      "excerpt": encoded[:3500], "context_truncated": True}
        return events

    def _configure_progress(self, progress: ProgressGuard) -> None:
        with self.lock:
            attempts, revision = self.config.stall_attempts, self.stall_policy_version
        progress.configure(attempts, revision)

    def _pause_stalled(self, run_id: str, step: int, reason: str, deadline: float,
                       attempts: int, revision: int) -> bool:
        with self.condition:
            if self.stop_event.is_set():
                raise Cancelled()
            if (not attempts or self.config.stall_attempts != attempts
                    or self.stall_policy_version != revision):
                return False  # The user changed the policy while the observation was being checked.
            self.paused = True
            self.pause_reason = reason
            self.store.update(run_id, status="paused", summary=reason)
            self.store.event(run_id, step, "stalled", {"message": reason})
            self.set_phase("paused")
            self.condition.notify_all()
        self._gate(deadline)
        return True

    def _run(self, run_id: str) -> None:
        run = self.store.run(run_id)
        deadline = time.monotonic() + self.config.max_run_minutes * 60
        errors = 0
        screen = None
        progress = ProgressGuard(self.config.stall_attempts, self.stall_policy_version)
        checklist = Checklist(self.store, run_id, self.config.work_path) if run["task_checklist"] else None
        try:
            self._gate(deadline)
            self.set_phase("observing")
            screen = self.tools.desktop.capture()
            for step in range(1, run["max_steps"] + 1):
                self._gate(deadline)
                self.store.update(run_id, step=step)
                self.set_phase("model")
                started = time.monotonic()
                decision = None
                action_dispatched = False
                try:
                    context_events = self._context_events(run_id)
                    checklist_context = {"checklist": checklist.snapshot()} if checklist else {}
                    decision, metrics = self.model.decide(
                        run["task"], self.store.run(run_id)["state"], self.store.memory(),
                        context_events, screen, self.stop_event,
                        budget={"current_step": step, "max_steps": run["max_steps"],
                                "steps_remaining": run["max_steps"] - step + 1},
                        filesystem=self.tools.filesystem_context(context_events), **checklist_context)
                    with self.lock:
                        self.last_model_seconds = time.monotonic() - started
                    # A pause/stop that arrived during inference must gate the proposed action.
                    self._gate(deadline)
                    if checklist:
                        if not checklist.snapshot() and decision["action"] not in {"set_checklist", "finish"}:
                            raise ModelError("Create the checklist with set_checklist before starting task tools.")
                        if updates := decision.get("checklist_updates"):
                            if checklist.apply_updates(updates):
                                self.store.event(run_id, step, "checklist_updated",
                                                 {"items": checklist.snapshot(), "message": "Evidence-checked progress update."})
                    self.set_phase("action")
                    self.store.event(run_id, step, "action_started", {"decision": decision})
                    action_dispatched = True
                    result = self.tools.execute(decision["action"], decision["arguments"], self.stop_event)
                    if result.get("ok", False) and not result.get("skipped", False):
                        self.store.update(run_id, state=decision["state"])
                    action_data = {"decision": decision, "result": result,
                                   "model_seconds": self.last_model_seconds, "metrics": metrics}
                    event_id = self.store.event(run_id, step, "action", action_data)
                    if checklist and checklist.observe({"id": event_id, "kind": "action", "data": action_data}):
                        self.store.event(run_id, step, "checklist_updated",
                                         {"items": checklist.snapshot(), "message": "Progress observed from tool results."})
                    errors = 0 if result.get("ok", False) else errors + 1
                    if decision["action"] == "finish":
                        summary = result["summary"]
                        if checklist:
                            remaining = [item for item in checklist.snapshot() if item["status"] != "verified"]
                            if remaining:
                                summary += "\n\nUnverified checklist requirements: " + "; ".join(
                                    item["title"] + " (" + item["status"] + ")" for item in remaining)
                        self.store.update(run_id, status="completed", summary=summary)
                        return
                    previous_screen = screen
                    if decision["action"] in self.tools.GUI_ACTIONS:
                        self._gate(deadline)
                        self.set_phase("observing")
                        if decision["action"] == "zoom_screenshot":
                            screen = self.tools.desktop.capture(result["capture_region"])
                        else:
                            screen = self.tools.desktop.capture()
                    else:
                        screen = None
                        # A file/terminal tool can change the GUI; don't accept stale coordinates.
                        self.tools.desktop.invalidate_frame()
                    self._configure_progress(progress)
                    reason = progress.observe(decision, result, previous_screen, screen)
                    if (reason and step < run["max_steps"]
                            and self._pause_stalled(run_id, step, reason, deadline, progress.attempt_limit, progress.revision)):
                        progress.reset()
                        # The user may have changed the desktop while it was paused.
                        self._gate(deadline)
                        self.set_phase("observing")
                        screen = self.tools.desktop.capture()
                except (ModelError, ToolError, OSError, ValueError) as exc:
                    if self.stop_event.is_set():
                        raise Cancelled() from exc
                    errors += 1
                    message = str(exc)[:2000]
                    error_data = {"message": message}
                    if decision:
                        error_data["decision"] = decision
                        error_data["message"] = decision["action"] + " failed: " + message
                        if not action_dispatched:
                            error_data["action_executed"] = False
                            error_data["message"] = "Decision rejected before tool execution: " + message
                    event_id = self.store.event(run_id, step, "error", error_data)
                    if checklist and checklist.observe({"id": event_id, "kind": "error", "data": error_data}):
                        self.store.event(run_id, step, "checklist_updated",
                                         {"items": checklist.snapshot(), "message": "Recorded unsuccessful attempt."})
                    if isinstance(exc, (ToolError, OSError)):
                        screen = None
                        self.tools.desktop.invalidate_frame()
                    self._configure_progress(progress)
                    reason = progress.error(decision, message)
                    if (reason and step < run["max_steps"]
                            and self._pause_stalled(run_id, step, reason, deadline, progress.attempt_limit, progress.revision)):
                        progress.reset()
                        errors = 0
                        self._gate(deadline)
                        self.set_phase("observing")
                        screen = self.tools.desktop.capture()
                        continue
                    if self.stop_event.wait(min(errors * 2, 10)):
                        raise Cancelled()
            self.store.update(run_id, status="limit_reached", summary="Step limit reached. Inspect the log and start a follow-up task.")
        except Cancelled:
            self.store.update(run_id, status="stopped", summary="Stopped by user. No further actions will execute.")
        except TimeoutError as exc:
            self.store.update(run_id, status="limit_reached", summary=str(exc))
        except Exception as exc:
            if self.stop_event.is_set():
                self.store.update(run_id, status="stopped", summary="Stopped by user. No further actions will execute.")
            else:
                self.store.event(run_id, self.store.run(run_id)["step"], "error", {"message": str(exc)[:2000]})
                self.store.update(run_id, status="failed", summary=str(exc)[:2000])
        finally:
            with self.lock:
                self.pause_reason = None
                self.paused = False
            self.set_phase("idle")

    def shutdown(self) -> None:
        self.stop()
        if self.thread:
            self.thread.join(timeout=15)
        if not self.thread or not self.thread.is_alive():
            with self.tools.desktop.lock:
                self.tools.desktop.marker.close()
