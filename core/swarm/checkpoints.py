"""Model-free, durable company handoffs. The database is authoritative."""
import json
import os
import re
import tempfile
from pathlib import Path


def render_handoff(snapshot):
    system = snapshot["system"]
    lines = [f"# Swarm handoff: {system['name']}", "",
             f"State: {system['state']}", f"Mission: {system['mission']}",
             f"Pause reasons: {system['reason'] or 'none'}",
             f"Event cursor: {snapshot['event_cursor']}", "",
             "Generated from saved records; no model call was made.", "",
             "## Continue safely", "",
             "Review blocked attempts and unresolved actions before resuming.",
             "Confirm all workers stopped, reconcile effects, and refresh quota observations.",
             "Resume is explicit; a handoff does not authorize replaying an action.", ""]
    # Include the full persisted context, not an inferred or lossy summary.
    for label in ("agents", "runs", "tasks", "attempts", "actions", "dependencies",
                  "messages", "budgets", "quotas", "events"):
        lines.extend([f"## {label.title()}", ""])
        value = json.dumps(snapshot[label], ensure_ascii=False, indent=2)
        # A dynamic fence prevents worker text containing backticks escaping it.
        longest = max((len(part) for part in re.findall(r'`+', value)), default=0)
        fence = "`" * max(3, longest + 1)
        lines.extend([fence + "json", value, fence, ""])
    if not snapshot["quotas"]:
        lines.extend(["Quota visibility: local budgets only; provider allowance is unverified.", ""])
    return "\n".join(lines)


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".swarm-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_handoff(store, system_id, directory):
    """Commit the snapshot first. Export errors propagate, never report saved files.

    Immutable checkpoint filenames make partial exports distinguishable. The
    SQLite snapshot remains available if either optional file export fails.
    The directory is chosen by the host, never a worker-supplied artifact path.
    """
    snapshot = store.save_checkpoint(system_id)
    destination = Path(directory) / snapshot["system"]["id"]
    json_path = destination / (snapshot["id"] + ".json")
    markdown_path = destination / (snapshot["id"] + ".md")
    _atomic_write(json_path, json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False))
    _atomic_write(markdown_path, render_handoff(snapshot))
    return {"snapshot": snapshot, "json": json_path, "markdown": markdown_path}


def download_handoff(snapshot, format):
    """Render a selected saved snapshot; no writes, paths, or model calls."""
    if format == "json":
        return json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False), "application/json"
    if format == "md":
        return render_handoff(snapshot), "text/markdown"
    raise ValueError("Unsupported handoff format")
