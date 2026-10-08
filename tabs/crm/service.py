"""CRM follow-ups and their evidence. One atomic store, scoped by owner.

Follows the custom-tab service convention. A batch's tasks and processed
message marker commit together; a failed write rolls back in-memory state.
"""
import copy
import hashlib
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from core import tab_api

api = tab_api.for_tab(__package__)
api.adopt_data_file("crm.json")
STATUSES = ("active", "in_progress", "waiting", "needs_review", "done", "dismissed")
PRIORITIES = ("urgent", "high", "normal", "low")
DEFAULTS = {"endpoint_id": None, "timezone": "UTC", "review_all": False,
            "auto_scan": False, "interval_minutes": 30, "lookback_days": 14, "max_messages": 40}


def key(value):
    return re.sub(r"\W+", " ", str(value).casefold()).strip()


def safe_url(value):
    parsed = urlparse(value or "")
    return value if parsed.scheme == "https" and parsed.hostname and not parsed.username else None


def valid_due(value):
    if not value:
        return None
    if len(value) == 10:
        date.fromisoformat(value)
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("A deadline with a time needs a timezone")
    return value


class CRMService:
    def __init__(self, name=None):
        self.name = name or "crm.json"
        self._lock = threading.RLock()
        self._data = api.read_json(self.name, {"settings": {}, "sources": {}, "messages": {}, "tasks": {}, "runs": []})

    @contextmanager
    def transaction(self):
        with self._lock:
            before = copy.deepcopy(self._data)
            try:
                yield
                api.write_json(self.name, self._data)
            except BaseException:
                self._data = before
                raise

    def settings(self, owner):
        with self._lock:
            return {**DEFAULTS, **self._data["settings"].get(owner, {})}

    def owners(self):
        with self._lock:
            return list(self._data["settings"])

    def configure(self, owner, fields):
        values = {**self.settings(owner), **fields}
        ZoneInfo(values["timezone"])
        for name, lo, hi in (("interval_minutes", 5, 1440), ("lookback_days", 1, 90), ("max_messages", 1, 100)):
            if not lo <= values[name] <= hi:
                raise ValueError(f"{name} must be between {lo} and {hi}")
        with self.transaction():
            self._data["settings"][owner] = values
        return values

    def sources(self, owner):
        with self._lock:
            return copy.deepcopy([s for s in self._data["sources"].values() if s["owner"] == owner])

    def source(self, owner, source_id):
        with self._lock:
            source = self._data["sources"].get(source_id)
            if not source or source["owner"] != owner:
                raise KeyError(source_id)
            return copy.deepcopy(source)

    def add_source(self, owner, kind, connection_id, label, folder="INBOX", conversations=None):
        if kind not in ("email", "connector", "discord", "document"):
            raise ValueError("Unsupported source type")
        if any(c in folder for c in "\r\n\x00") or len(folder) > 200:
            raise ValueError("Invalid mailbox folder")
        with self.transaction():
            existing = next((s for s in self._data["sources"].values() if s["owner"] == owner
                             and (s["kind"], s["connection_id"], s["folder"]) == (kind, connection_id, folder)), None)
            if existing:
                return copy.deepcopy(existing)
            sid = uuid.uuid4().hex[:12]
            source = {"id": sid, "owner": owner, "kind": kind, "connection_id": connection_id,
                      "label": label, "folder": folder, "conversations": conversations or [],
                      "enabled": True, "created_at": time.time(), "last_scan_at": None, "error": None}
            self._data["sources"][sid] = source
            return copy.deepcopy(source)

    def toggle_source(self, owner, source_id, enabled):
        self.source(owner, source_id)
        with self.transaction():
            self._data["sources"][source_id]["enabled"] = enabled
        return self.source(owner, source_id)

    def capture(self, source, message):
        """Durably queue a source message before paid extraction. Never reopen it."""
        identity = message["external_id"]
        mid = hashlib.sha256(f"{source['owner']}:{source['id']}:{identity}".encode()).hexdigest()[:32]
        with self.transaction():
            if mid not in self._data["messages"]:
                self._data["messages"][mid] = {**message, "id": mid, "source_id": source["id"],
                    "owner": source["owner"], "source_label": source["label"], "source_kind": source["kind"],
                    "url": safe_url(message.get("url")), "state": "pending", "attempts": 0,
                    "captured_at": time.time(), "error": None}
        return self.message(source["owner"], mid)

    def message(self, owner, message_id):
        with self._lock:
            message = self._data["messages"].get(message_id)
            if not message or message["owner"] != owner:
                raise KeyError(message_id)
            return copy.deepcopy(message)

    def pending(self, owner, source_id):
        with self._lock:
            return copy.deepcopy(sorted((m for m in self._data["messages"].values()
                if m["owner"] == owner and m["source_id"] == source_id and m["state"] != "processed"
                and m["attempts"] < 3), key=lambda m: m.get("sent_at") or ""))

    def known(self, owner, source_id):
        with self._lock:
            return {m["external_id"] for m in self._data["messages"].values()
                    if m["owner"] == owner and m["source_id"] == source_id}

    def retry_failed(self, owner):
        with self.transaction():
            for m in self._data["messages"].values():
                if m["owner"] == owner and m["state"] == "failed":
                    m.update(state="pending", attempts=0, error=None)

    def thread(self, owner, message):
        with self._lock:
            return copy.deepcopy(sorted((m for m in self._data["messages"].values()
                if m["owner"] == owner and m["source_id"] == message["source_id"]
                and m.get("thread_id") == message.get("thread_id")), key=lambda m: m.get("sent_at") or "")[-6:])

    def tasks(self, owner):
        with self._lock:
            return copy.deepcopy([t for t in self._data["tasks"].values() if t["owner"] == owner])

    def task(self, owner, task_id):
        with self._lock:
            task = self._data["tasks"].get(task_id)
            if not task or task["owner"] != owner:
                raise KeyError(task_id)
            return copy.deepcopy(task)

    def create_task(self, owner, fields):
        title = (fields.get("title") or "").strip()
        if not title:
            raise ValueError("A task needs a title")
        priority = fields.get("priority", "normal")
        if priority not in PRIORITIES:
            raise ValueError("Invalid priority")
        status = fields.get("status", "active")
        if status not in STATUSES:
            raise ValueError("Invalid status")
        due = valid_due(fields.get("due_date"))
        tid = uuid.uuid4().hex[:12]
        task = {"id": tid, "owner": owner, "title": title[:300], "notes": fields.get("notes", ""),
                "contact": fields.get("contact", ""), "project": fields.get("project", ""),
                "priority": priority, "priority_reason": "Set by you", "status": status,
                "due_date": due, "deadline_text": "", "deadline_kind": "manual", "snoozed_until": None,
                "evidence": [], "review_reason": None, "proposal": None, "overrides": [],
                "created_at": time.time(), "updated_at": time.time()}
        with self.transaction():
            self._data["tasks"][tid] = task
        return copy.deepcopy(task)

    def update_task(self, owner, task_id, fields):
        self.task(owner, task_id)
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError("Invalid status")
        if "priority" in fields and fields["priority"] not in PRIORITIES:
            raise ValueError("Invalid priority")
        if "title" in fields and not fields["title"].strip():
            raise ValueError("A task needs a title")
        for name in ("due_date", "snoozed_until"):
            if name in fields:
                fields[name] = valid_due(fields[name])
        allowed = ("title", "notes", "contact", "project", "due_date", "priority", "status", "snoozed_until")
        with self.transaction():
            task = self._data["tasks"][task_id]
            for name in allowed:
                if name in fields:
                    task[name] = fields[name]
                    if name not in task["overrides"]:
                        task["overrides"].append(name)
            if "due_date" in fields:
                task["deadline_kind"] = "manual"
            if "priority" in fields:
                task["priority_reason"] = "Set by you"
            if task["status"] != "needs_review":
                task["review_reason"] = None
            task["updated_at"] = time.time()
        return self.task(owner, task_id)

    def apply(self, owner, message_id, items):
        """Only validated extraction enters here. Tasks + marker are one commit."""
        message = self.message(owner, message_id)
        count = 0
        with self.transaction():
            if self._data["messages"][message_id]["state"] == "processed":
                return 0
            for item in items:
                evidence = {"message_id": message_id, "quote": item["evidence"], "url": message.get("url"),
                            "label": message["source_label"], "subject": message.get("subject", ""),
                            "sent_at": message.get("sent_at")}
                target = self._data["tasks"].get(item.get("match_task_id"))
                if target and (target["owner"] != owner or target.get("source_id") != message["source_id"]
                               or target.get("thread_id") != message.get("thread_id")):
                    raise ValueError("A task match must belong to this source thread")
                if target is None:
                    target = next((t for t in self._data["tasks"].values() if t["owner"] == owner
                        and t.get("source_id") == message["source_id"] and t.get("thread_id") == message.get("thread_id")
                        and key(t["title"]) == key(item["title"])), None)
                if target:
                    if evidence not in target["evidence"]:
                        target["evidence"].append(evidence)
                    # New evidence never reopens a dismissed/completed task or overwrites user edits.
                    changed = any(item.get(n) != target.get(n) for n in ("due_date", "status", "title", "priority", "project"))
                    if changed and target["status"] not in ("done", "dismissed"):
                        target["proposal"] = {**item, "message_id": message_id}
                        target["review_reason"] = "A later message suggests a change. Review it before applying."
                    target["updated_at"] = time.time()
                    continue
                tid = uuid.uuid4().hex[:12]
                self._data["tasks"][tid] = {**item, "id": tid, "owner": owner,
                    "source_id": message["source_id"], "thread_id": message.get("thread_id"),
                    "contact": message.get("sender", ""), "notes": "", "snoozed_until": None,
                    "evidence": [evidence], "proposal": None, "overrides": [],
                    "created_at": time.time(), "updated_at": time.time()}
                count += 1
            self._data["messages"][message_id].update(state="processed", error=None)
        return count

    def resolve_proposal(self, owner, task_id, accept):
        current = self.task(owner, task_id)
        if not current.get("proposal"):
            raise ValueError("No suggested change to review")
        with self.transaction():
            task = self._data["tasks"][task_id]
            if accept:
                proposed = task["proposal"]
                for field in ("title", "due_date", "status", "priority", "project", "deadline_text", "deadline_kind", "priority_reason"):
                    if field in proposed:
                        task[field] = proposed[field]
                if task["status"] == "needs_review":
                    task["status"] = "active"
            task.update(proposal=None, review_reason=None, updated_at=time.time())
        return self.task(owner, task_id)

    def fail_message(self, owner, message_id, reason):
        self.message(owner, message_id)
        with self.transaction():
            m = self._data["messages"][message_id]
            m.update(state="failed", attempts=m["attempts"] + 1, error=reason)

    def attach_card(self, owner, task_id, card_id):
        self.task(owner, task_id)
        with self.transaction():
            self._data["tasks"][task_id]["agent_card_id"] = card_id

    def record_scan(self, owner, source_id, result):
        self.source(owner, source_id)
        with self.transaction():
            self._data["sources"][source_id].update(last_scan_at=time.time(), error=result.get("error"))
            self._data["runs"].append({"id": uuid.uuid4().hex[:12], "owner": owner,
                                     "source_id": source_id, "at": time.time(), **result})
            # Keep per-owner history, so another person's scans cannot displace it.
            counts, keep = {}, []
            for run in reversed(self._data["runs"]):
                who = run["owner"]
                counts[who] = counts.get(who, 0) + 1
                if counts[who] <= 50:
                    keep.append(run)
            self._data["runs"] = list(reversed(keep))

    def snapshot(self, owner):
        tasks = self.tasks(owner)
        with self._lock:
            runs = copy.deepcopy([r for r in reversed(self._data["runs"]) if r["owner"] == owner])
            failed = [m for m in self._data["messages"].values() if m["owner"] == owner and m["state"] == "failed"]
        return {"tasks": tasks, "sources": self.sources(owner), "settings": self.settings(owner),
                "runs": runs, "failed_messages": len(failed)}

    def calendar_events(self, owner, start, end):
        zone = ZoneInfo(self.settings(owner)["timezone"])
        first = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(zone)
        last = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(zone)
        items = []
        for task in self.tasks(owner):
            due = task.get("due_date")
            if not due or task["status"] in ("done", "dismissed", "needs_review"):
                continue
            all_day = len(due) == 10
            in_range = first.date() <= date.fromisoformat(due) < last.date() if all_day else (
                first <= datetime.fromisoformat(due.replace("Z", "+00:00")) < last)
            if in_range:
                items.append({"id": task["id"], "title": task["title"], "start": due, "end": due,
                              "all_day": all_day, "source": "tab", "source_label": "From CRM",
                              "toggle_url": f"/api/tab-crm/tasks/{task['id']}/completed", "completed": False,
                              "location": task.get("contact", ""), "description": task.get("notes", "")})
        return items


crm_service = CRMService()
