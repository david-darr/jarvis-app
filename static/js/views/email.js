import { api, el, toast, confirmDialog, iconButton, emptyState } from "../api.js";
import { ICONS } from "../icons.js";

export async function render(container) {
  container.innerHTML = "";

  const wrap = el("div", { class: "view-constrained" });

  const header = el("div", { class: "view-header" }, [
    el("div", {}, [
      el("h2", { text: "Email" }),
      el("div", { class: "sub", text: "IMAP/SMTP accounts — credentials encrypted at rest, never shown again" }),
    ]),
  ]);

  const emailInput = el("input", { placeholder: "you@example.com" });
  const passInput = el("input", { type: "password", placeholder: "Password / app password" });
  const imapHost = el("input", { placeholder: "imap.example.com" });
  const imapPort = el("input", { type: "number", value: "993" });
  const smtpHost = el("input", { placeholder: "smtp.example.com" });
  const smtpPort = el("input", { type: "number", value: "465" });
  const addBtn = el("button", { class: "btn", text: "Add Account" });

  // Was six placeholder-only inputs crammed into one wrapping flex row
  // (audit 2026-09-03) — now real labeled fields grouped into rows.
  const form = el("div", { class: "glass card" }, [
    el("div", { class: "title", style: "margin-bottom:12px;", text: "Connect an account" }),
    el("div", { class: "form-grid", style: "margin-bottom:12px;" }, [
      el("div", { class: "field field-grow" }, [el("label", { text: "Email address" }), emailInput]),
      el("div", { class: "field field-grow" }, [el("label", { text: "Password" }), passInput]),
    ]),
    el("div", { class: "form-grid" }, [
      el("div", { class: "field field-grow" }, [el("label", { text: "IMAP host" }), imapHost]),
      el("div", { class: "field field-sm" }, [el("label", { text: "Port" }), imapPort]),
      el("div", { class: "field field-grow" }, [el("label", { text: "SMTP host" }), smtpHost]),
      el("div", { class: "field field-sm" }, [el("label", { text: "Port" }), smtpPort]),
      addBtn,
    ]),
  ]);

  const list = el("div", { id: "email-accounts-list", style: "margin-top:14px;" });
  wrap.append(header, form, list);
  container.append(wrap);

  addBtn.addEventListener("click", async () => {
    // Was a silent `return` on incomplete input — the button just did
    // nothing with no explanation (audit 2026-09-03).
    if (!emailInput.value.trim() || !passInput.value || !imapHost.value.trim() || !smtpHost.value.trim()) {
      toast("Fill in email, password, and both host fields first", "error");
      return;
    }
    await api("/api/email/accounts", {
      method: "POST",
      body: JSON.stringify({
        email: emailInput.value.trim(),
        password: passInput.value,
        imap_host: imapHost.value.trim(),
        imap_port: parseInt(imapPort.value, 10) || 993,
        smtp_host: smtpHost.value.trim(),
        smtp_port: parseInt(smtpPort.value, 10) || 465,
      }),
    });
    emailInput.value = ""; passInput.value = ""; imapHost.value = ""; smtpHost.value = "";
    await refresh(list, emailInput);
    toast("Account added", "success");
  });

  await refresh(list, emailInput);
}

async function refresh(list, focusTarget) {
  const accounts = await api("/api/email/accounts");
  list.innerHTML = "";
  if (accounts.length === 0) {
    list.appendChild(emptyState({
      icon: ICONS.email,
      title: "No accounts connected",
      hint: "Connect an IMAP/SMTP account above. Once connected, your Daily Brief can summarize unread mail and flag anything important.",
      actionLabel: "Connect an account",
      onAction: () => focusTarget && focusTarget.focus(),
    }));
    return;
  }
  for (const account of accounts) {
    const statusEl = el("span", { class: "meta", text: "" });
    const testBtn = el("button", { class: "btn", text: "Test connection", onclick: async () => {
      testBtn.disabled = true;
      statusEl.textContent = "Testing…";
      try {
        const result = await api(`/api/email/accounts/${account.id}/test`, { method: "POST" });
        statusEl.textContent = `IMAP: ${result.imap ? "OK" : "failed"} · SMTP: ${result.smtp ? "OK" : "failed"}`;
        const bothOk = result.imap && result.smtp;
        toast(
          bothOk ? `${account.email} connected successfully` : `${account.email}: ${result.imap_error || result.smtp_error || "connection problem"}`,
          bothOk ? "success" : "error",
        );
      } catch (e) {
        statusEl.textContent = "Test failed";
        toast(`Test failed: ${e.message}`, "error");
      } finally {
        testBtn.disabled = false;
      }
    }});

    const delBtn = iconButton(ICONS.trash, "Remove account", async () => {
      const ok = await confirmDialog({
        title: "Remove this account?",
        message: `${account.email} will be disconnected and its stored credentials deleted. Your mail itself isn't touched.`,
        confirmLabel: "Remove account",
      });
      if (!ok) return;
      await api(`/api/email/accounts/${account.id}`, { method: "DELETE" });
      await refresh(list, focusTarget);
      toast("Account removed", "success");
    }, { danger: true });

    const card = el("div", { class: "glass bracket card has-row-actions" }, [
      el("div", { class: "card-row" }, [
        el("div", {}, [
          el("div", { class: "title", text: account.email }),
          el("div", { class: "meta", text: `${account.imap_host} / ${account.smtp_host}` }),
          statusEl,
        ]),
        el("div", { class: "card-row", style: "gap:6px;" }, [testBtn, el("div", { class: "row-actions" }, [delBtn])]),
      ]),
    ]);
    list.appendChild(card);
  }
}
