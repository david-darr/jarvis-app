"""Tools-only MCP stdio helper and gated CLI supervisor; Python stdlib only.

Run by absolute path with python -I. No app imports, database, shell or owner
credential. The supervisor waits for its parent to establish process ownership
before spawning Codex. stdout is exclusively MCP or Codex JSONL.
"""
import json
import os
import socket
import subprocess
import sys
import uuid

MAX_MESSAGE = 65536
MAX_RESPONSE = 262144


def exchange(method, **fields):
    port = int(os.environ["JARVIS_SWARM_BRIDGE_PORT"])
    request = {"method": method, "token": os.environ["JARVIS_SWARM_BRIDGE_TOKEN"], **fields}
    with socket.create_connection(("127.0.0.1", port), timeout=65) as connection:
        connection.sendall(json.dumps(request).encode() + b"\n")
        with connection.makefile("rb") as stream:
            line = stream.readline(MAX_RESPONSE + 1)
    if len(line) > MAX_RESPONSE or not line.endswith(b"\n"):
        raise RuntimeError("Invalid worker bridge response")
    response = json.loads(line)
    if response.get("ok") is not True:
        raise RuntimeError(response.get("error") or "Worker bridge refused the request")
    return response["result"]


def serve():
    initialized = False
    nonce = uuid.uuid4().hex
    while True:
        line = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
        if not line:
            break
        if len(line) > MAX_MESSAGE:
            return 1
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            method = request.get("method")
            if request_id is None:
                continue  # MCP notifications have no response.
            if method == "initialize":
                exchange("list")  # fail initialization if the capability is stale
                initialized = True
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "jarvis-swarm", "version": "1.0.0"}}
            elif method == "ping":
                result = {}
            elif not initialized:
                raise RuntimeError("Initialize the worker first")
            elif method == "tools/list":
                result = {"tools": exchange("list")}
            elif method == "tools/call":
                params = request.get("params") or {}
                answer = exchange("call", id=f"{nonce}:{type(request_id).__name__}:{request_id}", name=params.get("name"),
                                  arguments=params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": answer["text"]}],
                          "isError": not answer["ok"]}
            else:
                raise RuntimeError("Unsupported MCP method")
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception:
            # No environment values, transport payloads or stack traces.
            response = {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32603, "message": "Swarm tool unavailable or request refused"}}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
    return 0


def supervise():
    # The parent attaches this waiting process to a Windows Job Object before
    # sending this line. Every subsequent child inherits the job, even if the
    # supervisor or Codex exits first. POSIX uses a new process group instead.
    line = sys.stdin.buffer.readline(1024 * 1024)
    if not line.endswith(b"\n"):
        return 1
    request = json.loads(line)
    process = subprocess.Popen(request["argv"], stdin=subprocess.PIPE,
                               cwd=request["cwd"], shell=False)
    process.communicate(request["prompt"].encode("utf-8"))
    return process.returncode


if __name__ == "__main__":
    try:
        sys.exit(supervise() if sys.argv[1:] == ["--supervise"] else serve())
    except Exception:
        sys.stderr.write("Swarm Codex helper failed\n")
        sys.exit(1)
