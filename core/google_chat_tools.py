"""Google Workspace chat actions shared by Claude, API/local models, and Codex."""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from core import google_workspace as google, permissions
from core.tool_registry import ToolContext


READS = {
    "drive": {"list", "info", "permissions", "revisions"},
    "sheets": {"get", "values"},
    "forms": {"get", "responses"},
}


async def execute(area: str, args: dict, ctx: ToolContext) -> str:
    if not ctx.is_admin:
        return "Google Workspace is available only in admin chats."
    action = args.get("action")
    allowed = READS.get(area, set()) | {"drive": google_drive_actions, "sheets": google_sheet_actions,
                                       "forms": google_form_actions}.get(area, frozenset())
    if action not in allowed:
        return "Unknown Google Workspace action."
    if action not in READS.get(area, set()):
        decision = await permissions.decide(
            surface=f"chat:{ctx.session_id}" if ctx.session_id else "none",
            tool=f"google_{area}_{action}", arguments=args,
            title=f"Google {area.title()}: {action}",
            description="This changes the connected Google account.", is_admin=True,
            force_prompt=bool(ctx.turn_taint and ctx.turn_taint.tainted),
        )
        if decision.behavior != "allow":
            return f"Not run: {decision.reason or 'not approved'}"
    if area == "drive":
        if action == "list":
            result = await google.list_files(query=args.get("query", ""), parent=args.get("parent"),
                                             kind=args.get("kind", "all"), trashed=bool(args.get("trashed")),
                                             page_token=args.get("page_token"))
        elif action == "info":
            result = await google.file_info(args.get("file_id") or "")
        elif action == "upload":
            if not args.get("local_path"):
                return "Choose a local_path to upload."
            path = Path(args["local_path"]).expanduser().resolve(strict=True)
            if not path.is_file() or path.stat().st_size > google.MAX_DOWNLOAD:
                return "Choose an existing file under 25 MB."
            result = await google.upload_file(path.name, path.read_bytes(),
                                              mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                                              args.get("parent"))
        elif action in google_drive_actions:
            result = await google.drive_action(action, file_id=args.get("file_id"), name=args.get("name"),
                                               parent=args.get("parent"), permission_id=args.get("permission_id"),
                                               email=args.get("email"), role=args.get("role"),
                                               permission_type=args.get("permission_type"), domain=args.get("domain"))
        else:
            return "Unknown Google Drive action."
    elif area == "sheets":
        if action in ("get", "values"):
            result = await google.sheet_get(args.get("file_id") or "", args.get("cell_range") if action == "values" else None)
        elif action in google_sheet_actions:
            result = await google.sheet_action(action, file_id=args.get("file_id"), title=args.get("title"),
                                               cell_range=args.get("cell_range"), values=args.get("values"),
                                               requests=args.get("requests"))
        else:
            return "Unknown Google Sheets action."
    elif area == "forms":
        if action in ("get", "responses"):
            result = await google.form_get(args.get("file_id") or "", responses=action == "responses",
                                           page_token=args.get("page_token"))
        elif action in google_form_actions:
            result = await google.form_action(action, file_id=args.get("file_id"), title=args.get("title"),
                                              requests=args.get("requests"), published=args.get("published"))
        else:
            return "Unknown Google Forms action."
    else:
        return "Unknown Google Workspace area."
    if action in READS[area] and ctx.turn_taint:
        ctx.turn_taint.mark("Google Workspace content")
    rendered = json.dumps(result, ensure_ascii=False)
    return rendered[:24000] + ("\n[Result truncated; narrow the request]" if len(rendered) > 24000 else "")


google_drive_actions = frozenset({"create_folder", "rename", "move", "copy", "trash", "restore", "star", "unstar", "delete", "permissions", "share", "update_permission", "revoke", "revisions", "upload"})
google_sheet_actions = frozenset({"create", "update", "append", "clear", "batch"})
google_form_actions = frozenset({"create", "batch", "publish"})
