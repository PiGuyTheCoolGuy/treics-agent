from __future__ import annotations

import copy
import json
from pathlib import Path

from .storage import Store


CHECK_KINDS = ("directory_listed", "file_written", "file_read", "command_succeeded",
               "output_equals", "output_contains")
STATUSES = ("pending", "attempted", "verified", "blocked")

CHECK_SCHEMA = {"type": "object", "additionalProperties": False,
                "properties": {"kind": {"enum": list(CHECK_KINDS)},
                               "target": {"type": "string", "maxLength": 500},
                               "expected": {"type": "string", "maxLength": 200}},
                "required": ["kind", "target", "expected"]}
ITEM_SCHEMA = {"type": "object", "additionalProperties": False,
               "properties": {"id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"},
                              "title": {"type": "string", "maxLength": 140}, "check": CHECK_SCHEMA},
               "required": ["id", "title", "check"]}
UPDATES_SCHEMA = {"type": "array", "maxItems": 4, "items": {
    "type": "object", "additionalProperties": False,
    "properties": {"id": ITEM_SCHEMA["properties"]["id"], "status": {"enum": list(STATUSES)},
                   "evidence_ids": {"type": "array", "maxItems": 3,
                                    "items": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}},
                   "note": {"type": "string", "maxLength": 160}},
    "required": ["id", "status", "evidence_ids", "note"]}}

CHECKLIST_PROMPT = """
Optional task checklist mode is ON. Before starting work, use set_checklist once to identify
up to eight concise requirements from the original task, each with a specific check.
set_checklist is controller bookkeeping, compatible with shell/file-only tasks; it does not
operate the desktop, access the network, or modify task files.
The checklist is a progress aid, not a replacement for the task or a mandatory execution order.
Use list_files for a directory_listed check. file_written/file_read targets are file paths.
command_succeeded targets the EXACT shell command and confirms execution only, not correct results.
output_equals/output_contains targets an EXACT shell command or read_file path; expected is the
literal output/content to check. Use expected="" for other kinds. Prefer small concrete checks.
The controller updates matching checks from actual tool results: failed attempts stay attempted,
and verified means only the stated criterion passed. Keep correctness checks separate from
file creation or script execution. Omitted or uncheckable requirements still remain part of the task.
The checklist context persists beyond recent history. Do not repeat a verified step unless new
evidence warrants it. Choose the next useful pending/attempted requirement, or revise the plan.
Optional checklist_updates on an ordinary action may reopen a check as pending, record a blocked
requirement with a note, or cite recorded event_id values. References must belong to this run and
cannot refer to the proposed action. Verified updates are independently checked against stored
tool results. Reopening clears old evidence. set_checklist can revise requirements with a reason;
changing a criterion resets it to pending. Updates need no separate action or model call.
Finish promptly when done or blocked; report any unverified requirements honestly. Never treat
checklist verification alone as proof of overall correctness, legality, or task completion.
"""


class ChecklistError(ValueError):
    pass


class Checklist:
    def __init__(self, store: Store, run_id: str, workspace: Path):
        self.store = store
        self.run_id = run_id
        self.workspace = workspace

    def snapshot(self) -> list[dict]:
        return self.store.run(self.run_id)["checklist"]

    def path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.workspace / path).resolve()

    def save(self, items: list[dict]) -> bool:
        if len(json.dumps(items, ensure_ascii=False)) > 5000:
            raise ChecklistError("Checklist is too large; shorten titles, targets, and notes.")
        if items == self.snapshot():
            return False
        self.store.update(self.run_id, checklist=items)
        return True

    def set_items(self, definitions: list[dict], reason: str) -> bool:
        if not reason.strip():
            raise ChecklistError("Give a reason for creating or revising the checklist.")
        if not 1 <= len(definitions) <= 8:
            raise ChecklistError("Use 1–8 checklist requirements.")
        old = {item["id"]: item for item in self.snapshot()}
        items = []
        seen = set()
        for definition in definitions:
            name, check = definition["id"], definition["check"]
            if name in seen or not definition["title"].strip() or not check["target"].strip():
                raise ChecklistError("Checklist IDs must be unique; titles and targets cannot be empty.")
            if check["kind"] == "output_contains" and not check["expected"]:
                raise ChecklistError("output_contains needs a nonempty expected value.")
            seen.add(name)
            previous = old.get(name)
            if previous and previous["check"] == check and previous["title"] == definition["title"]:
                items.append(copy.deepcopy(previous))
            else:
                items.append({**copy.deepcopy(definition), "status": "pending", "evidence_ids": [], "note": ""})
        return self.save(items)

    def relevant(self, check: dict, event: dict) -> bool:
        if event["data"].get("action_executed") is False:
            return False
        decision = event["data"].get("decision", {})
        action, args = decision.get("action"), decision.get("arguments", {})
        kind, target = check["kind"], check["target"]
        if kind == "command_succeeded":
            return action == "shell" and args.get("command") == target
        if kind in {"output_equals", "output_contains"} and action == "shell":
            return args.get("command") == target
        needed = {"directory_listed": "list_files", "file_written": "write_file",
                  "file_read": "read_file", "output_equals": "read_file", "output_contains": "read_file"}[kind]
        if action != needed:
            return False
        actual = args.get("path", "." if action == "list_files" else None)
        try:
            return isinstance(actual, str) and self.path(actual) == self.path(target)
        except (OSError, ValueError):
            return False

    def passes(self, check: dict, event: dict) -> bool:
        if not self.relevant(check, event) or event["kind"] != "action":
            return False
        result = event["data"].get("result", {})
        if result.get("ok") is not True:
            return False
        kind = check["kind"]
        if kind == "command_succeeded":
            return result.get("exit_code") == 0 and not result.get("timed_out")
        if kind == "directory_listed":
            return isinstance(result.get("entries"), list)
        if kind == "file_written":
            return self.path(result["path"]) == self.path(check["target"])
        if result.get("truncated") or result.get("offset", 0) != 0:
            return False
        if kind == "file_read":
            return isinstance(result.get("content"), str)
        output = result.get("output", result.get("content"))
        if not isinstance(output, str):
            return False
        return output == check["expected"] if kind == "output_equals" else check["expected"] in output

    def apply_updates(self, updates: list[dict]) -> bool:
        # Validate the whole batch before saving; one invalid reference must not
        # leave earlier entries partially updated or allow the proposed action.
        items = copy.deepcopy(self.snapshot())
        by_id = {item["id"]: item for item in items}
        seen = set()
        for update in updates:
            name, status, refs = update["id"], update["status"], update["evidence_ids"]
            if name not in by_id or name in seen:
                raise ChecklistError("Checklist updates require distinct existing IDs.")
            seen.add(name)
            evidence = [self.store.evidence(self.run_id, ref) for ref in refs]
            if any(event is None for event in evidence):
                raise ChecklistError("Evidence must reference recorded action/error event_ids from this run.")
            if status == "pending" and refs:
                raise ChecklistError("Reopening a requirement clears evidence; use evidence_ids=[].")
            if status == "attempted" and not refs:
                raise ChecklistError("An attempted requirement needs recorded tool evidence.")
            if status == "blocked" and not update["note"].strip():
                raise ChecklistError("Explain why the requirement is blocked.")
            item = by_id[name]
            newest = max(evidence, key=lambda event: event["id"]) if evidence else None
            if status == "verified" and (not newest or not self.passes(item["check"], newest)):
                raise ChecklistError(f"{name} cannot be verified: cited results do not pass its stated check.")
            if status in {"attempted", "verified"} and any(not self.relevant(item["check"], e) for e in evidence):
                raise ChecklistError("Cited evidence does not match the requirement's target/tool.")
            # Older successes cannot override a newer matching failure or a
            # recorded file change. Re-run/read the target to establish new evidence.
            if status == "verified":
                newer = self.store.events_after(self.run_id, max(refs))
                if len(newer) > 100:
                    raise ChecklistError("Evidence is too old to check safely; obtain fresh evidence.")
                for event in newer:
                    if self.relevant(item["check"], event) and not self.passes(item["check"], event):
                        raise ChecklistError("Newer evidence contradicts this check; verify it again.")
                    if self.file_changed(item["check"], event, newest):
                        raise ChecklistError("The file changed after this evidence; verify it again.")
            item.update(status=status, evidence_ids=list(refs), note=update["note"])
        return self.save(items)

    def file_changed(self, check: dict, event: dict, proof: dict | None = None) -> bool:
        if check["kind"] not in {"file_read", "file_written", "directory_listed", "output_equals", "output_contains"}:
            return False
        if check["kind"] in {"output_equals", "output_contains"}:
            # Shell targets are command strings, not filesystem paths. Only a
            # read_file observation establishes a path-dependent content check.
            if not proof or proof["data"].get("decision", {}).get("action") != "read_file":
                return False
        decision = event["data"].get("decision", {})
        result = event["data"].get("result", {})
        if decision.get("action") != "write_file" or not result.get("ok") or result.get("skipped"):
            return False
        path = self.path(decision["arguments"]["path"])
        target = self.path(check["target"])
        return path.parent == target if check["kind"] == "directory_listed" else path == target

    def observe(self, event: dict) -> bool:
        items = copy.deepcopy(self.snapshot())
        for item in items:
            if self.relevant(item["check"], event):
                passed = self.passes(item["check"], event)
                item.update(status="verified" if passed else "attempted", evidence_ids=[event["id"]],
                            note="" if passed else "Latest attempt did not pass the stated check.")
            elif item["status"] == "verified":
                proof = self.store.evidence(self.run_id, max(item["evidence_ids"]))
                if self.file_changed(item["check"], event, proof):
                    item.update(status="pending", evidence_ids=[], note="File changed; check it again.")
        return self.save(items)
