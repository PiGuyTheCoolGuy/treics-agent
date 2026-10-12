from __future__ import annotations

import dataclasses
import contextlib
import io
import json
import sqlite3
import threading
import unittest
from pathlib import Path
from unittest import mock

import test_agent as fixtures
from test_agent import decision
from treics_agent.checklist import Checklist, ChecklistError
from treics_agent.cli import main
from treics_agent.config import initialize_config, load_config, set_task_checklist
from treics_agent.model import ModelError, decision_schema, parse_decision
from treics_agent.storage import Store


def item(name, kind, target, expected=""):
    return {"id": name, "title": name.replace("_", " "),
            "check": {"kind": kind, "target": str(target), "expected": expected}}


class ChecklistTests(unittest.TestCase):
    tearDown = fixtures.AgentTests.tearDown
    build = fixtures.AgentTests.build
    complete = fixtures.AgentTests.complete

    def setUp(self):
        fixtures.AgentTests.setUp(self)
        self.config = dataclasses.replace(self.config, task_checklist=True)
        self.tools.config = self.config
        self.run_id = self.store.create_run("Checklist tests", 20, self.config.model, True)
        self.checklist = Checklist(self.store, self.run_id, self.config.work_path)

    def record(self, obj, result=None, run_id=None, kind="action"):
        data = {"decision": obj}
        if result is not None:
            data["result"] = result
        event_id = self.store.event(run_id or self.run_id, 1, kind, data)
        return self.store.evidence(run_id or self.run_id, event_id)

    def test_config_switch_preserves_credentials_and_defaults_off(self):
        path = Path(self.temp.name) / "config.toml"
        initialize_config(path)
        initial = load_config(path)
        self.assertFalse(initial.task_checklist)
        path.write_text(path.read_text() + "# user comment\n")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.dict("os.environ", {}):
            self.assertEqual(main(["--config", str(path), "checklist", "on"]), 0)
        self.assertNotIn(initial.dashboard_token, output.getvalue())
        self.assertTrue(load_config(path).task_checklist)
        self.assertEqual(load_config(path).dashboard_token, initial.dashboard_token)
        self.assertIn("# user comment", path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        set_task_checklist(False, path)
        self.assertFalse(load_config(path).task_checklist)
        self.assertEqual(path.read_text().count("task_checklist ="), 1)
        path.write_text(path.read_text().replace("task_checklist = false", '  "task_checklist" = false # setting'))
        set_task_checklist(True, path)
        self.assertTrue(load_config(path).task_checklist)
        path.write_text('task_checklist = "true"\n')
        with self.assertRaisesRegex(ValueError, "true or false"):
            load_config(path)

    def test_real_tool_observations_verify_specific_checks_and_reopen_changed_files(self):
        self.checklist.set_items([item("source", "file_written", "parser.py"),
                                 item("read_back", "file_read", "parser.py"),
                                 item("ran", "command_succeeded", "python3 parser.py"),
                                 item("correct_output", "output_equals", "python3 parser.py", "False\n")], "Initial plan")
        for obj in [decision("write_file", {"path": "parser.py", "content": "print('wrong')\n"}),
                    decision("read_file", {"path": "parser.py"}),
                    decision("shell", {"command": "python3 parser.py"})]:
            result = self.tools.execute(obj["action"], obj["arguments"], threading.Event())
            self.checklist.observe(self.record(obj, result))
        status = {row["id"]: row["status"] for row in self.checklist.snapshot()}
        self.assertEqual(status, {"source": "verified", "read_back": "verified", "ran": "verified", "correct_output": "attempted"})
        obj = decision("write_file", {"path": "parser.py", "content": "print(False)\n"})
        self.checklist.observe(self.record(obj, self.tools.execute(obj["action"], obj["arguments"], threading.Event())))
        self.assertEqual(self.checklist.snapshot()[1]["status"], "pending")
        obj = decision("shell", {"command": "python3 parser.py"})
        self.checklist.observe(self.record(obj, self.tools.execute(obj["action"], obj["arguments"], threading.Event())))
        self.assertEqual(self.checklist.snapshot()[3]["status"], "verified")

    def test_evidence_validation_rejects_failed_foreign_unknown_and_future_references_atomically(self):
        self.checklist.set_items([item("run", "command_succeeded", "false"),
                                 item("read", "file_read", "report.md")], "Initial plan")
        failed = self.record(decision("shell", {"command": "false"}), {"ok": False, "exit_code": 1})
        other_run = self.store.create_run("Other", 10, self.config.model)
        foreign = self.record(decision("shell", {"command": "false"}), {"ok": True, "exit_code": 0}, other_run)
        started_id = self.store.event(self.run_id, 2, "action_started", {"decision": decision("shell", {"command": "false"})})
        for ref in (failed["id"], foreign["id"], started_id, 999999, 10**100, True, "1"):
            before = self.checklist.snapshot()
            with self.assertRaises(ChecklistError):
                self.checklist.apply_updates([
                    {"id": "read", "status": "blocked", "evidence_ids": [], "note": "Missing report"},
                    {"id": "run", "status": "verified", "evidence_ids": [ref], "note": "Succeeded"}])
            self.assertEqual(self.checklist.snapshot(), before)

    def test_newer_failure_cannot_be_overridden_by_old_or_mixed_success_evidence(self):
        self.checklist.set_items([item("run", "command_succeeded", "work")], "Initial plan")
        success = self.record(decision("shell", {"command": "work"}), {"ok": True, "exit_code": 0})
        failed = self.record(decision("shell", {"command": "work"}), {"ok": False, "exit_code": 1})
        for refs in ([success["id"]], [success["id"], failed["id"]]):
            with self.assertRaises(ChecklistError):
                self.checklist.apply_updates([{"id": "run", "status": "verified", "evidence_ids": refs, "note": ""}])
        self.checklist.apply_updates([{"id": "run", "status": "pending", "evidence_ids": [], "note": "Try a corrected approach"}])
        self.assertEqual(self.checklist.snapshot()[0]["status"], "pending")

    def test_file_content_evidence_is_invalidated_by_later_writes_and_partial_reads_do_not_verify(self):
        self.checklist.set_items([item("content", "output_equals", "report.md", "correct")], "Initial plan")
        read = decision("read_file", {"path": "report.md"})
        partial = self.record(read, {"ok": True, "content": "correct", "offset": 0, "truncated": True})
        self.checklist.observe(partial)
        self.assertEqual(self.checklist.snapshot()[0]["status"], "attempted")
        full = self.record(read, {"ok": True, "content": "correct", "offset": 0, "truncated": False})
        self.checklist.observe(full)
        changed = self.record(decision("write_file", {"path": "report.md", "content": "changed"}),
                              {"ok": True, "path": str(self.config.work_path / "report.md")})
        self.checklist.observe(changed)
        self.assertEqual(self.checklist.snapshot()[0]["status"], "pending")
        with self.assertRaisesRegex(ChecklistError, "file changed"):
            self.checklist.apply_updates([{"id": "content", "status": "verified", "evidence_ids": [full["id"]], "note": ""}])

    def test_blocked_and_reopened_states_accept_notes_without_forcing_execution_order(self):
        self.checklist.set_items([item("first", "file_read", "input.txt"),
                                 item("second", "file_written", "output.md")], "Initial plan")
        self.checklist.apply_updates([{"id": "first", "status": "blocked", "evidence_ids": [], "note": "Input is missing"}])
        written = self.record(decision("write_file", {"path": "output.md", "content": "draft"}),
                              {"ok": True, "path": str(self.config.work_path / "output.md")})
        self.checklist.observe(written)
        self.assertEqual([row["status"] for row in self.checklist.snapshot()], ["blocked", "verified"])
        self.checklist.apply_updates([{"id": "first", "status": "pending", "evidence_ids": [], "note": "Input is now available"}])
        self.assertEqual(self.checklist.snapshot()[0]["status"], "pending")

    def test_existing_checklist_persists_past_history_and_revising_target_reopens_it(self):
        definition = item("read", "file_read", "report.md")
        self.checklist.set_items([definition], "Initial plan")
        event = self.record(decision("read_file", {"path": "report.md"}),
                            {"ok": True, "content": "report", "offset": 0, "truncated": False})
        self.checklist.observe(event)
        for _ in range(10):
            self.record(decision("shell", {"command": "printf unrelated"}), {"ok": True, "exit_code": 0})
        self.assertFalse(any(row["id"] == event["id"] for row in self.store.events(self.run_id, 6)))
        self.assertEqual(self.checklist.snapshot()[0]["evidence_ids"], [event["id"]])
        self.assertFalse(self.checklist.set_items([definition], "Same plan"))
        self.checklist.set_items([item("read", "file_read", "new.md")], "A different report is now required")
        self.assertEqual(self.checklist.snapshot()[0]["status"], "pending")
        self.store.close()
        self.store = Store(self.config.data_path)
        self.checklist = Checklist(self.store, self.run_id, self.config.work_path)
        self.assertEqual(self.checklist.snapshot()[0]["check"]["target"], "new.md")

    def test_enabled_agent_updates_checklist_without_extra_model_calls(self):
        (self.config.work_path / "source").mkdir()
        plan = [item("list", "directory_listed", "source"),
                item("report", "file_written", "audit.md"), item("read", "file_read", "audit.md")]
        model = self.build([decision("set_checklist", {"items": plan, "revision_reason": "Initial plan"}),
                            decision("list_files", {"path": "source"}),
                            decision("write_file", {"path": "audit.md", "content": "Inventory\n"}),
                            decision("read_file", {"path": "audit.md"}),
                            decision("finish", {"summary": "Saved and read back audit"})])
        run = self.complete(max_steps=12)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(model.calls, 5)
        self.assertEqual(model.checklists[0], [])
        self.assertEqual(model.checklists[2][0]["status"], "verified")
        self.assertTrue(all(row["status"] == "verified" for row in run["checklist"]))
        self.assertTrue(all(self.store.evidence(run["id"], row["evidence_ids"][0]) for row in run["checklist"]))

    def test_invalid_checklist_update_prevents_proposed_shell_execution(self):
        plan = [item("run", "command_succeeded", "printf correct")]
        attempt = decision("shell", {"command": "touch should-not-exist"})
        attempt["checklist_updates"] = [{"id": "run", "status": "verified", "evidence_ids": [999999], "note": ""}]
        self.build([decision("set_checklist", {"items": plan, "revision_reason": "Initial plan"}), attempt,
                    decision("finish", {"summary": "Blocked"})])
        with mock.patch.object(self.agent.stop_event, "wait", side_effect=lambda timeout: self.agent.stop_event.is_set()):
            run = self.complete(max_steps=6)
        self.assertFalse((self.config.work_path / "should-not-exist").exists())
        self.assertIn("Unverified checklist requirements", run["summary"])
        self.assertEqual(run["checklist"][0]["status"], "pending")
        rejected = next(e for e in self.store.events(run["id"]) if e["kind"] == "error")
        self.assertFalse(rejected["data"]["action_executed"])
        self.assertIsNone(self.store.evidence(run["id"], rejected["id"]))

    def test_repeated_unchanged_plans_pause_even_when_revision_reason_changes(self):
        plan = [item("read", "file_read", "report.md")]
        model = self.build([decision("set_checklist", {"items": plan, "revision_reason": reason})
                            for reason in ("Initial plan", "Check again", "Review plan", "Ensure plan exists")] +
                           [decision("finish", {"summary": "Blocked"})])
        run_id = self.agent.start("Plan loop", 12)
        fixtures.AgentTests.wait_for_status(self, run_id, "paused")
        self.assertEqual(model.calls, 4)
        self.assertIn("3 matching set_checklist", self.agent.snapshot()["pause_reason"])
        self.agent.resume()
        self.agent.thread.join(3)
        self.assertEqual(self.store.run(run_id)["status"], "completed")

    def test_off_schema_and_parser_reject_checklist_actions_and_updates(self):
        schema = decision_schema(task_checklist=False)
        self.assertNotIn("set_checklist", [c["properties"]["action"]["enum"][0] for c in schema["anyOf"]])
        obj = decision("set_checklist", {"items": [item("read", "file_read", "x")], "revision_reason": "Initial plan"})
        with self.assertRaises(ModelError):
            parse_decision(json.dumps(obj))
        self.assertEqual(parse_decision(json.dumps(obj), True), obj)
        obj = decision("finish", {"summary": "done"})
        obj["checklist_updates"] = []
        with self.assertRaises(ModelError):
            parse_decision(json.dumps(obj))
        self.assertEqual(parse_decision(json.dumps(obj), True), obj)

    def test_old_database_migration_keeps_previous_runs_and_defaults_their_checklists_off(self):
        directory = Path(self.temp.name) / "old-data"
        directory.mkdir()
        with sqlite3.connect(directory / "agent.db") as db:
            db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, task TEXT, status TEXT, created REAL, updated REAL, "
                       "step INTEGER DEFAULT 0, state TEXT DEFAULT '', summary TEXT DEFAULT '', max_steps INTEGER, model TEXT)")
            db.execute("INSERT INTO runs(id,task,status,created,updated,max_steps,model) VALUES ('old','Keep me','completed',1,1,6,'model')")
        store = Store(directory)
        try:
            run = store.run("old")
            self.assertEqual(run["task"], "Keep me")
            self.assertFalse(run["task_checklist"])
            self.assertEqual(run["checklist"], [])
        finally:
            store.close()

