// Slash commands — David's ask 2026-08-31, "just like what Odysseus has,
// /help that displays all the commands organized, as well as /demo and
// /notes /email /memory list, etc." Cross-checked the real registry shape
// against ~/odysseus/static/js/slashCommands.js (COMMANDS object, grouped by
// category, /help renders them grouped) rather than guessed — but scoped
// down hard to commands backed by real JARVIS functionality. Odysseus's
// registry has ~60 commands across Compare/Gallery/Cookbook/RAG/research/
// shell-access/etc.; we don't have most of those features at all, so this
// is a real, working subset, not a padded-out copy. "Memory" maps to our
// Skills service (the closest thing we have to Odysseus's separate
// persistent-memory-facts system — we don't have a second one).
import { api, el, confirmDialog } from "./api.js";

function switchTab(tabId) {
  const navItem = document.querySelector(`.nav-item[data-tab="${tabId}"]`);
  if (navItem) navItem.click();
}

function toolCommand(tabId, help) {
  return { category: "Tools", help, usage: `/${tabId}`, handler: () => { switchTab(tabId); return `Opened ${tabId[0].toUpperCase()}${tabId.slice(1)}.`; } };
}

export const COMMANDS = {
  help: {
    category: "Getting started",
    help: "Show this list",
    usage: "/help",
    handler: () => renderHelp(),
  },
  demo: {
    category: "Getting started",
    help: "Quick tour of what JARVIS can do",
    usage: "/demo",
    handler: () =>
      "Quick tour:\n" +
      "  Chats — multi-session, streaming, attach files, pin a workspace folder, insert saved Skills as prompts (the \"+\" menu)\n" +
      "  Notes — your task list (Active Priorities-style), with due dates that also show on Calendar\n" +
      "  Tasks — scheduled/recurring jobs the agent runs on its own\n" +
      "  Calendar — real events plus Notes' due-dated items in one view\n" +
      "  Email — connect an account, read/compose (agent never auto-sends)\n" +
      "  Brain — Skills: reusable saved prompts/procedures (insert into Chat via /prompt or the \"+\" menu)\n" +
      "  Cookbook — register other models (local or cloud) and pin a chat to one\n" +
      "  Settings — vault location, Discord connection\n" +
      "This is a text summary, not an interactive click-through tour — that's a real Odysseus feature we haven't built.",
  },
  new: {
    category: "Chats",
    help: "Create a new chat",
    usage: "/new",
    handler: async (args, ctx) => { await ctx.createSession(); return "New chat created."; },
  },
  rename: {
    category: "Chats",
    help: "Rename the current chat",
    usage: "/rename New title",
    handler: async (args, ctx) => {
      if (!ctx.sessionId()) return "No active chat.";
      const title = args.join(" ").trim();
      if (!title) return "Usage: /rename New title";
      await api(`/api/sessions/${ctx.sessionId()}`, { method: "PATCH", body: JSON.stringify({ title }) });
      await ctx.refreshSessions();
      return `Renamed to "${title}".`;
    },
  },
  star: {
    category: "Chats",
    help: "Toggle star on the current chat",
    usage: "/star",
    handler: async (args, ctx) => {
      if (!ctx.sessionId()) return "No active chat.";
      const session = await api(`/api/sessions/${ctx.sessionId()}`);
      await api(`/api/sessions/${ctx.sessionId()}/star`, { method: "POST", body: JSON.stringify({ starred: !session.starred }) });
      await ctx.refreshSessions();
      return session.starred ? "Unstarred." : "Starred.";
    },
  },
  delete: {
    category: "Chats",
    help: "Delete the current chat",
    usage: "/delete",
    handler: async (args, ctx) => {
      if (!ctx.sessionId()) return "No active chat.";
      const id = ctx.sessionId();
      const ok = await confirmDialog({
        title: "Delete this chat?",
        message: "This conversation and its full message history will be permanently deleted. This can't be undone.",
        confirmLabel: "Delete chat",
      });
      if (!ok) return "Cancelled — chat kept.";
      await api(`/api/sessions/${id}`, { method: "DELETE" });
      ctx.onCurrentSessionDeleted();
      await ctx.refreshSessions();
      return "Chat deleted.";
    },
  },

  notes: toolCommand("notes", "Open Notes"),
  tasks: toolCommand("tasks", "Open Tasks"),
  calendar: toolCommand("calendar", "Open Calendar"),
  email: toolCommand("email", "Open Email"),
  brain: toolCommand("brain", "Open Brain"),
  cookbook: toolCommand("cookbook", "Open Cookbook"),
  // Settings moved out of the main nav (David's ask 2026-08-31 — it's the
  // sidebar-footer user card now), so this can't click a .nav-item anymore
  // like the other /tool commands — opens the same floating window directly.
  settings: {
    category: "Tools",
    help: "Open Settings",
    usage: "/settings",
    handler: async () => {
      const settings = await import("./views/settings.js");
      await settings.openSettingsWindow();
      return "Opened Settings.";
    },
  },
  home: toolCommand("home", "Open Home"),
  library: toolCommand("library", "Open Library"),

  memory: {
    category: "Memory",
    help: "List saved Skills (JARVIS's memory/knowledge layer)",
    usage: "/memory list",
    handler: () => listSkills(),
  },
  skills: {
    category: "Memory",
    help: "List, or view one, saved Skills",
    usage: "/skills list  ·  /skills view name",
    handler: async (args) => {
      if (args[0] === "view" && args[1]) {
        const skill = await api(`/api/skills/${args[1]}`).catch(() => null);
        if (!skill) return `No skill named "${args[1]}".`;
        return `${skill.slug}\n${skill.description}\n\n${skill.body}`;
      }
      return listSkills();
    },
  },
  workspace: {
    category: "Agent",
    help: "Clear the current chat's workspace folder",
    usage: "/workspace clear",
    handler: async (args, ctx) => {
      if (args[0] !== "clear") return "Usage: /workspace clear (use the \"+\" menu to pick a folder)";
      if (!ctx.sessionId()) return "No active chat.";
      await api(`/api/sessions/${ctx.sessionId()}/workspace`, { method: "POST", body: JSON.stringify({ path: null }) });
      ctx.onWorkspaceCleared();
      return "Workspace cleared.";
    },
  },
  model: {
    category: "Settings",
    help: "Show the current chat's model",
    usage: "/model",
    handler: async (args, ctx) => {
      if (!ctx.sessionId()) return "No active chat.";
      const session = await api(`/api/sessions/${ctx.sessionId()}`);
      return session.model_endpoint_id ? `Pinned to endpoint ${session.model_endpoint_id}.` : "JARVIS (Claude) — the default.";
    },
  },
  models: {
    category: "Settings",
    help: "List registered model endpoints",
    usage: "/models",
    handler: async () => {
      const endpoints = await api("/api/models");
      if (endpoints.length === 0) return "No extra model endpoints registered yet. Add one in Cookbook.";
      return endpoints.map((e) => `${e.name} — ${e.model}`).join("\n");
    },
  },
};

async function listSkills() {
  const skills = await api("/api/skills");
  if (skills.length === 0) return "No skills saved yet. Add one in the Brain tab.";
  return skills.map((s) => `/${s.slug}${s.description ? " — " + s.description : ""}`).join("\n");
}

const CATEGORY_ORDER = ["Getting started", "Chats", "Tools", "Memory", "Agent", "Settings"];

function renderHelp() {
  const byCategory = {};
  for (const [name, def] of Object.entries(COMMANDS)) {
    const cat = def.category || "Other";
    (byCategory[cat] = byCategory[cat] || []).push({ name, ...def });
  }
  const lines = [];
  const order = [...CATEGORY_ORDER, ...Object.keys(byCategory).filter((c) => !CATEGORY_ORDER.includes(c))];
  for (const cat of order) {
    if (!byCategory[cat]) continue;
    lines.push(`${cat}:`);
    for (const cmd of byCategory[cat]) {
      lines.push(`  ${cmd.usage.padEnd(24)}${cmd.help}`);
    }
    lines.push("");
  }
  lines.push("Tip: saved Skills are also usable directly — see /skills list");
  return lines.join("\n").trim();
}

// Returns { handled: false } if text isn't a recognized slash command, else
// { handled: true, output: string } after running the handler. ctx bundles
// the bits handlers need without chat.js exposing its internals wholesale.
export async function runSlashCommand(text, ctx) {
  if (!text.startsWith("/")) return { handled: false };
  const [cmdToken, ...args] = text.slice(1).trim().split(/\s+/);
  const cmd = COMMANDS[(cmdToken || "").toLowerCase()];
  if (!cmd) {
    return { handled: true, output: `Unknown command: /${cmdToken}. Try /help.` };
  }
  try {
    const output = await cmd.handler(args, ctx);
    return { handled: true, output: output || "Done." };
  } catch (e) {
    return { handled: true, output: `Error: ${e.message}` };
  }
}
