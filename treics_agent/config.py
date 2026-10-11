from __future__ import annotations

import dataclasses
import os
import secrets
import tomllib
from pathlib import Path
from urllib.parse import urlsplit


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
    work_dir: str = "~/agent-workspace"
    data_dir: str = "~/.local/share/treics-agent"
    max_steps: int = 100
    max_run_minutes: int = 240
    shell_timeout: int = 120
    history_steps: int = 6
    browser: str = "firefox"
    bind: str = "127.0.0.1"
    port: int = 8010
    dashboard_token: str = ""

    def validate(self) -> None:
        integer_ranges = {
            "num_thread": (1, 256), "num_ctx": (4096, 65536),
            "num_predict": (128, 8192), "request_timeout": (10, 3600),
            "screen_width": (320, 1920), "screen_height": (240, 1200),
            "max_steps": (1, 10000), "max_run_minutes": (1, 1440),
            "shell_timeout": (1, 600), "history_steps": (1, 20), "port": (1024, 65535),
        }
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if field.name in integer_ranges:
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
        if not self.model.strip():
            raise ValueError("model cannot be empty")
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
    import json

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
