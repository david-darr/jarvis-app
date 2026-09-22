"""The only actions a Swarm worker can take.

Every call is validated against the live attempt, not against what the model
was shown. A model that invents a tool name it was never offered, addresses an
agent in another company, or claims to be the lead gets the same rejection as
one that was never given the tool at all: the surface a provider advertises is
a convenience, never the boundary.

Deliberately absent in C1: shell execution, file writes, repository access and
vault access. While an unauthenticated loopback request resolves to the local
admin (core/middleware.py), a worker able to run commands could call every
ordinary JARVIS API as the owner, so no tool filter here could honestly be
called containment. Those roles stay blocked until that boundary is real.
"""
import json

from .models import Conflict, NotFound

MAX_BODY = 8000
MAX_TASKS = 25


class ToolRejected(Exception):
    """A call the service refused. Returned to the model as a tool result."""


def _text(value, field, limit=MAX_BODY):
    if not isinstance(value, str) or not value.strip():
        raise ToolRejected(f"{field} is required")
    return value.strip()[:limit]


def _as_list(value, field):
    """Some models send an array as a JSON string.

    This is not the prose rescue the adapters refuse: the model really did
    call this tool, and only the encoding of one argument is wrong. The same
    leniency already exists for Ollama's tool arguments in the ordinary
    provider client.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ToolRejected(f"{field} must be an array of objects")
    if not isinstance(value, list) or not value:
        raise ToolRejected(f"{field} must be a non-empty array")
    return value


def _summarize(stored_result):
    """A stored result is JSON the runtime wrote; show its prose, not its shape."""
    if not stored_result:
        return "(no recorded result)"
    try:
        value = json.loads(stored_result)
    except (TypeError, ValueError):
        return str(stored_result)[:1500]
    if isinstance(value, dict):
        for field in ("output", "summary", "reason"):
            if value.get(field):
                return str(value[field])[:1500]
    return json.dumps(value)[:1500]


_SPECIALIST = {
    "get_assigned_work": {
        "description": "Read your current assignment: its objective, any saved progress, the results of tasks it depends on, and unread messages addressed to you. Call this first.",
        "parameters": {"type": "object", "properties": {}},
    },
    "send_message": {
        "description": "Ask a specific teammate a question, or answer one. Use this instead of guessing at another specialist's interface or decision.",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "The teammate's name, exactly as listed in your assignment"},
                "body": {"type": "string"},
            },
            "required": ["to", "body"],
        },
    },
    "record_finding": {
        "description": "Report something the lead should know that does not finish your task: a risk, a discovery, a correction.",
        "parameters": {
            "type": "object",
            "properties": {"summary": {"type": "string"}, "detail": {"type": "string"}},
            "required": ["summary"],
        },
    },
    "report_blocker": {
        "description": "End this step because you cannot continue. Say exactly what is blocking you and what would unblock it. This does not complete the task.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}, "needs": {"type": "string"}},
            "required": ["reason"],
        },
    },
    "submit_result": {
        "description": "Finish this task and hand the result to the lead for review. Include what you actually produced and how it can be checked.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "output": {"type": "string"},
                "evidence": {"type": "string", "description": "How someone else could verify this"},
            },
            "required": ["summary", "output"],
        },
    },
    "propose_task": {
        "description": "Suggest work the lead has not assigned. This creates nothing by itself; the lead decides.",
        "parameters": {
            "type": "object",
            "properties": {"objective": {"type": "string"}, "rationale": {"type": "string"}},
            "required": ["objective"],
        },
    },
}

_LEAD = {
    **_SPECIALIST,
    "finish_mission": {
        "description": "End the mission because it is genuinely met. Say what was delivered and how it satisfies the mission. Only call this when there is nothing further worth doing - if useful work remains, assign it instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "What the company achieved"},
                "delivered": {"type": "string", "description": "The work itself, or where to find it"},
            },
            "required": ["summary"],
        },
    },
    "review_work": {
        "description": "Decide on work your team has submitted. Accept it with evidence of why it meets the objective, or ask for a revision with a specific reason. A task only counts as finished once you accept it.",
        "parameters": {
            "type": "object",
            "properties": {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {"type": "string", "description": "The task id from your assignment"},
                            "verdict": {"type": "string", "enum": ["accept", "revise"]},
                            "note": {"type": "string", "description": "Evidence if accepting, the specific problem if revising"},
                        },
                        "required": ["task", "verdict", "note"],
                    },
                },
            },
            "required": ["decisions"],
        },
    },
    "assign_plan": {
        "description": "Break the mission into tasks and assign each to a teammate by name. Dependencies may only reference earlier tasks in this same plan, using their key.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "A short id for this task, used by later dependencies"},
                            "assignee": {"type": "string", "description": "A teammate's name, exactly as listed"},
                            "objective": {"type": "string"},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["key", "assignee", "objective"],
                    },
                },
            },
            "required": ["tasks"],
        },
    },
}

_MEMORY = {
    "search_memory": {
        "description": "Search this company's enabled JARVIS memory sources. Returns a few short snippets and references. Use it only when prior context would materially help the assigned work.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    "read_memory": {
        "description": "Read one bounded JARVIS memory item using a reference returned by search_memory.",
        "parameters": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    },
}

_FINISH_SHIFT = {
    "finish_shift": {
        "description": "End this scheduled work shift because its useful work is complete or no actionable work remains. Preserve a concise handoff for the next shift without ending the continuing company mission.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "next": {"type": "string", "description": "The most useful next work, if any"},
            },
            "required": ["summary"],
        },
    },
}


class ToolService:
    """One instance per attempt. Holds no credential and no model state."""

    def __init__(self, store, assignment=None, *, is_lead, memory=None, scheduled=False):
        self.store = store
        self.assignment = assignment
        self.is_lead = is_lead
        self.memory = memory
        self.scheduled = scheduled
        self.terminal = None
        self.memory_refs = set()

    def bind(self, assignment):
        """The runtime creates the assignment, so identity arrives here, once.

        A worker is built before its attempt exists. Binding late keeps the
        executing identity server-made: nothing a model says can change it.
        """
        if self.assignment is not None and self.assignment.attempt_id != assignment.attempt_id:
            raise Conflict("A tool service cannot be reused across attempts")
        self.assignment = assignment
        self.terminal = None
        self.memory_refs.clear()
        return self

    @property
    def definitions(self) -> dict:
        definitions = dict(_LEAD if self.is_lead else _SPECIALIST)
        if self.memory and self.memory.enabled:
            definitions.update(_MEMORY)
        if self.is_lead and self.scheduled:
            definitions.update(_FINISH_SHIFT)
        return definitions

    def schemas(self) -> list[dict]:
        """OpenAI-shaped function definitions; adapters translate as needed."""
        return [{"type": "function", "function": {"name": name, **body}}
                for name, body in self.definitions.items()]

    def _team(self):
        return [agent for agent in self.store.team(self.assignment.system_id) if agent["enabled"]]

    def _resolve(self, name):
        wanted = (name or "").strip().casefold()
        for agent in self._team():
            if agent["name"].casefold() == wanted or agent["id"] == name:
                return agent
        raise ToolRejected(f"No teammate called {name!r}. Use a name exactly as listed in your assignment.")

    def _lead_id(self):
        for agent in self._team():
            if agent["is_lead"]:
                return agent["id"]
        raise ToolRejected("This company has no lead")

    def call(self, name, arguments) -> str:
        """Run one validated tool and return the text the model sees."""
        if name not in self.definitions:
            raise ToolRejected(f"{name} is not a tool you can use.")
        if not isinstance(arguments, dict):
            raise ToolRejected("Tool arguments must be an object")
        try:
            handler = getattr(self, "_" + name)
            return handler(arguments)
        except (Conflict, NotFound, ValueError) as exc:
            raise ToolRejected(str(exc)) from exc

    # -- individual tools -------------------------------------------------

    def _get_assigned_work(self, _arguments):
        assignment = self.assignment
        team = self._team()
        lines = [f"Task: {assignment.objective}"]
        checkpoint = assignment.checkpoint or {}
        if checkpoint:
            lines.append("Saved progress: " + json.dumps(checkpoint)[:2000])
        upstream = self.store.dependency_results(assignment.task_id)
        if upstream:
            lines.append("Completed work this depends on:")
            for task in upstream:
                lines.append(f"- {task['objective']}: {_summarize(task.get('result'))}")
        roster = ", ".join(f"{agent['name']} ({'lead' if agent['is_lead'] else agent['role']})" for agent in team)
        lines.append("Team you can message: " + roster)
        if self.is_lead:
            waiting = self.store.tasks_in_review(assignment.system_id)
            names = {agent["id"]: agent["name"] for agent in team}
            if waiting:
                lines.append("Work waiting on your review (use review_work with these ids):")
                for task in waiting:
                    lines.append(f"- {task['id']} — {task['objective']} (from {names.get(task['agent_id'], 'a teammate')}): "
                                 f"{_summarize(task.get('result'))}")
            last_shift = self.store.latest_shift_summary(assignment.system_id)
            if last_shift:
                lines.append("Previous shift handoff: " + last_shift[:2000])
        messages = self.store.inbox(assignment.agent_id)
        if messages:
            names = {agent["id"]: agent["name"] for agent in team}
            lines.append("Unread messages:")
            for message in messages:
                lines.append(f"- from {names.get(message['sender_id'], 'the owner')}: {message['body'][:1500]}")
        return "\n".join(lines)

    def _send_message(self, arguments):
        recipient = self._resolve(arguments.get("to"))
        body = _text(arguments.get("body"), "body")
        if recipient["id"] == self.assignment.agent_id:
            raise ToolRejected("You cannot message yourself.")
        self.store.agent_message(self.assignment.system_id, self.assignment.agent_id, recipient["id"], body)
        return f"Delivered to {recipient['name']}. They will see it when they next work."

    def _record_finding(self, arguments):
        summary = _text(arguments.get("summary"), "summary", 500)
        detail = (arguments.get("detail") or "").strip()[:MAX_BODY]
        body = f"Finding: {summary}" + (f"\n\n{detail}" if detail else "")
        self.store.agent_message(self.assignment.system_id, self.assignment.agent_id,
                                 self._lead_id(), body, kind="finding")
        return "Recorded for the lead."

    def _propose_task(self, arguments):
        objective = _text(arguments.get("objective"), "objective", 1000)
        rationale = (arguments.get("rationale") or "").strip()[:2000]
        body = f"Proposed task: {objective}" + (f"\n\nWhy: {rationale}" if rationale else "")
        self.store.agent_message(self.assignment.system_id, self.assignment.agent_id,
                                 self._lead_id(), body, kind="proposal")
        return "Sent to the lead. Nothing is scheduled until the lead assigns it."

    def _report_blocker(self, arguments):
        reason = _text(arguments.get("reason"), "reason", 2000)
        needs = (arguments.get("needs") or "").strip()[:2000]
        self.terminal = {"status": "blocked", "reason": reason, "needs": needs}
        return "Blocker recorded. This step is finished; the lead will decide what happens next."

    def _submit_result(self, arguments):
        summary = _text(arguments.get("summary"), "summary", 1000)
        output = _text(arguments.get("output"), "output")
        evidence = (arguments.get("evidence") or "").strip()[:MAX_BODY]
        self.terminal = {"status": "submitted", "summary": summary, "output": output, "evidence": evidence}
        return "Submitted for review."

    def _finish_mission(self, arguments):
        if not self.is_lead:
            raise ToolRejected("Only the lead can end the mission.")
        summary = _text(arguments.get("summary"), "summary", 4000)
        delivered = (arguments.get("delivered") or "").strip()[:MAX_BODY]
        self.terminal = {"status": "concluded", "summary": summary, "delivered": delivered}
        return "Mission recorded as complete. No further cycles will start."

    def _finish_shift(self, arguments):
        if not self.is_lead or not self.scheduled:
            raise ToolRejected("Only the lead of a scheduled company can end a shift.")
        summary = _text(arguments.get("summary"), "summary", 4000)
        next_work = (arguments.get("next") or "").strip()[:4000]
        self.terminal = {"status": "shift_complete", "summary": summary, "next": next_work}
        return "Shift handoff recorded. The continuing mission remains available for its next scheduled window."

    def _search_memory(self, arguments):
        if not self.memory or not self.memory.enabled:
            raise ToolRejected("JARVIS memory is not enabled for this company.")
        query = _text(arguments.get("query"), "query", 500)
        results = self.memory.search(query)
        self.memory_refs.update(item.get("ref") for item in results if item.get("ref"))
        self.store.record_memory_access(self.assignment, "memory.search",
                                        {"query": query, "results": len(results)})
        return json.dumps(results, ensure_ascii=False)

    def _read_memory(self, arguments):
        if not self.memory or not self.memory.enabled:
            raise ToolRejected("JARVIS memory is not enabled for this company.")
        reference = _text(arguments.get("ref"), "ref", 1000)
        if reference not in self.memory_refs:
            raise ToolRejected("Read a reference returned by search_memory in this worker step.")
        text = self.memory.read(reference)
        self.store.record_memory_access(self.assignment, "memory.read",
                                        {"ref": reference, "characters": len(text)})
        return f"Source {reference}:\n{text}"

    def _review_work(self, arguments):
        if not self.is_lead:
            raise ToolRejected("Only the lead reviews submitted work.")
        decisions = _as_list(arguments.get("decisions"), "decisions")
        waiting = {task["id"]: task for task in self.store.tasks_in_review(self.assignment.system_id)}
        results = []
        for decision in decisions:
            if not isinstance(decision, dict):
                raise ToolRejected("Each decision must be an object")
            task_id = str(decision.get("task") or "")
            if task_id not in waiting:
                raise ToolRejected(f"Task {task_id!r} is not waiting for your review.")
            verdict = str(decision.get("verdict") or "").lower()
            if verdict not in ("accept", "revise"):
                raise ToolRejected("verdict must be 'accept' or 'revise'")
            note = _text(decision.get("note"), "note", 4000)
            self.store.review_task(task_id, self.assignment.agent_id, accept=verdict == "accept", note=note)
            results.append(f"{waiting[task_id]['objective']}: {verdict}d")
        self.terminal = {"status": "reviewed", "decisions": results}
        return "Recorded: " + "; ".join(results)

    def _assign_plan(self, arguments):
        if not self.is_lead:
            raise ToolRejected("Only the lead assigns work.")
        objective = self.assignment.objective if self.assignment else ""
        review_step = objective.startswith("Review the ") and objective.endswith(
            " submitted task(s) and decide on each one.")
        if review_step and not self.store.tasks_in_review(self.assignment.system_id):
            ending = "finish_mission, or finish_shift for a continuing scheduled mission"
            raise ToolRejected(
                "This review step has no submitted work remaining; its decisions were already recorded. "
                f"Do not assign replacement work. If the mission is complete, call {ending}; "
                "otherwise leave genuinely new work to the next company cycle.")
        for task in self.store.tasks_in_review(self.assignment.system_id):
            try:
                status = json.loads(task.get("result") or "{}").get("status")
            except (TypeError, ValueError):
                status = None
            if status == "submitted":
                raise ToolRejected("Review submitted work with review_work before assigning more.")
        tasks = _as_list(arguments.get("tasks"), "tasks")
        if len(tasks) > MAX_TASKS:
            raise ToolRejected(f"Assign at most {MAX_TASKS} tasks at a time")
        items = []
        for index, task in enumerate(tasks):
            if not isinstance(task, dict):
                raise ToolRejected("Each task must be an object")
            agent = self._resolve(task.get("assignee"))
            items.append({
                "key": str(task.get("key") or index),
                "agent_id": agent["id"],
                "objective": _text(task.get("objective"), "objective", 2000),
                "depends_on": [str(key) for key in (task.get("depends_on") or [])],
            })
        run = self.store.open_run(self.assignment.system_id)
        if not run:
            raise ToolRejected("This company has no open run.")
        created = self.store.create_plan(self.assignment.system_id, run["id"], self.assignment.agent_id,
                                         items, command_id=f"plan:{self.assignment.attempt_id}")
        self.terminal = {"status": "planned", "tasks": created}
        return f"Assigned {len(created)} task(s). They will start as their dependencies complete."
