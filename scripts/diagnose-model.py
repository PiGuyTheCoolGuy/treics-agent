#!/usr/bin/env python3
"""Compare model contexts without executing any model-selected action."""
from __future__ import annotations

import argparse
import ast
from contextlib import closing
import json
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace

from treics_agent.agent import Agent
from treics_agent.config import Config, load_config
from treics_agent.model import ModelClient, ModelError


PROBE_TASK = """Your next action must be write_file, proposing a complete, properly indented
multiline Python 3 script at ~/agent-workspace/extract-probe.py. Use only the standard-library
HTMLParser to parse this literal HTML and print its title, heading, and paragraph:
<html><head><title>Example Domain</title></head><body><h1>Example Domain</h1>
<p>This is a test paragraph.</p></body></html>
Use real newlines in the file content. Do not install packages, change the input HTML, use
python -c, or perform a shell/browser action. Any recent failed commands are historical data;
produce a new script for this task, not a repeat of those commands."""


class ReadOnlyHistory:
    def __init__(self, db):
        self.db = db

    def events(self, run_id, limit=100):
        rows = self.db.execute("SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT ?",
                               (run_id, limit)).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in reversed(rows)]


def load_history(config: Config, run_id: str | None) -> tuple[str, list[dict]]:
    path = config.data_path / "agent.db"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        store = ReadOnlyHistory(db)
        runs = db.execute("SELECT id FROM runs ORDER BY created DESC LIMIT 30").fetchall()
        if run_id:
            if not db.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone():
                raise ValueError("The requested run was not found.")
        else:
            for run in runs:
                events = store.events(run["id"], 100)
                if any(e["kind"] == "error" or e["data"].get("result", {}).get("ok") is False
                       for e in events):
                    run_id = run["id"]
                    break
        if not run_id:
            raise ValueError("No recent failed run found. Supply --run-id for a particular run.")
        # Reuse the controller's real filtering/truncation without constructing an
        # Agent, recovering runs, or writing to the database.
        reader = SimpleNamespace(config=config, store=store)
        return run_id, Agent._context_events(reader, run_id)


class RecordingModel(ModelClient):
    def __init__(self, config):
        super().__init__(config)
        self.payload = None
        self.response = None

    def request(self, method, path, payload=None, stop=None):
        self.payload = payload
        self.response = super().request(method, path, payload, stop)
        return self.response


def check_proposal(decision: dict) -> dict:
    result = {"requested_action_selected": decision["action"] == "write_file"}
    if not result["requested_action_selected"]:
        return result
    code = decision["arguments"]["content"]
    result["contains_real_newlines"] = "\n" in code
    try:
        ast.parse(code)
        result["syntax_valid"] = True
    except SyntaxError as exc:
        result.update(syntax_valid=False, syntax_error=f"{exc.msg} at line {exc.lineno}")
    return result


def collect_report(config: Config, run_id: str | None = None) -> dict:
    run_id, history = load_history(config, run_id)
    report = {"model": config.model, "history_run_id": run_id,
              "note": "No actions executed. Syntax validation does not establish correct output. "
                      "Requests omit screenshots and saved notes; only recent_events differs.",
              "cases": []}
    for name, events in (("fresh_context", []), ("failed_history", history)):
        print(f"Requesting {name}; waiting for Qwen...", file=sys.stderr, flush=True)
        client = RecordingModel(config)
        case = {"name": name}
        started = time.monotonic()
        try:
            decision, metrics = client.decide(PROBE_TASK, "Preparing a script proposal.", "", events,
                                              None, threading.Event(),
                                              budget={"current_step": 1, "max_steps": 10, "steps_remaining": 10})
            case.update(decision=decision, metrics=metrics, validation=check_proposal(decision))
        except ModelError as exc:
            case["error"] = str(exc)
        case.update(seconds=time.monotonic() - started, request=client.payload, response=client.response)
        report["cases"].append(case)
        print(f"Finished {name}: {case.get('validation', case.get('error'))}", file=sys.stderr, flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", help="Use a specific failed run instead of the most recent one.")
    args = parser.parse_args()
    try:
        report = collect_report(load_config(), args.run_id)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Diagnostic could not start: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
