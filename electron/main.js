// electron/main.js — the actual downloadable desktop app shell.
//
// Wraps the local FastAPI backend (app.py) in a real window with no visible
// browser chrome. This is the "front door #1" from JARVIS Plan's UI-direction
// decision (2026-08-31): the same backend is also reachable directly over
// plain HTTP as "front door #2" (the web-access path, no Electron involved)
// — this file only owns the desktop-shell half.
//
// Phase 8 (Packaging & distribution, 2026-09-01): the backend is spawned
// automatically as a child process instead of requiring a separately-run dev
// server — the "download it, double-click it, it works" requirement.
//
// Completed 2026-09-03 (David: "download the app just like any other
// mainstream app and have it work out of the box"): a full Python runtime
// with every dependency preinstalled now ships inside the installer, built
// by scripts/build_runtime.py and bundled as an extraResource. The packaged
// app no longer depends on anything being installed on the user's machine.
// See resolveBackendPython() below for the lookup order, and that script's
// docstring for why an embedded interpreter rather than a PyInstaller
// freeze (custom tabs are imported at runtime, so a frozen module graph
// would break them).

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

// Lookup order, most-specific first:
//   1. The bundled runtime (scripts/build_runtime.py) — what every packaged
//      install uses. Present in dev too once the script has been run.
//   2. The project's own .venv — the normal dev path.
//   3. A bare python/python3 on PATH — last resort for a source checkout
//      where neither of the above has been set up.
function resolveBackendPython() {
  const bundled = process.platform === "win32"
    ? path.join(REPO_ROOT, "runtime", "python.exe")
    : path.join(REPO_ROOT, "runtime", "bin", "python3");
  if (fs.existsSync(bundled)) return bundled;

  const venvPython = process.platform === "win32"
    ? path.join(REPO_ROOT, ".venv", "Scripts", "python.exe")
    : path.join(REPO_ROOT, ".venv", "bin", "python");
  if (fs.existsSync(venvPython)) return venvPython;

  return process.platform === "win32" ? "python" : "python3";
}

function startBackend() {
  const pythonExe = resolveBackendPython();
  console.log(`[backend] using interpreter: ${pythonExe}`);
  backendProcess = spawn(
    pythonExe,
    ["-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8420"],
    {
      cwd: REPO_ROOT,
      stdio: "pipe",
      // User data must NOT live inside the install directory: an update or
      // uninstall would take the per-install encryption key, saved sessions,
      // and encrypted credentials with it. app.getPath("userData") is the
      // OS-correct per-user location (%APPDATA%\JARVIS on Windows,
      // ~/Library/Application Support/JARVIS on macOS). core/constants.py
      // reads JARVIS_DATA_DIR and falls back to the in-repo data/ for dev.
      env: { ...process.env, JARVIS_DATA_DIR: path.join(app.getPath("userData"), "data") },
    },
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
    // No longer tells the user to go install Python — the runtime ships with
    // the app now, so if this screen appears it's a genuine fault (port 8420
    // already taken, antivirus quarantining the runtime, a corrupt install),
    // not something they forgot to set up.
    win.loadURL(
      "data:text/html,<body style='background:%230a0a0f;color:%23d8f4ff;font-family:system-ui,sans-serif;" +
      "display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;margin:0;" +
      "text-align:center;padding:0 40px;gap:10px;'>" +
      "<h2 style='color:%23ff5f5f;font-weight:400;margin:0;'>JARVIS couldn't start</h2>" +
      "<p style='color:%236b8a99;font-size:14px;max-width:460px;line-height:1.6;margin:0;'>" +
      "The backend didn't come up. The most common cause is another program already using port 8420. " +
      "Restarting your computer usually clears it. If it keeps happening, reinstalling JARVIS will " +
      "replace anything damaged &mdash; your data is stored separately and won't be lost.</p></body>",
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
