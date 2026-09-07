"""Built-in tasks — premade, one-click "turn on" tasks (David's ask
2026-08-31, matching Odysseus's builtin action registry —
src/builtin_actions.py's tidy_sessions/daily_brief/summarize_emails/
audit_skills/etc., surfaced in their Tasks tab as an "Add Task" preset
picker + admin-only action gating).

Odysseus ships ~15 builtin actions (tidy_sessions, tidy_documents,
consolidate_memory, tidy_research, tidy_calendar, summarize_emails,
draft_email_replies, email_auto_translate, extract_email_events,
classify_events, learn_sender_signatures, check_email_urgency, test_skills,
audit_skills, daily_brief). Scoped down hard to four with a real foundation
in JARVIS — no Documents/Library, no separate memory-facts store, no
Research feature, so most of that list has nothing to attach to.

Two kinds, matching Odysseus's own real "action" vs "llm" task-type split:
- "action": a plain Python function, deterministic, no model call, no token
  cost. Matches Odysseus's action_* functions in builtin_actions.py.
- "llm": gathers real live data, builds a grounded prompt from it, and runs
  that through Brain — same as Odysseus's daily_brief/summarize_emails
  (their scheduler prepends a persona; ours just embeds real data directly).
"""
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from core import email_triage as core_email_triage
from core import events
from core.session_manager import session_manager
from core.untrusted import wrap_untrusted
from services.calendar_service import calendar_service
from services.email_service import email_service
from services.notes_service import notes_service
from services.skills_service import list_skills, get_skill

logger = logging.getLogger(__name__)

EMPTY_SESSION_MAX_AGE_SECONDS = 24 * 60 * 60


async def _run_tidy_chats() -> str:
    sessions = session_manager.list_sessions()
    cutoff = time.time() - EMPTY_SESSION_MAX_AGE_SECONDS
    removed = []
    for s in sessions:
        if s.get("message_count", 0) == 0 and not s.get("starred") and s["updated_at"] < cutoff:
            session_manager.delete_session(s["id"])
            removed.append(s["title"])
    if not removed:
        return "No empty chats older than 24h to clean up."
    return f"Removed {len(removed)} empty chat(s): {', '.join(removed)}"


async def _run_tidy_calendar() -> str:
    now_iso = datetime.now(timezone.utc).isoformat()
    far_past = "2000-01-01T00:00:00Z"
    events = calendar_service.list_range(far_past, now_iso)
    removed = 0
    for e in events:
        # Only hand-created/synced calendar events, never Notes' due-dated
        # items — Notes stays the one source of truth for those (deleting
        # a "note" source_row here would just delete the calendar view of
        # it, not the note, so it's excluded on purpose rather than by bug).
        if e["source"] == "calendar" and e["end"] < now_iso:
            calendar_service.delete_event(e["id"])
            removed += 1
    if removed == 0:
        return "No past calendar events to clean up."
    return f"Removed {removed} past calendar event(s)."


UPCOMING_DAYS = 7
STALE_AFTER_DAYS = 14


def _logged_date(note: dict) -> Optional[str]:
    """The date an item was actually written, pulled out of its own text.

    Notes synced from the vault carry a created_at of when SYNC IMPORTED them,
    not when the line was written — so re-syncing reset every item's age to
    zero and the Stale section could never fire (David, 2026-09-06: The Bridge
    correctly reported the same items as "21 days old, logged 2026-08-14").
    Vault lines routinely date themselves ("Idea, 2026-08-14 (not yet
    scoped)"), so the honest age is in the text when it's there at all.
    """
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", note.get("text", ""))
    return match.group(1) if match else None


def _age_days(note: dict) -> tuple[int, Optional[str]]:
    """(age in days, the logged date if the text carried one)."""
    logged = _logged_date(note)
    if logged:
        try:
            return (date.today() - date.fromisoformat(logged)).days, logged
        except ValueError:
            pass
    return int((time.time() - note.get("created_at", time.time())) / 86400), None


async def _build_daily_brief_prompt() -> str:
    """Formatted to match The Bridge's own daily briefing (voice-line's
    discord_bot.py, 7am Discord post): bold one-line greeting, then bold
    section labels Calendar/Emails/Priorities/Stale, each a couple tight
    bullets, skipping a section entirely when there's nothing in it."""
    # Local dates, compared as dates. The old code built a UTC-midnight window
    # and asked for instants, which is both the wrong day boundary for a human
    # brief and unable to match all-day events at all.
    today = date.today()
    events = calendar_service.list_for_dates(today.isoformat(), today.isoformat())
    upcoming = [
        e for e in calendar_service.list_for_dates(
            (today + timedelta(days=1)).isoformat(),
            (today + timedelta(days=UPCOMING_DAYS)).isoformat(),
        )
    ]

    all_open_notes = notes_service.list_notes(include_completed=False)
    due_notes = [n for n in all_open_notes if n.get("due_date")]
    aged = sorted(((n, *_age_days(n)) for n in all_open_notes), key=lambda t: -t[1])
    stale_notes = [(n, age, logged) for n, age, logged in aged if age >= STALE_AFTER_DAYS][:5]
    stale_ids = {n["id"] for n, _, _ in stale_notes}
    # Priorities = the open work itself. The prompt has always ASKED for a
    # **Priorities** section, but no priorities were ever put in the data, so
    # the model could not write one no matter what (David, 2026-09-06). These
    # come from Active Priorities via vault_sync, grouped the way the vault
    # groups them.
    priorities: dict[str, list[str]] = {}
    for n in all_open_notes:
        if n["id"] in stale_ids:
            continue  # it gets its own line under Stale; don't say it twice
        priorities.setdefault(n.get("project") or "General", []).append(n["text"])

    lines = ["Give me a short daily brief using exactly this real data (don't invent anything beyond it):", ""]

    lines.append(f"TODAY is {today:%A, %B %d, %Y}.")
    lines.append("")

    lines.append(f"Today's calendar events ({len(events)}):")
    for e in events:
        lines.append(f"- {e['title']} at {e['start']}")
    if not events:
        lines.append("- (none)")
    lines.append("")

    lines.append(f"Coming up in the next {UPCOMING_DAYS} days ({len(upcoming)}):")
    for e in upcoming[:15]:
        lines.append(f"- {e['start']}: {e['title']}")
    if not upcoming:
        lines.append("- (none)")
    lines.append("")

    lines.append(f"Open priorities, grouped by project ({sum(len(v) for v in priorities.values())}):")
    for project, items in priorities.items():
        lines.append(f"  {project}:")
        for text in items[:6]:
            lines.append(f"  - {text[:300]}")
    if not priorities:
        lines.append("- (none)")
    lines.append("")

    accounts = email_service.list_accounts()
    if not accounts:
        lines.append("Connected email accounts: (none)")
    else:
        since_yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        total_unseen = 0
        email_lines = []
        for acct in accounts:
            try:
                unread = email_service.list_messages(acct["id"], limit=100, unseen_only=True, since=since_yesterday)
            except Exception as e:
                lines.append(f"Unread email on {acct['email']}: couldn't check ({e})")
                continue
            total_unseen += len(unread)
            email_lines.append(f"Unread email from the last day on {acct['email']} ({len(unread)}):")
            for m in unread:
                email_lines.append(f"- From: {m['from']} | Subject: {m['subject']} | Date: {m['date']}")
            if not unread:
                email_lines.append("- (none)")
        if total_unseen == 0:
            lines.append("No new unread mail from the last day across any connected account.")
        elif email_lines:
            # Sender/subject text is attacker-controlled — fence it so an
            # email titled "ignore previous instructions..." stays data
            # (core/untrusted.py, David's ask 2026-09-02).
            lines.append(wrap_untrusted("unread email headers", "\n".join(email_lines)))
    lines.append("")

    lines.append(f"Open due-dated notes ({len(due_notes)}):")
    for n in due_notes[:20]:
        lines.append(f"- {n['text']} (due {n['due_date']})")
    if not due_notes:
        lines.append("- (none)")
    lines.append("")

    lines.append(f"Stale open items, {STALE_AFTER_DAYS}+ days old, oldest first ({len(stale_notes)} shown):")
    for n, age, logged in stale_notes:
        suffix = f", logged {logged}" if logged else ""
        lines.append(f"- {n['text'][:300]} ({age}d old{suffix})")
    if not stale_notes:
        lines.append("- (none)")
    lines.append("")

    lines.append(
        "Write it up as a Discord message using Discord's own markdown, in this shape:\n"
        "- One short greeting line addressed to David, e.g. \"Good morning, David. Here's the lay "
        "of the land for today.\"\n"
        "- Then bold section labels — **Calendar**, **Emails**, **Priorities**, **Stale** — each "
        "followed by a few tight bullets.\n"
        "- Under **Calendar**, lead with anything happening today; if today is empty say so in a "
        "few words and then list the nearest upcoming deadlines with their dates, so the section "
        "is still useful rather than just 'nothing today'.\n"
        "- Under **Emails**, roll up the count, and call out anything important by sender and "
        "subject with a one-line reason (needs a reply, has a deadline, is financial or "
        "account-related, or is from someone I clearly know).\n"
        "- Under **Priorities**, two or three lines on what's actually open and moving, grouped "
        "sensibly. Summarize, don't just relist every bullet verbatim.\n"
        "- Under **Stale**, note how long they've sat and their logged date if given, e.g. "
        "\"(21 days old, oldest first — all logged 2026-08-14, no movement since)\".\n"
        "Skip any section that genuinely has no data. Keep the whole thing tight — this is a "
        "quick morning glance, not a full report. Never invent an item that isn't in the data "
        "above, and don't pad a thin section with commentary about it being thin."
    )
    return "\n".join(lines)


async def _run_sync_all() -> str:
    """Refresh every connected calendar feed, DAV source and registered API.

    David's ask 2026-09-06: these should keep themselves current instead of
    waiting for someone to press Sync on each one. Scheduled ahead of the
    Daily Brief so the brief reads fresh data rather than yesterday's.
    """
    from core import sync_engine

    results = await sync_engine.sync_all()
    summary = sync_engine.format_results(results)
    failed = [r for r in results if not r["ok"]]
    if failed:
        events.emit(
            "sync.failed",
            f"{len(failed)} source(s) failed to sync: " + ", ".join(r["name"] for r in failed),
            level="error",
        )
    return summary


TRIAGE_MAX_MESSAGES = 60


async def _run_triage_email() -> str:
    """Score the last day's unread mail for importance, for the Email tab.

    A model call rather than keyword rules: "important" here means needs a
    reply, carries a deadline, is financial/account-related, or comes from a
    real person — judgements that keyword lists get wrong in both directions.
    One small call a day, headers only, no message bodies fetched.
    """
    from core.brain import Brain

    accounts = email_service.list_accounts()
    if not accounts:
        core_email_triage.save([], 0, error="No email accounts connected.")
        return "No email accounts connected — nothing to triage."

    since = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    messages: list[dict] = []
    errors: list[str] = []
    for acct in accounts:
        try:
            for m in email_service.list_messages(acct["id"], limit=TRIAGE_MAX_MESSAGES,
                                                 unseen_only=True, since=since):
                messages.append({**m, "account": acct["email"]})
        except Exception as e:
            errors.append(f"{acct['email']}: {e}")

    if not messages:
        core_email_triage.save([], 0, error="; ".join(errors) or None)
        return "No unread mail from the last day." + (f" (errors: {'; '.join(errors)})" if errors else "")

    listing = "\n".join(
        f"{i}. From: {m.get('from')} | Subject: {m.get('subject')} | Date: {m.get('date')}"
        for i, m in enumerate(messages)
    )
    prompt = (
        "Below are unread email headers from the last day. Decide which ones actually matter.\n\n"
        + wrap_untrusted("unread email headers", listing)
        + "\n\nSomething is important if it needs a reply, carries a deadline, is financial or "
        "account/security related, or is from a real person rather than a mailing list. "
        "Marketing, promotions, job-alert digests, and social notifications are NOT important.\n\n"
        "Reply with ONE LINE PER IMPORTANT EMAIL, in exactly this format:\n"
        "INDEX | one short reason it matters\n"
        "Use the number from the list. No other text, no preamble, no bullets. "
        "If none of them are important, reply with exactly: NONE"
    )

    brain = Brain()
    try:
        await brain.connect()
        raw = await brain.run_turn(prompt)
    finally:
        await brain.disconnect()

    items = []
    for line in (raw or "").splitlines():
        line = line.strip().lstrip("-*• ").strip()
        if not line or line.upper().startswith("NONE"):
            continue
        head, sep, reason = line.partition("|")
        if not sep:
            continue
        try:
            idx = int(re.sub(r"\D", "", head))
        except ValueError:
            continue
        if 0 <= idx < len(messages):
            m = messages[idx]
            items.append({
                "from": m.get("from"), "subject": m.get("subject"), "date": m.get("date"),
                "account": m.get("account"), "reason": reason.strip()[:300],
            })

    core_email_triage.save(items, len(messages), error="; ".join(errors) or None)
    if not items:
        return f"Scanned {len(messages)} unread — nothing important."
    lines = [f"{len(items)} of {len(messages)} unread look important:"]
    for it in items:
        lines.append(f"- {it['from']} — {it['subject']} ({it['reason']})")
    return "\n".join(lines)


async def _build_audit_skills_prompt() -> str:
    skills = list_skills()
    if not skills:
        return "I have no saved Skills yet — just reply saying there's nothing to audit."
    lines = ["Review my saved Skills for staleness, duplication, or quality issues. Here they are in full:", ""]
    for s in skills:
        full = get_skill(s["slug"])
        if full:
            lines.append(f"### {full['slug']}\n{full['description']}\n{full['body']}\n")
    lines.append("Give me a short, plain summary of anything worth fixing — or say they all look fine.")
    return "\n".join(lines)


BUILTIN_TASKS = {
    "tidy_chats": {
        "label": "Tidy Empty Chats",
        "description": "Deletes chat sessions with zero messages that are more than 24h old. Never touches starred chats.",
        "kind": "action",
        "run": _run_tidy_chats,
        "default_interval_seconds": 24 * 60 * 60,
    },
    "tidy_calendar": {
        "label": "Clean Up Past Events",
        "description": "Removes calendar events that have already ended. Leaves Notes' due-dated items alone.",
        "kind": "action",
        "run": _run_tidy_calendar,
        "default_interval_seconds": 24 * 60 * 60,
    },
    "daily_brief": {
        "label": "Daily Brief",
        "description": "Formatted like The Bridge's daily briefing: today's calendar, unread email (flagging anything important), due-dated notes, and stale open items.",
        "kind": "llm",
        "build_prompt": _build_daily_brief_prompt,
        "default_interval_seconds": 24 * 60 * 60,
        # Anchored to the morning rather than a 24h interval, which would fire
        # at whatever time you happened to enable it and drift from there
        # (David, 2026-09-04). Local time.
        "default_daily_time": "06:00",
    },
    "sync_all": {
        "label": "Sync Everything",
        "description": "Refreshes every connected calendar feed, CalDAV/CardDAV source, and registered API (Canvas included) so nothing goes stale waiting to be synced by hand.",
        "kind": "action",
        "run": _run_sync_all,
        "default_interval_seconds": 24 * 60 * 60,
        # Deliberately before the Daily Brief's 06:00 so the brief reads data
        # refreshed minutes earlier, not yesterday's.
        "default_daily_time": "05:45",
    },
    "triage_email": {
        "label": "Triage Email",
        "description": "Scores the last day's unread mail and surfaces what actually matters on the Email tab — replies needed, deadlines, financial/security, real people.",
        "kind": "action",
        "run": _run_triage_email,
        "uses_model": True,
        "default_interval_seconds": 24 * 60 * 60,
        "default_daily_time": "05:50",
    },
    "audit_skills": {
        "label": "Audit Skills",
        "description": "Reviews your saved Skills for staleness, duplication, or quality issues.",
        "kind": "llm",
        "build_prompt": _build_audit_skills_prompt,
        "default_interval_seconds": 7 * 24 * 60 * 60,
    },
}


# Automations that should be running for everyone, switched on once at
# startup. "Daily so the user doesn't have to press it themselves" (David,
# 2026-09-06) is only true if they don't have to press Enable either.
AUTO_ENABLE = ("sync_all", "triage_email")


def autoenable_builtins() -> list[str]:
    """Turn on newly-shipped built-ins, once per install, never twice.

    Guarded by the auto_enabled_builtins setting rather than by "is there a
    task for this action" — otherwise disabling one would just bring it back
    on the next launch, which is worse than never enabling it at all.
    """
    from core.settings import get_setting, update_settings
    from services.task_service import task_service

    already = set(get_setting("auto_enabled_builtins") or [])
    existing_actions = {t.get("builtin_action") for t in task_service.list_tasks()}
    turned_on = []
    for action_id in AUTO_ENABLE:
        if action_id in already or action_id in existing_actions:
            continue
        defn = BUILTIN_TASKS.get(action_id)
        if defn is None:
            continue
        try:
            task_service.create_task(
                name=defn["label"],
                prompt=f"(built-in: {defn['label']})",
                schedule_kind="daily" if defn.get("default_daily_time") else "interval",
                run_time=defn.get("default_daily_time"),
                interval_seconds=None if defn.get("default_daily_time") else defn["default_interval_seconds"],
                builtin_action=action_id,
            )
            turned_on.append(action_id)
            logger.info("builtin_tasks: auto-enabled %r", defn["label"])
        except Exception:
            logger.exception("builtin_tasks: couldn't auto-enable %s", action_id)

    if turned_on:
        update_settings(auto_enabled_builtins=sorted(already | set(turned_on)))
    return turned_on


def migrate_builtin_schedules() -> int:
    """Move already-enabled built-ins onto a wall-clock schedule.

    Runs at startup. Only touches tasks that are (a) a built-in whose
    definition now carries a default_daily_time, and (b) still on a plain
    interval — so it converts the Daily Brief someone enabled last week, and
    leaves everything else, including anything already daily, alone. There is
    no UI for editing a task's schedule, so an interval here can only be the
    old default, never a deliberate choice.
    """
    from services.task_service import task_service  # local: avoids an import cycle

    migrated = 0
    for task in task_service.list_tasks():
        action_id = task.get("builtin_action")
        if not action_id or action_id not in BUILTIN_TASKS:
            continue
        daily_time = BUILTIN_TASKS[action_id].get("default_daily_time")
        if not daily_time or task.get("schedule_kind") != "interval":
            continue
        task_service.set_daily_schedule(task["id"], daily_time)
        migrated += 1
        logger.info("builtin_tasks: moved %r onto a daily %s schedule", task["name"], daily_time)
    return migrated


def list_builtin_tasks() -> list[dict]:
    return [
        {"action_id": k, "label": v["label"], "description": v["description"], "kind": v["kind"],
         "default_interval_seconds": v["default_interval_seconds"],
         "default_daily_time": v.get("default_daily_time"),
         # An "action" normally means no model call, but Triage Email is an
         # action that does call the model (it needs to control both what gets
         # stored and what gets reported). Explicit flag so the Tasks tab
         # can't mislabel it.
         "uses_model": v.get("uses_model", v["kind"] != "action")}
        for k, v in BUILTIN_TASKS.items()
    ]
