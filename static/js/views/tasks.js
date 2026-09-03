import { api, el, customSelect, toast, confirmDialog, iconButton, emptyState } from "../api.js";
import { ICONS } from "../icons.js";

// Built-in tasks gallery (David's ask 2026-08-31, matching Odysseus's
// premade-action preset picker — src/builtin_actions.py's tidy_sessions/
// daily_brief/summarize_emails/audit_skills/etc.) — a real, scoped-down
// subset (core/builtin_tasks.py) with a one-click Enable/Disable instead of
// filling out the custom name/prompt/schedule form by hand.

export async function render(container) {
  container.innerHTML = "";

  const header = el("div", { class: "view-header" }, [
    el("div", {}, [
      el("h2", { text: "Tasks" }),
      el("div", { class: "sub", text: "Scheduled automations — distinct from Notes' todos" }),
    ]),
  ]);

  const builtinCard = el("div", { class: "glass bracket card" });
  await refreshBuiltins(builtinCard);

  const form = el("div", { class: "glass card" });
  const nameInput = el("input", { placeholder: "e.g. Morning summary" });
  const promptInput = el("input", { placeholder: "e.g. Summarize my open notes" });
  const kindSelect = customSelect({}, [
    el("option", { value: "once", text: "Once, at a time" }),
    el("option", { value: "interval", text: "Every N minutes" }),
  ]);
  const runAtInput = el("input", { type: "datetime-local" });
  const intervalInput = el("input", { type: "number", placeholder: "30" });
  const channels = await api("/api/channels");
  const channelSelect = customSelect({ style: "font-size:12.5px;" }, [
    el("option", { value: "", text: "Tasks tab only" }),
    ...channels.map((c) => el("option", { value: c.id, text: `${c.label}${c.configured ? "" : " (not configured)"}` })),
  ]);
  const addBtn = el("button", { class: "btn", text: "Schedule" });

  // Labeled fields instead of seven bare placeholder-only inputs crammed
  // into one wrapping row (audit 2026-09-03).
  const runAtField = el("div", { class: "field" }, [el("label", { text: "Run at" }), runAtInput]);
  const intervalField = el("div", { class: "field field-sm" }, [el("label", { text: "Every (min)" }), intervalInput]);
  intervalField.style.display = "none";

  kindSelect.addEventListener("change", () => {
    const isOnce = kindSelect.value === "once";
    runAtField.style.display = isOnce ? "" : "none";
    intervalField.style.display = isOnce ? "none" : "";
  });

  form.append(
    el("div", { class: "title", style: "margin-bottom:12px;", text: "Schedule your own" }),
    el("div", { class: "form-grid" }, [
      el("div", { class: "field field-grow" }, [el("label", { text: "Name" }), nameInput]),
      el("div", { class: "field field-grow" }, [el("label", { text: "Prompt to run" }), promptInput]),
      el("div", { class: "field" }, [el("label", { text: "Schedule" }), kindSelect]),
      runAtField,
      intervalField,
      el("div", { class: "field" }, [el("label", { text: "Deliver to" }), channelSelect]),
      addBtn,
    ])
  );

  const list = el("div", { id: "tasks-list", style: "margin-top:14px;" });
  container.append(el("div", { class: "view-constrained" }, [header, builtinCard, form, list]));

  addBtn.addEventListener("click", async () => {
    const name = nameInput.value.trim();
    const prompt = promptInput.value.trim();
    if (!name || !prompt) return;
    const body = { name, prompt, schedule_kind: kindSelect.value, deliver_to_channel: channelSelect.value || null };
    if (kindSelect.value === "once") {
      if (!runAtInput.value) return;
      body.run_at = new Date(runAtInput.value).toISOString();
    } else {
      const minutes = parseInt(intervalInput.value, 10);
      if (!minutes) return;
      body.interval_seconds = minutes * 60;
    }
    await api("/api/tasks", { method: "POST", body: JSON.stringify(body) });
    nameInput.value = ""; promptInput.value = ""; runAtInput.value = ""; intervalInput.value = "";
    await refresh(list);
  });

  await refresh(list);
}

async function refreshBuiltins(card) {
  const [builtins, channels] = await Promise.all([api("/api/tasks/builtin"), api("/api/channels")]);
  card.innerHTML = "";
  card.append(
    el("div", { class: "title", text: "Built-in Tasks" }),
    el("div", { class: "meta", style: "margin:4px 0 12px;", text: "Premade automations — turn on with one click. \"Action\" ones run deterministic code, no model call; \"llm\" ones build a real prompt from your live data each run." }),
  );
  const grid = el("div", { style: "display:grid;grid-template-columns:1fr 1fr;gap:10px;" });
  for (const b of builtins) {
    const channelSelect = customSelect({ style: "font-size:11px;", disabled: b.enabled }, [
      el("option", { value: "", text: "Tasks tab only" }),
      ...channels.map((c) => el("option", { value: c.id, text: c.label })),
    ]);
    const toggleBtn = el("button", { class: b.enabled ? "btn danger" : "btn", text: b.enabled ? "Disable" : "Enable" });
    toggleBtn.addEventListener("click", async () => {
      if (b.enabled) {
        // "Disable" reads reversible, but it really DELETEs the task — which
        // also discards its run history for good (audit 2026-09-03). Worth
        // one confirm since re-enabling can't bring that history back.
        const ok = await confirmDialog({
          title: `Turn off ${b.label}?`,
          message: "The task stops running and its past run history is discarded. You can turn it back on any time, but the history won't return.",
          confirmLabel: "Turn it off",
        });
        if (!ok) return;
      }
      toggleBtn.disabled = true;
      if (b.enabled) {
        await api(`/api/tasks/${b.task_id}`, { method: "DELETE" });
        toast(`${b.label} turned off`, "success");
      } else {
        await api(`/api/tasks/builtin/${b.action_id}/enable`, { method: "POST", body: JSON.stringify({ deliver_to_channel: channelSelect.value || null }) });
        toast(`${b.label} turned on`, "success");
      }
      await refreshBuiltins(card);
      const list = document.getElementById("tasks-list");
      if (list) await refresh(list);
    });
    grid.appendChild(el("div", { class: "card-row", style: "justify-content:space-between;align-items:flex-start;border:1px solid var(--border);border-radius:8px;padding:10px;" }, [
      el("div", {}, [
        el("div", { class: "title", style: "font-size:12.5px;", text: b.label }),
        el("div", { class: "meta", style: "margin-top:3px;", text: b.description }),
        el("div", { class: "meta", style: "margin-top:3px;color:var(--text-faint);", text: `${b.kind === "action" ? "No model call" : "Uses model"} · every ${Math.round(b.default_interval_seconds / 3600)}h` }),
      ]),
      el("div", { style: "display:flex;flex-direction:column;gap:6px;align-items:stretch;" }, [channelSelect, toggleBtn]),
    ]));
  }
  card.appendChild(grid);
}

async function refresh(list) {
  const [tasks, channels] = await Promise.all([api("/api/tasks"), api("/api/channels")]);
  list.innerHTML = "";
  if (tasks.length === 0) {
    list.appendChild(emptyState({
      icon: ICONS.tasks,
      title: "No scheduled tasks",
      hint: "Turn on a built-in task above, or schedule your own prompt to run on a timer. Output can be delivered straight to Discord.",
    }));
    return;
  }
  for (const task of tasks) {
    const schedText = task.schedule_kind === "once"
      ? `Once at ${task.next_run_at ? new Date(task.next_run_at).toLocaleString() : "(done)"}`
      : `Every ${Math.round(task.interval_seconds / 60)}m — next ${new Date(task.next_run_at).toLocaleTimeString()}`;

    const runBtn = el("button", { class: "btn", text: "Run now", onclick: async () => {
      runBtn.disabled = true;
      runBtn.textContent = "Running...";
      const run = await api(`/api/tasks/${task.id}/run`, { method: "POST" }).catch(() => null);
      if (run) {
        if (run.error) toast(`${task.name} failed: ${run.error}`, "error");
        else if (run.delivered === false) toast(`${task.name} ran, but delivery failed — check Settings > Channels`, "error");
        else if (run.delivered === true) toast(`${task.name} ran and delivered`, "success");
        else toast(`${task.name} ran`, "success");
      }
      await refresh(list);
    }});
    const delBtn = iconButton(ICONS.trash, "Delete task", async () => {
      const ok = await confirmDialog({
        title: "Delete this task?",
        message: `"${task.name}" and its run history will be permanently deleted.`,
        confirmLabel: "Delete task",
      });
      if (!ok) return;
      await api(`/api/tasks/${task.id}`, { method: "DELETE" });
      await refresh(list);
      toast("Task deleted", "success");
    }, { danger: true });

    // Delivery target (David's ask 2026-08-31: task output sent to a
    // Settings > Channels destination instead of only sitting here).
    const deliverySelect = customSelect({ style: "font-size:11.5px;" }, [
      el("option", { value: "", text: "Tasks tab only" }),
      ...channels.map((c) => el("option", { value: c.id, text: `${c.label}${c.configured ? "" : " (not configured)"}` })),
    ]);
    deliverySelect.value = task.deliver_to_channel || "";
    deliverySelect.addEventListener("change", async () => {
      await api(`/api/tasks/${task.id}`, { method: "PATCH", body: JSON.stringify({ deliver_to_channel: deliverySelect.value }) });
    });

    // Real bug found live (David: "i didn't get any response") — Run now
    // actually executes and GET /api/tasks/{id}/runs records real output,
    // but nothing in this view ever displayed it. Fixed by showing the
    // most recent run's output/error directly in the card.
    const runsHost = el("div", { style: "margin-top:8px;" });

    const card = el("div", { class: "glass bracket card has-row-actions" }, [
      el("div", { class: "card-row" }, [
        el("div", {}, [
          el("div", { class: "title", text: task.name + (task.builtin_action ? " · built-in" : "") + (task.enabled ? "" : "  (disabled)") }),
          el("div", { class: "meta", text: schedText }),
        ]),
        el("div", { class: "card-row", style: "gap:6px;" }, [deliverySelect, runBtn, el("div", { class: "row-actions" }, [delBtn])]),
      ]),
      runsHost,
    ]);
    list.appendChild(card);

    if (task.last_run_at) {
      const runs = await api(`/api/tasks/${task.id}/runs`);
      const last = runs[0];
      if (last) {
        const when = new Date(last.ran_at * 1000).toLocaleString();
        runsHost.append(
          el("div", { class: "meta", style: "margin-top:4px;color:var(--text-faint);", text: `Last run: ${when}` }),
          el("div", {
            class: "meta",
            style: `margin-top:4px;white-space:pre-wrap;color:${last.error ? "var(--danger)" : "var(--text)"};`,
            text: last.error ? `Error: ${last.error}` : last.output,
          }),
        );
        // Delivery outcome was previously silent everywhere but the server
        // log (David found this live 2026-09-02 — a Discord bot with no
        // allowed_user_id set failed delivery with no visible error at all).
        if (last.delivered === false) {
          const chan = channels.find((c) => c.id === task.deliver_to_channel);
          runsHost.append(el("div", {
            class: "meta",
            style: "margin-top:4px;color:var(--danger);",
            text: `Delivery to ${chan ? chan.label : task.deliver_to_channel} failed — check Settings > Channels.`,
          }));
        }
      }
    }
  }
}
