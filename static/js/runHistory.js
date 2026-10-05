import { el } from "./api.js";

// Run history for scheduled tasks and work-board cards (the Tasks tab). The
// records come from services/task_service.py. Runs recorded before
// 2026-10-02 have no start time, so their duration shows as unknown rather
// than guessed.

const OUTCOMES = { succeeded: "Succeeded", failed: "Failed", lost: "Didn't finish" };
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

function runRow(run, deliveryLabel) {
  const delivery = run.delivered === true ? "delivered"
    : run.delivered === false ? `delivery${deliveryLabel ? ` to ${deliveryLabel}` : ""} failed` : "";
  const meta = [runTime(run), formatDuration(run.duration_seconds), run.model,
    run.attempt ? `attempt ${run.attempt}` : "", delivery].filter(Boolean).join(" · ");
  return el("details", { class: "run-row" }, [
    el("summary", {}, [
      el("span", { class: `run-outcome run-${run.outcome}`, text: outcomeLabel(run.outcome) }),
      el("span", { class: "run-meta", text: meta }),
    ]),
    el("div", { class: `run-text${run.error ? " is-error" : ""}`, text: run.error || run.output || "(no output)" }),
  ]);
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
