from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict

from .model import compact_arguments, observation_text
from .tools import Tools


class RecoveryMemory:
    """Bounded, factual per-run memory; advice only, never blocks or pauses actions."""

    MAX_FAILURES = 8
    MAX_CONTEXT = 4000

    def __init__(self):
        self.failures: OrderedDict[str, dict] = OrderedDict()
        self.references: dict[str, dict] = {}
        self.last_change_step = 0
        self.last_observation: tuple[str, str] | None = None
        self.repeated_observation: dict | None = None

    @staticmethod
    def signature(decision: dict | None, message: str = "") -> str:
        if decision:
            value = [decision["action"], decision.get("arguments", {})]
        else:
            value = ["model", message]
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def failure(self, decision: dict | None, step: int, message: str, result: dict | None = None,
                action_executed: bool | None = None) -> None:
        key = self.signature(decision, message)
        previous = self.failures.pop(key, {})
        item = {"action": decision["action"] if decision else "model",
                "arguments": compact_arguments(decision.get("arguments", {}), True) if decision else {},
                "failure_count": previous.get("failure_count", 0) + 1,
                "last_failed_step": step, "error": observation_text(message, 400, True)}
        if result:
            for name in ("exit_code", "timed_out", "cwd"):
                if name in result:
                    item[name] = observation_text(result[name], 300) if isinstance(result[name], str) else result[name]
        if action_executed is not None:
            item["action_executed"] = action_executed
        self.failures[key] = item
        if decision:
            self.references[key] = {name: value for name, value in decision.get("arguments", {}).items()
                                    if name in {"path", "cwd"} and isinstance(value, str)}
        while len(self.failures) > self.MAX_FAILURES:
            oldest, _ = self.failures.popitem(last=False)
            self.references.pop(oldest, None)
        self.last_observation = None
        self.repeated_observation = None

    def observe(self, decision: dict, result: dict, step: int,
                before: dict | None = None, after: dict | None = None) -> None:
        if result.get("ok") is False:
            self.failure(decision, step, result.get("output") or result.get("message") or "Tool reported failure.",
                         result, action_executed=not result.get("skipped", False))
            return
        key = self.signature(decision)
        self.failures.pop(key, None)
        self.references.pop(key, None)
        action = decision["action"]
        if action in Tools.GUI_ACTIONS:
            if not before or not after:
                self.last_observation = None
                self.repeated_observation = None
                return
            unchanged = all(before.get(name) == after.get(name) for name in ("width", "height", "source", "base64"))
            if not unchanged:
                self.last_observation = None
                self.repeated_observation = None
                if action not in {"screenshot", "zoom_screenshot", "wait"}:
                    self.last_change_step = step
                return
            observation = "desktop image unchanged"
        elif action in {"shell", "read_file", "list_files"}:
            stable = {name: value for name, value in result.items() if name not in {"message", "skipped"}}
            observation = hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        elif result.get("skipped"):
            observation = "requested content already present"
        else:
            self.last_observation = None
            self.repeated_observation = None
            if action == "write_file":
                self.last_change_step = step
            return
        if action == "shell" and result.get("directories") and not result.get("skipped"):
            self.last_change_step = step
        repeated = self.last_observation == (key, observation)
        count = self.repeated_observation["matching_calls"] + 1 if repeated and self.repeated_observation else 1
        self.last_observation = (key, observation)
        self.repeated_observation = {"action": action, "arguments": compact_arguments(decision.get("arguments", {})),
                                     "matching_calls": count, "last_step": step,
                                     "observation": "Same observed result; this alone does not prove progress or absence of side effects."}

    def path_events(self) -> list[dict]:
        return [{"data": {"decision": {"arguments": arguments}}} for arguments in self.references.values()]

    def snapshot(self) -> dict:
        context = {"failed_attempts": [], "omitted_failures": 0}
        if self.repeated_observation and self.repeated_observation["matching_calls"] >= 2:
            context["repeated_observation"] = copy.deepcopy(self.repeated_observation)
        for item in reversed(self.failures.values()):
            selected = copy.deepcopy(item)
            if self.last_change_step > item["last_failed_step"]:
                selected["observed_change_after_failure_step"] = self.last_change_step
            context["failed_attempts"].append(selected)
            if len(json.dumps(context, ensure_ascii=False)) > self.MAX_CONTEXT:
                context["failed_attempts"].pop()
        context["omitted_failures"] = len(self.failures) - len(context["failed_attempts"])
        return context
