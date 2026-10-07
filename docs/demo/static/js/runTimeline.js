import { api, el } from "./api.js";

// A run's timeline (roadmap phase 8, 2026-10-06; routes/run_routes.py): what
// one model run did, step by step - the tools it used and how each went,
// quota readings, how it ended - and what hangs off it: the run that started
// it, the runs and helpers it started, and a task's delivery. All text goes
// in through textContent: steps quote tool input and output.

const STEP_LABELS = {
  tool_started: "Started", tool_finished: "Finished", quota: "Quota", checkpoint: "Checkpoint",
  stopped: "Stopped", failed: "Failed", truncated: "More steps",
};
const SURFACES = { chat: "Chat", task: "Task", card: "Card", goal: "Goal", helper: "Helper", swarm: "Swarm", detached: "Background" };

export function surfaceLabel(surface) {
  return SURFACES[surface] || surface;
}

function clock(seconds) {
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

export function runSummary(run) {
  const took = run.ended_at && run.started_at ? `${Math.max(0, Math.round(run.ended_at - run.started_at))}s` : "";
  return [run.model, took, run.total_tokens != null ? `${run.total_tokens.toLocaleString()} tokens` : "tokens not reported",
    run.tool_calls ? `${run.tool_calls} tool${run.tool_calls === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · ");
}

function stepRow(step) {
  const failed = step.ok === false;
  const name = step.name ? ` ${step.name}` : "";
  const took = step.seconds != null ? ` · ${step.seconds}s` : "";
  return el("div", { class: `run-step run-step-${step.kind}${failed ? " is-error" : ""}` }, [
    el("span", { class: "run-step-time", text: clock(step.at) }),
    el("span", { class: "run-step-what", text: `${STEP_LABELS[step.kind] || step.kind}${name}${failed && step.kind === "tool_finished" ? " (failed)" : ""}${took}` }),
    ...(step.detail ? [el("div", { class: "run-step-detail", text: step.detail })] : []),
  ]);
}

export function runTimeline(detail) {
  const host = el("div", { class: "run-timeline" });
  const links = [];
  if (detail.parent) links.push(`Started by: ${surfaceLabel(detail.parent.surface)} ${detail.parent.label || detail.parent.id}`);
  if (detail.children?.length) links.push(`Started ${detail.children.length} run${detail.children.length === 1 ? "" : "s"}: ${detail.children.map((c) => surfaceLabel(c.surface)).join(", ")}`);
  if (detail.helpers?.length) links.push(`Helpers: ${detail.helpers.map((h) => `${h.status}${h.error ? ` (${h.error})` : ""}`).join(", ")}`);
  const delivery = detail.task_run?.delivery;
  if (delivery) links.push(`Delivery: ${delivery.status}${delivery.attempts ? `, ${delivery.attempts} attempt${delivery.attempts === 1 ? "" : "s"}` : ""}`);
  if (detail.detail) links.push(`Note: ${detail.detail}`);
  for (const line of links) host.append(el("div", { class: "meta run-link", text: line }));
  if (!detail.steps?.length) host.append(el("div", { class: "meta", text: "No steps recorded for this run (no tools used, or it ran before timelines existed)." }));
  for (const step of detail.steps || []) host.append(stepRow(step));
  return host;
}

// Fetch and show one run's timeline in `host`; a second call closes it.
export async function toggleRunTimeline(runId, host) {
  if (host.dataset.open) { host.replaceChildren(); delete host.dataset.open; return; }
  host.dataset.open = "1";
  host.replaceChildren(el("div", { class: "meta", text: "Loading steps…" }));
  try {
    host.replaceChildren(runTimeline(await api(`/api/runs/${encodeURIComponent(runId)}`)));
  } catch (problem) {
    host.replaceChildren(el("div", { class: "meta is-error", text: `Couldn't load this run: ${problem.message}` }));
  }
}
