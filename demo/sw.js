// The website's live demo (docs/demo): a service worker that lets the real
// Kairos interface run on a static site. It controls only pages under its
// own folder, and for them it
// - serves the app's absolute /static/... paths from the demo's copy,
// - answers /api/... from the sample data in fixtures.js (the same data
//   scripts/ui-smoke.cjs tests against), keeping changes in memory,
// - plays a short scripted reply when a chat message is sent.
// Nothing leaves the visitor's browser and no model is ever called.
importScripts("fixtures.js");

const SCOPE = new URL(self.registration.scope).pathname;  // e.g. /kairos/demo/
const state = { empty: false };
const { fixture, data } = self.kairosFixtures({ state });
const store = { sessions: {}, nextId: 1 };  // chats started in the demo

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;  // e.g. fonts; nothing else is fetched
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(fetch(SCOPE + url.pathname.slice(1) + url.search));
  } else if (url.pathname.startsWith("/api/")) {
    event.respondWith(api(event.request, url));
  } else if (url.pathname.startsWith("/generated-files/")) {
    event.respondWith(new Response("# Project brief\n\nA sample file from the Kairos demo.\n", { headers: { "Content-Type": "text/markdown" } }));
  }
});

async function api(request, url) {
  const route = url.pathname;
  const method = request.method;
  // Live streams (Swarm events): none in the demo. 204 tells EventSource to stop.
  if (route.startsWith("/api/swarm/") && route.endsWith("/events") && method === "GET") return new Response(null, { status: 204 });
  if (method === "GET") {
    const sessionMatch = route.match(/^\/api\/sessions\/([^/]+)$/);
    if (sessionMatch && store.sessions[sessionMatch[1]]) return json(store.sessions[sessionMatch[1]]);
    if (route.match(/^\/api\/sessions\/([^/]+)\/context$/) && store.sessions[route.split("/")[3]]) return json({ available: false });
    try { return json(fixture(url)); } catch (_) { return json({ detail: "Not available in the demo" }, 404); }
  }
  let body = {};
  try { body = await request.clone().json(); } catch (_) { /* form uploads and empty bodies */ }
  if (route === "/api/sessions" && method === "POST") {
    const id = "demo" + store.nextId++;
    const now = Date.now() / 1000;
    const session = { id, title: "New chat", created_at: now, updated_at: now, model_endpoint_id: "m1", messages: [] };
    store.sessions[id] = session;
    data.sessions.unshift({ id, title: session.title, updated_at: now });
    return json(session);
  }
  if (route === "/api/chat/stream" && method === "POST") return reply(body);
  if (route === "/api/notes" && method === "POST") {
    const note = { id: "demo-note-" + store.nextId++, text: body.text || "New note", completed: false, due_date: body.due_date || null };
    data.notes.unshift(note);
    return json(note);
  }
  const noteMatch = route.match(/^\/api\/notes\/([^/]+)$/);
  if (noteMatch) {
    const index = data.notes.findIndex((n) => n.id === noteMatch[1]);
    if (method === "DELETE" && index >= 0) data.notes.splice(index, 1);
    else if (index >= 0) Object.assign(data.notes[index], body);
    return json(index >= 0 ? data.notes[index] || { ok: true } : { ok: true });
  }
  const modelMatch = route.match(/^\/api\/sessions\/([^/]+)\/model$/);
  if (modelMatch) {
    if (store.sessions[modelMatch[1]]) store.sessions[modelMatch[1]].model_endpoint_id = body.model_endpoint_id;
    return json({ ok: true, model_override: body.model_override ?? null, effort: body.effort ?? null });
  }
  return json({ ok: true });
}

const REPLY = "*Demo reply.* This is the Kairos interface running on sample data, so no model answered you. "
  + "In the app, your message goes to the model you choose (Claude, Codex, a local model or any API), "
  + "with your vault, notes and tools at hand. Have a look around the other tabs, then download Kairos to try it for real.";

function reply(body) {
  const session = store.sessions[body.session_id];
  const now = Date.now() / 1000;
  if (session) {
    session.messages.push({ role: "user", content: String(body.message || ""), ts: now });
    session.messages.push({ role: "assistant", content: REPLY, ts: now + 1 });
    if (session.title === "New chat") session.title = String(body.message || "New chat").slice(0, 48);
    const listed = data.sessions.find((s) => s.id === session.id);
    if (listed) { listed.title = session.title; listed.updated_at = now; }
  }
  const words = REPLY.split(/(?<= )/);
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    async start(controller) {
      for (const word of words) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ chunk: word })}\n\n`));
        await new Promise((r) => setTimeout(r, 28));
      }
      controller.enqueue(encoder.encode(`data: ${JSON.stringify({ done: true })}\n\n`));
      controller.close();
    },
  });
  return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
}
