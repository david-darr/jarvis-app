// The website's live demo (docs/demo): lets the real Kairos interface run on
// a static site, or straight from disk, with no server and no service worker.
// It loads before the app's bundle and, inside this page only,
// - answers fetch("/api/...") from the sample data in fixtures.js (the same
//   data scripts/ui-smoke.cjs tests against), keeping changes in memory,
// - plays a short scripted reply when a chat message is sent,
// - stands in for the Swarm event stream, which has nothing to say here,
// - shows computer use: a sample frame, Take over, recording and skill review,
//   and a chat that "opens a website" when asked to.
// Nothing leaves the visitor's browser and no model is ever called.
(function () {
  const state = { empty: false };
  const { fixture, data, mutate, media } = window.kairosFixtures({ state, demo: true });
  const store = { sessions: {}, nextId: 1 };  // chats started in the demo
  const realFetch = window.fetch.bind(window);

  // The fixtures name the sample frame by the app's absolute path; the demo
  // serves it relative to its own folder (it may be opened from disk).
  const relative = (text) => text.split('"/static/img/computer-fixture.jpg"').join('"static/img/computer-fixture.jpg"');
  const json = (body, status = 200) => new Response(relative(JSON.stringify(body)), { status, headers: { "Content-Type": "application/json" } });

  // The app asks for absolute paths ("/api/notes"). From disk those resolve to
  // file:///api/notes, and on GitHub Pages to the site's root, so the route is
  // read from the path the app asked for, not from where it would resolve.
  function route(input) {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input && input.url;
    if (typeof raw !== "string") return null;
    const url = new URL(raw, location.href);
    const at = url.pathname.search(/\/(api|generated-files)\//);
    return at < 0 ? null : new URL(url.pathname.slice(at) + url.search, "http://demo");
  }

  window.fetch = async function (input, init = {}) {
    const url = route(input);
    if (!url) return realFetch(input, init);
    const method = (init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (url.pathname.startsWith("/generated-files/")) {
      return new Response("# Project brief\n\nA sample file from the Kairos demo.\n", { headers: { "Content-Type": "text/markdown" } });
    }
    return api(url, method, init.body);
  };

  // Live streams: a computer's live view sends its sample frame once a second
  // until the computer is stopped; anything else (Swarm events) has nothing to
  // say and reports itself closed, as the browser does when a server ends one.
  window.EventSource = class DemoEventSource extends EventTarget {
    constructor(url) {
      super();
      this.url = String(url); this.readyState = 2; this.withCredentials = false;
      this.onopen = this.onmessage = this.onerror = null;
      const frames = route(this.url);
      if (frames && /^\/api\/computer\/[^/]+\/frames$/.test(frames.pathname)) {
        this.readyState = 1;
        const send = () => {
          const frame = fixture(frames);
          if (!frame.owner) { this.close(); this.dispatchEvent(new MessageEvent("closed", { data: "{}" })); return; }
          this.dispatchEvent(new MessageEvent("frame", { data: relative(JSON.stringify(frame)) }));
        };
        this.timer = setInterval(send, 1000);
        setTimeout(send, 0);
      }
    }
    close() { this.readyState = 2; clearInterval(this.timer); }
  };
  Object.assign(window.EventSource, { CONNECTING: 0, OPEN: 1, CLOSED: 2 });

  async function api(url, method, rawBody) {
    const path = url.pathname;
    if (method === "GET") {
      const file = media(url);
      if (file) return new Response(file.base64 ? Uint8Array.from(atob(file.base64), c => c.charCodeAt(0)) : file.body,
        { headers: { 'Content-Type': file.type } });
      const sessionMatch = path.match(/^\/api\/sessions\/([^/]+)$/);
      if (sessionMatch && store.sessions[sessionMatch[1]]) return json(store.sessions[sessionMatch[1]]);
      if (path.match(/^\/api\/sessions\/([^/]+)\/context$/) && store.sessions[path.split("/")[3]]) return json({ available: false });
      try { return json(fixture(url)); } catch (_) { return json({ detail: "Not available in the demo" }, 404); }
    }
    let body = {};
    try { if (typeof rawBody === "string") body = JSON.parse(rawBody); } catch (_) { /* not JSON */ }
    if (path === "/api/settings/computer-use") { state.computerUse = body; return json({ ok: true }); }
    const updated = mutate(path, method, body);
    if (updated) { const { _status = 200, ...payload } = updated; return json(payload, _status); }
    if (path === "/api/sessions" && method === "POST") {
      const id = "demo" + store.nextId++;
      const now = Date.now() / 1000;
      const session = { id, title: "New chat", created_at: now, updated_at: now, model_endpoint_id: "m1", messages: [] };
      store.sessions[id] = session;
      data.sessions.unshift({ id, title: session.title, updated_at: now });
      return json(session);
    }
    if (path === "/api/chat/stream" && method === "POST") return reply(body);
    if (path === "/api/notes" && method === "POST") {
      const note = { id: "demo-note-" + store.nextId++, text: body.text || "New note", completed: false, due_date: body.due_date || null };
      data.notes.unshift(note);
      return json(note);
    }
    const noteMatch = path.match(/^\/api\/notes\/([^/]+)$/);
    if (noteMatch) {
      const index = data.notes.findIndex((n) => n.id === noteMatch[1]);
      if (method === "DELETE" && index >= 0) data.notes.splice(index, 1);
      else if (index >= 0) Object.assign(data.notes[index], body);
      return json(index >= 0 ? data.notes[index] || { ok: true } : { ok: true });
    }
    const modelMatch = path.match(/^\/api\/sessions\/([^/]+)\/model$/);
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
    // Asked to open or visit a website, the reply shows the computer at work.
    const usesComputer = /\b(?:open|browse|visit|website|computer)\b/i.test(body.message || "");
    if (usesComputer) {
      state.computerChat = body.session_id;
      state.stoppedOwners = (state.stoppedOwners || []).filter((owner) => owner !== "chat:" + body.session_id);
    }
    if (session) {
      session.messages.push({ role: "user", content: String(body.message || ""), ts: now });
      session.messages.push({ role: "assistant", content: REPLY, ts: now + 1, ...(usesComputer ? { run_id: "r-computer" } : {}) });
      if (session.title === "New chat") session.title = String(body.message || "New chat").slice(0, 48);
      const listed = data.sessions.find((s) => s.id === session.id);
      if (listed) { listed.title = session.title; listed.updated_at = now; }
    }
    const words = REPLY.split(/(?<= )/);
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      async start(controller) {
        const computerEvent = (kind, detail) => controller.enqueue(encoder.encode(relative(`data: ${JSON.stringify({
          run_id: "r-computer", tool_event: { at: now, kind, name: "computer", ok: true, detail: JSON.stringify(detail) } })}\n\n`)));
        if (usesComputer) {
          computerEvent("tool_started", { action: "open", url: "https://example.com/" });
          computerEvent("tool_finished", { action: "open", url: "https://example.com/", image_url: "static/img/computer-fixture.jpg" });
        }
        for (const word of words) {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify({ chunk: word })}\n\n`));
          await new Promise((r) => setTimeout(r, 28));
        }
        if (usesComputer) {
          state.stoppedOwners ||= []; state.stoppedOwners.push("chat:" + body.session_id);
          computerEvent("tool_finished", { action: "done", url: "https://example.com/" });
        }
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ done: true })}\n\n`));
        controller.close();
      },
    });
    return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const badge = document.createElement("div");
    badge.className = "demo-badge";
    badge.textContent = "Demo · sample data · nothing leaves your browser";
    document.body.append(badge);
  });
})();
