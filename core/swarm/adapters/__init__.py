"""Provider adapters: one normalized worker contract, three transports.

An adapter is admitted on what it can be observed to do, never on its vendor
name. `Capability` is the whole gate: a connection that cannot bound a step,
cannot be told to stop, or cannot say what it spent does not run unattended
work, whoever makes it.

Every worker built here is scoped to Swarm's typed tools. None of them
receives the app's repository directories, the hive-mind vault tools, a shell,
or the process-wide internal token that `core/codex_brain.py` exports to its
own children.
"""
from dataclasses import dataclass, field

from ..models import Capability

# One place to change the shared step bounds. These are dispatch bounds, not a
# claim about a provider's own billing.
# How many exchanges one step may take before the provider ends it. A turn is
# one message-and-response pair, and a tool call costs one: the model asks,
# the tool answers, the model speaks again. Tokens are bounded separately by
# the budget, so this only decides how many steps a worker gets inside that
# spend, not how much it may spend.
#
# A lead needs more than a specialist, found on David's run: its step is read
# the inbox, review what came back, then assign the next round, and it spent
# its eight on reading plus two teammate messages before it had assigned
# anything. A specialist's step is do one thing and submit it.
MAX_TURNS = 8
LEAD_MAX_TURNS = 20
MAX_TOOL_ROUNDS = 8
QUOTA_FRESHNESS_SECONDS = 900


@dataclass
class WorkerContext:
    """Everything an adapter may know. Deliberately no credential store, no
    session manager, and no app services: what is not here cannot leak."""
    endpoint: dict
    agent: dict
    system: dict
    tool_service: object
    scratch_dir: str
    model: str | None = None
    effort: str | None = None
    env: dict = field(default_factory=dict)

    @property
    def is_lead(self) -> bool:
        return bool(self.agent.get("is_lead"))


def capability_for(endpoint: dict) -> Capability:
    """What this connection can actually be asked to do.

    `api`/`local`: structured tool calling is the OpenAI standard and most
    current servers implement it, but a given server may not. It is marked
    supported here and verified on first use - the Swarm path fails the step
    explicitly rather than silently retrying without tools the way ordinary
    Chats do (core/providers/openai_compatible.py).

    `codex_cli`: the installed CLI takes structured tools only through MCP
    servers it launches itself. A Swarm tool server for Codex needs the
    authenticated per-attempt worker bridge from section 10 of the spec, which
    is its own change set. Until that exists this reports no structured tools,
    so admission blocks it with a specific reason instead of running a worker
    that can talk but cannot act.
    """
    kind = (endpoint or {}).get("kind") or "api"
    if kind == "claude_cli":
        return Capability(structured_tools=True, bounded_output=True, cancellable=True,
                          reports_usage=True, reports_quota=True)
    if kind == "codex_cli":
        return Capability(structured_tools=False, bounded_output=True, cancellable=True,
                          reports_usage=True, reports_quota=False)
    return Capability(structured_tools=True, bounded_output=True, cancellable=True,
                      reports_usage=True, reports_quota=False)


def build_worker(context: WorkerContext):
    kind = (context.endpoint or {}).get("kind") or "api"
    if kind == "claude_cli":
        from .claude_worker import ClaudeWorker
        return ClaudeWorker(context)
    if kind == "codex_cli":
        from .codex_worker import CodexWorker
        return CodexWorker(context)
    from .openai_worker import OpenAIWorker
    return OpenAIWorker(context)


def role_prompt(context: WorkerContext) -> str:
    """The worker's whole idea of who it is. Not JARVIS's landing zone.

    A Swarm worker is not the owner's assistant: it has no vault, no chat
    history and no repository. Saying so plainly is what stops it burning a
    step trying to read files it cannot reach.
    """
    agent, system = context.agent, context.system
    role = "lead" if context.is_lead else agent.get("role") or "specialist"
    lines = [
        f"You are {agent['name']}, the {role} of a working team called {system['name']}.",
        f"The team's mission: {system['mission']}",
        "",
        "How you work:",
        "- You have no shell, no file system, no repository and no vault access. "
        "Your only actions are the tools you were given. Do not claim to have run, read or written anything.",
        "- Call get_assigned_work first. It carries your objective, finished upstream work, your teammates' names, and unread messages.",
        "- Ask a teammate with send_message rather than assuming their interface or decision.",
        "- Think in the reply text if it helps, but every real action is a tool call.",
    ]
    if context.is_lead:
        lines += [
            "- You own the plan. Use assign_plan once you know what the work is, naming a teammate for each task. "
            "Depend a task on another only when it genuinely cannot start first.",
            "- Assign work to the teammates you actually have. Do not invent roles.",
            "- Spend this step deciding. End it with assign_plan, review_work or finish_mission - those are what "
            "move the company forward. Message a teammate only when your decision genuinely depends on their "
            "answer, because a step that runs out of exchanges ends without deciding anything.",
        ]
    else:
        lines += [
            "- Finish with submit_result when the objective is met, or report_blocker when it genuinely cannot be. One of the two ends your step.",
            "- submit_result must contain the real work, not a promise of it.",
        ]
    if agent.get("instructions"):
        lines += ["", "Your standing instructions from the owner:", agent["instructions"]]
    return "\n".join(lines)


def task_prompt(objective: str) -> str:
    return f"Your current objective:\n{objective}\n\nStart by calling get_assigned_work."
