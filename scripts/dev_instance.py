"""Set up the data for a development copy of JARVIS (2026-10-04).

A development copy runs beside the real app: `npm run start:dev` in
electron/ starts it with its own data folder, port and instance lock (see
electron/instance.js). This script decides what that data folder holds:

    python scripts/dev_instance.py fresh            an empty copy
    python scripts/dev_instance.py copy             a copy of your real data
    python scripts/dev_instance.py status           what the copy holds now

`copy` takes the real app's data folder as it is, then switches off
everything that would act on the outside world by itself or reach back into
the real app:

- scheduled tasks are disabled, and Ready or Running cards go to Backlog;
- built-ins the real app has not auto-enabled yet stay off;
- Discord bots are set aside (two bots on one token would fight, and a copy
  would answer in your real channels);
- remote access is off (the real app owns that port);
- Swarm schedules are disabled and any active company paused;
- chats forget their Claude and Codex thread IDs, so a dev turn never lands
  in a thread the real app resumes; they replay their saved transcript
  instead;
- the vault is copied beside the data and the copy points at it, so dev notes
  never touch the real vault.

Model connections, signed-in integrations and connected accounts are copied
as they are: they are what makes the copy useful, and they are the real
accounts. Anything done with them from the dev copy happens for real.

The real data folder is only ever read (SQLite may add its own empty
sidecar files beside a database it reads). An existing copy is never deleted:
--replace moves it aside with a timestamp.
"""
import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SQLITE_HEADER = b"SQLite format 3\x00"
INSTANCE_PORT = 8430  # electron/instance.js
# Rebuilt by the app; a copy of a running app's logs is only noise.
SKIP_DIRS = {"logs"}
MARKER = "dev_instance.json"


def app_data_dir() -> Path:
    """Electron's app.getPath("appData"), where both copies keep their folders."""
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"])
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def instance_root(appdata: Path, name: str) -> Path:
    return appdata / f"JARVIS-{name}"


def _is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_HEADER
    except OSError:
        return False


def _copy_sqlite(source: Path, target: Path) -> None:
    """A consistent snapshot even while the real app is writing to it, which
    a plain file copy of the database without its WAL is not."""
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def copy_data(source: Path, target: Path) -> None:
    for root, dirs, files in os.walk(source):
        rel = Path(root).relative_to(source)
        dirs[:] = [d for d in dirs if not (rel == Path(".") and d in SKIP_DIRS)]
        (target / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            if name.endswith(("-wal", "-shm", "-journal")):
                continue  # folded into the snapshot by _copy_sqlite
            src = Path(root) / name
            if _is_sqlite(src):
                _copy_sqlite(src, target / rel / name)
            else:
                shutil.copy2(src, target / rel / name)


def _instance_running(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _move_aside(root: Path) -> list[str]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved = []
    for part in ("data", "vault"):
        current = root / part
        if current.exists():
            aside = root / f"{part}.replaced-{stamp}"
            current.rename(aside)
            moved.append(str(aside))
    return moved


def _prepare(root: Path, replace: bool) -> list[str]:
    if (root / "data").exists():
        if not replace:
            raise SystemExit(f"{root / 'data'} already exists. Pass --replace to move it aside and start again.")
        return _move_aside(root)
    return []


def _vault_plan(source: Path, root: Path) -> tuple[Path | None, Path | None]:
    """(where the real vault is, where the copy's vault goes). A vault inside
    the real data folder (the default) travels with the data copy itself."""
    try:
        settings = json.loads((source / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    configured = settings.get("vault_dir")
    if not configured:
        return None, None
    real = Path(configured).resolve()
    try:
        inside = real.relative_to(source.resolve())
    except ValueError:
        return real, root / "vault"
    return real, root / "data" / inside


def cmd_fresh(args) -> None:
    root = instance_root(args.appdata, args.name)
    if _instance_running(args.port):
        raise SystemExit(f"A JARVIS is answering on port {args.port}. Quit the development copy first.")
    moved = _prepare(root, args.replace)
    (root / "data").mkdir(parents=True)
    _write_marker(root / "data", {"mode": "fresh", "created_at": time.time(), "moved_aside": moved})
    print(f"Empty development copy ready at {root / 'data'}.")
    for path in moved:
        print(f"Previous copy kept at {path}")
    print("Start it with: npm run start:dev (in electron/)")


def cmd_copy(args) -> None:
    source = (args.source or args.appdata / "JARVIS" / "data").resolve()
    root = instance_root(args.appdata, args.name)
    if not (source / "settings.json").exists():
        raise SystemExit(f"{source} does not look like a JARVIS data folder (no settings.json).")
    if root.resolve() == source.parent.resolve() or source.resolve().is_relative_to(root.resolve()):
        raise SystemExit("The source is the development copy itself.")
    if _instance_running(args.port):
        raise SystemExit(f"A JARVIS is answering on port {args.port}. Quit the development copy first.")
    moved = _prepare(root, args.replace)
    target = root / "data"
    try:
        report, vault_copy = _copy_and_switch_off(source, root, target)
    except BaseException:
        # A half-made copy may still have the real app's automations switched
        # on. Moved out of the way, it is never what start:dev opens.
        _quarantine(root)
        raise
    _write_marker(target, {"mode": "copy", "created_at": time.time(), "source": str(source),
                           "vault": str(vault_copy) if vault_copy else None, "moved_aside": moved, **report})
    print(f"Development copy ready at {target}.")
    _print_report(report)
    for path in moved:
        print(f"Previous copy kept at {path}")
    print("Model connections and signed-in accounts are the real ones: what the copy does with them happens for real.")
    print("Start it with: npm run start:dev (in electron/)")


def _quarantine(root: Path) -> None:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for part in ("data", "vault"):
        if (root / part).exists():
            (root / part).rename(root / f"{part}.failed-{stamp}")


def _copy_and_switch_off(source: Path, root: Path, target: Path) -> tuple[dict, Path | None]:
    print(f"Copying {source} ...")
    copy_data(source, target)

    real_vault, vault_copy = _vault_plan(source, root)
    if real_vault and vault_copy and not vault_copy.is_relative_to(target):
        if real_vault.is_dir():
            print(f"Copying the vault {real_vault} ...")
            shutil.copytree(real_vault, vault_copy, ignore=shutil.ignore_patterns(".git"))
        else:
            vault_copy.mkdir(parents=True)

    # The switch-offs run with the app's own code, bound to the copy's folder
    # by JARVIS_DATA_DIR, so they write exactly what the app would read.
    env = {**os.environ, "JARVIS_DATA_DIR": str(target)}
    env.pop("JARVIS_VAULT_DIR", None)
    result = subprocess.run([sys.executable, __file__, "_switch-off", str(target), str(vault_copy or "")],
                            cwd=REPO, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"Switching things off in the copy failed, so it was moved aside unused.\n{result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1]), vault_copy


def switch_off(data_dir: Path, vault_copy: str) -> dict:
    """Runs inside the copy (JARVIS_DATA_DIR=data_dir)."""
    sys.path.insert(0, str(REPO))
    from core.atomic_io import read_json, write_json_atomic
    from core.builtin_tasks import AUTO_ENABLE
    from core import session_manager_store as store
    from core.settings import get_setting, update_settings

    report = {}
    tasks_file = data_dir / "tasks.json"
    tasks = read_json(str(tasks_file), {})
    disabled, held = [], []
    for task in tasks.values():
        if task.get("schedule_kind") == "card":
            if task.get("status") in ("ready", "running"):
                task.update({"status": "backlog", "claimed_until": None})
                task.pop("run_started_at", None)
                held.append(task["name"])
        else:
            task.pop("run_started_at", None)
            if task.get("enabled"):
                task["enabled"] = False
                disabled.append(task["name"])
    if tasks:
        write_json_atomic(str(tasks_file), tasks)
    report["tasks_disabled"] = disabled
    report["cards_moved_to_backlog"] = held

    changes = {"remote_access_enabled": False,
               "auto_enabled_builtins": sorted(set(get_setting("auto_enabled_builtins") or []) | set(AUTO_ENABLE))}
    if get_setting("discord_bot_token_encrypted"):
        changes["discord_bot_token_encrypted"] = None
    if vault_copy:
        changes["vault_dir"] = vault_copy
    update_settings(**changes)
    report["remote_access"] = "off"
    report["vault_dir"] = get_setting("vault_dir") or "default (inside the copy)"

    bots = data_dir / "discord_bots.json"
    if bots.exists():
        names = [b.get("name") for b in (read_json(str(bots), {}) or {}).values()]
        bots.rename(data_dir / "discord_bots.dev-disabled.json")
        report["discord_bots_set_aside"] = names
    else:
        report["discord_bots_set_aside"] = []

    forgotten = 0
    for header in store.list_sessions(include_agents=True):
        doc = store.get_session(header["id"])
        if doc and (doc.get("claude_session_id") or doc.get("codex_thread_id")):
            doc.update({"claude_session_id": None, "claude_synced_through": 0, "codex_thread_id": None})
            store.save_session(doc, rebuild_messages_from=store.MESSAGES_UNCHANGED)
            forgotten += 1
    report["chats_detached_from_cli_threads"] = forgotten

    report["swarm_paused"] = _switch_off_swarm(data_dir / "swarm" / "swarm.sqlite3")
    return report


def _switch_off_swarm(path: Path) -> list[str]:
    if not path.exists():
        return []
    db = sqlite3.connect(path)
    try:
        with db:
            # The real app's lease on the runtime belongs to the real database.
            db.execute("DELETE FROM runtime_lock")
            db.execute("UPDATE systems SET configuration=json_set(configuration, '$.schedule.enabled', json('false')) "
                       "WHERE json_extract(configuration, '$.schedule') IS NOT NULL")
        active = [tuple(r) for r in db.execute(
            "SELECT id, name FROM systems WHERE state NOT IN ('idle','paused','pausing','archived','stopped')")]
    finally:
        db.close()
    if active:
        from core.swarm.store import SwarmStore
        store = SwarmStore(path)
        for system_id, _ in active:
            store.pause(system_id, "dev_copy")
    return [name for _, name in active]


def _write_marker(data_dir: Path, record: dict) -> None:
    (data_dir / MARKER).write_text(json.dumps(record, indent=2), encoding="utf-8")


def _print_report(report: dict) -> None:
    print("Switched off in the copy:")
    print(f"  scheduled tasks disabled: {', '.join(report['tasks_disabled']) or 'none'}")
    print(f"  cards moved to Backlog: {', '.join(report['cards_moved_to_backlog']) or 'none'}")
    print(f"  Discord bots set aside: {', '.join(n or '(unnamed)' for n in report['discord_bots_set_aside']) or 'none'}")
    print(f"  remote access: {report['remote_access']}")
    print(f"  Swarm companies paused: {', '.join(report['swarm_paused']) or 'none'} (schedules disabled)")
    print(f"  chats detached from Claude/Codex threads: {report['chats_detached_from_cli_threads']}")
    print(f"  vault: {report['vault_dir']}")


def cmd_status(args) -> None:
    root = instance_root(args.appdata, args.name)
    marker = root / "data" / MARKER
    if not marker.exists():
        print(f"No development copy at {root / 'data'}." if not (root / "data").exists()
              else f"{root / 'data'} exists but was not made by this script.")
        return
    record = json.loads(marker.read_text(encoding="utf-8"))
    print(f"{root / 'data'}: {record['mode']} copy made {time.strftime('%Y-%m-%d %H:%M', time.localtime(record['created_at']))}")
    if record["mode"] == "copy":
        print(f"  from {record['source']}")
        _print_report(record)
    print(f"  running now: {'yes' if _instance_running(args.port) else 'no'} (port {args.port})")


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_switch-off"]:
        print(json.dumps(switch_off(Path(argv[1]), argv[2])))
        return
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["fresh", "copy", "status"])
    parser.add_argument("--name", default="dev", help="instance name (default dev)")
    parser.add_argument("--replace", action="store_true", help="move an existing copy aside and make a new one")
    parser.add_argument("--source", type=Path, help="data folder to copy (default: the real app's)")
    parser.add_argument("--appdata", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=INSTANCE_PORT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not __import__("re").fullmatch(r"[a-z0-9][a-z0-9-]{0,19}", args.name):
        parser.error("--name is 1 to 20 lowercase letters, digits or dashes")
    args.appdata = args.appdata or app_data_dir()
    {"fresh": cmd_fresh, "copy": cmd_copy, "status": cmd_status}[args.command](args)


if __name__ == "__main__":
    main()
