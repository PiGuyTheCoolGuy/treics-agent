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

    def reset(self) -> None:
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
        action = decision["action"]
        signature = json.dumps([action, decision["arguments"]], sort_keys=True, ensure_ascii=False)
        if action in Tools.GUI_ACTIONS:
            self.last_text_result = None
            if not self.same_screen(before, after):
                self.reset()
                return None
            evidence = "the desktop screenshot stayed essentially unchanged"
        elif action in {"shell", "read_file", "list_files"}:
            # The same output is evidence of a possible loop, not proof that a command
            # had no effects. Deliberate polling can be continued with Resume.
            observation = signature + json.dumps(result, sort_keys=True, ensure_ascii=False)
            fingerprint = hashlib.sha256(observation.encode()).hexdigest()
            if fingerprint != self.last_text_result:
                self.reset()
            self.last_text_result = fingerprint
            evidence = "the tool returned the same result"
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
