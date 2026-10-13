from __future__ import annotations

import dataclasses
import json
import os
import re
import secrets
import stat
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlsplit


def validate_model_name(model: str) -> None:
    if (not isinstance(model, str) or not 1 <= len(model) <= 256
            or any(char.isspace() or ord(char) < 32 for char in model)):
        raise ValueError("model must be a model name of 1–256 characters without whitespace")


def validate_stall_attempts(attempts: int) -> None:
    if type(attempts) is not int or not 0 <= attempts <= 1000:
        raise ValueError("stall_attempts must be an integer from 0 to 1000; 0 means manual control")


@dataclasses.dataclass(frozen=True)
class Config:
    ollama_url: str = "http://192.168.152.1:11435"
    ollama_token: str = ""
    model: str = "qwen3-vl:30b-a3b-instruct"
    num_thread: int = 16
    num_ctx: int = 8192
    num_predict: int = 1600
    keep_alive: str = "30m"
    request_timeout: int = 1200
    display: str = ":99"
    xauthority: str = "~/.Xauthority"
    screen_width: int = 1024
    screen_height: int = 768
    stream_fps: int = 5
    stream_quality: int = 70
    work_dir: str = "~/agent-workspace"
    data_dir: str = "~/.local/share/treics-agent"
    max_steps: int = 100
    max_run_minutes: int = 240
    shell_timeout: int = 120
    history_steps: int = 6
    stall_attempts: int = 0
    task_checklist: bool = False
    browser: str = "firefox"
    bind: str = "127.0.0.1"
    port: int = 8010
    dashboard_token: str = ""

    def validate(self) -> None:
        integer_ranges = {
            "num_thread": (1, 256), "num_ctx": (4096, 65536),
            "num_predict": (128, 8192), "request_timeout": (10, 3600),
            "screen_width": (320, 1920), "screen_height": (240, 1200),
            "stream_fps": (1, 15), "stream_quality": (30, 95),
            "max_steps": (1, 10000), "max_run_minutes": (1, 1440),
            "shell_timeout": (1, 600), "history_steps": (1, 20), "port": (1024, 65535),
            "stall_attempts": (0, 1000),
        }
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if field.name == "task_checklist":
                if type(value) is not bool:
                    raise ValueError("task_checklist must be true or false")
            elif field.name in integer_ranges:
                low, high = integer_ranges[field.name]
                if type(value) is not int or not low <= value <= high:
                    raise ValueError(f"{field.name} must be an integer from {low} to {high}")
            elif not isinstance(value, str):
                raise ValueError(f"{field.name} must be a string")
        url = urlsplit(self.ollama_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise ValueError("ollama_url must be an http(s) URL without embedded credentials")
        if url.path not in ("", "/") or url.query or url.fragment:
            raise ValueError("ollama_url must be the server origin, without an API path")
        validate_model_name(self.model)
        if self.dashboard_token and len(self.dashboard_token) < 24:
            raise ValueError("dashboard_token must contain at least 24 characters")

    @property
    def work_path(self) -> Path:
        return Path(self.work_dir).expanduser().resolve()

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser().resolve()

    def desktop_env(self) -> dict[str, str]:
        return {**os.environ, "DISPLAY": self.display,
                "XAUTHORITY": str(Path(self.xauthority).expanduser())}


def config_path() -> Path:
    return Path(os.environ.get("TREICS_AGENT_CONFIG", "~/.config/treics-agent/config.toml")).expanduser()


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    values = tomllib.loads(path.read_text()) if path.exists() else {}
    known = {field.name for field in dataclasses.fields(Config)}
    if unknown := values.keys() - known:
        raise ValueError(f"Unknown configuration fields: {', '.join(sorted(unknown))}")
    for name in ("ollama_url", "ollama_token", "model", "dashboard_token"):
        env_name = "TREICS_AGENT_" + name.upper()
        if env_name in os.environ:
            values[name] = os.environ[env_name]
    cfg = Config(**values)
    cfg.validate()
    return cfg


def initialize_config(path: Path | None = None) -> Path:
    path = path or config_path()
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg = dataclasses.replace(Config(), dashboard_token=secrets.token_urlsafe(32))
    lines = [f"{k} = {json.dumps(v)}" for k, v in dataclasses.asdict(cfg).items()]
    with path.open("x") as handle:
        os.chmod(path, 0o600)
        handle.write("# Local configuration; contains credentials.\n" + "\n".join(lines) + "\n")
    return path


def set_model(model: str, path: Path | None = None) -> Path:
    validate_model_name(model)
    if "TREICS_AGENT_MODEL" in os.environ and os.environ["TREICS_AGENT_MODEL"] != model:
        raise ValueError("TREICS_AGENT_MODEL fixes the model. Remove that environment override and restart before switching.")
    return _set_setting("model", model, path)


def set_stall_attempts(attempts: int, path: Path | None = None) -> Path:
    validate_stall_attempts(attempts)
    return _set_setting("stall_attempts", attempts, path)


def _set_setting(name: str, value: str | int, path: Path | None) -> Path:
    path = (path or config_path()).expanduser()
    if not path.exists():
        raise ValueError("Configuration is missing; run treics-agent init first.")
    source = path.read_text()
    load_config(path)
    setting = name + " = " + json.dumps(value)
    escaped = re.escape(name)
    pattern = rf"""^[ \t]*(?:{escaped}|"{escaped}"|'{escaped}')[ \t]*=.*$"""
    if re.search(pattern, source, re.MULTILINE):
        proposed = re.sub(pattern, lambda _: setting, source, flags=re.MULTILINE)
    else:
        proposed = setting + "\n" + source
    values = tomllib.loads(proposed)
    if values.get(name) != value:
        raise ValueError(f"Could not update the {name} setting; configuration was not changed.")
    Config(**values).validate()
    # Replace atomically: a failed write cannot truncate credentials or other settings.
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(proposed)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def set_task_checklist(enabled: bool, path: Path | None = None) -> Path:
    path = (path or config_path()).expanduser()
    if not path.exists():
        raise ValueError("Configuration is missing; run treics-agent init first.")
    source = path.read_text()
    load_config(path)  # Reject invalid existing configuration without changing it.
    setting = "task_checklist = " + ("true" if enabled else "false")
    # The setting belongs at the TOML root, before any table declarations.
    split = re.search(r"^\s*\[", source, re.MULTILINE)
    root, rest = (source[:split.start()], source[split.start():]) if split else (source, "")
    pattern = r"""^[ \t]*(?:task_checklist|"task_checklist"|'task_checklist')[ \t]*=.*$"""
    if re.search(pattern, root, re.MULTILINE):
        root = re.sub(pattern, setting, root, flags=re.MULTILINE)
    else:
        root = root.rstrip() + "\n" + setting + "\n"
    proposed = root + rest
    if tomllib.loads(proposed).get("task_checklist") is not enabled:
        raise ValueError("Could not update the root task_checklist setting; configuration was not changed.")
    path.write_text(proposed)
    return path
