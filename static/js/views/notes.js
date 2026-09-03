import { api, el, toast, confirmDialog, iconButton, emptyState } from "../api.js";
import { ICONS } from "../icons.js";

export async function render(container) {
  container.innerHTML = "";

  const wrap = el("div", { class: "view-constrained" });

  const header = el("div", { class: "view-header" }, [
    el("div", {}, [
      el("h2", { text: "Notes" }),
      el("div", { class: "sub", text: "Active Priorities, todos, and reminders — one unified list" }),
    ]),
  ]);

  const textInput = el("input", { placeholder: "e.g. Finish the CS 571 lab writeup" });
  const dueInput = el("input", { type: "datetime-local" });
  const addBtn = el("button", { class: "btn", text: "Add Note" });

  const form = el("div", { class: "glass card" }, [
    el("div", { class: "form-grid" }, [
      el("div", { class: "field field-grow" }, [el("label", { text: "Note" }), textInput]),
      el("div", { class: "field" }, [el("label", { text: "Due (optional)" }), dueInput]),
      addBtn,
    ]),
  ]);

  const list = el("div", { id: "notes-list", style: "margin-top:14px;" });

  wrap.append(header, form, list);
  container.append(wrap);

  const submit = async () => {
    const text = textInput.value.trim();
    if (!text) { textInput.focus(); return; }
    const due = dueInput.value ? new Date(dueInput.value).toISOString() : null;
    await api("/api/notes", { method: "POST", body: JSON.stringify({ text, due_date: due }) });
    textInput.value = "";
    dueInput.value = "";
    await refresh(list, textInput);
  };
  addBtn.addEventListener("click", submit);
  textInput.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });

  await refresh(list, textInput);
}

async function refresh(list, focusTarget) {
  const notes = await api("/api/notes");
  list.innerHTML = "";
  if (notes.length === 0) {
    list.appendChild(emptyState({
      icon: ICONS.notes,
      title: "No notes yet",
      hint: "Track priorities, todos, and reminders here. Anything with a due date also shows up on your Calendar.",
      actionLabel: "Add your first note",
      onAction: () => focusTarget && focusTarget.focus(),
    }));
    return;
  }
  for (const note of notes) {
    const checkbox = el("input", { type: "checkbox" });
    checkbox.checked = note.completed;
    checkbox.addEventListener("change", async () => {
      await api(`/api/notes/${note.id}`, { method: "PATCH", body: JSON.stringify({ completed: checkbox.checked }) });
      await refresh(list, focusTarget);
    });

    const dueText = note.due_date ? new Date(note.due_date).toLocaleString() : null;
    const delBtn = iconButton(ICONS.trash, "Delete note", async () => {
      const ok = await confirmDialog({
        title: "Delete this note?",
        message: `"${note.text}" will be permanently deleted.`,
        confirmLabel: "Delete note",
      });
      if (!ok) return;
      await api(`/api/notes/${note.id}`, { method: "DELETE" });
      await refresh(list, focusTarget);
      toast("Note deleted", "success");
    }, { danger: true });

    const card = el("div", { class: "glass bracket card has-row-actions" }, [
      el("div", { class: "card-row" }, [
        el("div", { class: "card-row", style: "gap:10px;" }, [
          checkbox,
          el("div", {}, [
            el("div", { class: "title", style: note.completed ? "text-decoration:line-through;opacity:0.5;" : "", text: note.text }),
            dueText ? el("div", { class: "meta", text: `Due ${dueText}` }) : null,
          ]),
        ]),
        el("div", { class: "row-actions" }, [delBtn]),
      ]),
    ]);
    list.appendChild(card);
  }
}
