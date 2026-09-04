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

const { app, BrowserWindow, ipcMain, dialog, Tray, Menu, shell } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const { autoUpdater } = require("electron-updater");

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

// Close-to-tray (David's ask 2026-09-03). Remote access is served by the
// backend, which is a child of this process — so closing the window used to
// kill the very thing you were trying to reach from your phone. Closing now
// hides the window and leaves everything running, the way Discord and Slack
// behave; the tray icon is how you get it back or genuinely quit.
let tray = null;
let mainWindow = null;
let isQuitting = false;

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

// 25s was too tight for a real first launch: the OS scans a ~240MB bundle,
// and the backend seeds skills and syncs the vault before it answers. Two
// minutes costs nothing when startup is fast and avoids declaring failure on
// a machine that was merely busy.
const BACKEND_TIMEOUT_MS = 120000;

async function waitForBackend(timeoutMs = BACKEND_TIMEOUT_MS, onProgress) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const res = await fetch(`${BACKEND_URL}/api/health`);
      if (res.ok) return true;
    } catch (_) { /* not up yet */ }
    if (onProgress) onProgress(Date.now() - started);
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

function showWindow() {
  if (!mainWindow) { createWindow(); return; }
  if (!mainWindow.isVisible()) mainWindow.show();
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
}

function buildTray() {
  if (tray) return;
  tray = new Tray(path.join(__dirname, "icon.ico"));
  tray.setToolTip("JARVIS — running in the background");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Open JARVIS", click: showWindow },
    {
      label: "Open in browser",
      click: () => shell.openExternal(BACKEND_URL),
    },
    { type: "separator" },
    {
      // The only way to actually stop it. Spelled out because the whole
      // point of this feature is that closing the window does NOT do this.
      label: "Quit JARVIS (stops remote access)",
      click: () => { isQuitting = true; app.quit(); },
    },
  ]));
  // Double-click is the convention people expect from a tray icon.
  tray.on("double-click", showWindow);
}

async function createWindow() {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    backgroundColor: "#0a0a0f",
    autoHideMenuBar: true,
    // The .ico, not the .png: it carries 16/24/32/48/64/128/256px variants,
    // so Windows picks the right one for the taskbar, Alt-Tab, and the
    // window corner instead of downscaling one large bitmap for all of them
    // (David's ask 2026-09-03 — the taskbar icon looked small and soft).
    icon: path.join(__dirname, "icon.ico"),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  mainWindow = win;
  buildTray();

  // Closing hides instead of quitting, so the backend — and therefore
  // remote access — keeps running. Quit deliberately, from the tray menu.
  win.on("close", (e) => {
    if (isQuitting) return;
    e.preventDefault();
    win.hide();
    // Said once, the first time, so it isn't a mystery where the app went.
    if (tray && !win.__toldAboutTray) {
      win.__toldAboutTray = true;
      tray.displayBalloon({
        title: "JARVIS is still running",
        content: "Remote access stays available. Reopen or quit from the icon in the system tray.",
      });
    }
  });

  win.on("closed", () => { mainWindow = null; });

  // A real file, not a data: URL. Newer Electron restricts top-level data:
  // navigation, so the old splash and error screens silently failed to render
  // and left only the window's black background — which is exactly what a
  // slow first launch on macOS looked like: a blank window with no
  // explanation (David, 2026-09-03).
  await win.loadFile(path.join(__dirname, "splash.html"));

  const say = (msg, detail, isError) => {
    win.webContents
      .executeJavaScript(
        `window.setStatus(${JSON.stringify(msg)}, ${JSON.stringify(detail || "")}, ${!!isError})`,
      )
      .catch(() => {});
  };

  if (SHOULD_SPAWN_BACKEND) {
    say("Starting the local engine…", "First launch takes longer while your system checks the app.");
    startBackend();
  }

  const ok = await waitForBackend(BACKEND_TIMEOUT_MS, (elapsed) => {
    if (elapsed > 15000) {
      say("Still starting…", `Waiting for the local engine (${Math.round(elapsed / 1000)}s). ` +
          "First launch can take a minute or two.");
    }
  });

  if (!ok) {
    say(
      "JARVIS couldn't start",
      "The local engine didn't come up in time.\n\n" +
      "Most often this is another program already using port 8420, or the app still being " +
      "scanned by your system on first launch — try opening it again.\n\n" +
      `Details are logged to:\n${path.join(app.getPath("userData"), "data", "logs", "backend.log")}`,
      true,
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

// -- auto-update (David's ask 2026-09-03: "work like any other mainstream
// app"). Without this, whoever installs a build is stranded on it forever —
// including for security fixes — and retrofitting updates later means
// exactly those users never receive the update that adds updating. So it
// ships in the first public build, not after.
//
// Downloads in the background and installs on quit, so an update never
// interrupts what someone's in the middle of. The only prompt is an
// optional "restart now" once a download has finished.
function setupAutoUpdate() {
  // In dev there's no published release to check against, and
  // electron-updater throws rather than no-oping.
  if (!app.isPackaged) return;

  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on("error", (err) => {
    // Never surfaced to the user: being offline, or GitHub being briefly
    // unreachable, is not something they need a dialog about.
    console.log(`[updater] ${err == null ? "unknown error" : err.message}`);
  });
  autoUpdater.on("update-available", (info) => console.log(`[updater] downloading ${info.version}`));
  autoUpdater.on("update-not-available", () => console.log("[updater] up to date"));

  autoUpdater.on("update-downloaded", async (info) => {
    const { response } = await dialog.showMessageBox({
      type: "info",
      buttons: ["Restart now", "Later"],
      defaultId: 0,
      cancelId: 1,
      title: "Update ready",
      message: `JARVIS ${info.version} is ready to install.`,
      detail: "It will be applied automatically the next time you quit, or you can restart now.",
    });
    if (response === 0) autoUpdater.quitAndInstall();
  });

  autoUpdater.checkForUpdates().catch(() => {});
  // Long-running desktop app: check again periodically so someone who never
  // quits still gets updates. Six hours is quiet enough to be invisible.
  setInterval(() => autoUpdater.checkForUpdates().catch(() => {}), 6 * 60 * 60 * 1000);
}

// Single-instance lock — mandatory now that closing only hides the window.
// Without it, clicking the Start Menu shortcut while JARVIS sits in the tray
// launches a SECOND copy, which then fails to bind port 8420 and shows the
// "couldn't start" screen while the original is running fine. Instead, the
// second launch hands off to the first, which simply reveals itself.
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
} else {
  app.on("second-instance", showWindow);

  app.whenReady().then(() => {
    createWindow();
    setupAutoUpdate();

    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
      else showWindow();
    });
  });
}

// Deliberately does NOT quit when the window closes: the window is hidden,
// not destroyed, and the backend must keep serving remote access. Quitting
// happens only via the tray menu (which sets isQuitting first).
app.on("window-all-closed", () => {});

app.on("before-quit", () => {
  isQuitting = true;
  stopBackend();
});
