"""OAuth sign-in for MCP servers (Hermes track, MCP catalog stage B, 2026-09-23).

55 of the 65 catalog servers want OAuth 2.1 rather than an API key. The
protocol work - discovery, client registration, PKCE, the code exchange -
is the mcp package's OAuthClientProvider, the same class Hermes Agent's
tools/mcp_oauth*.py wraps. This module supplies the parts that are JARVIS's:

- Storage. Tokens and the client registration live encrypted on the
  integration item (core/secret_storage), like an API key.
- The callback. It lands on this backend's own loopback address
  (/api/integrations/oauth/callback), Hermes's dashboard-bridge pattern, so
  there is no second listener. The browser that brings it back has no app
  cookie; the unguessable `state` of a sign-in an admin started is what
  admits it, and a callback matching no pending sign-in does nothing.
- Refresh. refresh_due() renews tokens close to expiry before a chat
  connects or takes a turn, and core/integrations.py hands the current
  access token to both brains as a bearer header, the path API keys already
  use. A Claude chat's connection fingerprint includes that header, so a
  refreshed token reconnects the chat at its next turn boundary (resuming
  the same CLI session) instead of failing mid-chat on the old one.

Sign-in has to happen on the machine running JARVIS: the provider sends the
browser back to 127.0.0.1.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from core import integrations
from core.atomic_io import write_json_atomic
from core.secret_storage import decrypt, encrypt

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/integrations/oauth/callback"  # under local_api_base(), which ends in /api
SIGN_IN_TIMEOUT_SECONDS = 300
START_TIMEOUT_SECONDS = 30
REFRESH_MARGIN_SECONDS = 300
CLIENT_NAME = "JARVIS"


def _item(item_id: str) -> dict:
    item = integrations.get_integration(item_id)
    if item is None or item.get("kind") != "mcp_server":
        raise KeyError(f"no such MCP server: {item_id}")
    return item


def _save_oauth(item_id: str, oauth: Optional[dict]) -> None:
    data = integrations._load()
    if item_id not in data:
        return
    if oauth is None:
        data[item_id].pop("oauth", None)
    else:
        data[item_id]["oauth"] = oauth
    write_json_atomic(integrations.INTEGRATIONS_FILE, data)


def _oauth(item_id: str) -> dict:
    return dict(_item(item_id).get("oauth") or {})


def _secret(oauth: dict, key: str) -> Optional[dict]:
    raw = oauth.get(key)
    return json.loads(decrypt(raw)) if raw else None


class ItemTokenStorage:
    """The SDK's TokenStorage, kept on the integration item. A registration
    made for another callback address (the backend's port can differ between
    launches) reads as none, so the SDK registers again rather than fail on a
    provider that pins the exact redirect URI."""

    def __init__(self, item_id: str, redirect_uri: str):
        self.item_id, self.redirect_uri = item_id, redirect_uri

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        tokens = _secret(_oauth(self.item_id), "tokens")
        return OAuthToken.model_validate(tokens) if tokens else None

    async def set_tokens(self, tokens) -> None:
        oauth = _oauth(self.item_id)
        oauth["tokens"] = encrypt(tokens.model_dump_json(exclude_none=True))
        oauth["expires_at"] = time.time() + tokens.expires_in if tokens.expires_in else None
        oauth.pop("needs_sign_in", None)
        _save_oauth(self.item_id, oauth)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        oauth = _oauth(self.item_id)
        client = _secret(oauth, "client")
        if not client or oauth.get("redirect_uri") != self.redirect_uri:
            return None
        return OAuthClientInformationFull.model_validate(client)

    async def set_client_info(self, client_info) -> None:
        oauth = _oauth(self.item_id)
        oauth["client"] = encrypt(client_info.model_dump_json(exclude_none=True))
        oauth["redirect_uri"] = self.redirect_uri
        _save_oauth(self.item_id, oauth)


@dataclass
class _SignIn:
    item_id: str
    code: asyncio.Future
    task: Optional[asyncio.Task] = None
    started: float = field(default_factory=time.time)


# state -> the sign-in waiting for the browser to come back.
_pending: dict[str, _SignIn] = {}
# item id -> why its last sign-in failed, for the status the UI polls.
_last_error: dict[str, str] = {}
_refresh_lock = asyncio.Lock()


def _client_metadata(redirect_uri: str):
    from mcp.shared.auth import OAuthClientMetadata
    return OAuthClientMetadata(client_name=CLIENT_NAME, redirect_uris=[redirect_uri],
                               grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                               token_endpoint_auth_method="none")


async def _connect_signed_in(url: str, provider) -> None:
    """Open the MCP server with the provider as its auth: the server's 401
    starts the SDK's flow, and a successful tool listing proves the token."""
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client
    async with httpx2.AsyncClient(auth=provider, timeout=30) as http:
        async with Client(streamable_http_client(url, http_client=http), read_timeout_seconds=30) as client:
            await client.list_tools()


def _remember_endpoints(item_id: str, context) -> None:
    """What a refresh needs later, when there is no provider object: the
    token endpoint and the RFC 8707 resource the tokens were issued for."""
    oauth = _oauth(item_id)
    if context.oauth_metadata and context.oauth_metadata.token_endpoint:
        oauth["token_endpoint"] = str(context.oauth_metadata.token_endpoint)
    oauth["resource"] = (context.get_resource_url()
                         if context.should_include_resource_param(context.protocol_version) else None)
    _save_oauth(item_id, oauth)


async def start_sign_in(item_id: str, redirect_uri: str) -> dict:
    """Begin signing in to one MCP server. Returns {"url": ...} for the
    browser, or {"signed_in": True} when stored tokens already work."""
    from mcp.client.auth import OAuthClientProvider
    item = _item(item_id)
    if item.get("mcp_type") != "http" or not item.get("url"):
        raise ValueError("only an http MCP server can be signed in to")
    if item.get("auth") != "oauth":
        data = integrations._load()
        data[item_id]["auth"] = "oauth"
        write_json_atomic(integrations.INTEGRATIONS_FILE, data)
    _last_error.pop(item_id, None)
    for state, pending in list(_pending.items()):
        if pending.item_id == item_id:  # a new attempt replaces an abandoned one
            _pending.pop(state, None)
            if pending.task:
                pending.task.cancel()

    loop = asyncio.get_running_loop()
    url_ready: asyncio.Future = loop.create_future()
    sign_in = _SignIn(item_id=item_id, code=loop.create_future())

    async def redirect_handler(authorization_url: str) -> None:
        if not url_ready.done():
            url_ready.set_result(authorization_url)

    async def callback_handler():
        return await sign_in.code

    provider = OAuthClientProvider(server_url=item["url"], client_metadata=_client_metadata(redirect_uri),
                                   storage=ItemTokenStorage(item_id, redirect_uri),
                                   redirect_handler=redirect_handler, callback_handler=callback_handler)

    async def run() -> None:
        try:
            await asyncio.wait_for(_connect_signed_in(item["url"], provider), SIGN_IN_TIMEOUT_SECONDS)
            _remember_endpoints(item_id, provider.context)
            logger.info("signed in to MCP server %s", item["name"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _last_error[item_id] = _describe(e)
            logger.warning("MCP sign-in to %s failed: %s", item["name"], _last_error[item_id])
            raise
        finally:
            for state, pending in list(_pending.items()):
                if pending is sign_in:
                    _pending.pop(state, None)

    sign_in.task = asyncio.create_task(run())
    done, _ = await asyncio.wait({url_ready, sign_in.task}, timeout=START_TIMEOUT_SECONDS,
                                 return_when=asyncio.FIRST_COMPLETED)
    if url_ready in done:
        state = (parse_qs(urlparse(url_ready.result()).query).get("state") or [""])[0]
        if not state:
            sign_in.task.cancel()
            raise ValueError("the server's sign-in address carried no state")
        _pending[state] = sign_in
        return {"url": url_ready.result()}
    if sign_in.task in done:
        error = sign_in.task.exception()
        if error is None:
            return {"signed_in": True}
        raise ValueError(_describe(error))
    sign_in.task.cancel()
    raise ValueError("the server did not answer the sign-in request in time")


def _describe(error: BaseException) -> str:
    # anyio wraps task-group failures; the first real cause is the useful part.
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return (str(error) or type(error).__name__)[:300]


async def finish_sign_in(state: str, code: Optional[str], iss: Optional[str], error: Optional[str]) -> tuple[bool, str]:
    """The browser came back. Returns (ok, message) for the page it lands on."""
    from mcp.shared.auth import AuthorizationCodeResult
    sign_in = _pending.pop(state or "", None)
    if sign_in is None:
        return False, "This sign-in link is not one JARVIS is waiting for. Start again from Settings."
    name = (integrations.get_integration(sign_in.item_id) or {}).get("name", "the server")
    if error or not code:
        _last_error[sign_in.item_id] = f"the provider refused: {error or 'no code returned'}"
        sign_in.task.cancel()
        return False, f"{name} did not grant access ({error or 'no code returned'})."
    sign_in.code.set_result(AuthorizationCodeResult(code=code, state=state, iss=iss))
    try:
        await asyncio.wait_for(asyncio.shield(sign_in.task), 60)
    except Exception:
        return False, f"Signing in to {name} failed: {_last_error.get(sign_in.item_id, 'no answer from the server')}"
    return True, f"Signed in to {name}. You can close this page and go back to JARVIS."


def status(item_id: str) -> dict:
    oauth = _oauth(item_id)
    return {
        "signed_in": bool(oauth.get("tokens")),
        "pending": any(p.item_id == item_id for p in _pending.values()),
        "error": _last_error.get(item_id) or ("sign-in expired; sign in again" if oauth.get("needs_sign_in") else None),
    }


def sign_out(item_id: str) -> None:
    """Forget the tokens and the registration. The provider may still list
    JARVIS as a connected app until it is removed on the provider's side."""
    _item(item_id)
    _save_oauth(item_id, None)
    _last_error.pop(item_id, None)


def _token_request(oauth: dict, tokens: dict) -> tuple[dict, dict]:
    client = _secret(oauth, "client") or {}
    data = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client.get("client_id", "")}
    if oauth.get("resource"):
        data["resource"] = oauth["resource"]
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    method, secret = client.get("token_endpoint_auth_method"), client.get("client_secret")
    if method == "client_secret_basic" and secret:
        import base64
        from urllib.parse import quote
        pair = f"{quote(client['client_id'], safe='')}:{quote(secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    elif method == "client_secret_post" and secret:
        data["client_secret"] = secret
    return data, headers


async def _refresh(item_id: str, oauth: dict) -> None:
    import httpx2
    tokens = _secret(oauth, "tokens") or {}
    data, headers = _token_request(oauth, tokens)
    async with httpx2.AsyncClient(timeout=20) as http:
        response = await http.post(oauth["token_endpoint"], data=data, headers=headers)
    if response.status_code in (400, 401):
        # invalid_grant and friends: the refresh token is dead. Keep the
        # registration, drop the tokens, and say so in Settings.
        fresh = _oauth(item_id)
        fresh.pop("tokens", None)
        fresh["needs_sign_in"] = True
        _save_oauth(item_id, fresh)
        logger.warning("MCP token refresh for %s refused (%s); sign-in needed", item_id, response.status_code)
        return
    response.raise_for_status()
    new = response.json()
    # RFC 6749 section 6: an omitted refresh token or scope is unchanged.
    new.setdefault("refresh_token", tokens.get("refresh_token"))
    new.setdefault("scope", tokens.get("scope"))
    fresh = _oauth(item_id)
    fresh["tokens"] = encrypt(json.dumps({k: v for k, v in new.items() if v is not None}))
    fresh["expires_at"] = time.time() + new["expires_in"] if new.get("expires_in") else None
    _save_oauth(item_id, fresh)
    logger.info("refreshed MCP token for %s", item_id)


async def refresh_due(only_ids: Optional[list[str]] = None, margin: float = REFRESH_MARGIN_SECONDS) -> None:
    """Renew every signed-in server's token that expires within `margin`
    seconds. A failure is logged, never raised: the chat still starts, and
    that one server is reported unreachable as any down server is."""
    async with _refresh_lock:
        for item in integrations._load().values():
            oauth = item.get("oauth") or {}
            if item.get("kind") != "mcp_server" or item.get("auth") != "oauth" or not oauth.get("tokens"):
                continue
            if only_ids is not None and item["id"] not in only_ids:
                continue
            expires_at = oauth.get("expires_at")
            if not expires_at or expires_at - time.time() > margin or not oauth.get("token_endpoint"):
                continue
            if not (_secret(oauth, "tokens") or {}).get("refresh_token"):
                continue
            try:
                await _refresh(item["id"], oauth)
            except Exception as e:
                logger.warning("MCP token refresh for %s failed: %s", item["name"], _describe(e))


def access_token(item: dict) -> Optional[str]:
    """The stored access token for core/integrations.py's runtime config."""
    tokens = _secret(item.get("oauth") or {}, "tokens")
    return (tokens or {}).get("access_token")
