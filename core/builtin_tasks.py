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
import time
from datetime import datetime, timedelta, timezone

from core.session_manager import session_manager
from core.untrusted import wrap_untrusted
from services.calendar_service import calendar_service
from services.email_service import email_service
from services.notes_service import notes_service
from services.skills_service import list_skills, get_skill

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


async def _build_daily_brief_prompt() -> str:
    """Formatted to match The Bridge's own daily briefing (voice-line's
    discord_bot.py, 7am Discord post): bold one-line greeting, then bold
    section labels Calendar/Emails/Priorities/Stale, each a couple tight
    bullets, skipping a section entirely when there's nothing in it."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    events = calendar_service.list_range(today_start.isoformat(), today_end.isoformat())

    all_open_notes = notes_service.list_notes(include_completed=False)
    due_notes = [n for n in all_open_notes if n.get("due_date")]
    stale_cutoff = time.time() - 14 * 24 * 60 * 60
    stale_notes = sorted(
        (n for n in all_open_notes if n["created_at"] < stale_cutoff),
        key=lambda n: n["created_at"],
    )[:3]

    lines = ["Give me a short daily brief using exactly this real data (don't invent anything beyond it):", ""]

    lines.append(f"Today's calendar events ({len(events)}):")
    for e in events:
        lines.append(f"- {e['title']} at {e['start']}")
    if not events:
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

    lines.append(f"Stale open notes, 14+ days old, oldest first ({len(stale_notes)} shown):")
    for n in stale_notes:
        age_days = int((time.time() - n["created_at"]) / 86400)
        lines.append(f"- {n['text']} ({age_days}d old)")
    if not stale_notes:
        lines.append("- (none)")
    lines.append("")

    lines.append(
        "Write it up as a Discord message using Discord's own markdown: a short bold one-line "
        "greeting, then bold section labels (**Calendar**, **Emails**, **Priorities**, **Stale**) "
        "each followed by one or two tight bullet points, skipping a section entirely if there's "
        "nothing to report for it. For Emails, just roll up the count unless something looks "
        "important (needs a reply, has a deadline, is financial/account-related, or is from "
        "someone I clearly know), call those out by sender and subject with a one-line reason. "
        "Keep it tight, this is a quick morning glance, not a full report."
    )
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
    },
    "audit_skills": {
        "label": "Audit Skills",
        "description": "Reviews your saved Skills for staleness, duplication, or quality issues.",
        "kind": "llm",
        "build_prompt": _build_audit_skills_prompt,
        "default_interval_seconds": 7 * 24 * 60 * 60,
    },
}


def list_builtin_tasks() -> list[dict]:
    return [
        {"action_id": k, "label": v["label"], "description": v["description"], "kind": v["kind"], "default_interval_seconds": v["default_interval_seconds"]}
        for k, v in BUILTIN_TASKS.items()
    ]
