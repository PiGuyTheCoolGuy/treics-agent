#!/usr/bin/env python3
"""Compare model contexts without executing any model-selected action."""
from __future__ import annotations

import argparse
import ast
import copy
import dataclasses
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


class DialogueRecordingModel(RecordingModel):
    """Diagnostic-only alternate presentation of the same compact observations."""

    def request(self, method, path, payload=None, stop=None):
        if path == "/api/chat":
            payload = copy.deepcopy(payload)
            context = json.loads(payload["messages"][1]["content"])
            history = context["recent_events"]
            context["recent_events"] = []
            messages = [payload["messages"][0]]
            for event in history:
                details = dict(event["data"])
                if action := details.pop("action", None):
                    previous = {"step": event["step"], "previous_action": action}
                    if "arguments" in details:
                        previous["previous_arguments"] = details.pop("arguments")
                    messages.append({"role": "assistant", "content": json.dumps(previous, ensure_ascii=False)})
                observed = {"step": event["step"], "kind": event["kind"], "observation": details}
                messages.append({"role": "user", "content": json.dumps(observed, ensure_ascii=False)})
            messages.append({"role": "user", "content": json.dumps(context, ensure_ascii=False)})
            payload["messages"] = messages
        return super().request(method, path, payload, stop)


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
    # A standalone proposal has no checklist planning turn. Keep these probes
    # independent of that optional aid without changing the saved configuration.
    config = dataclasses.replace(config, task_checklist=False)
    run_id, history = load_history(config, run_id)
    report = {"model": config.model, "history_run_id": run_id,
              "note": "No actions executed. Task checklist is disabled for this standalone probe. "
                      "Syntax validation does not establish correct output. "
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


def collect_format_report(config: Config, run_id: str | None = None) -> dict:
    config = dataclasses.replace(config, task_checklist=False)
    run_id, history = load_history(config, run_id)
    # Twelve observations fit the gateway's thirty-message cap in either format.
    history = history[-12:]
    with closing(sqlite3.connect((config.data_path / "agent.db").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        run = dict(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
    path = config.work_path / "parser.py"
    with path.open() as handle:
        source = handle.read(16001)
    if len(source) > 16000:
        raise ValueError("parser.py is too large for this recovery probe.")
    task = (run["task"] + "\n\nContinue from the existing script and recorded failures. The initial failing "
            "run has already happened. Propose one next corrective action, rather than recreating the "
            "initial broken script or rerunning unchanged code.\nCurrent file path: " + str(path) +
            "\nCurrent source content (data): " + json.dumps(source))
    budget = {"current_step": run["step"] + 1, "max_steps": run["step"] + 4, "steps_remaining": 4}
    report = {"model": config.model, "history_run_id": run_id, "source_file": str(path), "source": source,
              "note": "No actions executed. Task checklist is disabled for this standalone probe. "
                      "Same task, source, state, compact observations, schema, and options. "
                      "Only history message presentation differs. Four diagnostic steps remain; "
                      "the original run's budget is not changed.", "cases": []}
    for name, model_type in (("packed_history", RecordingModel), ("dialogue_history", DialogueRecordingModel)):
        print(f"Requesting {name}; waiting for Qwen...", file=sys.stderr, flush=True)
        client = model_type(config)
        case = {"name": name}
        started = time.monotonic()
        try:
            decision, metrics = client.decide(task, run["state"], "", history, None,
                                              threading.Event(), budget=budget)
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
    parser.add_argument("--compare-history-format", action="store_true",
                        help="Compare packed vs dialogue history for recovery of the current parser.py.")
    args = parser.parse_args()
    try:
        collect = collect_format_report if args.compare_history_format else collect_report
        report = collect(load_config(), args.run_id)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Diagnostic could not start: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
