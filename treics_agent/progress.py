from __future__ import annotations

import base64
import hashlib
import io
import json
from collections import deque

from .tools import Tools


class ProgressGuard:
    """Pause likely loops using observations, not the model's claimed progress."""

    def __init__(self):
        self.attempts: deque[str] = deque(maxlen=12)
        self.last_text_result: str | None = None
        self.recent_errors: deque[str | None] = deque(maxlen=12)

    def reset(self) -> None:
        self.recent_errors.clear()
        self._reset_attempts()

    def _reset_attempts(self) -> None:
        self.attempts.clear()
        self.last_text_result = None

    @staticmethod
    def same_screen(before: dict | None, after: dict | None) -> bool:
        if not before or not after:
            return False
        if (before["width"], before["height"]) != (after["width"], after["height"]):
            return False
        if before["base64"] == after["base64"]:
            return True
        from PIL import Image

        # Ignore small cursor/blink changes, while retaining page and window changes.
        samples = []
        for frame in (before, after):
            with Image.open(io.BytesIO(base64.b64decode(frame["base64"]))) as image:
                samples.append(image.convert("RGB").resize((256, 192)).tobytes())
        changed = sum(max(abs(a - b) for a, b in zip(samples[0][i:i + 3], samples[1][i:i + 3])) > 24
                      for i in range(0, len(samples[0]), 3))
        return changed <= (256 * 192) // 1000

    def observe(self, decision: dict, result: dict,
                before: dict | None, after: dict | None) -> str | None:
        self.recent_errors.append(None)
        action = decision["action"]
        signature = json.dumps([action, decision["arguments"]], sort_keys=True, ensure_ascii=False)
        if action in Tools.GUI_ACTIONS:
            self.last_text_result = None
            if not self.same_screen(before, after):
                self._reset_attempts()
                return None
            evidence = "the desktop screenshot stayed essentially unchanged"
        elif action in {"shell", "read_file", "list_files"}:
            # The same output is evidence of a possible loop, not proof that a command
            # had no effects. Deliberate polling can be continued with Resume.
            # Existing-directory skips have the same observed postcondition as
            # the first successful mkdir. A changed message is not progress.
            stable_result = result
            if action == "shell" and "directories" in result:
                stable_result = {key: value for key, value in result.items()
                                 if key not in {"skipped", "message"}}
            observation = signature + json.dumps(stable_result, sort_keys=True, ensure_ascii=False)
            fingerprint = hashlib.sha256(observation.encode()).hexdigest()
            if fingerprint != self.last_text_result:
                self._reset_attempts()
            self.last_text_result = fingerprint
            evidence = "the tool returned the same result"
        elif action == "write_file" and result.get("skipped"):
            self.last_text_result = None
            evidence = "the file already contained exactly the requested content"
        else:
            self.reset()
            return None
        self.attempts.append(signature)
        count = self.attempts.count(signature)
        if count >= 3:
            return (f"Automatically paused after {count} matching {action} actions: {evidence}. "
                    "Review the desktop and activity log. Resume requests a new decision; "
                    "Stop ends this task.")
        return None

    def error(self, decision: dict | None, message: str) -> str | None:
        action = decision["action"] if decision else "model"
        signature = action + ": " + message
        self.recent_errors.append(signature)
        count = self.recent_errors.count(signature)
        if count >= 3:
            return (f"Automatically paused after {count} repeated {action} errors within twelve attempts: "
                    f"{message[:500]}. Resume requests a new decision; Stop ends this task.")
        return None
