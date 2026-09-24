"""An OAuth-protected MCP server for scripts/test_chat.py's sign-in tests.

Built on the mcp package's own authorization server, so discovery, client
registration, PKCE and the token endpoint are the real protocol, not a
hand-rolled imitation. Consent is automatic: /authorize redirects straight
back with a code, standing in for the person clicking "Allow".

    python scripts/mcp_oauth_fixture.py PORT

MCP_OAUTH_TOKEN_TTL sets the access-token lifetime in seconds (default 3600),
so a test can make a token expire. One tool, `whoami`, answers "OK".
"""
import os
import secrets
import sys
import time

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken, construct_redirect_uri
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthToken

PORT = int(sys.argv[1])
BASE = f"http://127.0.0.1:{PORT}"
TTL = int(os.environ.get("MCP_OAUTH_TOKEN_TTL", "3600"))


class Provider:
    def __init__(self):
        self.clients, self.codes, self.refresh, self.access = {}, {}, {}, {}

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def register_client(self, client_info):
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client, params):
        code = secrets.token_urlsafe(24)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + 300, client_id=client.client_id,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client, authorization_code):
        return self.codes.get(authorization_code)

    def _issue(self, client_id, scopes, resource):
        access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self.access[access] = AccessToken(token=access, client_id=client_id, scopes=scopes,
                                          expires_at=int(time.time()) + TTL, resource=resource)
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes)
        return OAuthToken(access_token=access, expires_in=TTL, refresh_token=refresh,
                          scope=" ".join(scopes) or None)

    async def exchange_authorization_code(self, client, authorization_code):
        self.codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token):
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self.refresh.pop(refresh_token.token, None)  # rotate, as many real servers do
        return self._issue(client.client_id, scopes or refresh_token.scopes, None)

    async def load_access_token(self, token):
        found = self.access.get(token)
        if found and found.expires_at and found.expires_at < time.time():
            return None
        return found

    async def revoke_token(self, token):
        self.access.pop(getattr(token, "token", ""), None)
        self.refresh.pop(getattr(token, "token", ""), None)


server = MCPServer("oauth-fixture", auth_server_provider=Provider(), auth=AuthSettings(
    issuer_url=BASE, resource_server_url=f"{BASE}/mcp",
    client_registration_options=ClientRegistrationOptions(enabled=True)))


@server.tool()
def whoami() -> str:
    """Say that the call got through."""
    return "OK"


if __name__ == "__main__":
    server.run("streamable-http", host="127.0.0.1", port=PORT)
