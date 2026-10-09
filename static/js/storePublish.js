import { api, el, toast, openPanelDialog } from "./api.js";

const post = (path, body = {}, signal) => api(path, { method: "POST", body: JSON.stringify(body), signal });
const slugPattern = /^[a-z][a-z0-9_]{0,63}$/;
const versionPattern = /^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/;

export function shareButton(kind, localId) {
  return el("button", { type: "button", class: "btn quiet store-share", text: "Share to store", "aria-haspopup": "dialog", onclick: event => publishDialog(kind, localId, event.currentTarget) });
}

export async function publishDialog(kind, localId, opener = document.activeElement) {
  const fields = {};
  const form = el("div", { class: "store-publish-fields" });
  for (const [key, label, max] of [["slug", "Slug", 64], ["name", "Name", 120], ["description", "Description", 2000], ["version", "Version", 100]]) {
    const input = el(key === "description" ? "textarea" : "input", { "aria-label": label, maxlength: max, required: true, ...(key === "description" ? { rows: 3 } : {}) });
    fields[key] = input;
    form.append(el("label", { class: "field" }, [el("span", { text: label }), input]));
  }
  fields.version.value = "1.0.0";
  const message = el("div", { class: "meta", role: "status", text: "Loading your item..." });
  const files = el("div", { class: "store-publish-preview", hidden: true, "aria-label": "Files to publish" });
  const publicCheck = el("input", { type: "checkbox", "data-public-confirm": "" });
  const consent = el("label", { class: "tab-build-checkbox-row", hidden: true }, [publicCheck,
    el("span", { text: "I understand this will be public on GitHub under the MIT licence" })]);
  const previewButton = el("button", { type: "button", class: "btn", text: "Preview", disabled: true });
  const publishButton = el("button", { type: "button", class: "btn primary", text: "Open pull request", disabled: true });
  const closeButton = el("button", { type: "button", class: "btn quiet", text: "Cancel" });
  const body = el("div", { class: "store-publish-body" }, [
    el("p", { class: "muted", text: "Review every file before opening a public GitHub pull request. Store items are available after review and merge." }),
    form, message, files, consent,
  ]);
  const dialog = openPanelDialog({ title: "Share to store", body, wide: true, className: "store-publish-panel",
    group: "tool-store", opener, owner: document.querySelector("#view-content")?.firstElementChild, footer: el("div", {}, [closeButton, previewButton, publishButton]) });
  const { backdrop, close, signal } = dialog;
  let preview = null, busy = false, generation = 0;
  closeButton.addEventListener("click", close, { signal });
  const values = () => ({ kind, local_id: localId, ...Object.fromEntries(Object.entries(fields).map(([key, input]) => [key, input.value.trim()])) });
  const valid = () => slugPattern.test(fields.slug.value.trim()) && !["routes", "services", "views", "con", "prn", "aux", "nul"].includes(fields.slug.value.trim())
    && !/^(com|lpt)[1-9]$/.test(fields.slug.value.trim()) && versionPattern.test(fields.version.value.trim())
    && fields.name.value.trim() && fields.description.value.trim();
  const buttons = () => { previewButton.disabled = busy || !valid(); publishButton.disabled = busy || !preview || !publicCheck.checked; };
  for (const input of Object.values(fields)) input.addEventListener("input", () => {
    generation++; preview = null; publicCheck.checked = false; files.hidden = consent.hidden = true; buttons();
  });
  publicCheck.addEventListener("change", buttons);
  previewButton.addEventListener("click", async () => {
    const current = generation;
    busy = true; preview = null; publicCheck.checked = false; buttons(); message.textContent = "Checking every file...";
    try {
      const result = await post("/api/store/publish/prepare", values(), signal);
      if (current !== generation || !backdrop.isConnected) return;
      preview = result;
      files.replaceChildren(...Object.entries(result.files).map(([path, content]) => el("section", {}, [el("h5", { text: path }), el("pre", { text: content })])));
      if (result.removed_files?.length) files.append(el("p", { text: "Files removed by this update:\n" + result.removed_files.join("\n") }));
      files.hidden = consent.hidden = false;
      message.textContent = "These are the exact files that will be uploaded.";
    } catch (error) { if (backdrop.isConnected) message.textContent = error.message; }
    finally { if (backdrop.isConnected) { busy = false; buttons(); } }
  });
  publishButton.addEventListener("click", async () => {
    if (!preview || !publicCheck.checked || busy) return;
    busy = true; buttons(); message.textContent = "Opening your pull request...";
    Object.values(fields).forEach(input => { input.disabled = true; }); publicCheck.disabled = true;
    try {
      const result = await post("/api/store/publish", { ...values(), preview_hash: preview.preview_hash, confirmed_public: true }, signal);
      if (!backdrop.isConnected) return;
      message.replaceChildren(el("a", { class: "store-pr-link", href: result.url, target: "_blank", rel: "noopener", text: `Pull request #${result.number}` }));
      toast("Pull request opened", "success"); closeButton.textContent = "Done";
      previewButton.hidden = publishButton.hidden = true;
      document.dispatchEvent(new CustomEvent("kairos:store-published"));
    } catch (error) {
      if (!backdrop.isConnected) return;
      message.textContent = error.message;
      preview = null; publicCheck.checked = false; files.hidden = consent.hidden = true;
      Object.values(fields).forEach(input => { input.disabled = false; }); publicCheck.disabled = false;
    } finally { if (backdrop.isConnected) { busy = false; buttons(); } }
  });
  const initialGeneration = generation;
  try {
    const defaults = await post("/api/store/publish/export", { kind, local_id: localId }, signal);
    if (!backdrop.isConnected || generation !== initialGeneration) return;
    for (const [key, input] of Object.entries(fields)) input.value = defaults[key];
    message.textContent = "Use a slug starting with a lowercase letter, then lowercase letters, digits or underscores.";
    buttons(); fields.slug.focus();
  } catch (error) { if (backdrop.isConnected) message.textContent = error.message; }
}

export async function githubStrip(host) {
  let account, generation = 0;
  const submissions = el("div", { class: "store-submissions" });
  const strip = el("div", { class: "store-github-strip" });
  host.replaceChildren(strip, el("h4", { text: "My submissions" }), submissions);
  async function loadSubmissions() {
    submissions.replaceChildren(el("p", { class: "meta", text: account?.signed_in ? "Loading submissions..." : "Sign in to see your submissions." }));
    if (!account?.signed_in) return;
    try {
      const items = await api("/api/store/submissions");
      if (!host.isConnected) return;
      submissions.replaceChildren(...items.map(item => el("div", { class: "store-submission" }, [
        el("a", { href: item.url, target: "_blank", rel: "noopener", text: `#${item.number} ${item.title}` }),
        el("span", { class: `tool-store-badge submission-${item.state}`, text: item.state }),
      ])));
      if (!items.length) submissions.append(el("p", { class: "meta", text: "No submissions yet." }));
    } catch (error) { submissions.textContent = error.message; }
  }
  function draw() {
    strip.replaceChildren();
    if (account.signed_in) {
      strip.append(el("span", { text: `GitHub: @${account.login}` }), el("button", { type: "button", class: "btn quiet", text: "Sign out of GitHub", onclick: async () => {
        generation++;
        try { account = await post("/api/store/github/sign-out"); draw(); await loadSubmissions(); }
        catch (error) { toast(error.message, "error"); }
      } }));
    } else {
      strip.append(el("button", { type: "button", class: "btn quiet", text: "Sign in with GitHub", disabled: !account.configured, onclick: signIn }));
      if (!account.configured) strip.append(el("span", { class: "meta", text: "GitHub sign-in isn't set up in this build yet" }));
    }
  }
  async function signIn() {
    const current = ++generation;
    strip.querySelector("button").disabled = true;
    try { await waitForApproval(await post("/api/store/github/start"), current); }
    catch (error) { draw(); strip.append(el("span", { class: "meta", text: error.message })); }
  }
  // Also resumes a sign-in started by an earlier, since-redrawn view: the
  // server keeps the pending code (services/github_account.py status()).
  async function waitForApproval(code, current) {
    try {
      const notice = el("span", { class: "meta", text: "Enter this code on GitHub:" });
      strip.replaceChildren(notice, el("strong", { class: "store-device-code", text: code.user_code }),
        el("button", { type: "button", class: "btn quiet", text: "Copy code", onclick: async () => {
          try { await navigator.clipboard.writeText(code.user_code); toast("Code copied", "success"); } catch { toast("Select and copy the code above", "error"); }
        } }), el("a", { href: code.verification_uri, target: "_blank", rel: "noopener", text: "Enter code on GitHub" }));
      let interval = code.interval;
      const expires = Date.now() + code.expires_in * 1000;
      while (Date.now() < expires) {
        await new Promise(resolve => setTimeout(resolve, interval * 1000));
        if (!host.isConnected || current !== generation) return;
        const result = await post("/api/store/github/poll");
        if (result.signed_in) { account = result; draw(); await loadSubmissions(); return; }
        interval = result.interval || interval;
      }
      throw new Error("GitHub sign-in expired. Start again.");
    } catch (error) { draw(); strip.append(el("span", { class: "meta", text: error.message })); }
  }
  // The view owns this handler; a detached view must not keep fetching.
  const published = () => { if (host.isConnected) loadSubmissions(); else document.removeEventListener("kairos:store-published", published); };
  document.addEventListener("kairos:store-published", published);
  try {
    account = await api("/api/store/github");
    if (!host.isConnected) return;
    draw();
    if (account.pending) waitForApproval(account.pending, ++generation);
    await loadSubmissions();
  } catch (error) { strip.textContent = error.message; }
}
