from __future__ import annotations

import copy
from collections import OrderedDict, deque

from .config import validate_click_repeat_limit
from .progress import ProgressGuard


class ClickGuard:
    """Withhold repeated click targets on visually similar/returned desktop scenes."""

    MAX_SCENES = 12
    MAX_TARGETS = 64

    def __init__(self, limit: int, revision: int = 0):
        self.limit = None
        self.revision = None
        self.configure(limit, revision)

    def configure(self, limit: int, revision: int) -> None:
        validate_click_repeat_limit(limit)
        if (limit, revision) == (self.limit, self.revision):
            return
        self.limit, self.revision = limit, revision
        self.scenes: deque[dict] = deque(maxlen=self.MAX_SCENES)
        self.last_blocked: dict | None = None
        self.alternative_required = False

    @staticmethod
    def same_scene(before: dict, after: dict) -> bool:
        if any(before.get(name) != after.get(name) for name in ("desktop_width", "desktop_height", "view")):
            return False
        return ProgressGuard.same_screen(before, after)

    def _scene(self, frame: dict) -> dict:
        for scene in reversed(self.scenes):
            if self.same_scene(scene["frame"], frame):
                scene["frame"] = dict(frame)
                return scene
        scene = {"frame": dict(frame), "targets": OrderedDict()}
        self.scenes.append(scene)
        return scene

    def assess(self, target: tuple[int, int, str], frame: dict, step: int) -> dict | None:
        if not self.limit:
            return None
        scene = self._scene(frame)
        count = scene["targets"].get(target, 0)
        if count < self.limit:
            return None
        return self.reject("repeat_limit", target, step, count)

    def reject(self, reason: str, target: tuple[int, int, str], step: int, count: int = 0) -> dict:
        x, y, button = target
        message = (f"Click withheld at desktop ({x}, {y}): {count} matching attempts already ran "
                   f"on this visually similar desktop scene (limit {self.limit}). "
                   if reason == "repeat_limit" else
                   "Choose another action before proposing another click. " if reason == "alternative_required" else
                   "Click withheld because the desktop changed since the model's image. ")
        message += "No mouse input was sent. Inspect/zoom or choose another approach allowed by the task."
        self.last_blocked = {"reason": reason, "desktop_target": {"x": x, "y": y, "button": button},
                             "step": step, "matching_attempts": count, "limit": self.limit, "message": message}
        self.alternative_required = True
        return copy.deepcopy(self.last_blocked)

    def record(self, target: tuple[int, int, str], frame: dict) -> None:
        if not self.limit:
            return
        targets = self._scene(frame)["targets"]
        targets[target] = targets.pop(target, 0) + 1
        while len(targets) > self.MAX_TARGETS:
            targets.popitem(last=False)

    def observe_action(self, action: str) -> None:
        if action != "click":
            self.alternative_required = False

    def snapshot(self) -> dict:
        return {"enabled": bool(self.limit), "repeat_limit": self.limit,
                "request_alternative": self.alternative_required,
                "last_blocked": copy.deepcopy(self.last_blocked)}
