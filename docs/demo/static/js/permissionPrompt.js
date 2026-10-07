// The approval prompt, shared by every feature that can ask.
//
// David's ask 2026-09-18: when something needs a permission it does not have,
// ask properly - one time, always, or reject - inside the feature that is
// asking, rather than reporting that permission is missing.
//
// Everything shown here is text. The tool name, the target and the arguments
// come from a model's request, so they are rendered as content and never as
// markup: a request cannot dress itself up as a different question, and
// nothing in it is ever executed to build this dialog.
//
// Dismissing rejects. That is deliberate - closing a question must never be
// read as agreement, and the backend denies on silence anyway.
import { api, el } from "./api.js";

let open = null;

export function dismissPermissionPrompt() {
  open?.close();
  open = null;
}

/**
 * Show one request and answer it.
 * @param {object} request  the payload from the server
 * @param {(answer: object) => void} [onAnswered]
 * @param {{mount?: HTMLElement}} [options]  defaults to the document body, so
 *   a caller does not need a view root in scope to raise a modal.
 */
export function showPermissionPrompt(request, onAnswered, { mount = document.body } = {}) {
  dismissPermissionPrompt();
  const previous = document.activeElement;
  const status = el("p", { role: "alert", class: "permission-error" });
  const buttons = el("div", { class: "permission-choices" });

  const dialog = el("dialog", { class: "permission-dialog", "aria-labelledby": "permission-title" }, [
    el("div", { class: "permission-head" }, [
      el("h2", { id: "permission-title", text: "Permission needed" }),
      el("span", { class: "permission-tool", text: request.tool }),
    ]),
    el("p", { class: "permission-title", text: request.title || request.tool }),
    request.description ? el("p", { class: "muted", text: request.description }) : null,
    request.target ? el("p", { class: "permission-target", text: request.target }) : null,
    el("details", { class: "permission-args" }, [
      el("summary", { text: "What it asked for" }),
      el("pre", { text: typeof request.arguments === "string" ? request.arguments : JSON.stringify(request.arguments, null, 2) }),
    ]),
    status, buttons,
  ]);

  let answered = false;
  async function answer(choice) {
    if (answered) return;
    answered = true;
    for (const node of buttons.children) node.disabled = true;
    try {
      await api(`/api/permissions/${encodeURIComponent(request.id)}/answer`,
                { method: "POST", body: JSON.stringify({ choice: choice.id }) });
      onAnswered?.(choice);
      dialog.close();
    } catch (problem) {
      answered = false;
      for (const node of buttons.children) node.disabled = false;
      status.textContent = problem.message;
    }
  }

  for (const choice of request.choices || []) {
    const isReject = choice.behavior === "deny";
    buttons.append(el("button", {
      type: "button",
      class: `btn${choice.id === "once" ? " primary" : ""}${isReject ? " danger" : ""}`,
      text: choice.label,
      "data-choice": choice.id,
      onclick: () => answer(choice),
    }));
  }

  dialog.addEventListener("close", () => {
    if (open === dialog) open = null;
    dialog.remove();
    if (!answered) {
      // Closing is a rejection, not a shrug. Tell the server so the model is
      // refused immediately rather than waiting out the timeout.
      const reject = (request.choices || []).find(choice => choice.behavior === "deny");
      if (reject) api(`/api/permissions/${encodeURIComponent(request.id)}/answer`,
                      { method: "POST", body: JSON.stringify({ choice: reject.id }) }).catch(() => {});
    }
    if (previous?.isConnected) previous.focus();
  });

  open = dialog;
  mount.append(dialog);
  dialog.showModal();
  buttons.querySelector("button")?.focus();
  return dialog;
}
