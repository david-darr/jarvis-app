import { api, el, toast, confirmDialog } from "../api.js";

// Cookbook (David's ask 2026-09-01: "can we develop the cookbook tab now")
// — browse/pull/manage local models. Two backends, toggled via a segmented
// tab pair (David's follow-up ask same day: "shouldn't we add something
// for users that dont want to download Ollama, why dont we do what
// Odysseus and other ai harnesses such as Hermes does"):
//   - Ollama (core/ollama_client.py) — the original build, needs Ollama
//     installed separately.
//   - Built-in Engine (core/llamacpp_engine.py) — a real `llama-cpp-python
//     [server]` subprocess over a directly-downloaded GGUF file, no
//     separate app install at all. Proven end-to-end live (real GGUF
//     download, real server spawn, real chat completion) before this UI
//     was written.
// Both register into the exact same Settings model-endpoint system, so
// either path ends up as a normal "local" model in the chat model picker.

let activeSection = "ollama";
let ollamaPollTimer = null;
let enginePollTimer = null;

export async function render(container) {
  container.innerHTML = "";
  clearPolls();

  const header = el("div", { class: "view-header" }, [
    el("div", {}, [
      el("h2", { text: "Cookbook" }),
      el("div", { class: "sub", text: "Browse, download, and manage local models." }),
    ]),
  ]);

  const tabs = el("div", { class: "segmented-tabs" });
  const body = el("div", {});

  function renderTabs() {
    tabs.innerHTML = "";
    for (const [id, label] of [["ollama", "Ollama"], ["engine", "Built-in Engine"]]) {
      tabs.appendChild(el("button", {
        type: "button",
        class: "segmented-tab" + (activeSection === id ? " active" : ""),
        text: label,
        onclick: () => switchSection(id),
      }));
    }
  }

  async function switchSection(id) {
    clearPolls();
    activeSection = id;
    renderTabs();
    body.innerHTML = "";
    if (id === "ollama") await renderOllamaSection(body);
    else await renderEngineSection(body);
  }

  container.append(header, tabs, body);
  await switchSection(activeSection);

  return clearPolls;
}

function clearPolls() {
  if (ollamaPollTimer) { clearInterval(ollamaPollTimer); ollamaPollTimer = null; }
  if (enginePollTimer) { clearInterval(enginePollTimer); enginePollTimer = null; }
}

// -- Ollama backend -----------------------------------------------------

async function renderOllamaSection(container) {
  const status = await api("/api/cookbook/status");
  if (!status.reachable) {
    container.appendChild(
      el("div", { class: "coming-soon" }, [
        el("h3", { text: "Ollama not reachable" }),
        el("p", { text: "This backend manages models through a local Ollama server (http://localhost:11434), and it's either not installed or not running." }),
        el("p", { text: "Install it from ollama.com and make sure it's running, or switch to the Built-in Engine tab above — that one needs no separate install at all." }),
      ]),
    );
    return;
  }

  const installedSection = el("div", { style: "margin-top:18px;" });
  const catalogSection = el("div", { style: "margin-top:18px;" });
  container.append(installedSection, catalogSection);
  await refreshOllamaInstalled(installedSection, catalogSection);
  await renderOllamaCatalog(catalogSection, installedSection);
}

async function refreshOllamaInstalled(installedSection, catalogSection) {
  installedSection.innerHTML = "";
  installedSection.appendChild(el("div", { class: "title", style: "margin-bottom:8px;", text: "Installed" }));

  let installed, running;
  try {
    [installed, running] = await Promise.all([
      api("/api/cookbook/installed"),
      api("/api/cookbook/running"),
    ]);
  } catch (e) {
    installedSection.appendChild(el("div", { class: "empty-state", text: "Couldn't reach Ollama: " + e.message }));
    return;
  }

  const runningNames = new Set(running.map((r) => r.name));

  if (installed.length === 0) {
    installedSection.appendChild(el("div", { class: "empty-state", text: "No models installed yet — pull one from the catalog below." }));
    return;
  }

  for (const m of installed) {
    const sizeGb = (m.size / 1e9).toFixed(1);
    const delBtn = el("button", { class: "btn danger", text: "Delete", onclick: async () => {
      const ok = await confirmDialog({
        title: "Delete this model?",
        message: `${m.name} (${sizeGb} GB) will be removed from disk. You'd need to re-download it to use it again.`,
        confirmLabel: "Delete model",
      });
      if (!ok) return;
      await api(`/api/cookbook/models/${encodeURIComponent(m.name)}`, { method: "DELETE" });
      await refreshOllamaInstalled(installedSection, catalogSection);
      toast(`${m.name} deleted`, "success");
    }});
    const registerBtn = el("button", { class: "btn", text: "Use in Chat", onclick: async () => {
      registerBtn.disabled = true; registerBtn.textContent = "Adding...";
      await api("/api/cookbook/register", { method: "POST", body: JSON.stringify({ name: m.name }) });
      registerBtn.textContent = "Added — see Settings";
    }});
    const card = el("div", { class: "glass bracket card", style: "margin-bottom:8px;" }, [
      el("div", { class: "card-row" }, [
        el("div", {}, [
          el("div", { class: "title", style: "font-size:12.5px;", text: m.name + (runningNames.has(m.name) ? " · running" : "") }),
          el("div", { class: "meta", text: `${sizeGb} GB` }),
        ]),
        el("div", { class: "card-row", style: "gap:6px;" }, [registerBtn, delBtn]),
      ]),
    ]);
    installedSection.appendChild(card);
  }
}

async function renderOllamaCatalog(catalogSection, installedSection) {
  catalogSection.innerHTML = "";
  catalogSection.appendChild(el("div", { class: "title", style: "margin-bottom:8px;", text: "Catalog" }));
  catalogSection.appendChild(el("div", { class: "meta", style: "margin-bottom:10px;", text: "A curated list, not a live search of every model Ollama offers." }));

  const catalog = await api("/api/cookbook/catalog");
  const grid = el("div", { style: "display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px;" });
  catalogSection.appendChild(grid);

  for (const item of catalog) {
    const statusEl = el("span", { class: "meta" });
    const pullBtn = el("button", { class: "btn", text: "Pull" });
    pullBtn.addEventListener("click", async () => {
      pullBtn.disabled = true;
      await api(`/api/cookbook/pull/${encodeURIComponent(item.name)}`, { method: "POST" });
      watchOllamaPull(item.name, statusEl, pullBtn, installedSection, catalogSection);
    });
    const card = el("div", { class: "glass bracket card" }, [
      el("div", { class: "title", style: "font-size:12.5px;", text: `${item.label} (${item.params})` }),
      el("div", { class: "meta", style: "margin:4px 0 8px;", text: item.description }),
      el("div", { class: "card-row" }, [statusEl, pullBtn]),
    ]);
    grid.appendChild(card);
  }
}

function watchOllamaPull(name, statusEl, pullBtn, installedSection, catalogSection) {
  if (ollamaPollTimer) clearInterval(ollamaPollTimer);
  ollamaPollTimer = setInterval(async () => {
    const progress = await api(`/api/cookbook/pull/${encodeURIComponent(name)}/status`);
    if (progress.status === "error") {
      statusEl.textContent = "Failed: " + (progress.error || "unknown error");
      statusEl.style.color = "var(--danger)";
      pullBtn.disabled = false;
      clearInterval(ollamaPollTimer); ollamaPollTimer = null;
      return;
    }
    if (progress.done) {
      statusEl.textContent = "Done.";
      clearInterval(ollamaPollTimer); ollamaPollTimer = null;
      await refreshOllamaInstalled(installedSection, catalogSection);
      return;
    }
    const pct = progress.total ? Math.round((progress.completed / progress.total) * 100) : null;
    statusEl.textContent = pct != null ? `${progress.status} ${pct}%` : (progress.status || "pulling...");
  }, 1000);
}

// -- Built-in engine backend ---------------------------------------------

async function renderEngineSection(container) {
  container.appendChild(el("div", { class: "meta", style: "margin:0 0 14px;", text: "No separate app to install — models download directly and run through a bundled local server (llama.cpp)." }));

  const statusRow = el("div", {});
  const downloadedSection = el("div", { style: "margin-top:18px;" });
  const catalogSection = el("div", { style: "margin-top:18px;" });
  container.append(statusRow, downloadedSection, catalogSection);

  await refreshEngineStatus(statusRow, downloadedSection);
  await refreshEngineDownloaded(downloadedSection, catalogSection, statusRow);
  await renderEngineCatalog(catalogSection, downloadedSection, statusRow);
}

async function refreshEngineStatus(statusRow, downloadedSection) {
  statusRow.innerHTML = "";
  const st = await api("/api/cookbook/engine/status");
  if (st.running) {
    const stopBtn = el("button", { class: "btn danger", text: "Stop", onclick: async () => {
      await api("/api/cookbook/engine/stop", { method: "POST" });
      await refreshEngineStatus(statusRow, downloadedSection);
      await refreshEngineDownloaded(downloadedSection, null, statusRow);
    }});
    statusRow.appendChild(
      el("div", { class: "glass bracket card" }, [
        el("div", { class: "card-row" }, [
          el("div", {}, [
            el("div", { class: "title", style: "font-size:12.5px;", text: `Running: ${st.model}` }),
            el("div", { class: "meta", text: st.base_url }),
          ]),
          stopBtn,
        ]),
      ]),
    );
  } else {
    statusRow.appendChild(el("div", { class: "meta", text: "No model currently running." }));
  }
}

async function refreshEngineDownloaded(downloadedSection, catalogSection, statusRow) {
  downloadedSection.innerHTML = "";
  downloadedSection.appendChild(el("div", { class: "title", style: "margin-bottom:8px;", text: "Downloaded" }));

  const downloaded = await api("/api/cookbook/engine/downloaded");
  const st = await api("/api/cookbook/engine/status");
  if (downloaded.length === 0) {
    downloadedSection.appendChild(el("div", { class: "empty-state", text: "No models downloaded yet — pull one from the catalog below." }));
    return;
  }

  for (const m of downloaded) {
    const sizeGb = (m.size / 1e9).toFixed(1);
    const isRunning = st.running && st.model === m.name;
    const startBtn = el("button", { class: "btn", text: isRunning ? "Running" : "Start", disabled: isRunning, onclick: async () => {
      startBtn.disabled = true; startBtn.textContent = "Starting...";
      try {
        await api(`/api/cookbook/engine/start/${encodeURIComponent(m.name)}`, { method: "POST" });
        toast(`${m.name} started`, "success");
      } catch (e) {
        toast(`Couldn't start ${m.name}: ${e.message}`, "error");
      }
      await refreshEngineStatus(statusRow, downloadedSection);
      await refreshEngineDownloaded(downloadedSection, catalogSection, statusRow);
    }});
    const registerBtn = el("button", { class: "btn", text: "Use in Chat", disabled: !isRunning, onclick: async () => {
      registerBtn.disabled = true; registerBtn.textContent = "Adding...";
      await api("/api/cookbook/engine/register", { method: "POST", body: JSON.stringify({ name: m.name }) });
      registerBtn.textContent = "Added — see Settings";
    }});
    const delBtn = el("button", { class: "btn danger", text: "Delete", onclick: async () => {
      const ok = await confirmDialog({
        title: "Delete this model?",
        message: `${m.name} (${sizeGb} GB) will be removed from disk${isRunning ? " and stopped" : ""}. You'd need to re-download it to use it again.`,
        confirmLabel: "Delete model",
      });
      if (!ok) return;
      await api(`/api/cookbook/engine/models/${encodeURIComponent(m.name)}`, { method: "DELETE" });
      await refreshEngineStatus(statusRow, downloadedSection);
      await refreshEngineDownloaded(downloadedSection, catalogSection, statusRow);
      toast(`${m.name} deleted`, "success");
    }});
    const card = el("div", { class: "glass bracket card", style: "margin-bottom:8px;" }, [
      el("div", { class: "card-row" }, [
        el("div", {}, [
          el("div", { class: "title", style: "font-size:12.5px;", text: m.name + (isRunning ? " · running" : "") }),
          el("div", { class: "meta", text: `${sizeGb} GB` }),
        ]),
        el("div", { class: "card-row", style: "gap:6px;" }, [startBtn, registerBtn, delBtn]),
      ]),
    ]);
    downloadedSection.appendChild(card);
  }
}

async function renderEngineCatalog(catalogSection, downloadedSection, statusRow) {
  catalogSection.innerHTML = "";
  catalogSection.appendChild(el("div", { class: "title", style: "margin-bottom:8px;", text: "Catalog" }));
  catalogSection.appendChild(el("div", { class: "meta", style: "margin-bottom:10px;", text: "A curated list of GGUF models, each verified reachable before being listed." }));

  const catalog = await api("/api/cookbook/engine/catalog");
  const grid = el("div", { style: "display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px;" });
  catalogSection.appendChild(grid);

  for (const item of catalog) {
    const statusEl = el("span", { class: "meta" });
    const downloadBtn = el("button", { class: "btn", text: "Download" });
    downloadBtn.addEventListener("click", async () => {
      downloadBtn.disabled = true;
      await api(`/api/cookbook/engine/download/${encodeURIComponent(item.name)}`, { method: "POST" });
      watchEngineDownload(item.name, statusEl, downloadBtn, downloadedSection, catalogSection, statusRow);
    });
    const card = el("div", { class: "glass bracket card" }, [
      el("div", { class: "title", style: "font-size:12.5px;", text: `${item.label} (${item.params})` }),
      el("div", { class: "meta", style: "margin:4px 0 8px;", text: item.description }),
      el("div", { class: "card-row" }, [statusEl, downloadBtn]),
    ]);
    grid.appendChild(card);
  }
}

function watchEngineDownload(name, statusEl, downloadBtn, downloadedSection, catalogSection, statusRow) {
  if (enginePollTimer) clearInterval(enginePollTimer);
  enginePollTimer = setInterval(async () => {
    const progress = await api(`/api/cookbook/engine/download/${encodeURIComponent(name)}/status`);
    if (progress.status === "error") {
      statusEl.textContent = "Failed: " + (progress.error || "unknown error");
      statusEl.style.color = "var(--danger)";
      downloadBtn.disabled = false;
      clearInterval(enginePollTimer); enginePollTimer = null;
      return;
    }
    if (progress.done) {
      statusEl.textContent = "Done.";
      clearInterval(enginePollTimer); enginePollTimer = null;
      await refreshEngineDownloaded(downloadedSection, catalogSection, statusRow);
      return;
    }
    const pct = progress.total ? Math.round((progress.completed / progress.total) * 100) : null;
    statusEl.textContent = pct != null ? `Downloading ${pct}%` : "Downloading...";
  }, 1000);
}
