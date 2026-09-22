"""A private, attempt-scoped socket. It is not an owner/admin HTTP API.

The MCP child knows a random capability, never a database path or owner token.
Only the runtime's consumer may execute a request, after recording its intent.
"""
import asyncio
import hmac
import json
import secrets

from .models import Conflict

MAX_MESSAGE = 65536
MAX_REQUESTS = 128


class WorkerBridge:
    def __init__(self, service, queue):
        self.service = service
        self.queue = queue
        self.token = secrets.token_urlsafe(32)
        self.server = None
        self.port = None
        self.revoked = False
        self.requests = {}
        self.clients = set()
        self.lock = asyncio.Lock()

    def validate(self):
        if self.revoked:
            raise Conflict("This worker's access has ended")
        is_lead = self.service.store.validate_worker_attempt(self.service.assignment)
        if is_lead != self.service.is_lead:
            raise Conflict("Worker role no longer matches its assignment")

    async def start(self):
        self.validate()
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0, limit=MAX_MESSAGE)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def _request(self, request):
        if not isinstance(request, dict) or not isinstance(request.get("token"), str):
            raise Conflict("Worker authentication required")
        if not hmac.compare_digest(request["token"], self.token):
            raise Conflict("Worker authentication required")
        self.validate()
        if request.get("method") == "list":
            return [{"name": name, "description": body["description"], "inputSchema": body["parameters"]}
                    for name, body in self.service.definitions.items()]
        if request.get("method") != "call":
            raise Conflict("Unknown worker operation")
        key = request.get("id")
        name, arguments = request.get("name"), request.get("arguments")
        if not isinstance(key, str) or not key or len(key) > 128:
            raise Conflict("A bounded request id is required")
        if not isinstance(name, str) or name not in self.service.definitions or not isinstance(arguments, dict):
            raise Conflict("Invalid worker tool call")
        signature = json.dumps([name, arguments], sort_keys=True)
        # Concurrent retries share the same future. A changed replay never runs.
        async with self.lock:
            self.validate()
            if key in self.requests:
                old, future = self.requests[key]
                if old != signature:
                    raise Conflict("Worker request changed on replay")
            else:
                if self.service.terminal is not None:
                    raise Conflict("This step already has a result")
                if len(self.requests) >= MAX_REQUESTS:
                    raise Conflict("Worker request ceiling reached")
                future = asyncio.get_running_loop().create_future()
                self.requests[key] = (signature, future)
                await self.queue.put((key, name, arguments, future))
        return await asyncio.shield(future)

    async def _client(self, reader, writer):
        self.clients.add(writer)
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            if not line.endswith(b"\n") or len(line) > MAX_MESSAGE:
                raise Conflict("Invalid worker request size")
            request = json.loads(line)
            result = await asyncio.wait_for(self._request(request), 60)
            response = {"ok": True, "result": result}
        except (Exception, asyncio.CancelledError) as exc:
            # Never reflect a capability or arbitrary payload into an error.
            response = {"ok": False, "error": str(exc) if isinstance(exc, Conflict) else "Worker request ended"}
        try:
            encoded = json.dumps(response).encode() + b"\n"
            if len(encoded) > 262144:
                encoded = b'{"ok":false,"error":"Worker response exceeds the size limit"}\n'
            writer.write(encoded)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self.clients.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def close(self):
        self.revoked = True
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for _, future in self.requests.values():
            if not future.done():
                future.set_result({"ok": False, "text": "This worker's access has ended"})
        for writer in list(self.clients):
            writer.close()
