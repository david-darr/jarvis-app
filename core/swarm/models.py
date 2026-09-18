"""Small, provider-independent execution contracts for Swarm."""
from dataclasses import dataclass, field
from enum import StrEnum
from typing import AsyncIterator, Protocol


class SwarmError(Exception):
    pass


class Conflict(SwarmError):
    pass


class NotFound(SwarmError):
    pass


class PersistenceFault(SwarmError):
    pass


class EventKind(StrEnum):
    TEXT = "visible_text"
    CHECKPOINT = "checkpoint"
    USAGE = "usage"
    ACTION_STARTED = "action_started"
    ACTION_FINISHED = "action_finished"
    RESULT = "result"
    # An account-level allowance reading, which belongs to the whole pool
    # rather than this attempt. The runtime forwards it to the store's quota
    # table; it is never recorded as this attempt's own usage.
    QUOTA = "quota"


@dataclass(frozen=True)
class Capability:
    """What an adapter can actually do, established by observation.

    A vendor name proves nothing. An adapter that cannot bound a step, cannot
    report whether it stopped, or cannot account for what it spent is not
    admitted to autonomous work, whoever makes it.
    """
    structured_tools: bool = False
    bounded_output: bool = False
    cancellable: bool = False
    reports_usage: bool = False
    reports_quota: bool = False

    def blocked_reason(self) -> str | None:
        if not self.structured_tools:
            return "This endpoint does not support structured tool calls, so it cannot take actions."
        if not self.bounded_output:
            return "This endpoint cannot bound a single step, so its usage cannot be admitted."
        if not self.cancellable:
            return "This endpoint cannot be asked to stop, so a company pause could not reach it."
        if not self.reports_usage:
            return "This endpoint does not report token usage, so spending cannot be tracked."
        return None


@dataclass(frozen=True)
class WorkerEvent:
    kind: EventKind
    event_id: str
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Assignment:
    attempt_id: str
    system_id: str
    run_id: str
    agent_id: str
    task_id: str
    objective: str
    max_units: int
    checkpoint: dict
    # The account this attempt draws on. Quota readings are recorded against
    # the pool, not the attempt, because the allowance is shared.
    pool_id: str = ""


class Worker(Protocol):
    """Yield action intent BEFORE executing it, and outcomes afterward.

    Events are acknowledged by advancing the iterator. cancel() returning True
    means all execution (including descendants) has actually stopped. A real
    adapter must enforce max_units or advertise that it cannot do bounded work.
    Milestone A exercises this contract with fake workers only.
    """

    def events(self, assignment: Assignment) -> AsyncIterator[WorkerEvent]: ...

    async def cancel(self) -> bool: ...
