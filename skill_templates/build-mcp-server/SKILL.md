---
name: build-mcp-server
description: Choose or build an MCP server, connect it to Kairos and explain sign-in and tool review.
---

# Build an MCP connection

First identify the service, operations needed, an existing endpoint if known
and whether sign-in is required. Check Tool Store > Tools and the MCP catalog
before writing a server. Prefer an existing suitable server. Kairos connects
servers per chat through its Integrations menu; adding one alone does not select
it in every chat. Use tool search and description before calling a connected tool.

## Transport and authentication

Remote servers use HTTP MCP endpoints. The current add form allows HTTP and
HTTPS with a host, without embedded username/password or a fragment. Prefer
HTTPS for remote services. Local command servers use stdio and are configured
through advanced integration settings with a command and argument list. The
chat add tool only adds HTTP servers; do not pretend a local command is a URL.
Kairos launches local servers with a restricted environment. Declare required
configuration deliberately and keep credentials out of source and output.

Choose `auth: "none"` for an endpoint requiring no sign-in or `auth: "oauth"`
for an OAuth endpoint. OAuth is completed by the person in Tool Store > Tools
using a browser on the Kairos machine. Kairos stores tokens and refreshes them.
For bearer-token endpoints use the existing manual form's Bearer token option;
the chat tool does not collect tokens. Server implementation and service
authorization must match the actual service, not a guessed sign-in flow.

## Minimal Python server

Kairos's current test fixtures use the installed MCP SDK's `MCPServer`:

```python
from mcp.server.mcpserver import MCPServer

server = MCPServer("reference")

@server.tool()
def echo(text: str) -> str:
    """Return the supplied text."""
    return text

if __name__ == "__main__":
    server.run()
```

This example runs over stdio. Kairos's HTTP test fixture instead calls
`server.run("streamable-http", host="127.0.0.1", port=8765)` and connects at
`/mcp`. Test the endpoint and arrange HTTPS hosting for remote deployment;
the example is not a hosted service. Use clear tool names, descriptions, typed inputs and bounded
outputs. Authenticate access to private data and enforce authorization inside
the server. Test listing tools, valid calls, invalid inputs, failures and absence
of sensitive values in output. Write server code in its own project, not Kairos's
app folders. Do not claim a working URL before a server is running there.

## Connect and review

Finish a ready HTTP connection with
`add_mcp_server(name, url, auth)` using `none` or `oauth`. This admin-only tool
always asks the person to approve the name and URL, including in Auto mode,
then uses Kairos's existing add-and-check path. A down or signed-out result
needs connection repair or sign-in, not a claim of success.

The first successful check pins the initial tools. Later additions or changes
to schemas/descriptions are held for review. Tell the person which tools need
acceptance in **Tool Store > Tools**; the chat tool never accepts held tools.
Repeat Check after repair or sign-in, then select the connection in the chat.

## Share to the Kairos Store

Only HTTP connection metadata can be shared, not a local command server or
the server's code. Use the connection card's **Share to store** button and
review the public MIT submission after GitHub sign-in. `server.json` contains
exactly `name`, `url`, `transport: "http"` and `auth_type: "none"` or `"oauth"`.
The Store requires HTTPS without credentials, query strings or fragments and
port 443 or no explicit port. Keep secrets out. Store slugs match
`[a-z][a-z0-9_]{0,63}` excluding `routes`, `services`, `views`; use a semver
version. The single server.json payload is at most 100,000 bytes. The Store
shares a connection template, never the person's tokens or acceptance state.
