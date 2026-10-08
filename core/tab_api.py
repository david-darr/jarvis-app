"""Stable tab contract, v1.

Tabs import Kairos through this module and bind a handle with
``api = tab_api.for_tab(__package__)``. v1 offers auth dependencies, confined
atomic JSON storage and migration, encryption of the tab's own values,
untrusted-text wrapping, tool-free models, read-only mail, Library and
connection metadata, backlog cards, persistent chats and daily sync.
It grants no access to Kairos's stored credentials. This is an interface,
not a Python sandbox: approved source still runs with the app's privileges.
Encryption authenticates a kairos-tab:<slug>: prefix inside each token;
only core's adoption of a tab-owned legacy JSON file converts old app-key
tokens, including stores moved by earlier development builds.

API_VERSION 1 keeps working across updates. New capabilities are additions,
or a v2 offered alongside v1; existing v1 signatures and behavior stay valid.
"""
import asyncio
import imaplib
import os
import re
import tempfile
from contextlib import contextmanager

from core import atomic_io, model_endpoints, secret_storage, tab_folders, token_usage
from core.constants import DATA_DIR
from core.middleware import require_user, require_admin
from core.mail_utils import message_time
from core.untrusted import wrap_untrusted as _wrap_untrusted

API_VERSION = 1
# A tab with a name colliding with an app store must not adopt credentials.
_APP_STORES = {"auth.json", "sessions.json", "password_resets.json", "settings.json",
    "model_endpoints.json", "email_accounts.json", "connectors.json", "discord_bots.json",
    "integrations.json", "google_workspace.json"}


def wrap_untrusted(label, text):
    return _wrap_untrusted(label, text)


def is_admin(username):
    from core.auth import auth_manager
    return auth_manager.is_admin(username)


def list_models():
    return [{k: e.get(k) for k in ("id", "name", "kind", "model")}
            for e in model_endpoints.list_endpoints()]


def model_context_size(endpoint_id):
    """Configured input window, used to bound source text before completion."""
    endpoint = model_endpoints.get_endpoint(endpoint_id) or {}
    return (endpoint.get("num_ctx") or 16384) if endpoint.get("kind") == "local" else None


_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,127}")


async def model_choices(endpoint_id):
    """Connection-scoped picker metadata, never credentials or provider URLs."""
    from core import model_discovery
    endpoint = model_endpoints.get_endpoint(endpoint_id)
    if endpoint is None:
        raise ValueError("Choose a completion model")
    return [{"id": row["id"], "name": row.get("display_name") or row["id"]}
            for row in await model_discovery.list_for_endpoint(endpoint)]


async def complete(endpoint_id, system, prompt, timeout=180, model=None):
    async def run():
        endpoint = model_endpoints.get_endpoint(endpoint_id)
        if endpoint is None:
            raise ValueError("Choose a completion model")
        if endpoint["kind"] == "codex_cli":
            raise ValueError("Select Claude CLI, a local model or an API model for tool-free completion")
        if endpoint["kind"] == "claude_cli":
            # Callers check membership against model_choices() when the choice
            # is saved; re-listing here would fail every call while the live
            # catalog is unreachable. Only the id's shape is checked per call.
            if model is not None and not _MODEL_ID.fullmatch(model):
                raise ValueError("Choose a model this connection offers")
            from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
            from core import claude_cli
            directory = tempfile.TemporaryDirectory(prefix="kairos-tab-reader-", ignore_cleanup_errors=True)
            try:
                options = ClaudeAgentOptions(tools=[], mcp_servers={}, strict_mcp_config=True,
                    setting_sources=[], skills=[], plugins=[], cwd=directory.name, max_turns=1,
                    cli_path=claude_cli.preferred_cli_path(),
                    model=model if model is not None else endpoint.get("model") or None, system_prompt=system,
                    extra_args={"no-session-persistence": None, "disable-slash-commands": None})
                result = None
                async for message in query(prompt=prompt, options=options):
                    if isinstance(message, ResultMessage):
                        result = message
                        if message.usage:
                            token_usage.record_usage(endpoint_id, message.usage)
                # Drain the stream so the CLI releases its Windows working directory.
                if result is None:
                    raise ValueError("The completion model did not return a result")
                if result.is_error:
                    raise ValueError("The completion model failed")
                return result.result or ""
            finally:
                try:
                    directory.cleanup()
                except OSError:
                    # A transient filesystem lock must not discard the model result.
                    pass
        if model is not None and model != endpoint.get("model"):
            raise ValueError("Choose a model this connection offers")
        from core.providers.openai_compatible import run_turn
        base, configured_model, api_key, num_ctx = model_endpoints.resolve_runtime(endpoint_id)
        def usage(value):
            token_usage.record_usage(endpoint_id, value)
        return await run_turn(base, configured_model, api_key,
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            tools=None, tool_executor=None, num_ctx=num_ctx, on_usage=usage)
    return await asyncio.wait_for(run(), timeout)


_HEADER_UNSAFE = re.compile(r"[\r\n\x00]")


def send_email(user, account_id, to, subject, body, in_reply_to=None):
    """Send one plain-text email from a connected account, for an admin's
    explicit action only (a button the person pressed, after confirming the
    recipient). Never call it from background work or on a model's say."""
    from email.utils import parseaddr
    from services.email_service import email_service
    if not is_admin(user):
        raise PermissionError("Only an admin can send email")
    if not any(a.get("id") == account_id for a in email_accounts()):
        raise ValueError("The email account is not connected")
    for value in (to, subject, in_reply_to or ""):
        if not isinstance(value, str) or _HEADER_UNSAFE.search(value):
            raise ValueError("Invalid email header")
    address = parseaddr(to)[1]
    if "@" not in address or address != to.strip():
        raise ValueError("Enter one recipient address")
    if not body.strip() or len(body) > 50_000:
        raise ValueError("The message must be 1 to 50,000 characters")
    email_service.send_message(account_id, address, subject[:300], body, in_reply_to=in_reply_to or None)


def email_accounts():
    from services.email_service import email_service
    return [{k: a.get(k) for k in ("id", "email", "name")} for a in email_service.list_accounts()]


class _ReadOnlyMailbox:
    __slots__ = ("__box",)

    def __init__(self, box):
        self.__box = box

    def uid(self, command, *args):
        command = command.lower()
        for arg in args:
            if isinstance(arg, (str, bytes)):
                controls = ("\r", "\n", "\x00") if isinstance(arg, str) else (b"\r", b"\n", b"\x00")
                if any(c in arg for c in controls):
                    raise ValueError("Invalid IMAP argument")
        if command == "fetch":
            if len(args) != 2 or not isinstance(args[1], str):
                raise ValueError("Only read-only message fetches are allowed")
            items = args[1].upper().strip()
            if items.startswith("(") and items.endswith(")"):
                items = items[1:-1].strip()
            if not items.split() or any(i not in ("RFC822.SIZE", "BODY.PEEK[]") for i in items.split()):
                raise ValueError("Only RFC822.SIZE and BODY.PEEK[] may be fetched")
        elif command != "search":
            raise ValueError("Only UID search and read-only fetch are allowed")
        return self.__box.uid(command, *args)

    def response(self, name):
        if name.upper() != "UIDVALIDITY":
            raise ValueError("Only UIDVALIDITY is exposed")
        return self.__box.response("UIDVALIDITY")


@contextmanager
def open_mailbox(account_id, folder):
    from services.email_service import email_service
    if not isinstance(folder, str) or any(c in folder for c in "\r\n\x00") or len(folder) > 200:
        raise ValueError("Invalid mailbox folder")
    account = email_service.get_account(account_id, decrypted=True)
    if account is None:
        raise ValueError("The connected email account was removed")
    with imaplib.IMAP4_SSL(account["imap_host"], account["imap_port"], timeout=20) as box:
        box.login(account["email"], secret_storage.decrypt(account["password_encrypted"]))
        quoted = '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'
        status, _ = box.select(quoted, readonly=True)
        if status != "OK":
            raise ValueError("The mailbox folder could not be opened")
        yield _ReadOnlyMailbox(box)


def message_connections():
    from core.connectors import store
    from core import discord_bots_store
    choices = [{"kind": "connector", "id": c["id"], "label": c["name"] + " · " + c["kind"]}
               for c in store.list_records() if c["kind"] in ("slack", "telegram", "email")]
    return choices + [{"kind": "discord", "id": b["id"], "label": b["name"] + " · Discord"}
                      for b in discord_bots_store.list_bots()]


def documents():
    from services.documents_service import list_documents
    return [{k: d[k] for k in ("id", "title")} for d in list_documents()]


def document(document_id):
    from services.documents_service import get_document
    return get_document(document_id)


def backlog_card(card_id):
    from services.task_service import task_service
    return task_service.get_task(card_id)


def create_backlog_card(title, description, agent_id):
    from services.task_service import task_service
    return task_service.create_task(title, description, "card", status="backlog", agent_id=agent_id)


def delete_backlog_card(card_id):
    from services.task_service import task_service
    task_service.delete_task(card_id)


class TabAPI:
    def __init__(self, slug, entry):
        self.slug, self._entry = slug, entry

    @property
    def is_on(self):
        from core.custom_tabs import _folder_allowed
        return _folder_allowed(self._entry)

    @property
    def data_dir(self):
        parent = os.path.join(DATA_DIR, "tab-data")
        directory = os.path.join(parent, self.slug)
        if tab_folders.is_link(parent) or tab_folders.is_link(directory):
            raise ValueError("Tab data directories cannot be links")
        os.makedirs(directory, exist_ok=True)
        return directory

    def _file(self, name):
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name)
                or name.endswith((".", " "))
                or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I)):
            raise ValueError("Use a plain data filename")
        path = os.path.join(self.data_dir, name)
        if tab_folders.is_link(path):
            raise ValueError("Tab data files cannot be links")
        return path

    def read_json(self, name, default):
        return atomic_io.read_json(self._file(name), default)

    def write_json(self, name, value):
        atomic_io.write_json_atomic(self._file(name), value)

    def adopt_data_file(self, old_name):
        # Migration is restricted to the tab's former store, never app secrets.
        if old_name != self.slug + ".json" or old_name in _APP_STORES:
            raise ValueError("Only the tab's own legacy data file may be adopted")
        target = self._file(old_name)
        source = os.path.join(DATA_DIR, old_name)
        if tab_folders.is_link(source):
            raise ValueError("Legacy data files cannot be links")
        if os.path.isfile(source) and not os.path.exists(target):
            value = atomic_io.read_json(source, None)
            atomic_io.write_json_atomic(target, self._namespace_legacy_values(value))
            os.remove(source)
        elif os.path.isfile(target):
            # A2 dev copies may already have adopted app-key tokens. Walk the
            # owned store again: conversion is idempotent and never lenient
            # through the public decrypt interface.
            value = atomic_io.read_json(target, None)
            converted = self._namespace_legacy_values(value)
            if converted != value:
                atomic_io.write_json_atomic(target, converted)

    def _namespace_legacy_values(self, value):
        if isinstance(value, dict):
            return {k: self._namespace_legacy_values(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._namespace_legacy_values(v) for v in value]
        if isinstance(value, str):
            from cryptography.fernet import InvalidToken
            try:
                plain = secret_storage.decrypt(value)
            except (InvalidToken, ValueError, UnicodeError):
                return value
            # Already namespaced tokens (including other tabs) stay untouched.
            if not plain.startswith("kairos-tab:"):
                return self.encrypt(plain)
        return value

    def encrypt(self, text):
        # The authenticated ciphertext includes its owner; app-key tokens and
        # other tabs' ciphertext can never pass this handle's decrypt check.
        return secret_storage.encrypt(f"kairos-tab:{self.slug}:" + text)

    def decrypt(self, token):
        plain = secret_storage.decrypt(token)
        prefix = f"kairos-tab:{self.slug}:"
        if not plain.startswith(prefix):
            raise ValueError("Encrypted value does not belong to this tab")
        return plain[len(prefix):]

    def chat_session(self, key, title, untrusted=None, model_endpoint_id=None):
        """Get/create this tab's persistent chat for ``key``.

        ``untrusted``: a short label for outside text the tab places in the
        chat (e.g. "CRM source messages"). The chat is then marked so every
        turn treats its history as untrusted and shell commands ask first.
        ``model_endpoint_id`` picks the model for a newly created chat only.
        """
        from core.session_manager import session_manager
        if model_endpoint_id is not None and model_endpoints.get_endpoint(model_endpoint_id) is None:
            model_endpoint_id = None
        # Preserve the original slug:key identity of existing tab chats.
        session_id = session_manager.get_or_create_channel_session(f"{self.slug}:{key}", title, model_endpoint_id)
        if untrusted:
            session_manager.set_untrusted_context(session_id, f"{self.slug} tab: {str(untrusted)[:80]}")
        return session_id

    def append_chat_message(self, session_id, role, text):
        from core.session_manager import session_manager
        session_manager.append_message(session_id, role, text)

    def register_sync(self, name, fn):
        from core import sync_engine
        async def sync():
            if self.is_on:
                return await fn()
        sync_engine.register_provider(name, sync)
        _sync_names.setdefault(self.slug, set()).add(name)


_sync_names = {}


def unregister_sync(slug):
    from core import sync_engine
    for name in _sync_names.pop(slug, set()):
        sync_engine.unregister_provider(name)


def for_tab(package):
    parts = package.split(".") if isinstance(package, str) else []
    if len(parts) < 2 or parts[0] != "kairos_tabs" or parts[1] not in tab_folders._registered:
        raise ValueError("A registered tab package is required")
    entry = tab_folders._registered[parts[1]]
    if not tab_folders._allowed(entry):
        raise ValueError("The tab is not approved or enabled")
    return TabAPI(parts[1], entry)

