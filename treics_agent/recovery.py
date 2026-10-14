from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict, deque

from .model import compact_arguments, observation_text
from .click_review import comparison_image, image_point
from .progress import ProgressGuard
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
        self.gui_attempts: deque[str] = deque(maxlen=12)
        self.gui_feedback: dict | None = None
        self._full_frame: dict | None = None
        self.click_review: dict | None = None
        self.click_review_image: str | None = None

    def consume_click_review(self) -> None:
        self.click_review = None
        self.click_review_image = None

    def inspect_click(self, screen: dict) -> None:
        if not self.click_review or not self.click_review.get("desktop_target"):
            return
        self.click_review["inspection"] = {
            "captured_at": screen.get("captured_at"), "desktop_region": screen["source"],
            "last_click_in_image": image_point(screen, self.click_review["desktop_target"]),
        }
        # Old proposal coordinates belong to a different image; retain the factual native target.
        self.click_review.pop("arguments", None)
        self.click_review["reference_crop"] = None
        self.click_review_image = None

    def _clear_gui(self) -> None:
        self.gui_attempts.clear()
        self.gui_feedback = None
        self._full_frame = None
        self.consume_click_review()

    def _gui_comparison(self, before: dict | None, after: dict | None) -> str:
        # A zoom is an observation, not a desktop change. Keep one full frame so
        # the ordinary full capture after a crop-local click has a comparable baseline.
        reference = before
        if before and before.get("view", "desktop") == "desktop":
            if self._full_frame and self.screen_comparison(self._full_frame, before) != "essentially_unchanged":
                self.gui_attempts.clear()
                self.gui_feedback = None
                self.repeated_observation = None
            self._full_frame = before
        elif before and before.get("view") == "zoom" and self._full_frame:
            reference = self._full_frame
        comparison = self.screen_comparison(reference, after)
        if after and after.get("view", "desktop") == "desktop":
            self._full_frame = after
        return comparison

    @staticmethod
    def screen_comparison(before: dict | None, after: dict | None) -> str:
        if not before or not after:
            return "unavailable"
        if any(before.get(name) != after.get(name) for name in ("width", "height", "source", "view")):
            return "view_changed"
        try:
            return "essentially_unchanged" if ProgressGuard.same_screen(before, after) else "changed"
        except (ValueError, OSError, KeyError):
            return "unavailable"

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
        if decision:
            self._clear_gui()  # A failed tool may have had partial effects.

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
            comparison = self._gui_comparison(before, after)
            if action in Tools.INPUT_ACTIONS:
                self.last_observation = None
                self.repeated_observation = None
                arguments = compact_arguments(decision.get("arguments", {}))
                effective = {"action": action, "arguments": arguments}
                if click := result.get("last_click"):
                    effective["desktop_target"] = {name: click[name] for name in ("x", "y", "button", "clicks")}
                if "keys_sent" in result:
                    effective["keys_sent"] = result["keys_sent"]
                # Compare executed desktop coordinates rather than crop-local coordinates.
                identity = [action, effective.get("desktop_target") or effective.get("keys_sent") or decision.get("arguments", {}),
                            {name: (after or {}).get(name) for name in ("desktop_width", "desktop_height")}]
                fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if comparison == "essentially_unchanged":
                    self.gui_attempts.append(fingerprint)
                    count = self.gui_attempts.count(fingerprint)
                else:
                    self.gui_attempts.clear()
                    count = 0
                self.gui_feedback = {**effective, "step": step, "input_sent": True, "ui_effect_verified": False,
                                     "screen_comparison": comparison, "matching_unchanged_attempts": count,
                                     "input_window": self.gui_attempts.maxlen,
                                     "guidance": "Inspect the current image to verify the intended effect; input delivery is not task success."}
                if count >= 2:
                    guidance = ("Repeated input with no substantial visible change. Reinspect target/focus or zoom; "
                                "choose another approach allowed by the task unless intentional repeated input is needed. "
                                "Do not adjust coordinates blindly.")
                    self.gui_feedback["guidance"] = guidance
                    self.repeated_observation = {**effective, "matching_calls": count, "last_step": step,
                                                 "observation": guidance}
                if comparison == "changed":
                    self.last_change_step = step
                if action == "click" and after:
                    evidence = comparison_image(before, after, effective.get("desktop_target"))
                    self.click_review = {
                        "step": step, "intent": observation_text(decision.get("reason", ""), 200),
                        "arguments": arguments, "desktop_target": effective.get("desktop_target"),
                        "screen_comparison": comparison,
                        "before_captured_at": (before or {}).get("captured_at"),
                        "after_captured_at": after.get("captured_at"),
                        "reference_crop": evidence.get("desktop_region"),
                    }
                    self.click_review_image = evidence.get("base64")
                return
            # Looking again or waiting must not erase evidence of previous ineffective input.
            if comparison == "changed":
                self._clear_gui()
            if comparison != "essentially_unchanged":
                self.last_observation = None
                self.repeated_observation = None
                return
            if self.gui_feedback and self.gui_feedback["matching_unchanged_attempts"] >= 2:
                return
            observation = "desktop image essentially unchanged"
        elif action in {"shell", "read_file", "list_files"}:
            self._clear_gui()
            stable = {name: value for name, value in result.items() if name not in {"message", "skipped"}}
            observation = hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        elif result.get("skipped"):
            self._clear_gui()
            observation = "requested content already present"
        else:
            self._clear_gui()
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
        if self.gui_feedback:
            context["gui_feedback"] = copy.deepcopy(self.gui_feedback)
        if self.click_review:
            context["click_review"] = copy.deepcopy(self.click_review)
        if self.repeated_observation and self.repeated_observation["matching_calls"] >= 2:
            context["repeated_observation"] = copy.deepcopy(self.repeated_observation)
        if self.click_review and self.click_review.get("inspection"):
            for name in ("gui_feedback", "repeated_observation"):
                if context.get(name, {}).get("action") == "click":
                    context[name].pop("arguments", None)
        for item in reversed(self.failures.values()):
            selected = copy.deepcopy(item)
            if self.last_change_step > item["last_failed_step"]:
                selected["observed_change_after_failure_step"] = self.last_change_step
            context["failed_attempts"].append(selected)
            if len(json.dumps(context, ensure_ascii=False)) > self.MAX_CONTEXT:
                context["failed_attempts"].pop()
        context["omitted_failures"] = len(self.failures) - len(context["failed_attempts"])
        return context
