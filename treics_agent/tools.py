from __future__ import annotations

import base64
import ctypes
import ctypes.util
import io
import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config
from .click_marker import ClickMarker
from .model import ARGUMENT_SCHEMAS, Cancelled, validate_arguments
from .checklist import Checklist
from .storage import Store


class ToolError(Exception):
    pass


def number(value, name: str, low: float, high: float, integer: bool = False):
    if type(value) not in (int, float) or not low <= value <= high or (integer and type(value) is not int):
        raise ToolError(f"{name} must be {'an integer' if integer else 'a number'} between {low} and {high}.")
    return value


def text(value, name: str, maximum: int = 100000) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\0" in value:
        raise ToolError(f"{name} must be text of at most {maximum} characters without NUL bytes.")
    return value


@lru_cache(maxsize=1)
def x11_keysyms():
    """Use the same keysym lookup as xdotool, without connecting to DISPLAY."""
    try:
        library = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        library.XStringToKeysym.argtypes = [ctypes.c_char_p]
        library.XStringToKeysym.restype = ctypes.c_ulong
        return library
    except (OSError, AttributeError) as exc:
        raise ToolError("Cannot validate keyboard keys: libX11 is unavailable. Run scripts/install-guest.sh.") from exc


class Desktop:
    def __init__(self, config: Config):
        self.config = config
        # GUI operations may wait on X11. Publishing/reading cached state must not.
        self.lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.frame: dict | None = None
        self.image: bytes | None = None
        self.run_directory: Path | None = None
        self.last_click: dict | None = None
        self.marker = ClickMarker(config)
        self.marker_error: str | None = None

    def command(self, args: list[str], **kwargs) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(args, env=self.config.desktop_env(), check=True,
                                  timeout=15, capture_output=True, **kwargs)
        except FileNotFoundError as exc:
            raise ToolError(f"Missing desktop command: {args[0]}. Run scripts/install-guest.sh.") from exc
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or b"").decode(errors="replace")[:500]
            raise ToolError(f"{args[0]} failed: {message}. Check the X11 desktop service and DISPLAY.") from exc
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"Desktop command {args[0]} timed out.") from exc

    @staticmethod
    def jpeg(image) -> bytes:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=85)
        return output.getvalue()

    def zoom_region(self, arguments: dict) -> dict:
        """Translate a rectangle in the current image into native desktop pixels."""
        if not self.frame:
            raise ToolError("No current screenshot. Use screenshot before zoom_screenshot.")
        frame = self.frame
        x = number(arguments["x"], "x", 0, frame["width"] - 1, True)
        y = number(arguments["y"], "y", 0, frame["height"] - 1, True)
        width = number(arguments["width"], "width", 1, frame["width"] - x, True)
        height = number(arguments["height"], "height", 1, frame["height"] - y, True)
        scale = number(arguments.get("scale", 3), "scale", 2, 6, True)
        source = frame.get("source", {"x": 0, "y": 0, "width": frame["desktop_width"],
                                     "height": frame["desktop_height"]})
        left = source["x"] + math.floor(x * source["width"] / frame["width"])
        top = source["y"] + math.floor(y * source["height"] / frame["height"])
        right = source["x"] + math.ceil((x + width) * source["width"] / frame["width"])
        bottom = source["y"] + math.ceil((y + height) * source["height"] / frame["height"])
        return {"x": left, "y": top, "width": right - left, "height": bottom - top,
                "scale": scale, "desktop_width": frame["desktop_width"],
                "desktop_height": frame["desktop_height"]}

    def record_click(self, x: int, y: int, button: str, clicks: int, source: dict) -> dict:
        click = {"x": x, "y": y, "button": button, "clicks": clicks,
                 "clicked_at": time.time(), "source": source}
        with self.state_lock:
            self.last_click = click
        try:
            self.marker.show(x, y)
            error = None
        except Exception as exc:
            # Drawing failure must never turn a completed click into a retry.
            error = str(exc)[:300]
            self.marker.close()
        with self.state_lock:
            self.marker_error = error
        return dict(click)

    def pointer_coordinates(self) -> tuple[int, int]:
        location = self.command(["xdotool", "getmouselocation", "--shell"])
        values = dict(line.split("=", 1) for line in location.stdout.decode().splitlines() if "=" in line)
        try:
            return int(values["X"]), int(values["Y"])
        except (KeyError, ValueError) as exc:
            raise ToolError("Could not read the desktop pointer location; no click was sent.") from exc

    def move_pointer(self, x: int, y: int) -> None:
        # xdotool --sync waits for movement, even if the pointer is already there.
        if self.pointer_coordinates() != (x, y):
            self.command(["xdotool", "mousemove", "--sync", str(x), str(y)])

    def snapshot(self) -> dict:
        with self.state_lock:
            return {"last_click": dict(self.last_click) if self.last_click else None,
                    "click_marker_error": self.marker_error,
                    "screen": {k: v for k, v in self.frame.items() if k != "base64"} if self.frame else None}

    def preview_image(self) -> bytes | None:
        with self.state_lock:
            return self.image

    def invalidate_frame(self) -> None:
        with self.state_lock:
            self.frame = None

    def capture(self, region: dict | None = None) -> dict:
        from PIL import Image, ImageDraw

        with self.lock, tempfile.TemporaryDirectory(prefix="treic-screen-") as directory:
            path = Path(directory) / "screen.png"
            with self.marker.hidden():
                self.command(["scrot", "-o", str(path)])
            with Image.open(path) as original:
                screen_width, screen_height = original.size
                image = original.convert("RGB")
                source = {"x": 0, "y": 0, "width": screen_width, "height": screen_height}
                view = image
                if region:
                    if (screen_width, screen_height) != (region["desktop_width"], region["desktop_height"]):
                        self.invalidate_frame()
                        raise ToolError("Desktop resolution changed. Use screenshot before zooming again.")
                    source = {key: region[key] for key in ("x", "y", "width", "height")}
                    view = image.crop((source["x"], source["y"], source["x"] + source["width"],
                                       source["y"] + source["height"]))
                    factor = min(region["scale"], self.config.screen_width / view.width,
                                 self.config.screen_height / view.height)
                    view = view.resize((max(1, round(view.width * factor)), max(1, round(view.height * factor))),
                                       Image.Resampling.LANCZOS)
                else:
                    view = image.copy()
                    view.thumbnail((self.config.screen_width, self.config.screen_height), Image.Resampling.LANCZOS)
                # Human previews always show the full desktop, with an annotation.
                image.thumbnail((self.config.screen_width, self.config.screen_height), Image.Resampling.LANCZOS)
                if self.last_click:
                    cx = round(self.last_click["x"] * image.width / screen_width)
                    cy = round(self.last_click["y"] * image.height / screen_height)
                    draw = ImageDraw.Draw(image)
                    draw.ellipse((cx - 11, cy - 11, cx + 11, cy + 11), outline="white", width=4)
                    draw.ellipse((cx - 10, cy - 10, cx + 10, cy + 10), outline="#ff4040", width=2)
                    for line in ((cx - 20, cy, cx - 14, cy), (cx + 14, cy, cx + 20, cy),
                                 (cx, cy - 20, cx, cy - 14), (cx, cy + 14, cx, cy + 20)):
                        draw.line(line, fill="white", width=4)
                        draw.line(line, fill="#ff4040", width=2)
                preview = self.jpeg(image)
                frame = {"width": view.width, "height": view.height,
                         "desktop_width": screen_width, "desktop_height": screen_height,
                         "view": "zoom" if region else "desktop", "source": source,
                         "captured_at": time.time(), "base64": base64.b64encode(self.jpeg(view)).decode()}
                with self.state_lock:
                    self.image = preview
                    self.frame = frame
            if self.run_directory:
                self.run_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = self.run_directory / "screen.tmp"
                temporary.write_bytes(self.image)
                temporary.replace(self.run_directory / "screen.jpg")
            return dict(self.frame)

    def coordinates(self, x, y) -> tuple[int, int]:
        if not self.frame:
            raise ToolError("No current screenshot. Use screenshot before a coordinate action.")
        frame = self.frame
        x = number(x, "x", 0, frame["width"] - 1, integer=True)
        y = number(y, "y", 0, frame["height"] - 1, integer=True)
        source = frame.get("source", {"x": 0, "y": 0, "width": frame["desktop_width"],
                                     "height": frame["desktop_height"]})
        return (min(source["x"] + source["width"] - 1, source["x"] + round(x * source["width"] / frame["width"])),
                min(source["y"] + source["height"] - 1, source["y"] + round(y * source["height"] / frame["height"])))

    @staticmethod
    def key_combination(keys: list[str]) -> str:
        if not isinstance(keys, list) or not 1 <= len(keys) <= 6:
            raise ToolError("keys must be a list of 1–6 X11 key names.")
        if any(not isinstance(k, str) or len(k) > 64 or not re.fullmatch(r"[A-Za-z0-9_]+", k) for k in keys):
            raise ToolError("Invalid X11 key name.")
        named = ("Return", "Tab", "ISO_Left_Tab", "Escape", "BackSpace", "space", "Delete", "Insert",
                 "Home", "End", "Left", "Right", "Up", "Down", "Prior", "Next", "Page_Up", "Page_Down",
                 "Print", "Pause", "Menu", "Caps_Lock", "Num_Lock", "Scroll_Lock",
                 "Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
                 "Super_L", "Super_R", "Meta_L", "Meta_R", "KP_Enter", "KP_Tab",
                 "KP_Home", "KP_End", "KP_Left", "KP_Right", "KP_Up", "KP_Down",
                 "KP_Prior", "KP_Next", "KP_Page_Up", "KP_Page_Down", "KP_Insert", "KP_Delete")
        aliases = {name.lower(): name for name in named}
        aliases.update({f"f{number}": f"F{number}" for number in range(1, 36)})
        modifiers = {"ctrl": "Control_L", "alt": "Alt_L", "shift": "Shift_L",
                     "super": "Super_L", "meta": "Meta_L"}
        aliases.update({name: name for name in modifiers})
        aliases.update({"enter": "Return", "esc": "Escape", "control": "ctrl",
                        "pageup": "Page_Up", "pgup": "Page_Up", "pagedown": "Page_Down", "pgdn": "Page_Down"})
        normalized = [aliases.get(key.lower(), key) for key in keys]
        library = x11_keysyms()
        for original, key in zip(keys, normalized):
            symbol = library.XStringToKeysym(modifiers.get(key, key).encode("ascii"))
            # xdotool also accepts explicit decimal X11 keycodes.
            if not symbol and not (key.isdecimal() and 8 <= int(key) <= 255):
                raise ToolError(f"Unknown X11 key name {original!r}; no shortcut was sent. "
                                "Use keys such as F4, Return, Escape, Left, or ctrl.")
        # Letter case is intentional: 'a' and 'A' can send different modifiers.
        return "+".join(normalized)

    def key(self, keys: list[str]) -> str:
        combination = self.key_combination(keys)
        completed = self.command(["xdotool", "key", "--clearmodifiers", combination])
        warning = (completed.stderr or b"").decode(errors="replace").strip()
        if "no such key name" in warning.lower() or "failure converting key sequence" in warning.lower():
            raise ToolError(f"xdotool rejected shortcut {combination!r}: {warning[:500]}. "
                            "Part of the shortcut may have been sent; inspect the desktop before retrying.")
        return combination


class Tools:
    GUI_ACTIONS = {"screenshot", "zoom_screenshot", "click", "move", "drag", "scroll", "type", "press", "hotkey",
                   "open_browser", "wait"}
    ARGS = {
        "screenshot": (set(), set()),
        "zoom_screenshot": ({"x", "y", "width", "height"}, {"scale"}),
        "click": ({"x", "y"}, {"button", "clicks"}),
        "move": ({"x", "y"}, set()), "drag": ({"x", "y", "end_x", "end_y"}, {"duration"}),
        "scroll": ({"amount"}, {"x", "y"}), "type": ({"text"}, {"paste_keys"}),
        "press": ({"key"}, set()), "hotkey": ({"keys"}, set()),
        "shell": ({"command"}, {"cwd", "timeout"}), "read_file": ({"path"}, {"offset", "limit"}),
        "write_file": ({"path", "content"}, set()), "list_files": (set(), {"path"}),
        "open_browser": ({"url"}, set()), "wait": ({"seconds"}, set()),
        "remember": ({"text"}, set()), "finish": ({"summary"}, set()),
        "set_checklist": ({"items", "revision_reason"}, set()),
    }

    def __init__(self, config: Config, store: Store, desktop: Desktop | None = None):
        self.config = config
        self.store = store
        self.desktop = desktop or Desktop(config)
        self.config.work_path.mkdir(parents=True, exist_ok=True)
        self.process_lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.run_id: str | None = None

    @staticmethod
    def kill(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def cancel(self) -> None:
        with self.process_lock:
            if self.process:
                self.kill(self.process)
        self.desktop.marker.cancel()

    def resolve(self, value) -> Path:
        path = Path(text(value, "path", 4096)).expanduser()
        return path if path.is_absolute() else self.config.work_path / path

    def filesystem_context(self, events: list[dict]) -> dict:
        """Read bounded metadata for the workspace and recently referenced paths."""
        paths = [self.config.work_path]
        for event in reversed(events):
            arguments = event["data"].get("decision", {}).get("arguments", {})
            for key in ("path", "cwd"):
                if value := arguments.get(key):
                    try:
                        path = self.resolve(value)
                    except (ToolError, OSError, ValueError):
                        continue
                    if path not in paths and len(paths) < 12:
                        paths.append(path)
        context = {"paths": [], "paths_truncated": False,
                   "workspace_entries": [], "entries_truncated": False}
        for path in paths:
            entry = {"path": str(path)}
            try:
                info = path.stat()
                entry["kind"] = ("directory" if stat.S_ISDIR(info.st_mode) else
                                 "file" if stat.S_ISREG(info.st_mode) else "other")
                if entry["kind"] == "file":
                    entry["bytes"] = info.st_size
            except FileNotFoundError:
                entry["kind"] = "missing"
            except OSError as exc:
                entry.update(kind="unavailable", error=str(exc)[:300])
            context["paths"].append(entry)
            if len(json.dumps(context, ensure_ascii=False)) > 3000:
                context["paths"].pop()
                context["paths_truncated"] = True
        try:
            with os.scandir(self.config.work_path) as iterator:
                for entry in iterator:
                    if len(context["workspace_entries"]) == 20:
                        context["entries_truncated"] = True
                        break
                    context["workspace_entries"].append({
                        "name": entry.name, "directory": entry.is_dir(follow_symlinks=False)})
                    if len(json.dumps(context, ensure_ascii=False)) > 3000:
                        context["workspace_entries"].pop()
                        context["entries_truncated"] = True
                        break
            context["workspace_entries"].sort(key=lambda entry: entry["name"])
        except OSError as exc:
            context["workspace_error"] = str(exc)[:300]
        return context

    @staticmethod
    def directory_setup_paths(command: str, cwd: Path) -> list[Path]:
        # Recognize only plain, unquoted mkdir -p operands. Anything with shell
        # expansion, operators, quoting or additional options runs normally.
        if not re.fullmatch(r"[A-Za-z0-9_./~ \t-]+", command):
            return []
        words = command.split()
        if words[:2] != ["mkdir", "-p"]:
            return []
        operands = words[2:]
        if operands[:1] == ["--"]:
            operands = operands[1:]
        if not operands or len(operands) > 32 or any(
                word.startswith("-") or not re.fullmatch(r"(?:~/)?[A-Za-z0-9_./-]+", word)
                for word in operands):
            return []
        paths = [Path(word).expanduser() for word in operands]
        return [path if path.is_absolute() else cwd / path for path in paths]

    @staticmethod
    def directory_observations(paths: list[Path]) -> list[dict]:
        return [{"path": str(path), "directory": path.is_dir()} for path in paths]

    def shell(self, arguments: dict, stop: threading.Event) -> dict:
        command = text(arguments["command"], "command", 32000)
        cwd = self.resolve(arguments.get("cwd", str(self.config.work_path)))
        timeout = number(arguments.get("timeout", self.config.shell_timeout), "timeout", 1, 600, True)
        if not cwd.is_dir():
            raise ToolError(f"Working directory does not exist: {cwd}. No command was started, "
                            "so directory creation inside it could not run. Omit cwd to use the "
                            f"existing workspace ({self.config.work_path}), or use write_file, "
                            "which creates parent directories automatically.")
        if stop.is_set():
            raise Cancelled()
        setup_paths = self.directory_setup_paths(command, cwd)
        directories = self.directory_observations(setup_paths)
        if directories and all(entry["directory"] for entry in directories):
            return {"ok": True, "exit_code": 0, "timed_out": False, "output": "",
                    "truncated": False, "cwd": str(cwd), "skipped": True,
                    "directories": directories,
                    "message": "These directories already exist; no command was run. "
                               "Continue with the next task step. write_file creates parent "
                               "directories automatically; no mkdir or touch is needed first."}
        process = subprocess.Popen(["/bin/bash", "-o", "pipefail", "-lc", command], cwd=cwd, env=self.config.desktop_env(),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        with self.process_lock:
            self.process = process
        deadline = time.monotonic() + timeout
        output = bytearray()
        truncated = timed_out = False
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if stop.is_set():
                        self.kill(process)
                        raise Cancelled()
                    if time.monotonic() > deadline:
                        timed_out = True
                        self.kill(process)
                        break
                    for key, _ in selector.select(0.1):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        available = max(0, 48000 - len(output))
                        output.extend(chunk[:available])
                        truncated |= len(chunk) > available
                process.wait(timeout=5)
            if stop.is_set():
                raise Cancelled()
            result = {"ok": process.returncode == 0 and not timed_out,
                      "exit_code": process.returncode, "timed_out": timed_out,
                      "output": output.decode(errors="replace"), "truncated": truncated, "cwd": str(cwd)}
            if setup_paths:
                result["directories"] = self.directory_observations(setup_paths)
                if result["ok"]:
                    result["message"] = ("Directory setup completed successfully. Empty output is normal. "
                                         "Continue with the next task step; do not repeat setup.")
            return result
        finally:
            if process.poll() is None:
                self.kill(process)
            process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            with self.process_lock:
                if self.process is process:
                    self.process = None

    def execute(self, action: str, arguments: dict, stop: threading.Event) -> dict:
        if stop.is_set():
            raise Cancelled()
        if action not in self.ARGS or not isinstance(arguments, dict):
            raise ToolError("Unknown tool or invalid arguments.")
        required, optional = self.ARGS[action]
        if missing := required - arguments.keys():
            raise ToolError("Missing arguments: " + ", ".join(sorted(missing)))
        if extra := arguments.keys() - required - optional:
            raise ToolError("Unexpected arguments: " + ", ".join(sorted(extra)))
        a = arguments
        d = self.desktop
        if action == "set_checklist":
            if not self.config.task_checklist or not self.run_id:
                raise ToolError("Task checklist mode is disabled or no task is active.")
            validate_arguments(a, ARGUMENT_SCHEMAS[action], "set_checklist.arguments")
            checklist = Checklist(self.store, self.run_id, self.config.work_path)
            changed = checklist.set_items(a["items"], a["revision_reason"])
            return {"ok": True, "skipped": not changed, "items": checklist.snapshot(),
                    "message": "Checklist saved." if changed else "Checklist unchanged; continue with task work."}
        if action == "shell":
            return self.shell(a, stop)
        if action == "read_file":
            path = self.resolve(a["path"])
            offset = number(a.get("offset", 0), "offset", 0, 10000000, True)
            limit = number(a.get("limit", 12000), "limit", 1, 48000, True)
            with path.open(encoding="utf-8", errors="replace") as handle:
                # Read a bounded prefix rather than loading an arbitrarily large file.
                handle.read(offset)
                content = handle.read(limit + 1)
            return {"ok": True, "path": str(path), "offset": offset,
                    "content": content[:limit], "truncated": len(content) > limit}
        if action == "write_file":
            path = self.resolve(a["path"])
            content = text(a["content"], "content")
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = content.encode("utf-8")
            try:
                if path.is_file() and path.stat().st_size == len(encoded):
                    with path.open("rb") as handle:
                        if handle.read(len(encoded) + 1) == encoded:
                            return {"ok": True, "path": str(path), "characters": len(content),
                                    "skipped": True, "message": "The file already contains exactly this "
                                    "content; no write was performed. Continue by running or verifying "
                                    "it, or making the next requested change."}
            except OSError:
                # A file may be writable without being readable; inability to
                # compare it must not prevent the requested write.
                pass
            path.write_text(content, encoding="utf-8")
            return {"ok": True, "path": str(path), "characters": len(content)}
        if action == "list_files":
            path = self.resolve(a.get("path", "."))
            entries = []
            with os.scandir(path) as iterator:
                for entry in iterator:
                    entries.append({"name": entry.name, "directory": entry.is_dir(follow_symlinks=False)})
                    if len(entries) == 400:
                        break
            return {"ok": True, "path": str(path), "entries": entries, "limit": 400}
        if action == "remember":
            self.store.remember(text(a["text"], "text", 6000))
            return {"ok": True, "message": "Persistent notes updated."}
        if action == "finish":
            return {"ok": True, "summary": text(a["summary"], "summary", 12000)}
        with d.lock:
            result = {"ok": True, "message": f"{action} completed; a full desktop screen will be captured."}
            if action == "zoom_screenshot":
                region = d.zoom_region(a)
                return {"ok": True, "capture_region": region,
                        "message": "A fresh zoomed image will be attached next. Use its local pixel coordinates "
                                   "for click/move/drag/scroll; the controller adds the crop offset and "
                                   "handles scaling. The next ordinary GUI action restores the full view."}
            if action in ("click", "move", "drag"):
                x, y = d.coordinates(a["x"], a["y"])
                if action == "click":
                    button = a.get("button", "left")
                    if button not in ("left", "right", "middle"):
                        raise ToolError("button must be left, right or middle.")
                    clicks = number(a.get("clicks", 1), "clicks", 1, 2, True)
                elif action == "drag":
                    end_x, end_y = d.coordinates(a["end_x"], a["end_y"])
                    duration = number(a.get("duration", 0.6), "duration", 0.1, 5)
                d.move_pointer(x, y)
                if action == "click":
                    pointer_x, pointer_y = d.pointer_coordinates()
                    d.command(["xdotool", "click", "--repeat", str(clicks), "--delay", "100",
                               str({"left": 1, "middle": 2, "right": 3}[button])])
                    result["last_click"] = d.record_click(pointer_x, pointer_y, button, clicks,
                                                         {"x": a["x"], "y": a["y"],
                                                          "mapped_x": x, "mapped_y": y,
                                                          "view": d.frame.get("view", "desktop"),
                                                          "region": d.frame.get("source")})
                    if d.marker_error:
                        result["click_marker_error"] = d.marker_error
                elif action == "drag":
                    d.command(["xdotool", "mousedown", "1"])
                    try:
                        if stop.wait(duration / 2):
                            raise Cancelled()
                        d.move_pointer(end_x, end_y)
                        if stop.wait(duration / 2):
                            raise Cancelled()
                    finally:
                        d.command(["xdotool", "mouseup", "1"])
            elif action == "scroll":
                amount = number(a["amount"], "amount", -30, 30, True)
                if ("x" in a) != ("y" in a):
                    raise ToolError("Provide both x and y, or neither.")
                if "x" in a:
                    x, y = d.coordinates(a["x"], a["y"])
                    d.move_pointer(x, y)
                if amount:
                    d.command(["xdotool", "click", "--repeat", str(abs(amount)), "--delay", "30",
                               "4" if amount > 0 else "5"])
            elif action == "type":
                content = text(a["text"], "text", 32000)
                keys = a.get("paste_keys", ["ctrl", "v"])
                # Reject invalid paste keys before altering the clipboard.
                d.key_combination(keys)
                # xclip forks to keep serving the selection; do not capture its inherited pipes.
                subprocess.run(["xclip", "-selection", "clipboard"], input=content.encode(),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=self.config.desktop_env(), check=True, timeout=10)
                result["keys_sent"] = d.key(keys)
            elif action == "press":
                result["keys_sent"] = d.key([a["key"]])
            elif action == "hotkey":
                result["keys_sent"] = d.key(a["keys"])
            elif action == "open_browser":
                url = text(a["url"], "url", 4096)
                parsed = urlsplit(url)
                if parsed.scheme not in ("https", "http") or not parsed.hostname:
                    raise ToolError("Browser URLs must start with http:// or https://.")
                browser = self.config.browser
                subprocess.Popen([browser, url], env=self.config.desktop_env(),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            elif action == "wait":
                if stop.wait(number(a["seconds"], "seconds", 0.1, 30)):
                    raise Cancelled()
            elif action != "screenshot":
                raise ToolError("Unsupported action.")
            if stop.wait(0.25):
                raise Cancelled()
            return result
