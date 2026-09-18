"""Transactional Swarm persistence, with no dependency on live app stores.

The caller supplies a path and clock. SQLite transactions serialize admission
across connections; a process lock protects a connection shared by threads.
This is an internal engine API, not an HTTP authorization boundary.
"""
import json
import math
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .budget import BudgetLimit, units
from .models import Conflict, NotFound, PersistenceFault


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _id():
    return uuid.uuid4().hex


SCHEMA = """
CREATE TABLE systems (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, mission TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'idle', pause_epoch INTEGER NOT NULL DEFAULT 0,
 reason TEXT, revision INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE TABLE agents (
 id TEXT PRIMARY KEY, system_id TEXT NOT NULL REFERENCES systems(id),
 name TEXT NOT NULL, role TEXT NOT NULL, is_lead INTEGER NOT NULL DEFAULT 0,
 pool_id TEXT NOT NULL REFERENCES pools(id), UNIQUE(system_id,id)
);
CREATE UNIQUE INDEX one_lead ON agents(system_id) WHERE is_lead=1;
CREATE TABLE pools (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE runs (
 id TEXT PRIMARY KEY, system_id TEXT NOT NULL REFERENCES systems(id),
 objective TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'running', UNIQUE(system_id,id)
);
CREATE UNIQUE INDEX one_open_run ON runs(system_id) WHERE state IN ('running','paused');
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, system_id TEXT NOT NULL, run_id TEXT NOT NULL,
 agent_id TEXT NOT NULL, objective TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'ready',
 revision INTEGER NOT NULL DEFAULT 0, checkpoint TEXT NOT NULL DEFAULT '{}',
 result TEXT, created_at REAL NOT NULL,
 UNIQUE(system_id,id),
 FOREIGN KEY(system_id,run_id) REFERENCES runs(system_id,id),
 FOREIGN KEY(system_id,agent_id) REFERENCES agents(system_id,id)
);
CREATE TABLE dependencies (
 task_id TEXT NOT NULL REFERENCES tasks(id), depends_on TEXT NOT NULL REFERENCES tasks(id),
 PRIMARY KEY(task_id,depends_on), CHECK(task_id != depends_on)
);
CREATE TABLE attempts (
 id TEXT PRIMARY KEY, system_id TEXT NOT NULL, run_id TEXT NOT NULL,
 task_id TEXT NOT NULL, agent_id TEXT NOT NULL, pool_id TEXT NOT NULL REFERENCES pools(id),
 runtime_id TEXT NOT NULL, generation INTEGER NOT NULL, epoch INTEGER NOT NULL,
 task_revision INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'reserved',
 lease_until REAL NOT NULL, max_units INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0,
 held INTEGER NOT NULL, usage_complete INTEGER NOT NULL DEFAULT 0,
 stopped INTEGER NOT NULL DEFAULT 0, error TEXT,
 FOREIGN KEY(system_id,task_id) REFERENCES tasks(system_id,id),
 FOREIGN KEY(system_id,agent_id) REFERENCES agents(system_id,id),
 FOREIGN KEY(system_id,run_id) REFERENCES runs(system_id,id)
);
CREATE UNIQUE INDEX one_task_attempt ON attempts(task_id) WHERE state IN ('reserved','started','unknown');
CREATE UNIQUE INDEX one_agent_attempt ON attempts(agent_id) WHERE state IN ('reserved','started','unknown');
CREATE TABLE budgets (
 scope TEXT NOT NULL CHECK(scope IN ('system','agent','run','pool')),
 target TEXT NOT NULL, ceiling INTEGER NOT NULL, pause_percent INTEGER NOT NULL,
 checkpoint_reserve INTEGER NOT NULL, PRIMARY KEY(scope,target)
);
CREATE TABLE quotas (
 pool_id TEXT NOT NULL REFERENCES pools(id), bucket TEXT NOT NULL,
 used_percent REAL, status TEXT NOT NULL, observed_at REAL NOT NULL,
 valid_until REAL NOT NULL, resets_at REAL, PRIMARY KEY(pool_id,bucket)
);
CREATE TABLE usage (
 attempt_id TEXT NOT NULL REFERENCES attempts(id), event_id TEXT NOT NULL,
 amount INTEGER NOT NULL, PRIMARY KEY(attempt_id,event_id)
);
CREATE TABLE events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, system_id TEXT NOT NULL REFERENCES systems(id),
 kind TEXT NOT NULL, entity_id TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE messages (
 id TEXT PRIMARY KEY, system_id TEXT NOT NULL REFERENCES systems(id),
 sender_id TEXT, recipient_id TEXT NOT NULL, body TEXT NOT NULL,
 created_at REAL NOT NULL, read_at REAL,
 FOREIGN KEY(system_id,sender_id) REFERENCES agents(system_id,id),
 FOREIGN KEY(system_id,recipient_id) REFERENCES agents(system_id,id)
);
CREATE TABLE actions (
 attempt_id TEXT NOT NULL REFERENCES attempts(id), action_id TEXT NOT NULL,
 intent TEXT NOT NULL, result TEXT, PRIMARY KEY(attempt_id,action_id)
);
CREATE TABLE worker_events (
 attempt_id TEXT NOT NULL REFERENCES attempts(id), event_id TEXT NOT NULL,
 kind TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(attempt_id,event_id)
);
CREATE TABLE commands (
 scope TEXT NOT NULL, command_id TEXT NOT NULL, payload TEXT NOT NULL,
 result TEXT NOT NULL, PRIMARY KEY(scope,command_id)
);
CREATE TABLE runtime_lock (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime_id TEXT NOT NULL,
 generation INTEGER NOT NULL, lease_until REAL NOT NULL
);
CREATE TABLE checkpoints (
 id TEXT PRIMARY KEY, system_id TEXT NOT NULL REFERENCES systems(id),
 cursor INTEGER NOT NULL, snapshot TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX system_events ON events(system_id,id);
CREATE INDEX eligible_tasks ON tasks(system_id,state,created_at);
"""


class SwarmStore:
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.fault = None
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        try:
            # Check the version under the same write lock as schema creation:
            # two processes opening a new database must not both migrate it.
            self.db.execute("BEGIN IMMEDIATE")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        self.db.execute(statement)
                version = 1
            if version == 1:
                self.db.execute("ALTER TABLE pools ADD COLUMN owner TEXT")
                self.db.execute("ALTER TABLE systems ADD COLUMN configuration TEXT NOT NULL DEFAULT '{}'")
                self.db.execute("CREATE TABLE agent_settings (agent_id TEXT PRIMARY KEY REFERENCES agents(id), instructions TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1)")
                self.db.execute("""UPDATE pools SET owner=(SELECT MIN(s.owner) FROM agents a
                    JOIN systems s ON s.id=a.system_id WHERE a.pool_id=pools.id)
                    WHERE (SELECT COUNT(DISTINCT s.owner) FROM agents a JOIN systems s
                    ON s.id=a.system_id WHERE a.pool_id=pools.id)=1""")
                self.db.execute("PRAGMA user_version=2")
                version = 2
            if version == 2:
                # Real workers need two things version 2 has no room for: which
                # model connection an agent actually runs on, and which real
                # provider account a pool draws down. Existing rows keep NULL:
                # an unassigned agent simply cannot execute, and a pool with no
                # account identity is never silently treated as a fresh
                # allowance (see _pool_for_account).
                self.db.execute("ALTER TABLE agent_settings ADD COLUMN endpoint_id TEXT")
                self.db.execute("ALTER TABLE agent_settings ADD COLUMN model TEXT")
                self.db.execute("ALTER TABLE agent_settings ADD COLUMN effort TEXT")
                self.db.execute("ALTER TABLE pools ADD COLUMN account_key TEXT")
                self.db.execute("PRAGMA user_version=3")
            elif version != 3:
                raise PersistenceFault(f"Unsupported Swarm schema version: {version}")
            self.db.commit()
        except (sqlite3.Error, PersistenceFault) as exc:
            if self.db.in_transaction:
                self.db.rollback()
            self.db.close()
            raise PersistenceFault(f"Cannot initialize Swarm database: {exc}") from exc

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            if self.fault:
                raise PersistenceFault(self.fault)
            try:
                self.db.execute("BEGIN IMMEDIATE")
                yield self.db
                self.db.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                if self.db.in_transaction:
                    self.db.rollback()
                raise Conflict(str(exc)) from exc
            except sqlite3.Error as exc:
                if self.db.in_transaction:
                    self.db.rollback()
                self.fault = f"Swarm persistence failed: {exc}"
                raise PersistenceFault(self.fault) from exc
            except BaseException:
                if self.db.in_transaction:
                    self.db.rollback()
                raise

    @staticmethod
    def _one(db, table, key):
        # Table names are internal literals only, never caller/model input.
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise NotFound(f"{table}: {key}")
        return dict(row)

    def get_system(self, system_id, *, owner=None):
        with self._lock:
            system = self._one(self.db, "systems", system_id)
            if owner is not None and system["owner"] != owner:
                raise NotFound(system_id)
            return system

    def get_task(self, task_id):
        with self._lock:
            return self._one(self.db, "tasks", task_id)

    def get_attempt(self, attempt_id):
        with self._lock:
            return self._one(self.db, "attempts", attempt_id)

    def _event(self, db, system_id, kind, entity_id, data):
        db.execute("INSERT INTO events(system_id,kind,entity_id,data,created_at) VALUES(?,?,?,?,?)",
                   (system_id, kind, entity_id, _json(data), self.clock()))

    @staticmethod
    def _limit(db, scope, target, limit):
        db.execute("INSERT INTO budgets VALUES(?,?,?,?,?)",
                   (scope, target, limit.ceiling, limit.pause_percent, limit.checkpoint_reserve))

    def create_pool(self, name, limit: BudgetLimit):
        pool_id = _id()
        with self.transaction() as db:
            db.execute("INSERT INTO pools(id,name) VALUES(?,?)", (pool_id, name))
            self._limit(db, "pool", pool_id, limit)
        return pool_id

    def create_system(self, owner, name, mission, *, pool_id, system_limit: BudgetLimit,
                      lead_limit: BudgetLimit):
        if not all(isinstance(x, str) and x.strip() for x in (owner, name, mission)):
            raise ValueError("owner, name and mission are required")
        system_id, lead_id = _id(), _id()
        with self.transaction() as db:
            self._one(db, "pools", pool_id)
            db.execute("INSERT INTO systems(id,owner,name,mission,created_at) VALUES(?,?,?,?,?)",
                       (system_id, owner, name, mission, self.clock()))
            db.execute("INSERT INTO agents VALUES(?,?,?,?,?,?)",
                       (lead_id, system_id, "Lead", "lead", 1, pool_id))
            self._limit(db, "system", system_id, system_limit)
            self._limit(db, "agent", lead_id, lead_limit)
            self._event(db, system_id, "system.created", system_id, {"lead_id": lead_id})
        return system_id, lead_id

    def add_agent(self, system_id, name, role, pool_id, limit: BudgetLimit):
        agent_id = _id()
        with self.transaction() as db:
            self._one(db, "systems", system_id)
            db.execute("INSERT INTO agents VALUES(?,?,?,?,?,?)",
                       (agent_id, system_id, name, role, 0, pool_id))
            self._limit(db, "agent", agent_id, limit)
            self._event(db, system_id, "agent.created", agent_id, {"role": role})
        return agent_id

    def create_run(self, system_id, objective, limit: BudgetLimit):
        run_id = _id()
        with self.transaction() as db:
            system = self._one(db, "systems", system_id)
            if system["state"] != "idle":
                raise Conflict("A run can start only in an idle system")
            db.execute("INSERT INTO runs(id,system_id,objective) VALUES(?,?,?)", (run_id, system_id, objective))
            self._limit(db, "run", run_id, limit)
            db.execute("UPDATE systems SET state='active',revision=revision+1 WHERE id=?", (system_id,))
            self._event(db, system_id, "run.started", run_id, {"objective": objective})
        return run_id

    @staticmethod
    def _replay(db, scope, command_id, payload):
        if not isinstance(command_id, str) or not command_id:
            raise ValueError("command_id is required")
        row = db.execute("SELECT * FROM commands WHERE scope=? AND command_id=?", (scope, command_id)).fetchone()
        if row:
            if row["payload"] != _json(payload):
                raise Conflict("Command ID reused with different input")
            return json.loads(row["result"])
        return None

    @staticmethod
    def _remember(db, scope, command_id, payload, result):
        db.execute("INSERT INTO commands VALUES(?,?,?,?)", (scope, command_id, _json(payload), _json(result)))

    def create_task(self, system_id, run_id, agent_id, objective, *, command_id, dependencies=()):
        dependencies = sorted(set(dependencies))
        payload = ["create_task", run_id, agent_id, objective, dependencies]
        with self.transaction() as db:
            replay = self._replay(db, system_id, command_id, payload)
            if replay is not None:
                return replay
            run = self._one(db, "runs", run_id)
            agent = self._one(db, "agents", agent_id)
            if run["system_id"] != system_id or agent["system_id"] != system_id:
                raise Conflict("Task references another system")
            if run["state"] not in ("running", "paused"):
                raise Conflict("Run is closed")
            task_id = _id()
            db.execute("INSERT INTO tasks(id,system_id,run_id,agent_id,objective,created_at) VALUES(?,?,?,?,?,?)",
                       (task_id, system_id, run_id, agent_id, objective, self.clock()))
            for dependency in dependencies:
                self._dependency(db, task_id, dependency)
            self._event(db, system_id, "task.created", task_id, {"objective": objective})
            self._remember(db, system_id, command_id, payload, task_id)
        return task_id

    def _dependency(self, db, task_id, dependency):
        task, parent = self._one(db, "tasks", task_id), self._one(db, "tasks", dependency)
        if task["run_id"] != parent["run_id"] or task["system_id"] != parent["system_id"]:
            raise Conflict("Dependencies must belong to the same run")
        if task["state"] != "ready":
            raise Conflict("Cannot change dependencies after dispatch")
        cycle = db.execute("""WITH RECURSIVE chain(id) AS (
            SELECT ? UNION SELECT d.depends_on FROM dependencies d JOIN chain c ON d.task_id=c.id
        ) SELECT 1 FROM chain WHERE id=?""", (dependency, task_id)).fetchone()
        if cycle:
            raise Conflict("Dependency cycle")
        db.execute("INSERT OR IGNORE INTO dependencies VALUES(?,?)", (task_id, dependency))

    def add_dependency(self, task_id, dependency):
        with self.transaction() as db:
            self._dependency(db, task_id, dependency)
            task = self._one(db, "tasks", task_id)
            self._event(db, task["system_id"], "task.dependency", task_id, {"depends_on": dependency})

    def send_message(self, system_id, sender_id, recipient_id, body, *, command_id):
        payload = ["message", sender_id, recipient_id, body]
        with self.transaction() as db:
            replay = self._replay(db, system_id, command_id, payload)
            if replay is not None:
                return replay
            for agent_id in (sender_id, recipient_id):
                if agent_id is not None and self._one(db, "agents", agent_id)["system_id"] != system_id:
                    raise Conflict("Message crosses system boundary")
            message_id = _id()
            db.execute("INSERT INTO messages(id,system_id,sender_id,recipient_id,body,created_at) VALUES(?,?,?,?,?,?)",
                       (message_id, system_id, sender_id, recipient_id, body, self.clock()))
            self._event(db, system_id, "message.queued", message_id, {"recipient_id": recipient_id})
            self._remember(db, system_id, command_id, payload, message_id)
        return message_id

    def acquire_runtime(self, runtime_id, *, ttl=30):
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM runtime_lock WHERE singleton=1").fetchone()
            if row and row["lease_until"] > self.clock():
                raise Conflict("A Swarm runtime already owns this database")
            generation = row["generation"] + 1 if row else 1
            db.execute("INSERT OR REPLACE INTO runtime_lock VALUES(1,?,?,?)",
                       (runtime_id, generation, self.clock() + ttl))
            # Reserved work never started; started work may have side effects.
            for attempt in db.execute("SELECT * FROM attempts WHERE state IN ('reserved','started')").fetchall():
                if attempt["state"] == "reserved":
                    db.execute("UPDATE attempts SET state='cancelled',held=0,stopped=1 WHERE id=?", (attempt["id"],))
                    db.execute("UPDATE tasks SET state='ready',revision=revision+1 WHERE id=?", (attempt["task_id"],))
                else:
                    db.execute("UPDATE attempts SET state='unknown',error='Runtime interrupted' WHERE id=?", (attempt["id"],))
                    db.execute("UPDATE tasks SET state='blocked',revision=revision+1 WHERE id=?", (attempt["task_id"],))
                self._pause(db, attempt["system_id"], "restart_recovery")
                self._event(db, attempt["system_id"], "attempt.recovered", attempt["id"], {"previous": attempt["state"]})
        return generation

    def _runtime(self, db, runtime_id, generation):
        row = db.execute("SELECT * FROM runtime_lock WHERE singleton=1").fetchone()
        if not row or row["runtime_id"] != runtime_id or row["generation"] != generation or row["lease_until"] <= self.clock():
            raise Conflict("Runtime lease is stale")

    def heartbeat(self, runtime_id, generation, *, ttl=30):
        with self.transaction() as db:
            self._runtime(db, runtime_id, generation)
            db.execute("UPDATE runtime_lock SET lease_until=? WHERE singleton=1", (self.clock() + ttl,))
            db.execute("UPDATE attempts SET lease_until=? WHERE runtime_id=? AND generation=? AND state IN ('reserved','started')",
                       (self.clock() + ttl, runtime_id, generation))

    def release_runtime(self, runtime_id, generation):
        with self.transaction() as db:
            self._runtime(db, runtime_id, generation)
            db.execute("UPDATE runtime_lock SET lease_until=0 WHERE singleton=1")

    @staticmethod
    def _budget_rows(db, attempt):
        return [(scope, attempt[column]) for scope, column in (
            ("system", "system_id"), ("agent", "agent_id"), ("run", "run_id"), ("pool", "pool_id"))]

    @staticmethod
    def _accounting(db, scope, target):
        column = {"system": "system_id", "agent": "agent_id", "run": "run_id", "pool": "pool_id"}[scope]
        row = db.execute(f"SELECT COALESCE(SUM(used),0),COALESCE(SUM(held),0) FROM attempts WHERE {column}=?", (target,)).fetchone()
        return row[0], row[1]

    @staticmethod
    def _quota_threshold(db, pool_id):
        row = db.execute("SELECT pause_percent FROM budgets WHERE scope='pool' AND target=?", (pool_id,)).fetchone()
        return row[0] if row else 80

    def _quota_blocks(self, db, row, *, threshold=None):
        """Whether one account reading should stop work.

        Found on the first live run, 2026-09-16: the real Claude CLI reports
        `allowed_warning` on the seven-day window at 50% utilization. Treating
        any non-allowed status as a halt stopped a company before it did
        anything, and ignored the threshold the owner had configured. So the
        number governs, and the flag only decides what to do when there is no
        number: a rejection always stops, a warning with no utilization is
        treated as no known headroom, and a warning with a figure below the
        owner's threshold is recorded and worked through.
        """
        if row["status"] == "rejected":
            return True
        if row["used_percent"] is None:
            return row["status"] != "allowed"
        if threshold is None:
            threshold = self._quota_threshold(db, row["pool_id"])
        return row["used_percent"] >= threshold

    def _budget_reason(self, db, attempt, requested=0):
        for scope, target in self._budget_rows(db, attempt):
            row = db.execute("SELECT * FROM budgets WHERE scope=? AND target=?", (scope, target)).fetchone()
            if row is None:
                return f"missing_budget:{scope}"
            limit = BudgetLimit(row["ceiling"], row["pause_percent"], row["checkpoint_reserve"])
            used, held = self._accounting(db, scope, target)
            if limit.exhausted(used) or not limit.admits(used, held, requested):
                return f"budget:{scope}:{target}"
        for row in db.execute("SELECT * FROM quotas WHERE pool_id=?", (attempt["pool_id"],)):
            if row["valid_until"] <= self.clock():
                return "quota_stale:" + row["bucket"]
            if self._quota_blocks(db, row):
                return "quota:" + row["bucket"]
        return None

    def _company_reason(self, db, system_id, run_id):
        for agent in db.execute("""SELECT a.* FROM agents a LEFT JOIN agent_settings s ON s.agent_id=a.id
                WHERE a.system_id=? AND COALESCE(s.enabled,1)=1""", (system_id,)).fetchall():
            reason = self._budget_reason(db, {"system_id": system_id, "run_id": run_id,
                                              "agent_id": agent["id"], "pool_id": agent["pool_id"]})
            if reason:
                return reason
        return None

    def reserve(self, task_id, runtime_id, generation, max_units, *, ttl=30):
        units(max_units, "max_units", positive=True)
        with self.transaction() as db:
            self._runtime(db, runtime_id, generation)
            task = self._one(db, "tasks", task_id)
            system = self._one(db, "systems", task["system_id"])
            if system["state"] != "active" or task["state"] != "ready":
                return None
            if db.execute("SELECT 1 FROM dependencies d JOIN tasks t ON t.id=d.depends_on WHERE d.task_id=? AND t.state!='done'", (task_id,)).fetchone():
                return None
            if db.execute("SELECT 1 FROM attempts WHERE agent_id=? AND state IN ('reserved','started','unknown')", (task["agent_id"],)).fetchone():
                return None
            agent = self._one(db, "agents", task["agent_id"])
            disabled = db.execute("SELECT 1 FROM agent_settings WHERE agent_id=? AND enabled=0", (agent["id"],)).fetchone()
            if disabled:
                return None
            attempt = {**task, "pool_id": agent["pool_id"]}
            reason = (self._company_reason(db, task["system_id"], task["run_id"])
                      or self._budget_reason(db, attempt, max_units))
            if reason:
                self._pause(db, task["system_id"], reason)
                return None
            attempt_id = _id()
            revision = task["revision"] + 1
            db.execute("""INSERT INTO attempts(id,system_id,run_id,task_id,agent_id,pool_id,
                runtime_id,generation,epoch,task_revision,lease_until,max_units,held)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (attempt_id, task["system_id"], task["run_id"], task_id, task["agent_id"],
                        agent["pool_id"], runtime_id, generation, system["pause_epoch"], revision,
                        self.clock() + ttl, max_units, max_units))
            db.execute("UPDATE tasks SET state='running',revision=? WHERE id=?", (revision, task_id))
            self._event(db, task["system_id"], "attempt.reserved", attempt_id, {"max_units": max_units})
            return attempt_id

    def _live(self, db, attempt_id, runtime_id, generation):
        self._runtime(db, runtime_id, generation)
        attempt = self._one(db, "attempts", attempt_id)
        task = self._one(db, "tasks", attempt["task_id"])
        if (attempt["runtime_id"] != runtime_id or attempt["generation"] != generation
                or attempt["state"] not in ("reserved", "started")
                or attempt["lease_until"] <= self.clock() or task["revision"] != attempt["task_revision"]):
            raise Conflict("Attempt is stale")
        return attempt

    def begin(self, attempt_id, runtime_id, generation):
        with self.transaction() as db:
            attempt = self._live(db, attempt_id, runtime_id, generation)
            system = self._one(db, "systems", attempt["system_id"])
            if attempt["state"] != "reserved":
                raise Conflict("Attempt has already started")
            reason = self._company_reason(db, attempt["system_id"], attempt["run_id"])
            if system["state"] != "active" or system["pause_epoch"] != attempt["epoch"] or reason:
                if reason:
                    self._pause(db, system["id"], reason)
                db.execute("UPDATE attempts SET state='cancelled',held=0,stopped=1 WHERE id=?", (attempt_id,))
                db.execute("UPDATE tasks SET state='ready',revision=revision+1 WHERE id=?", (attempt["task_id"],))
                return False
            db.execute("UPDATE attempts SET state='started' WHERE id=?", (attempt_id,))
            self._event(db, system["id"], "attempt.started", attempt_id, {})
            return True

    def _pause(self, db, system_id, reason):
        system = self._one(db, "systems", system_id)
        if system["state"] in ("archived", "stopped"):
            return
        reasons = json.loads(system["reason"]) if system["reason"] else []
        if reason in reasons:
            return
        reasons.append(reason)
        db.execute("UPDATE systems SET state='pausing',reason=?,pause_epoch=pause_epoch+1,revision=revision+1 WHERE id=?",
                   (_json(reasons), system_id))
        db.execute("UPDATE runs SET state='paused' WHERE system_id=? AND state='running'", (system_id,))
        self._event(db, system_id, "system.pausing", system_id, {"reason": reason})

    def pause(self, system_id, reason="manual"):
        with self.transaction() as db:
            self._pause(db, system_id, reason)

    def record_usage(self, attempt_id, event_id, amount):
        units(amount)
        if not event_id:
            raise ValueError("usage event_id is required")
        with self.transaction() as db:
            self._record_usage(db, attempt_id, event_id, amount)

    def _record_usage(self, db, attempt_id, event_id, amount):
        self._one(db, "attempts", attempt_id)
        previous = db.execute("SELECT amount FROM usage WHERE attempt_id=? AND event_id=?", (attempt_id, event_id)).fetchone()
        if previous:
            if previous[0] != amount:
                raise Conflict("Usage event changed on replay")
            return
        db.execute("INSERT INTO usage VALUES(?,?,?)", (attempt_id, event_id, amount))
        db.execute("UPDATE attempts SET used=used+?,held=MAX(0,held-?) WHERE id=?", (amount, amount, attempt_id))
        updated = self._one(db, "attempts", attempt_id)
        reason = self._budget_reason(db, updated)
        if updated["used"] > updated["max_units"]:
            reason = "worker_exceeded_reservation"
        if reason:
            self._pause(db, updated["system_id"], reason)
        # A local agent limit must not mask a simultaneous pool limit.
        pool_limit = db.execute("SELECT * FROM budgets WHERE scope='pool' AND target=?", (updated["pool_id"],)).fetchone()
        limit = BudgetLimit(pool_limit["ceiling"], pool_limit["pause_percent"], pool_limit["checkpoint_reserve"])
        used, held = self._accounting(db, "pool", updated["pool_id"])
        if limit.exhausted(used) or not limit.admits(used, held, 0):
            for row in db.execute("SELECT DISTINCT system_id FROM agents WHERE pool_id=?", (updated["pool_id"],)).fetchall():
                self._pause(db, row[0], "budget:pool:" + updated["pool_id"])
        self._event(db, updated["system_id"], "usage.recorded", attempt_id, {"event_id": event_id, "units": amount})

    def settle_usage(self, attempt_id, *, total_units, evidence):
        """Release uncertain usage only with a final, authoritative total.

        This is a host reconciliation operation, not an agent permission.
        All final per-request charges must already have been recorded using
        their original IDs. Never invent an anonymous delta that could later
        be double-counted when the real request event arrives.
        """
        units(total_units)
        if not evidence:
            raise ValueError("Final usage evidence is required")
        with self.transaction() as db:
            attempt = self._one(db, "attempts", attempt_id)
            if not attempt["stopped"] or attempt["state"] in ("reserved", "started"):
                raise Conflict("Worker must be confirmed stopped before usage settlement")
            if total_units != attempt["used"]:
                raise Conflict("Final total must match the recorded request charges")
            if total_units == attempt["used"] and attempt["usage_complete"]:
                return
            db.execute("UPDATE attempts SET held=0,usage_complete=1 WHERE id=?", (attempt_id,))
            self._event(db, attempt["system_id"], "usage.settled", attempt_id,
                        {"total_units": total_units, "evidence": evidence})

    def update_quota(self, pool_id, bucket, *, used_percent, status, observed_at, valid_until, resets_at=None):
        if status not in ("allowed", "warning", "rejected", "unknown"):
            raise ValueError("Invalid quota status")
        if used_percent is not None and (isinstance(used_percent, bool) or not math.isfinite(used_percent) or not 0 <= used_percent <= 100):
            raise ValueError("Invalid quota percentage")
        if not all(math.isfinite(x) for x in (observed_at, valid_until)) or observed_at > self.clock() or valid_until < observed_at:
            raise ValueError("Invalid quota observation times")
        if resets_at is not None and not math.isfinite(resets_at):
            raise ValueError("Invalid quota reset time")
        with self.transaction() as db:
            self._one(db, "pools", pool_id)
            old = db.execute("SELECT observed_at FROM quotas WHERE pool_id=? AND bucket=?", (pool_id, bucket)).fetchone()
            if old and old[0] > observed_at:
                return
            db.execute("INSERT OR REPLACE INTO quotas VALUES(?,?,?,?,?,?,?)",
                       (pool_id, bucket, used_percent, status, observed_at, valid_until, resets_at))
            reading = {"pool_id": pool_id, "bucket": bucket, "status": status, "used_percent": used_percent}
            if self._quota_blocks(db, reading) or valid_until <= self.clock():
                for row in db.execute("SELECT DISTINCT system_id FROM agents WHERE pool_id=?", (pool_id,)).fetchall():
                    self._pause(db, row[0], "quota:" + bucket)

    def worker_event(self, attempt_id, runtime_id, generation, event_id, kind, data):
        if not event_id:
            raise ValueError("event_id is required")
        encoded = _json(data)
        with self.transaction() as db:
            attempt = self._live(db, attempt_id, runtime_id, generation)
            if attempt["state"] != "started":
                raise Conflict("Attempt has not started")
            old = db.execute("SELECT kind,data FROM worker_events WHERE attempt_id=? AND event_id=?", (attempt_id, event_id)).fetchone()
            if old:
                if old["kind"] != kind or old["data"] != encoded:
                    raise Conflict("Worker event changed on replay")
                return
            if kind not in ("visible_text", "checkpoint", "action_started", "action_finished"):
                raise ValueError("Unknown worker event")
            if kind == "checkpoint":
                db.execute("UPDATE tasks SET checkpoint=? WHERE id=?", (encoded, attempt["task_id"]))
            elif kind == "action_started":
                system = self._one(db, "systems", attempt["system_id"])
                if system["state"] != "active" or system["pause_epoch"] != attempt["epoch"]:
                    raise Conflict("System paused before action admission")
                db.execute("INSERT INTO actions VALUES(?,?,?,NULL)", (attempt_id, data["action_id"], _json(data["intent"])))
            elif kind == "action_finished":
                row = db.execute("SELECT * FROM actions WHERE attempt_id=? AND action_id=?", (attempt_id, data["action_id"])).fetchone()
                if not row or row["result"] is not None:
                    raise Conflict("Action is missing or already resolved")
                db.execute("UPDATE actions SET result=? WHERE attempt_id=? AND action_id=?",
                           (_json(data["result"]), attempt_id, data["action_id"]))
            db.execute("INSERT INTO worker_events VALUES(?,?,?,?)", (attempt_id, event_id, kind, encoded))
            self._event(db, attempt["system_id"], kind, attempt_id, data)

    def finish(self, attempt_id, runtime_id, generation, result, *, usage_complete=False):
        with self.transaction() as db:
            attempt = self._live(db, attempt_id, runtime_id, generation)
            if attempt["state"] != "started":
                raise Conflict("Attempt has not started")
            if db.execute("SELECT 1 FROM actions WHERE attempt_id=? AND result IS NULL", (attempt_id,)).fetchone():
                raise Conflict("Unresolved side effect prevents completion")
            db.execute("UPDATE attempts SET state='succeeded',stopped=1,usage_complete=?,held=CASE WHEN ? THEN 0 ELSE held END WHERE id=?",
                       (int(usage_complete), int(usage_complete), attempt_id))
            db.execute("UPDATE tasks SET state='review',result=?,revision=revision+1 WHERE id=?", (_json(result), attempt["task_id"]))
            self._event(db, attempt["system_id"], "task.review", attempt["task_id"], result)

    def accept_result(self, task_id, *, expected_revision, evidence):
        if not evidence:
            raise ValueError("Review evidence is required")
        with self.transaction() as db:
            task = self._one(db, "tasks", task_id)
            if task["state"] != "review" or task["revision"] != expected_revision:
                raise Conflict("Task is not at the expected review revision")
            db.execute("UPDATE tasks SET state='done',revision=revision+1 WHERE id=?", (task_id,))
            self._event(db, task["system_id"], "task.accepted", task_id, {"evidence": evidence})
            pending = db.execute("SELECT 1 FROM tasks WHERE run_id=? AND state!='done'", (task["run_id"],)).fetchone()
            if not pending:
                db.execute("UPDATE runs SET state='completed' WHERE id=?", (task["run_id"],))
                db.execute("UPDATE systems SET state='idle',revision=revision+1 WHERE id=? AND state='active'", (task["system_id"],))

    def review_task(self, task_id, reviewer_id, *, accept, note):
        """The lead's verdict on submitted work.

        Acceptance is the only route from review to done, and it always
        records who decided and on what evidence. A rejection returns the task
        to its owner with the reason attached rather than silently discarding
        the attempt's history.
        """
        note = (note or "").strip()
        if not note:
            raise ValueError("A review needs evidence or a reason")
        with self.transaction() as db:
            task = self._one(db, "tasks", task_id)
            reviewer = self._one(db, "agents", reviewer_id)
            if reviewer["system_id"] != task["system_id"] or not reviewer["is_lead"]:
                raise Conflict("Only the lead reviews submitted work")
            if task["state"] != "review":
                raise Conflict("That task is not waiting for review")
            if accept:
                db.execute("UPDATE tasks SET state='done',revision=revision+1 WHERE id=?", (task_id,))
                self._event(db, task["system_id"], "task.accepted", task_id,
                            {"evidence": note[:4000], "reviewer_id": reviewer_id})
                pending = db.execute("SELECT 1 FROM tasks WHERE run_id=? AND state NOT IN ('done','cancelled')",
                                     (task["run_id"],)).fetchone()
                if not pending:
                    db.execute("UPDATE runs SET state='completed' WHERE id=?", (task["run_id"],))
                    db.execute("UPDATE systems SET state='idle',revision=revision+1 WHERE id=? AND state='active'",
                               (task["system_id"],))
                    self._event(db, task["system_id"], "run.completed", task["run_id"], {})
            else:
                db.execute("UPDATE tasks SET state='ready',revision=revision+1 WHERE id=?", (task_id,))
                message_id = _id()
                db.execute("INSERT INTO messages(id,system_id,sender_id,recipient_id,body,created_at) VALUES(?,?,?,?,?,?)",
                           (message_id, task["system_id"], reviewer_id, task["agent_id"],
                            f"Revision requested on '{task['objective']}': {note[:4000]}", self.clock()))
                self._event(db, task["system_id"], "task.revision_requested", task_id,
                            {"reviewer_id": reviewer_id, "note": note[:4000]})
            return {"task_id": task_id, "state": "done" if accept else "ready"}

    def conclude_mission(self, system_id, *, reason, summary=None):
        """Record why a company stopped opening cycles, once.

        Continuing operation needs an end that is written down rather than
        inferred from silence. The reason is the company's own: the lead said
        the mission was met, a ceiling was reached, or a cycle produced nothing.
        """
        with self.transaction() as db:
            system = self._one(db, "systems", system_id)
            if db.execute("SELECT 1 FROM events WHERE system_id=? AND kind='mission.concluded'", (system_id,)).fetchone():
                return False
            self._event(db, system_id, "mission.concluded", system_id,
                        {"reason": reason, "summary": (summary or "")[:4000]})
            return True

    def mission_concluded(self, system_id):
        """The current ending, if the company has one.

        A conclusion is not necessarily final: answering what a team was
        waiting for reopens the mission, so a reopening after the last
        conclusion means there is no ending to report.
        """
        with self._lock:
            row = self.db.execute(
                """SELECT kind,data FROM events WHERE system_id=?
                   AND kind IN ('mission.concluded','mission.reopened') ORDER BY id DESC LIMIT 1""",
                (system_id,)).fetchone()
            return json.loads(row[1]) if row and row[0] == "mission.concluded" else None

    def run_count(self, system_id):
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM runs WHERE system_id=?", (system_id,)).fetchone()[0]

    def eligible_tasks(self, system_id):
        """Ready work that could actually be claimed right now.

        "Ready" is not the same as claimable: a task whose dependency will
        never finish stays ready forever. Asking the nominal state is what let
        a wedged company look busy instead of stuck, so this applies the same
        dependency rule `reserve` does.
        """
        with self._lock:
            return [dict(row) for row in self.db.execute(
                """SELECT t.* FROM tasks t WHERE t.system_id=? AND t.state='ready'
                   AND NOT EXISTS (SELECT 1 FROM dependencies d JOIN tasks p ON p.id=d.depends_on
                                   WHERE d.task_id=t.id AND p.state!='done')
                   ORDER BY t.created_at,t.id""", (system_id,))]

    def blocked_for_owner(self, system_id):
        """Work that stopped because someone needs something from the owner."""
        with self._lock:
            return [dict(row) for row in self.db.execute(
                """SELECT * FROM tasks WHERE system_id=? AND state='review' AND result IS NOT NULL
                   AND json_valid(result) AND json_extract(result,'$.status')='blocked'
                   ORDER BY created_at""", (system_id,))]

    def answer_blockers(self, system_id, note):
        """The owner answered, so held work becomes claimable again.

        The answer itself already reached the team as an ordinary message;
        this only returns the tasks that stopped for it, so the next attempt
        starts with the answer sitting in its inbox.
        """
        with self.transaction() as db:
            rows = db.execute(
                """SELECT * FROM tasks WHERE system_id=? AND state='review' AND result IS NOT NULL
                   AND json_valid(result) AND json_extract(result,'$.status')='blocked'""",
                (system_id,)).fetchall()
            for row in rows:
                db.execute("UPDATE tasks SET state='ready',revision=revision+1 WHERE id=?", (row["id"],))
                self._event(db, system_id, "task.unblocked", row["id"], {"note": (note or "")[:2000]})
            if rows:
                self._event(db, system_id, "mission.reopened", system_id, {"note": (note or "")[:2000]})
            return len(rows)

    def reopen_for_start(self, system_id):
        """A stopped company can be started again.

        Found while diagnosing a wedged company: stop leaves the system
        stopped, opening a run requires idle, so Start returned a conflict and
        the usual escape hatch did not work.
        """
        with self.transaction() as db:
            system = self._one(db, "systems", system_id)
            if system["state"] == "stopped":
                db.execute("UPDATE systems SET state='idle',reason=NULL,revision=revision+1 WHERE id=?", (system_id,))
                self._event(db, system_id, "system.reopened", system_id, {})

    def tasks_in_review(self, system_id):
        with self._lock:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM tasks WHERE system_id=? AND state='review' ORDER BY created_at,id", (system_id,))]

    def interrupt(self, attempt_id, runtime_id, generation, *, stopped, reason):
        with self.transaction() as db:
            attempt = self._live(db, attempt_id, runtime_id, generation)
            pending = db.execute("SELECT 1 FROM actions WHERE attempt_id=? AND result IS NULL", (attempt_id,)).fetchone()
            unknown = not stopped or bool(pending)
            state = "unknown" if unknown else "cancelled"
            db.execute("UPDATE attempts SET state=?,stopped=?,error=?,held=CASE WHEN state='reserved' THEN 0 ELSE held END WHERE id=?",
                       (state, int(stopped), reason, attempt_id))
            db.execute("UPDATE tasks SET state=?,revision=revision+1 WHERE id=?",
                       ("blocked" if unknown else "ready", attempt["task_id"]))
            self._pause(db, attempt["system_id"], reason)
            self._event(db, attempt["system_id"], "attempt.interrupted", attempt_id, {"stopped": stopped, "unknown": unknown})

    def reconcile(self, attempt_id, *, evidence, stopped, retry_checkpoint, action_results=None):
        """Explicit, evidence-backed reconciliation; never automatically retries."""
        if not stopped or not evidence or not isinstance(retry_checkpoint, dict) or not retry_checkpoint:
            raise Conflict("Confirmed stop, evidence and an explicit safe continuation checkpoint are required")
        with self.transaction() as db:
            attempt = self._one(db, "attempts", attempt_id)
            if attempt["state"] != "unknown":
                raise Conflict("Attempt does not need reconciliation")
            for action_id, result in (action_results or {}).items():
                db.execute("UPDATE actions SET result=? WHERE attempt_id=? AND action_id=? AND result IS NULL",
                           (_json(result), attempt_id, action_id))
            if db.execute("SELECT 1 FROM actions WHERE attempt_id=? AND result IS NULL", (attempt_id,)).fetchone():
                raise Conflict("Side effects are still unresolved")
            # Reconciliation is where the uncertainty ends, so the conservative
            # hold ends with it. Keeping it after a confirmed stop would leak
            # an agent's allocation one interrupted attempt at a time until it
            # could no longer be admitted to any work. Recorded usage stays;
            # only the reservation against unknown usage is released.
            db.execute("UPDATE attempts SET state='cancelled',stopped=1,held=0,usage_complete=1 WHERE id=?", (attempt_id,))
            db.execute("UPDATE tasks SET state='ready',checkpoint=?,revision=revision+1 WHERE id=?",
                       (_json(retry_checkpoint), attempt["task_id"]))
            self._event(db, attempt["system_id"], "attempt.reconciled", attempt_id,
                        {"evidence": evidence, "released": attempt["held"], "recorded": attempt["used"]})

    def finalize_pause(self, system_id):
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM attempts WHERE system_id=? AND state IN ('reserved','started')", (system_id,)).fetchone():
                raise Conflict("Workers have not reached a recorded stop state")
            changed = db.execute("UPDATE systems SET state='paused',revision=revision+1 WHERE id=? AND state='pausing'", (system_id,)).rowcount
            if changed:
                self._event(db, system_id, "system.paused", system_id, {})

    def enforce_limits(self):
        """Check every member, including idle specialists, before further work.

        Local budgets are always required. Quota rows, when configured, must
        remain fresh. No quota rows means local-budget-only fake-worker mode;
        real adapters must establish their quota capability before admission.
        """
        with self.transaction() as db:
            for row in db.execute("""SELECT a.id AS agent_id,a.system_id,a.pool_id,r.id AS run_id
                    FROM agents a JOIN systems s ON s.id=a.system_id
                    JOIN runs r ON r.system_id=s.id
                    LEFT JOIN agent_settings config ON config.agent_id=a.id
                    WHERE s.state='active' AND r.state='running' AND COALESCE(config.enabled,1)=1""").fetchall():
                reason = self._budget_reason(db, dict(row))
                if reason:
                    self._pause(db, row["system_id"], reason)

    def pausing_systems(self):
        with self._lock:
            return [row[0] for row in self.db.execute("SELECT id FROM systems WHERE state='pausing'")]

    def resume(self, system_id):
        """Explicit manual resume only in milestone A; never changes ceilings."""
        with self.transaction() as db:
            system = self._one(db, "systems", system_id)
            if system["state"] != "paused":
                raise Conflict("System is not paused")
            if db.execute("SELECT 1 FROM attempts WHERE system_id=? AND state IN ('reserved','started','unknown')", (system_id,)).fetchone():
                raise Conflict("Unreconciled workers prevent resume")
            for agent in db.execute("""SELECT a.* FROM agents a LEFT JOIN agent_settings s ON s.agent_id=a.id
                    WHERE a.system_id=? AND COALESCE(s.enabled,1)=1""", (system_id,)).fetchall():
                run = db.execute("SELECT * FROM runs WHERE system_id=? AND state='paused'", (system_id,)).fetchone()
                if run:
                    reason = self._budget_reason(db, {"system_id": system_id, "agent_id": agent["id"], "run_id": run["id"], "pool_id": agent["pool_id"]})
                    if reason:
                        raise Conflict(reason)
            db.execute("UPDATE runs SET state='running' WHERE system_id=? AND state='paused'", (system_id,))
            active = db.execute("SELECT 1 FROM runs WHERE system_id=? AND state='running'", (system_id,)).fetchone()
            db.execute("UPDATE systems SET state=?,reason=NULL,revision=revision+1 WHERE id=?", ("active" if active else "idle", system_id))
            self._event(db, system_id, "system.resumed", system_id, {})

    def ready_tasks(self, system_id):
        with self._lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM tasks WHERE system_id=? AND state='ready' ORDER BY created_at,id", (system_id,))]

    # -- Real-worker support (milestone C1). Every method below is called by
    # the tool service on behalf of one authenticated attempt; none of them
    # accepts an identity supplied by a model.

    def team(self, system_id):
        """Roster with execution configuration. Never includes a credential."""
        with self._lock:
            return [dict(row) for row in self.db.execute("""SELECT a.*,
                    COALESCE(s.instructions,'') AS instructions, COALESCE(s.enabled,1) AS enabled,
                    s.endpoint_id, s.model, s.effort FROM agents a
                    LEFT JOIN agent_settings s ON s.agent_id=a.id
                    WHERE a.system_id=? ORDER BY a.is_lead DESC,a.rowid""", (system_id,))]

    def agent_config(self, agent_id):
        with self._lock:
            row = self.db.execute("""SELECT a.*, COALESCE(s.instructions,'') AS instructions,
                    COALESCE(s.enabled,1) AS enabled, s.endpoint_id, s.model, s.effort
                    FROM agents a LEFT JOIN agent_settings s ON s.agent_id=a.id WHERE a.id=?""",
                                  (agent_id,)).fetchone()
            if row is None:
                raise NotFound(f"agents: {agent_id}")
            return dict(row)

    def open_run(self, system_id):
        with self._lock:
            row = self.db.execute("SELECT * FROM runs WHERE system_id=? AND state IN ('running','paused')",
                                  (system_id,)).fetchone()
            return dict(row) if row else None

    def create_plan(self, system_id, run_id, lead_id, items, *, command_id):
        """Write a lead's whole plan or none of it.

        Dependencies may only reference earlier items in the same plan or
        tasks already in this run, which makes a cycle unrepresentable rather
        than merely rejected. A half-written plan would leave tasks nothing
        can satisfy, so this is one transaction.
        """
        if not isinstance(items, list) or not items:
            raise ValueError("A plan needs at least one task")
        if len(items) > 50:
            raise ValueError("A plan is limited to 50 tasks")
        payload = ["create_plan", run_id, items]
        with self.transaction() as db:
            replay = self._replay(db, system_id, command_id, payload)
            if replay is not None:
                return replay
            lead = self._one(db, "agents", lead_id)
            run = self._one(db, "runs", run_id)
            if lead["system_id"] != system_id or not lead["is_lead"]:
                raise Conflict("Only this company's lead can assign work")
            if run["system_id"] != system_id or run["state"] not in ("running", "paused"):
                raise Conflict("Run is closed")
            created = {}
            for index, item in enumerate(items):
                key = str(item.get("key") or index)
                objective = (item.get("objective") or "").strip()
                if not objective:
                    raise ValueError("Every task needs an objective")
                agent = self._one(db, "agents", item["agent_id"])
                if agent["system_id"] != system_id:
                    raise Conflict("Task references another company's agent")
                settings = db.execute("SELECT enabled,endpoint_id FROM agent_settings WHERE agent_id=?",
                                      (agent["id"],)).fetchone()
                if settings is not None and not settings["enabled"]:
                    raise Conflict(f"{agent['name']} is not on the team any more")
                dependencies = []
                for reference in item.get("depends_on") or []:
                    reference = str(reference)
                    if reference in created:
                        dependencies.append(created[reference])
                        continue
                    existing = db.execute("SELECT id FROM tasks WHERE id=? AND system_id=? AND run_id=?",
                                          (reference, system_id, run_id)).fetchone()
                    if not existing:
                        raise Conflict(f"Unknown dependency: {reference}")
                    dependencies.append(existing[0])
                task_id = _id()
                db.execute("INSERT INTO tasks(id,system_id,run_id,agent_id,objective,created_at) VALUES(?,?,?,?,?,?)",
                           (task_id, system_id, run_id, agent["id"], objective, self.clock()))
                for dependency in dependencies:
                    self._dependency(db, task_id, dependency)
                self._event(db, system_id, "task.created", task_id, {"objective": objective, "agent_id": agent["id"]})
                created[key] = task_id
            self._remember(db, system_id, command_id, payload, created)
            return created

    def dependency_results(self, task_id):
        """What this task's upstream work produced, for the worker's context."""
        with self._lock:
            return [dict(row) for row in self.db.execute(
                """SELECT t.id,t.objective,t.state,t.result FROM dependencies d
                   JOIN tasks t ON t.id=d.depends_on WHERE d.task_id=? ORDER BY t.created_at,t.id""",
                (task_id,))]

    def agent_message(self, system_id, sender_id, recipient_id, body, *, kind="message"):
        """A peer message. Sender identity comes from the live attempt only."""
        body = (body or "").strip()
        if not body:
            raise ValueError("A message needs a body")
        with self.transaction() as db:
            sender = self._one(db, "agents", sender_id)
            recipient = self._one(db, "agents", recipient_id)
            if sender["system_id"] != system_id or recipient["system_id"] != system_id:
                raise Conflict("Messages cannot cross companies")
            message_id = _id()
            db.execute("INSERT INTO messages(id,system_id,sender_id,recipient_id,body,created_at) VALUES(?,?,?,?,?,?)",
                       (message_id, system_id, sender_id, recipient_id, body[:20000], self.clock()))
            self._event(db, system_id, "message." + kind, message_id,
                        {"sender_id": sender_id, "recipient_id": recipient_id})
            return {"id": message_id}

    def inbox(self, agent_id, *, limit=20, mark_read=True):
        """Unread messages addressed to this agent, oldest first."""
        with self.transaction() as db:
            agent = self._one(db, "agents", agent_id)
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM messages WHERE system_id=? AND recipient_id=? AND read_at IS NULL ORDER BY created_at,id LIMIT ?",
                (agent["system_id"], agent_id, limit))]
            if mark_read and rows:
                db.execute("UPDATE messages SET read_at=? WHERE id IN (%s)" % ",".join("?" * len(rows)),
                           (self.clock(), *[row["id"] for row in rows]))
            return rows

    def events(self, system_id, after=0):
        with self._lock:
            return [{**dict(row), "data": json.loads(row["data"])} for row in self.db.execute(
                "SELECT * FROM events WHERE system_id=? AND id>? ORDER BY id", (system_id, after))]

    def save_checkpoint(self, system_id, *, owner=None, command_id=None):
        with self.transaction() as db:
            system = self._owned(db, owner, system_id) if owner is not None else self._one(db, "systems", system_id)
            if command_id:
                replay = self._replay(db, system_id, command_id, ["checkpoint"])
                if replay is not None:
                    row = self._one(db, "checkpoints", replay["id"])
                    return {"id": row["id"], **json.loads(row["snapshot"])}
            if owner is not None and system["state"] == "archived":
                raise Conflict("Restore the system before saving a new handoff")
            cursor = db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE system_id=?", (system_id,)).fetchone()[0]
            snapshot = {"system": system, "event_cursor": cursor, "saved_at": self.clock()}
            for table in ("agents", "runs", "tasks", "attempts", "messages"):
                snapshot[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE system_id=? ORDER BY id", (system_id,))]
            snapshot["actions"] = [dict(row) for row in db.execute("SELECT a.* FROM actions a JOIN attempts p ON p.id=a.attempt_id WHERE p.system_id=?", (system_id,))]
            snapshot["dependencies"] = [dict(row) for row in db.execute("SELECT d.* FROM dependencies d JOIN tasks t ON t.id=d.task_id WHERE t.system_id=?", (system_id,))]
            snapshot["quotas"] = [dict(row) for row in db.execute("SELECT DISTINCT q.* FROM quotas q JOIN agents a ON a.pool_id=q.pool_id WHERE a.system_id=?", (system_id,))]
            snapshot["budgets"] = [dict(row) for row in db.execute("""SELECT * FROM budgets WHERE
                (scope='system' AND target=?) OR (scope='agent' AND target IN (SELECT id FROM agents WHERE system_id=?))
                OR (scope='run' AND target IN (SELECT id FROM runs WHERE system_id=?))
                OR (scope='pool' AND target IN (SELECT pool_id FROM agents WHERE system_id=?))""", (system_id,) * 4)]
            for budget in snapshot["budgets"]:
                budget["used"], budget["held"] = self._accounting(db, budget["scope"], budget["target"])
            # Visible partial output and all observable events remain recoverable.
            snapshot["events"] = [dict(row) for row in db.execute("SELECT * FROM events WHERE system_id=? AND id<=? ORDER BY id", (system_id, cursor))]
            checkpoint_id = _id()
            db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?)", (checkpoint_id, system_id, cursor, _json(snapshot), self.clock()))
            if command_id:
                self._remember(db, system_id, command_id, ["checkpoint"], {"id": checkpoint_id})
            return {"id": checkpoint_id, **snapshot}

    def complete_lifecycle(self, system_id, command_id):
        with self.transaction() as db:
            system = self._one(db, "systems", system_id)
            result = {"status": system["state"], "id": system_id}
            db.execute("UPDATE commands SET result=? WHERE scope=? AND command_id=?", (_json(result), system_id, command_id))
            return result

    def latest_checkpoint(self, system_id):
        with self._lock:
            row = self.db.execute("SELECT * FROM checkpoints WHERE system_id=? ORDER BY rowid DESC LIMIT 1", (system_id,)).fetchone()
            return {"id": row["id"], **json.loads(row["snapshot"])} if row else None

    # Owner-facing operations keep authorization and mutation in one transaction.
    # The service derives owner from authentication, never from request JSON.
    def _owned(self, db, owner, system_id):
        system = self._one(db, "systems", system_id)
        if system["owner"] != owner:
            raise NotFound("System not found")
        return system

    def _owned_pool(self, db, owner, pool_id):
        pool = self._one(db, "pools", pool_id)
        if pool["owner"] != owner:
            raise NotFound("Allocation group not found")
        return pool

    @staticmethod
    def _revision(system, expected):
        if system["revision"] != expected:
            raise Conflict("This system changed. Refresh before trying again.")

    def _replace_limit(self, db, scope, target, data):
        limit = BudgetLimit(**data)
        used, held = self._accounting(db, scope, target)
        if not limit.admits(used, held, 0):
            raise Conflict("The allocation cannot be less than recorded and reserved usage")
        db.execute("DELETE FROM budgets WHERE scope=? AND target=?", (scope, target))
        self._limit(db, scope, target, limit)

    def _pool_for_account(self, db, owner, account_key, label, fallback_pool_id, limit):
        """One pool per real provider account, per owner.

        Agents on the same account must share a pool or each would be handed
        its own copy of one real allowance. An agent with no connection yet
        keeps the system's own allocation group; it cannot execute anyway.
        """
        if not account_key:
            return fallback_pool_id
        row = db.execute("SELECT id FROM pools WHERE owner=? AND account_key=?", (owner, account_key)).fetchone()
        if row:
            return row[0]
        existing = db.execute("SELECT account_key FROM pools WHERE id=?", (fallback_pool_id,)).fetchone()
        if existing and existing[0] is None:
            # The group created for this system has not been claimed by an
            # account yet, so it becomes this account's pool rather than
            # stranding the allocation the owner just configured.
            db.execute("UPDATE pools SET account_key=? WHERE id=?", (account_key, fallback_pool_id))
            return fallback_pool_id
        pool_id = _id()
        db.execute("INSERT INTO pools VALUES(?,?,?,?)", (pool_id, label or account_key, owner, account_key))
        self._limit(db, "pool", pool_id, BudgetLimit(**limit))
        return pool_id

    def _team_member(self, db, owner, system_id, pool_id, data, is_lead, *, existing=False):
        agent_id = data.get("id")
        account_key = data.get("account_key")
        target_pool = self._pool_for_account(db, owner, account_key, data.get("account_label"),
                                             pool_id, data.get("pool_limit") or data["limit"])
        if agent_id:
            agent = self._one(db, "agents", agent_id)
            if agent["system_id"] != system_id or bool(agent["is_lead"]) != is_lead:
                raise NotFound("Agent not found")
            if agent["pool_id"] != target_pool:
                used, held = self._accounting(db, "agent", agent_id)
                if used or held:
                    raise Conflict("This agent has recorded usage on its current provider account. "
                                   "Remove it and add a new specialist to move it to another account.")
                db.execute("UPDATE agents SET pool_id=? WHERE id=?", (target_pool, agent_id))
            db.execute("UPDATE agents SET name=?,role=? WHERE id=?", (data["name"], data["role"], agent_id))
        else:
            if existing and is_lead:
                raise Conflict("The existing lead must be preserved")
            agent_id = _id()
            db.execute("INSERT INTO agents VALUES(?,?,?,?,?,?)",
                       (agent_id, system_id, data["name"], data["role"], int(is_lead), target_pool))
        db.execute("""INSERT INTO agent_settings(agent_id,instructions,enabled,endpoint_id,model,effort)
            VALUES(?,?,1,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET
            instructions=excluded.instructions, enabled=1, endpoint_id=excluded.endpoint_id,
            model=excluded.model, effort=excluded.effort""",
                   (agent_id, data["instructions"], data.get("endpoint_id"), data.get("model"), data.get("effort")))
        self._replace_limit(db, "agent", agent_id, data["limit"])
        return agent_id

    def owner_create(self, owner, data, command_id):
        with self.transaction() as db:
            scope, payload = "owner:" + owner, ["create_system", data]
            replay = self._replay(db, scope, command_id, payload)
            if replay is not None:
                return replay
            pool_id = data.get("pool_id")
            if pool_id:
                self._owned_pool(db, owner, pool_id)
            else:
                pool_id = _id()
                db.execute("INSERT INTO pools VALUES(?,?,?,NULL)", (pool_id, data["name"] + " allocation", owner))
                self._limit(db, "pool", pool_id, BudgetLimit(**data["pool_limit"]))
            system_id = _id()
            configuration = {"mode": data["mode"], "run_limit": data["run_limit"]}
            db.execute("INSERT INTO systems(id,owner,name,mission,created_at,configuration) VALUES(?,?,?,?,?,?)",
                       (system_id, owner, data["name"], data["mission"], self.clock(), _json(configuration)))
            self._limit(db, "system", system_id, BudgetLimit(**data["system_limit"]))
            self._team_member(db, owner, system_id, pool_id, data["lead"], True)
            for member in data["specialists"]:
                self._team_member(db, owner, system_id, pool_id, member, False)
            self._event(db, system_id, "system.created", system_id, {"name": data["name"]})
            result = {"id": system_id}
            self._remember(db, scope, command_id, payload, result)
            return result

    def owner_update(self, owner, system_id, data, command_id, expected_revision):
        with self.transaction() as db:
            system = self._owned(db, owner, system_id)
            payload = ["update", data, expected_revision]
            replay = self._replay(db, system_id, command_id, payload)
            if replay is not None:
                return replay
            self._revision(system, expected_revision)
            if system["state"] not in ("idle", "paused", "stopped"):
                raise Conflict("Pause or stop the system before editing setup")
            if db.execute("SELECT 1 FROM attempts WHERE system_id=? AND state IN ('reserved','started','unknown')", (system_id,)).fetchone():
                raise Conflict("Reconcile unfinished workers before editing setup")
            lead = db.execute("SELECT * FROM agents WHERE system_id=? AND is_lead=1", (system_id,)).fetchone()
            pool_id = lead["pool_id"]
            self._owned_pool(db, owner, pool_id)
            if data.get("pool_id") not in (None, pool_id):
                raise Conflict("Changing allocation groups requires usage reconciliation")
            members = [data["lead"], *data["specialists"]]
            retained = [member["id"] for member in members if member.get("id")]
            if len(retained) != len(set(retained)):
                raise Conflict("Agent IDs must be unique")
            for agent in db.execute("SELECT * FROM agents WHERE system_id=?", (system_id,)).fetchall():
                self._owned_pool(db, owner, agent["pool_id"])
                if agent["id"] not in retained:
                    if agent["is_lead"]:
                        raise Conflict("The existing lead must be preserved")
                    if db.execute("SELECT 1 FROM tasks WHERE agent_id=? AND state NOT IN ('done','cancelled')", (agent["id"],)).fetchone():
                        raise Conflict("An agent with unfinished tasks cannot be removed")
                    db.execute("INSERT INTO agent_settings(agent_id,enabled) VALUES(?,0) ON CONFLICT(agent_id) DO UPDATE SET enabled=0", (agent["id"],))
            self._replace_limit(db, "system", system_id, data["system_limit"])
            self._team_member(db, owner, system_id, pool_id, data["lead"], True, existing=True)
            for member in data["specialists"]:
                self._team_member(db, owner, system_id, pool_id, member, False, existing=True)
            db.execute("UPDATE systems SET name=?,mission=?,configuration=?,revision=revision+1 WHERE id=?",
                       (data["name"], data["mission"], _json({"mode": data["mode"], "run_limit": data["run_limit"]}), system_id))
            self._event(db, system_id, "system.updated", system_id, {})
            result = {"id": system_id, "revision": expected_revision + 1}
            self._remember(db, system_id, command_id, payload, result)
            return result

    def owner_message(self, owner, system_id, body, command_id):
        with self.transaction() as db:
            system = self._owned(db, owner, system_id)
            payload = ["owner_message", body]
            replay = self._replay(db, system_id, command_id, payload)
            if replay is not None:
                return replay
            if system["state"] == "archived":
                raise Conflict("Restore the system before sending a message")
            lead = db.execute("SELECT id FROM agents WHERE system_id=? AND is_lead=1", (system_id,)).fetchone()[0]
            message_id = _id()
            db.execute("INSERT INTO messages(id,system_id,recipient_id,body,created_at) VALUES(?,?,?,?,?)",
                       (message_id, system_id, lead, body, self.clock()))
            result = {"id": message_id, "status": "queued"}
            self._event(db, system_id, "message.queued", message_id, {"recipient_id": lead})
            self._remember(db, system_id, command_id, payload, result)
            return result

    def owner_lifecycle(self, owner, system_id, action, command_id, expected_revision, *, blocked=None):
        """`blocked` is the service's capability verdict, or None to proceed.

        The store never decides whether a provider can execute; it only
        records the outcome. Accepting a start does not dispatch work - the
        service performs the start and resume transitions outside this
        transaction, the same way it already drives pause and stop.
        """
        with self.transaction() as db:
            system = self._owned(db, owner, system_id)
            payload = ["lifecycle", action, expected_revision]
            replay = self._replay(db, system_id, command_id, payload)
            if replay is not None:
                return replay
            self._revision(system, expected_revision)
            if action in ("start", "resume"):
                if blocked is not None:
                    result = {"status": "blocked", **blocked}
                else:
                    required = ("idle", "stopped") if action == "start" else ("paused",)
                    if system["state"] not in required:
                        raise Conflict("The system is not in a state that allows this action")
                    if db.execute("SELECT 1 FROM attempts WHERE system_id=? AND state IN ('reserved','started','unknown')", (system_id,)).fetchone():
                        raise Conflict("Reconcile unfinished workers before running again")
                    if not db.execute("""SELECT 1 FROM agents a LEFT JOIN agent_settings s ON s.agent_id=a.id
                            WHERE a.system_id=? AND COALESCE(s.enabled,1)=1 AND s.endpoint_id IS NOT NULL""",
                                      (system_id,)).fetchone():
                        raise Conflict("Give the team model connections before running it")
                    result = {"status": "accepted", "action": action}
            elif action in ("pause", "stop"):
                if system["state"] == "archived":
                    raise Conflict("Restore the system first")
                self._pause(db, system_id, "manual_" + action)
                result = {"status": "accepted", "action": action}
            elif action in ("archive", "restore"):
                required = ("idle", "paused", "stopped") if action == "archive" else ("archived",)
                if system["state"] not in required:
                    raise Conflict("The system is not in a state that allows this action")
                if db.execute("SELECT 1 FROM attempts WHERE system_id=? AND state IN ('reserved','started','unknown')", (system_id,)).fetchone():
                    raise Conflict("Unfinished workers prevent archiving")
                if db.execute("SELECT 1 FROM runs WHERE system_id=? AND state IN ('running','paused')", (system_id,)).fetchone():
                    raise Conflict("Stop the current run before archiving")
                state = "archived" if action == "archive" else "stopped"
                db.execute("UPDATE systems SET state=?,revision=revision+1 WHERE id=?", (state, system_id))
                self._event(db, system_id, "system." + action, system_id, {})
                result = {"status": state}
            else:
                raise ValueError("Unknown lifecycle action")
            self._remember(db, system_id, command_id, payload, result)
            return result

    def finalize_stop(self, system_id):
        with self.transaction() as db:
            system = self._one(db, "systems", system_id)
            if system["state"] == "stopped":
                return
            if system["state"] != "paused":
                raise Conflict("Workers must reach a recorded stop state first")
            db.execute("UPDATE runs SET state='cancelled' WHERE system_id=? AND state IN ('running','paused')", (system_id,))
            db.execute("UPDATE tasks SET state='cancelled',revision=revision+1 WHERE system_id=? AND state IN ('ready','review')", (system_id,))
            # Unknown attempts and their blocked tasks survive a manual stop.
            db.execute("UPDATE systems SET state='stopped',revision=revision+1 WHERE id=?", (system_id,))
            self._event(db, system_id, "system.stopped", system_id, {})

    def owner_systems(self, owner, offset=0, limit=50):
        with self.transaction() as db:
            total = db.execute("SELECT COUNT(*) FROM systems WHERE owner=?", (owner,)).fetchone()[0]
            rows = db.execute("SELECT * FROM systems WHERE owner=? ORDER BY created_at DESC,id LIMIT ? OFFSET ?", (owner, limit, offset))
            items = []
            for row in rows.fetchall():
                item = dict(row)
                item["configuration"] = json.loads(item["configuration"])
                item["active_tasks"] = db.execute("SELECT COUNT(*) FROM tasks WHERE system_id=? AND state='running'", (item["id"],)).fetchone()[0]
                items.append(item)
            return {"items": items, "total": total, "offset": offset}

    def owner_pools(self, owner):
        with self.transaction() as db:
            return [dict(row) for row in db.execute("SELECT id,name FROM pools WHERE owner=? ORDER BY name,id", (owner,))]

    COLLECTIONS = {"tasks", "messages", "runs", "attempts", "events", "checkpoints"}

    def _page(self, db, system_id, collection, offset, limit):
        if collection not in self.COLLECTIONS:
            raise NotFound("Collection not found")
        columns = "id,system_id,cursor,created_at" if collection == "checkpoints" else "*"
        total = db.execute(f"SELECT COUNT(*) FROM {collection} WHERE system_id=?", (system_id,)).fetchone()[0]
        rows = db.execute(f"SELECT {columns} FROM {collection} WHERE system_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?",
                          (system_id, limit, offset))
        return {"items": [dict(row) for row in rows], "total": total, "offset": offset}

    def owner_page(self, owner, system_id, collection, offset=0, limit=50):
        with self.transaction() as db:
            self._owned(db, owner, system_id)
            return self._page(db, system_id, collection, offset, limit)

    def owner_snapshot(self, owner, system_id):
        with self.transaction() as db:
            system = self._owned(db, owner, system_id)
            system["configuration"] = json.loads(system["configuration"])
            # The connection fields belong here too: the team view names what
            # each agent runs on, and without them it would say "no model
            # connection" for an agent that has one. Identifiers and public
            # model names only - no URL, no key.
            agents = [dict(row) for row in db.execute("""SELECT a.*,COALESCE(s.instructions,'') AS instructions,
                COALESCE(s.enabled,1) AS enabled, s.endpoint_id, s.model, s.effort
                FROM agents a LEFT JOIN agent_settings s ON s.agent_id=a.id
                WHERE a.system_id=? ORDER BY a.is_lead DESC,a.rowid""", (system_id,))]
            budgets = [dict(row) for row in db.execute("""SELECT b.* FROM budgets b WHERE
                (scope='system' AND target=?) OR (scope='agent' AND target IN (SELECT id FROM agents WHERE system_id=?))
                OR (scope='run' AND target IN (SELECT id FROM runs WHERE system_id=?)) OR
                (scope='pool' AND target IN (SELECT a.pool_id FROM agents a JOIN pools p ON p.id=a.pool_id WHERE a.system_id=? AND p.owner=?))""",
                (system_id, system_id, system_id, system_id, owner))]
            for budget in budgets:
                budget["used"], budget["held"] = self._accounting(db, budget["scope"], budget["target"])
            quotas = [dict(row) for row in db.execute("""SELECT DISTINCT q.* FROM quotas q JOIN agents a ON a.pool_id=q.pool_id
                JOIN pools p ON p.id=q.pool_id WHERE a.system_id=? AND p.owner=?""", (system_id, owner))]
            unresolved_pools = db.execute("SELECT COUNT(*) FROM agents a JOIN pools p ON p.id=a.pool_id WHERE a.system_id=? AND (p.owner IS NULL OR p.owner!=?)", (system_id, owner)).fetchone()[0]
            cursor = db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE system_id=?", (system_id,)).fetchone()[0]
            # The work graph needs its edges. dependencies cannot join
            # COLLECTIONS: it has no system_id column and _page filters on
            # one. Both ends are joined back to tasks so an edge leaving this
            # system can never reach the UI, even if a future writer regresses
            # the cross-system rejection on the write path.
            dependencies = [dict(row) for row in db.execute(
                """SELECT d.* FROM dependencies d JOIN tasks t ON t.id=d.task_id
                   JOIN tasks p ON p.id=d.depends_on WHERE t.system_id=? AND p.system_id=?""",
                (system_id, system_id))]
            # Every worker still needing reconciliation, whatever page its
            # attempt landed on. The attempts page holds the newest fifty, so
            # a stuck worker would otherwise become invisible - and its task
            # permanently held - once fifty newer attempts existed.
            unknown = [dict(row) for row in db.execute(
                "SELECT * FROM attempts WHERE system_id=? AND state='unknown' ORDER BY rowid", (system_id,))]
            conclusion = db.execute(
                "SELECT data FROM events WHERE system_id=? AND kind='mission.concluded' ORDER BY id DESC LIMIT 1",
                (system_id,)).fetchone()
            return {"system": system, "agents": agents, "budgets": budgets, "quotas": quotas,
                    "dependencies": dependencies, "unknown_attempts": unknown,
                    "conclusion": json.loads(conclusion[0]) if conclusion else None,
                    "cycles": db.execute("SELECT COUNT(*) FROM runs WHERE system_id=?", (system_id,)).fetchone()[0],
                    "allocation_ownership_unresolved": bool(unresolved_pools), "event_cursor": cursor,
                    "pages": {key: self._page(db, system_id, key, 0, 50) for key in sorted(self.COLLECTIONS)}}

    def owner_events(self, owner, system_id, after=0, limit=100):
        with self.transaction() as db:
            self._owned(db, owner, system_id)
            return [{**dict(row), "data": json.loads(row["data"]), "version": 1} for row in db.execute(
                "SELECT * FROM events WHERE system_id=? AND id>? ORDER BY id LIMIT ?", (system_id, after, limit))]

    def owner_checkpoint(self, owner, system_id, checkpoint_id):
        with self.transaction() as db:
            self._owned(db, owner, system_id)
            row = db.execute("SELECT * FROM checkpoints WHERE id=? AND system_id=?", (checkpoint_id, system_id)).fetchone()
            if not row:
                raise NotFound("Checkpoint not found")
            # Ambiguous legacy account groups may contain another owner's
            # aggregate usage. Do not export those snapshots through the UI.
            if db.execute("SELECT 1 FROM agents a JOIN pools p ON p.id=a.pool_id WHERE a.system_id=? AND (p.owner IS NULL OR p.owner!=?)", (system_id, owner)).fetchone():
                raise Conflict("Allocation ownership must be resolved before exporting this legacy checkpoint")
            return {"id": row["id"], **json.loads(row["snapshot"])}
