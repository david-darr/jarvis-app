import { api, el, toast } from "./api.js";
import { toggleRunTimeline } from "./runTimeline.js";

// Run history for scheduled tasks and work-board cards (the Tasks tab). The
// records come from services/task_service.py. Runs recorded before
// 2026-10-02 have no start time, so their duration shows as unknown rather
// than guessed. Since roadmap phase 4 (2026-10-06) a run can be stopped or
// late, and its channel message is tried again until it lands (core/outbox.py).

const OUTCOMES = { succeeded: "Succeeded", failed: "Failed", lost: "Didn't finish", stopped: "Stopped" };
const SHOWN = 10;

export function outcomeLabel(outcome) {
  return OUTCOMES[outcome] || outcome;
}

export function formatDuration(seconds) {
  if (seconds == null) return "duration unknown";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

// When a run began, falling back to when it ended for older records.
export function runTime(run) {
  return new Date((run.started_at || run.ran_at) * 1000).toLocaleString();
}

function lateLabel(run) {
  if (!run.late_seconds) return "";
  const due = run.scheduled_for ? new Date(run.scheduled_for).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
  return `late${due ? `: due ${due}` : ""}`;
}

function clock(seconds) {
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

// The run's channel message, from the outbox: landed, still being tried,
// given up, or cut off mid-send. Older records only say delivered or not.
export function deliveryState(run, deliveryLabel) {
  const to = deliveryLabel ? ` to ${deliveryLabel}` : "";
  const d = run.delivery;
  if (!d) {
    return run.delivered === true ? { text: "delivered" }
      : run.delivered === false ? { text: `delivery${to} failed`, error: true } : null;
  }
  if (d.status === "delivered") return { text: `delivered${to}` };
  if (d.status === "pending" || d.status === "sending") {
    return { text: d.attempts ? `delivery${to} didn't go through yet; trying again at ${clock(d.next_try_at)} (attempt ${d.attempts})`
      : `delivering${to}` };
  }
  if (d.status === "failed") return { text: `delivery${to} failed for a day and was given up`, error: true, retry: true };
  return { text: `JARVIS closed while sending${to}; it may not have arrived`, error: true, retry: true };
}

function deliveryRow(run, deliveryLabel) {
  const state = deliveryState(run, deliveryLabel);
  if (!state) return null;
  const row = el("div", { class: `run-delivery${state.error ? " is-error" : ""}` }, [el("span", { text: state.text })]);
  if (state.retry) {
    row.append(el("button", { class: "btn quiet", text: "Send again", onclick: async (event) => {
      event.currentTarget.disabled = true;
      try {
        const delivery = await api(`/api/tasks/deliveries/${run.delivery.id}/retry`, { method: "POST" });
        row.replaceWith(deliveryRow({ ...run, delivery }, deliveryLabel) || el("span"));
      } catch (problem) { toast(problem.message, "error"); }
    } }));
  }
  return row;
}

function runRow(run, deliveryLabel) {
  const meta = [runTime(run), formatDuration(run.duration_seconds), run.model,
    run.attempt ? `attempt ${run.attempt}` : "", lateLabel(run)].filter(Boolean).join(" · ");
  return el("details", { class: "run-row" }, [
    el("summary", {}, [
      el("span", { class: `run-outcome run-${run.outcome}`, text: outcomeLabel(run.outcome) }),
      el("span", { class: "run-meta", text: meta }),
    ]),
    ...[deliveryRow(run, deliveryLabel)].filter(Boolean),
    el("div", { class: `run-text${run.error ? " is-error" : ""}`, text: run.error || run.output || "(no output)" }),
    ...(run.run_id ? [stepsEl(run.run_id)] : []),
  ]);
}

// What the model did in this run, step by step (roadmap phase 8).
function stepsEl(runId) {
  const host = el("div", { class: "run-timeline-host" });
  return el("div", {}, [el("button", { class: "btn quiet run-steps", text: "Steps", onclick: () => toggleRunTimeline(runId, host) }), host]);
}

// Newest first, the latest ten until "Show all". Each row opens to its output.
export function runHistory(runs, { deliveryLabel } = {}) {
  const host = el("div", { class: "run-history" });
  if (!runs.length) {
    host.append(el("div", { class: "meta", text: "No runs yet." }));
    return host;
  }
  const draw = (limit) => {
    host.replaceChildren(...runs.slice(0, limit).map((run) => runRow(run, deliveryLabel)));
    if (runs.length > limit) {
      host.append(el("button", { class: "btn quiet run-more", text: `Show all ${runs.length}`, onclick: () => draw(runs.length) }));
    }
  };
  draw(SHOWN);
  return host;
}
