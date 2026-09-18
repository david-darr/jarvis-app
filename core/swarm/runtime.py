"""Explicitly started coordinator; production worker dispatch is not registered.

Importing this module starts nothing. The host service owns its lifecycle.
Real adapters must satisfy the Worker contract before future integration.
"""
import asyncio
import json
import uuid
from dataclasses import dataclass, field

from .checkpoints import save_handoff
from .models import Assignment, Conflict, EventKind, PersistenceFault


@dataclass
class _Execution:
    system_id: str
    worker: object
    runner: asyncio.Task
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    pending: asyncio.Task | None = None
    started: bool = False
    iterator: object = None
    stop_confirmed: bool | None = None


class SwarmRuntime:
    def __init__(self, store, handoff_directory, *, poll_interval=0.25,
                 lease_seconds=30, stop_timeout=5):
        if not 0 < poll_interval < lease_seconds / 2 or stop_timeout <= 0:
            raise ValueError("Invalid runtime timing bounds")
        self.store = store
        self.handoff_directory = handoff_directory
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.stop_timeout = stop_timeout
        self.runtime_id = uuid.uuid4().hex
        self.generation = None
        self.fault = None
        self.active = {}
        self.handoffs = {}
        self._monitor_task = None
        self._closing = False
        self._detached = set()

    async def start(self):
        if self.generation is not None:
            raise Conflict("Runtime already started")
        self.generation = self.store.acquire_runtime(self.runtime_id, ttl=self.lease_seconds)
        try:
            self._observe()
        except Exception as exc:
            self._fail(exc)
            raise
        self._monitor_task = asyncio.create_task(self._monitor())
        return self

    def _fail(self, exc):
        self.fault = self.fault or exc
        for execution in self.active.values():
            execution.stop.set()

    def _observe(self):
        self.store.enforce_limits()
        for system_id in self.store.pausing_systems():
            executions = [item for item in self.active.values() if item.system_id == system_id]
            for execution in executions:
                execution.stop.set()
            if not executions:
                self.store.finalize_pause(system_id)
                self.handoffs[system_id] = save_handoff(self.store, system_id, self.handoff_directory)

    async def _monitor(self):
        try:
            while not self._closing:
                await asyncio.sleep(self.poll_interval)
                self.store.heartbeat(self.runtime_id, self.generation, ttl=self.lease_seconds)
                self._observe()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._fail(exc)

    def _detach(self, task):
        """Retain cancellation-resistant work and consume its eventual exception.

        Such a task is never evidence of a stopped native worker; its attempt
        stays unknown and prevents reuse until explicit reconciliation.
        """
        self._detached.add(task)

        def done(completed):
            self._detached.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)

    async def _stop(self, execution):
        if not execution.started:
            return True
        if execution.stop_confirmed is not None:
            return execution.stop_confirmed
        cancellation = asyncio.create_task(execution.worker.cancel())
        waiting = {cancellation}
        if execution.pending is not None:
            execution.pending.cancel()
            waiting.add(execution.pending)
        done, pending = await asyncio.wait(waiting, timeout=self.stop_timeout)
        for task in pending:
            task.cancel()
            self._detach(task)
        for task in done - {cancellation}:
            if not task.cancelled():
                task.exception()
        confirmed = False
        if cancellation in done and not cancellation.cancelled():
            try:
                confirmed = cancellation.result() is True and not pending
            except Exception:
                pass
        # Close the iterator BEFORE recording a confirmed stop or result. A
        # hanging finalizer must produce an unknown attempt, never success.
        if not pending and execution.iterator is not None and hasattr(execution.iterator, "aclose"):
            closing = asyncio.create_task(execution.iterator.aclose())
            closed, still_closing = await asyncio.wait({closing}, timeout=self.stop_timeout)
            if still_closing:
                closing.cancel()
                self._detach(closing)
                confirmed = False
            elif closing.cancelled() or closing.exception() is not None:
                confirmed = False
        execution.stop_confirmed = confirmed
        return confirmed

    async def run_task(self, task_id, worker, *, max_units):
        if self.fault:
            raise PersistenceFault(f"Runtime stopped: {self.fault}")
        if self.generation is None or self._closing:
            raise Conflict("Runtime is not accepting work")
        execution = None
        attempt_id = None
        iterator = None
        try:
            self._observe()
            attempt_id = self.store.reserve(task_id, self.runtime_id, self.generation,
                                            max_units, ttl=self.lease_seconds)
            if attempt_id is None:
                self._observe()
                return None
            task = self.store.get_task(task_id)
            execution = _Execution(task["system_id"], worker, asyncio.current_task())
            self.active[attempt_id] = execution
            # A queued pause can win between claim and actual launch.
            await asyncio.sleep(0)
            if execution.stop.is_set() or self._closing or self.fault:
                self.store.interrupt(attempt_id, self.runtime_id, self.generation,
                                     stopped=True, reason="launch_cancelled")
                return attempt_id
            if not self.store.begin(attempt_id, self.runtime_id, self.generation):
                return attempt_id
            execution.started = True
            pool_id = self.store.get_attempt(attempt_id)["pool_id"]
            assignment = Assignment(attempt_id, task["system_id"], task["run_id"],
                                    task["agent_id"], task_id, task["objective"],
                                    max_units, json.loads(task["checkpoint"]), pool_id)
            iterator = worker.events(assignment).__aiter__()
            execution.iterator = iterator
            while not execution.stop.is_set():
                execution.pending = asyncio.create_task(anext(iterator))
                while not execution.pending.done() and not execution.stop.is_set():
                    await asyncio.wait({execution.pending}, timeout=self.poll_interval)
                if execution.stop.is_set():
                    break
                event = execution.pending.result()
                execution.pending = None
                if event.kind == EventKind.USAGE:
                    self.store.record_usage(attempt_id, event.event_id, event.data["units"])
                elif event.kind == EventKind.QUOTA:
                    # An account reading belongs to the pool, never to this
                    # attempt's own usage. update_quota pauses every company on
                    # the account when the reading is unhealthy or stale.
                    self._record_quota(pool_id, event.data)
                elif event.kind == EventKind.RESULT:
                    stopped = await self._stop(execution)
                    if stopped:
                        self.store.finish(attempt_id, self.runtime_id, self.generation,
                                          event.data["result"],
                                          usage_complete=event.data.get("usage_complete") is True)
                    else:
                        self.store.interrupt(attempt_id, self.runtime_id, self.generation,
                                             stopped=False, reason="stop_unconfirmed")
                    return attempt_id
                else:
                    self.store.worker_event(attempt_id, self.runtime_id, self.generation,
                                            event.event_id, event.kind, event.data)
                self._observe()
            stopped = await self._stop(execution)
            self.store.interrupt(attempt_id, self.runtime_id, self.generation,
                                 stopped=stopped, reason="company_pause")
            return attempt_id
        except BaseException as exc:
            if isinstance(exc, PersistenceFault):
                self._fail(exc)
            if execution is not None:
                stopped = await self._stop(execution)
                try:
                    attempt = self.store.get_attempt(attempt_id)
                    if attempt["state"] in ("reserved", "started"):
                        # The message, not just the class: a bare
                        # "worker_error:RuntimeError" on an attempt cost a
                        # whole live run to diagnose.
                        detail = str(exc).strip() or type(exc).__name__
                        self.store.interrupt(attempt_id, self.runtime_id, self.generation,
                                             stopped=stopped,
                                             reason=f"worker_error:{type(exc).__name__}: {detail}"[:500])
                except (PersistenceFault, Conflict) as record_error:
                    self._fail(record_error)
            raise
        finally:
            self.active.pop(attempt_id, None)
            if not self.fault:
                try:
                    self._observe()
                except Exception as exc:
                    self._fail(exc)
                    raise

    async def pause(self, system_id, reason="manual"):
        self.store.pause(system_id, reason)
        self._observe()
        runners = [item.runner for item in self.active.values() if item.system_id == system_id]
        if runners:
            await asyncio.gather(*runners, return_exceptions=True)
        if self.fault:
            raise PersistenceFault(f"Pause could not be fully persisted: {self.fault}")
        self._observe()
        if system_id not in self.handoffs:
            self.handoffs[system_id] = save_handoff(self.store, system_id, self.handoff_directory)
        return self.handoffs[system_id]

    def _record_quota(self, pool_id, data):
        now = self.store.clock()
        freshness = data.get("freshness") or 900
        self.store.update_quota(
            pool_id, str(data.get("bucket") or "account"),
            used_percent=data.get("used_percent"),
            status=data.get("status") or "unknown",
            observed_at=now, valid_until=now + float(freshness),
            resets_at=data.get("resets_at"))

    async def run_ready(self, system_id, worker_factory, *, max_units):
        """Dispatch one ready batch; dependencies require accepted review first.

        The caller drives later batches. Initiative loops and PM decisions are
        milestone D, not an unbounded loop hidden in this foundation.
        """
        return await asyncio.gather(*(self.run_task(task["id"], worker_factory(task), max_units=max_units)
                                      for task in self.store.ready_tasks(system_id)))

    async def run_cycle(self, system_id, worker_factory, *, max_units, max_stages=8, advance=None):
        """One bounded cycle of real work, then stop.

        A stage is one batch of eligible tasks. When nothing is eligible, the
        caller's `advance` decides deterministically - in ordinary code, with
        no model call - whether there is more to set up, such as sending
        finished work to the lead for review. Returning False ends the cycle.

        This is deliberately finite. Repeating cycles under a standing mission
        is milestone D, and hiding an unbounded loop in this foundation would
        be exactly the token-burning behaviour the specification rules out.
        """
        stages = 0
        while stages < max_stages:
            if self.fault or self._closing:
                break
            if self.store.get_system(system_id)["state"] != "active":
                break
            claimed = []
            if self.store.ready_tasks(system_id):
                claimed = [attempt for attempt in
                           await self.run_ready(system_id, worker_factory, max_units=max_units) if attempt]
            if claimed:
                stages += 1
                continue
            # Nothing ran. Found on the first working live run: a task whose
            # dependency is still in review stays "ready" but cannot be
            # claimed, so a cycle that counted those as stages spun through
            # its whole budget without doing anything and never reached the
            # step that would have unblocked it.
            if advance is None or not advance(system_id):
                break
        return stages

    async def stop(self, system_id):
        """Cancel a run without discarding unknown effects or usage holds."""
        await self.pause(system_id, "manual_stop")
        self.store.finalize_stop(system_id)
        self.handoffs[system_id] = save_handoff(self.store, system_id, self.handoff_directory)
        return self.handoffs[system_id]

    async def close(self):
        self._closing = True
        for execution in self.active.values():
            execution.stop.set()
        if self.active:
            await asyncio.gather(*(item.runner for item in list(self.active.values())), return_exceptions=True)
        if self._monitor_task:
            self._monitor_task.cancel()
            await self._monitor_task
        if self.generation is not None and not self.fault:
            self.store.release_runtime(self.runtime_id, self.generation)
