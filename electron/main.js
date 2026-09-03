// electron/main.js — the actual downloadable desktop app shell.
//
// Wraps the local FastAPI backend (app.py) in a real window with no visible
// browser chrome. This is the "front door #1" from JARVIS Plan's UI-direction
// decision (2026-08-31): the same backend is also reachable directly over
// plain HTTP as "front door #2" (the web-access path, no Electron involved)
// — this file only owns the desktop-shell half.
//
// Phase 8 (Packaging & distribution, 2026-09-01): the backend is now spawned
// automatically as a child process instead of requiring a separately-run dev
// server — the actual "download it, double-click it, it works" requirement.
// Real remaining gap, not silently glossed over: this still shells out to a
// Python interpreter that must exist on the machine (the project's own
// `.venv` in dev, or a bare `python`/`python3` on PATH otherwise) — it does
// NOT bundle a standalone Python runtime the way a real PyInstaller-frozen
// backend would. A genuinely dependency-free end-user install needs that
// bundling as its own follow-up; not built this pass.

const { app, BrowserWindow, ipcMain, dialog } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

// In a packaged build, main.js runs from inside the app's asar archive, and
// the backend source is bundled separately as an extraResource (see
// electron/package.json's "build.extraResources") rather than living at
// "..\" the way it does in this dev checkout.
const REPO_ROOT = app.isPackaged ? path.join(process.resourcesPath, "backend") : path.join(__dirname, "..");
const BACKEND_URL = process.env.JARVIS_BACKEND_URL || "http://127.0.0.1:8420";
// If JARVIS_BACKEND_URL is set, a backend is already running elsewhere (e.g.
// scripts/run_remote.py, or a manually-started dev server) — every dev/test
// workflow used throughout this project's build relies on that, so spawning
// a second backend in that case would be wrong, not just redundant.
const SHOULD_SPAWN_BACKEND = !process.env.JARVIS_BACKEND_URL;

let backendProcess = null;

function resolveBackendPython() {
  const venvPython = process.platform === "win32"
    ? path.join(REPO_ROOT, ".venv", "Scripts", "python.exe")
    : path.join(REPO_ROOT, ".venv", "bin", "python");
  if (fs.existsSync(venvPython)) return venvPython;
  return process.platform === "win32" ? "python" : "python3";
}

function startBackend() {
  const pythonExe = resolveBackendPython();
  backendProcess = spawn(
    pythonExe,
    ["-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8420"],
    { cwd: REPO_ROOT, stdio: "pipe" },
  );
  backendProcess.stdout.on("data", (d) => process.stdout.write(`[backend] ${d}`));
  backendProcess.stderr.on("data", (d) => process.stderr.write(`[backend] ${d}`));
  backendProcess.on("exit", (code) => console.log(`[backend] exited with code ${code}`));
}

function stopBackend() {
  if (backendProcess) {
    backendProcess.kill();
    backendProcess = null;
  }
}

async function waitForBackend(timeoutMs = 25000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const res = await fetch(`${BACKEND_URL}/api/health`);
      if (res.ok) return true;
    } catch (_) { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

async function createWindow() {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    backgroundColor: "#0a0a0f",
    autoHideMenuBar: true,
    // David's real logo (2026-08-31) — a proper multi-resolution .ico is
    // real icon-design work for the actual packaging pass; a single PNG
    // works fine for the window/taskbar icon in the meantime.
    icon: path.join(__dirname, "icon.png"),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  win.loadURL(
    "data:text/html,<body style='background:%230a0a0f;color:%2300d4ff;font-family:sans-serif;" +
    "display:flex;align-items:center;justify-content:center;height:100vh;margin:0;'>Starting JARVIS...</body>",
  );

  if (SHOULD_SPAWN_BACKEND) startBackend();
  const ok = await waitForBackend();
  if (!ok) {
    win.loadURL(
      "data:text/html,<body style='background:%230a0a0f;color:%23ff5f5f;font-family:sans-serif;" +
      "display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center;padding:0 40px;'>" +
      "JARVIS's backend didn't start. Confirm Python and its dependencies are installed " +
      "(see README), then relaunch.</body>",
    );
    return;
  }
  win.loadURL(BACKEND_URL);
}

// Brain's skill import and Library's document import used to need this kind
// of native file-open dialog + IPC round trip, since the renderer has no
// direct filesystem access (contextIsolation/nodeIntegration both off, on
// purpose). Both switched to a plain HTML file input + FileReader instead
// (David's ask 2026-09-01) — works identically in this Electron shell and
// the plain-HTTP web-access path, so the dedicated IPC handlers that used to
// live here (pick-skill-file, pick-document-file) were removed as dead code.
// pick-vault-folder below still needs the real thing: picking a *directory*
// has no browser-native equivalent the way picking a file does.

// Settings tab's "select an existing vault on your device" (David's ask,
// 2026-08-31) — same pattern as the skill-file picker above, just an
// openDirectory dialog instead of openFile, and it returns a path rather
// than file contents (core/vault.py reads the directory itself).
ipcMain.handle("pick-vault-folder", async () => {
  const result = await dialog.showOpenDialog({
    title: "Select Vault Folder",
    properties: ["openDirectory"],
  });
  if (result.canceled || result.filePaths.length === 0) return null;
  return result.filePaths[0];
});

app.whenReady().then(() => {
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", stopBackend);
