# CRM prebuilt tab

CRM is offered in the New Tab gallery for every installation. Its source ships in the application; personal data stays in `DATA_DIR/crm.json`. Fresh installations have no sources, model selection, sample tasks or enabled scanning. Removing the tab disables it and preserves records.

The implementation follows the saved `build-custom-tab` skill: `routes/tab_crm.py` exports its router and manifest, `static/js/views/crm.js` exports `render`, and `services/crm_service.py` owns an atomic JSON store. No changes to `app.py`, `app.js` or the shared icon registry are needed. The stylesheet uses shared appearance tokens.

## Source collection and extraction

An administrator selects existing Email accounts, Slack/Telegram/email connectors, Discord bots or Library documents in Sources. Messaging sources capture newly admitted messages only, optionally limited to conversation IDs. Existing connector admission rules still apply. Historical messaging imports, Gmail Pub/Sub and attachment OCR are outside this implementation.

The dedicated mailbox reader includes read and unread messages in the selected folder and lookback. It uses read-only IMAP selection, UID search/fetch, UIDVALIDITY identity, a size check and BODY.PEEK. MIME bodies become plain text; attachments and embedded content are not executed. A bounded set of earlier references supplies thread context. Generic IMAP messages open their captured source in CRM because IMAP supplies no universal web permalink. Slack uses `chat.getPermalink`, Telegram public messages and Discord use native URLs when available. URLs and source identities come from adapters, never the model.

Scans durably queue messages before extraction, then commit tasks and the processed marker together. Retries are capped at three until a person retries failed messages. Per-owner background jobs prevent overlapping scans; failures remain visible by source. Automatic scans run only while Kairos is running and CRM is enabled. Pausing a source or disabling CRM during extraction prevents publication and retains the queue. Administrator access is checked again before publication.

Extraction uses a selected local/API model through Kairos's existing transport, with no tools or executor. Claude CLI uses an empty tool set, strict empty MCP configuration, temporary working directory, empty settings/skills/plugins and no session persistence. Codex CLI is excluded from unattended extraction until an equivalent tool-free boundary exists. Source text is wrapped as untrusted data. Output is strictly validated, including exact evidence and deadline quotes and task matches restricted to the same owner/source/thread. Local input is bounded against its configured context size; truncated or ambiguous results require review.

Relative deadlines use the message timestamp and configured IANA timezone. No deadline quote means no deadline. Date-only precision is preserved. Windows timezone data is supplied by the hash-pinned `tzdata` dependency. Quotes from earlier context, inferred dates or incomplete context require review. Model classifications still need user judgment; exact quote validation establishes provenance, not semantic accuracy.

## Follow-ups and review

Tasks have deadlines, priority explanations, contact/project fields, source evidence, status, notes and optional snoozing. Contacts groups follow-ups by their recorded contact; it does not merge identities across providers. Later messages can propose changes; they do not overwrite edits or reopen completed/dismissed work. Suggested changes can be accepted or rejected. Users can manage manual tasks without connection-admin access.

Active, reviewed CRM deadlines appear in Calendar by reading the owned CRM records directly. Calendar completion updates CRM status; completed work remains in CRM's Done view. Assignment creates a backlog card in the existing Work Board and requires a separate Ready/Run action.

## Validation performed locally

- `python scripts/test_crm.py`: 28 isolated checks cover ownership, atomic rollback, deadlines, exact evidence, deduplication, proposals, bounded input, read-only UID collection, selected conversation scopes, failures/retries, asynchronous scan status, pause during extraction, shipped-tab visibility and tool-free model options.
- `python scripts/test_connectors.py`: 24 connector checks pass, including admission before CRM capture and Slack's returned permalink.
- Existing tool and integration checks pass; `python scripts/test_chat.py DependencyPinningTests CustomTabApprovalTests` passes all five targeted checks.
- `node scripts/run-electron-test.cjs crm-smoke`: actual renderer with isolated synthetic API, desktop/mobile layouts, manual editing, completion/reopening, settings, contacts, empty states and source text safety. External requests are rejected by the harness.
- A temporary live scan against local `llama3.2:latest` extracted a synthetic proposal request, resolved Friday 3pm to `2026-10-09T15:00:00-04:00`, retained its exact quote and created no duplicate on a repeated scan. Temporary records were removed after the check. Real mailbox ingestion and native provider links have not been exercised against connected accounts.

`Kairos (crm)` is a separate local review profile at `%APPDATA%/JARVIS-crm`, port 8431, using this checkout. It starts with CRM enabled, a local extraction connection, review-all enabled and automatic scanning off. Add an account in Email or Settings > Channels, or a document in Library, then select it under CRM > Sources. The installed app and existing dev profile are separate.

## Harness references

These patterns informed the implementation; no external harness code is required at runtime.

- [Hermes email inbox triage](https://github.com/NousResearch/hermes-agent/blob/main/skills/email/email-inbox-triage/SKILL.md): thread context and actionable dispositions informed evidence-based follow-ups and priority explanations.
- [Hermes email gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/email/): transport metadata informed source identity and threaded evidence, while personal mailbox scans use a separate reader.
- [Hermes scheduled tasks](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/): deterministic collection is separated from model reasoning and scan history is separated from extracted work.
- OpenClaw's email automation was examined as a reference for stable dispatch identity and treating incoming mail as untrusted input. Provider-specific Gmail push ingestion remains future work.
