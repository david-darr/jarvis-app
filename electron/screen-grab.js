// Person-initiated still screenshots for Chat. No model tool or background stream.
const { app, BrowserWindow, desktopCapturer, globalShortcut, ipcMain, screen } = require("electron");
const { execFile } = require("child_process");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const DEFAULT_SHORTCUT = "CommandOrControl+Shift+6";
function stillImage(image) {
  return `data:image/jpeg;base64,${image.toJPEG(84).toString("base64")}`;
}

// claimShortcut false (a named development instance, electron/instance.js):
// Quick Entry still opens from the tray and capture still works, but no
// global shortcut is registered, so the real app keeps its own.
function installScreenGrab({ getMainWindow, showMainWindow, python, repoRoot, sideBrowser, globalShortcut: claimShortcut = true }) {
  let quickWindow = null;
  let pickerWindow = null;
  let overlayWindow = null;
  let captureResolve = null;
  let captureReject = null;
  let overlayImage = null;
  let movingToOverlay = false;
  let returnToQuick = false;
  let shortcut = DEFAULT_SHORTCUT;
  const modelRequests = new Map();
  const quickRequests = new Map();
  const preferenceFile = path.join(app.getPath("userData"), "screen-grab.json");

  function isMain(event) {
    const win = getMainWindow();
    return !!win && !win.isDestroyed() && event.sender === win.webContents;
  }
  function isQuick(event) { return !!quickWindow && !quickWindow.isDestroyed() && event.sender === quickWindow.webContents; }
  function isPicker(event) { return !!pickerWindow && !pickerWindow.isDestroyed() && event.sender === pickerWindow.webContents; }
  function isOverlay(event) { return !!overlayWindow && !overlayWindow.isDestroyed() && event.sender === overlayWindow.webContents; }

  function setShortcut(value) {
    if (!claimShortcut) return { ok: false, error: "This development copy has no global shortcut. Open Quick Entry from its tray icon." };
    if (typeof value !== "string" || value.length > 80 || !/^(?=.*(?:CommandOrControl|Control|Alt|Shift|Super|Meta))[-+A-Za-z0-9]+(?:\+[-+A-Za-z0-9]+)+$/.test(value)) {
      return { ok: false, error: "Use an Electron shortcut such as CommandOrControl+Shift+6." };
    }
    if (globalShortcut.isRegistered(shortcut)) globalShortcut.unregister(shortcut);
    let registered = false;
    try { registered = globalShortcut.register(value, showQuickEntry); } catch { registered = false; }
    if (!registered) {
      globalShortcut.register(shortcut, showQuickEntry);
      return { ok: false, error: "That shortcut is already in use." };
    }
    shortcut = value;
    fs.mkdirSync(path.dirname(preferenceFile), { recursive: true });
    fs.writeFileSync(preferenceFile, JSON.stringify({ shortcut }), "utf8");
    return { ok: true, shortcut };
  }

  function showQuickEntry() {
    if (captureResolve) return;
    if (!quickWindow || quickWindow.isDestroyed()) {
      quickWindow = new BrowserWindow({
        width: 560, height: 338, minWidth: 460, minHeight: 320,
        frame: false, alwaysOnTop: true, skipTaskbar: true, show: false,
        backgroundColor: "#101113",
        webPreferences: { contextIsolation: true, nodeIntegration: false,
          preload: path.join(__dirname, "screen-grab-preload.js") },
      });
      quickWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      quickWindow.loadFile(path.join(__dirname, "screen-grab-quick.html"));
      quickWindow.on("closed", () => { quickWindow = null; });
    }
    quickWindow.center();
    quickWindow.show();
    quickWindow.focus();
    quickWindow.webContents.send("screen-grab:shown");
  }

  function finishCapture(value, error = null) {
    if (!captureResolve) return;
    const resolve = captureResolve;
    const reject = captureReject;
    captureResolve = null;
    captureReject = null;
    if (pickerWindow && !pickerWindow.isDestroyed()) pickerWindow.destroy();
    if (overlayWindow && !overlayWindow.isDestroyed()) overlayWindow.destroy();
    pickerWindow = null;
    overlayWindow = null;
    overlayImage = null;
    if (error) reject(error);
    else resolve(value);
    if (returnToQuick && quickWindow && !quickWindow.isDestroyed()) {
      quickWindow.show();
      quickWindow.focus();
    }
    returnToQuick = false;
  }

  async function sourceImage(id) {
    const displays = screen.getAllDisplays();
    const maxWidth = Math.max(1, ...displays.map(d => Math.ceil(d.size.width * d.scaleFactor)));
    const maxHeight = Math.max(1, ...displays.map(d => Math.ceil(d.size.height * d.scaleFactor)));
    const sources = await desktopCapturer.getSources({
      types: ["screen", "window"],
      thumbnailSize: { width: maxWidth, height: maxHeight },
    });
    const source = sources.find(item => item.id === id);
    if (!source || source.thumbnail.isEmpty()) throw new Error("That screen or window is no longer available.");
    return source.thumbnail;
  }

  async function openPicker() {
    const sources = await desktopCapturer.getSources({
      types: ["screen", "window"], thumbnailSize: { width: 300, height: 170 },
      fetchWindowIcons: true,
    });
    pickerWindow = new BrowserWindow({
      width: 800, height: 660, minWidth: 650, minHeight: 510, show: false,
      backgroundColor: "#101113", frame: false, autoHideMenuBar: true,
      webPreferences: { contextIsolation: true, nodeIntegration: false,
        preload: path.join(__dirname, "screen-grab-preload.js") },
    });
    pickerWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    pickerWindow.on("closed", () => {
      pickerWindow = null;
      if (!overlayWindow && !movingToOverlay) finishCapture(null);
    });
    await pickerWindow.loadFile(path.join(__dirname, "screen-grab-picker.html"));
    const choices = sources
      .filter(source => !source.thumbnail.isEmpty())
      .map(source => ({
        id: source.id, name: source.name, kind: source.id.startsWith("screen:") ? "screen" : "window",
        image: source.thumbnail.toDataURL(),
      }));
    if (sideBrowser.isOpen()) {
      const image = await sideBrowser.captureStill();
      if (image && !image.isEmpty()) choices.unshift({
        id: "jarvis-browser", name: "JARVIS side browser", kind: "tab", image: image.resize({ width: 300 }).toDataURL(),
      });
    }
    pickerWindow.webContents.send("screen-grab:sources", choices);
    pickerWindow.show();
  }

  function capture(fromQuick = false) {
    if (captureResolve) return Promise.reject(new Error("A capture is already open."));
    return new Promise((resolve, reject) => {
      captureResolve = resolve;
      captureReject = reject;
      returnToQuick = fromQuick;
      if (fromQuick) quickWindow.hide();
      openPicker().catch(error => finishCapture(null, error));
    });
  }

  async function openRegionOverlay(sourceId) {
    const point = screen.getCursorScreenPoint();
    const selected = (await desktopCapturer.getSources({
      types: ["screen"], thumbnailSize: { width: 0, height: 0 },
    })).find(source => source.id === sourceId);
    const display = screen.getAllDisplays().find(item => String(item.id) === selected?.display_id) ||
      screen.getDisplayNearestPoint(point);
    pickerWindow?.hide();
    await new Promise(resolve => setTimeout(resolve, 110));
    const sources = await desktopCapturer.getSources({
      types: ["screen"],
      thumbnailSize: {
        width: Math.ceil(display.size.width * display.scaleFactor),
        height: Math.ceil(display.size.height * display.scaleFactor),
      },
    });
    const source = sources.find(item => item.id === sourceId) ||
      sources.find(item => item.display_id === String(display.id)) || sources[0];
    if (!source || source.thumbnail.isEmpty()) throw new Error("Couldn't capture this display.");
    overlayImage = source.thumbnail;
    movingToOverlay = true;
    if (pickerWindow && !pickerWindow.isDestroyed()) pickerWindow.destroy();
    pickerWindow = null;
    overlayWindow = new BrowserWindow({
      ...display.bounds, frame: false, alwaysOnTop: true, skipTaskbar: true,
      movable: false, resizable: false, show: false, backgroundColor: "#10141c",
      webPreferences: { contextIsolation: true, nodeIntegration: false,
        preload: path.join(__dirname, "screen-grab-preload.js") },
    });
    overlayWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    overlayWindow.on("closed", () => { overlayWindow = null; finishCapture(null); });
    await overlayWindow.loadFile(path.join(__dirname, "screen-grab-overlay.html"));
    overlayWindow.webContents.send("screen-grab:overlay-image", overlayImage.toDataURL());
    overlayWindow.show();
    overlayWindow.focus();
    movingToOverlay = false;
  }

  function windowAtPoint(x, y) {
    return new Promise(resolve => {
      execFile(python(), [path.join(repoRoot, "scripts", "window_at_point.py"), String(x), String(y)],
        { timeout: 3000, windowsHide: true }, (error, stdout) => {
          if (error) return resolve(null);
          try { resolve(JSON.parse(stdout)); } catch { resolve(null); }
        });
    });
  }

  ipcMain.handle("screen-grab:capture", event => {
    if (!isMain(event) && !isQuick(event)) throw new Error("Capture is available only in JARVIS.");
    return capture(isQuick(event));
  });
  ipcMain.handle("screen-grab:shortcut", event => {
    if (!isMain(event) && !isQuick(event)) return null;
    return claimShortcut ? shortcut : "";
  });
  ipcMain.handle("screen-grab:set-shortcut", (event, value) => {
    if (!isMain(event) && !isQuick(event)) return { ok: false, error: "Denied." };
    return setShortcut(value);
  });
  ipcMain.handle("screen-grab:models", event => {
    if (!isQuick(event)) return [];
    const win = getMainWindow();
    if (!win || win.isDestroyed()) return [];
    const id = crypto.randomUUID();
    return new Promise(resolve => {
      const timer = setTimeout(() => { modelRequests.delete(id); resolve([]); }, 5000);
      modelRequests.set(id, models => { clearTimeout(timer); resolve(models); });
      win.webContents.send("screen-grab:models-request", id);
    });
  });
  ipcMain.on("screen-grab:models-reply", (event, id, models) => {
    if (!isMain(event) || typeof id !== "string" || !Array.isArray(models)) return;
    modelRequests.get(id)?.(models.filter(item => typeof item.id === "string" && typeof item.label === "string")
      .map(item => ({ id: item.id, label: item.label, supportsImages: item.supportsImages === true })));
    modelRequests.delete(id);
  });
  ipcMain.on("screen-grab:quick-close", event => { if (isQuick(event)) quickWindow.hide(); });
  ipcMain.on("screen-grab:quick-size", (event, height) => {
    if (isQuick(event) && Number.isInteger(height) && height >= 338 && height <= 620) quickWindow.setSize(560, height);
  });
  ipcMain.on("screen-grab:quick-send", (event, draft) => {
    if (!isQuick(event) || !draft || typeof draft.text !== "string" || draft.text.length > 20000 ||
        typeof draft.modelEndpointId !== "string" || !draft.modelEndpointId ||
        (draft.image && (typeof draft.image !== "string" || !/^data:image\/(png|jpeg);base64,/.test(draft.image) || draft.image.length > 35_000_000))) return;
    if (!draft.text.trim() && !draft.image) return;
    const win = getMainWindow();
    if (!win || win.isDestroyed()) return;
    const requestId = crypto.randomUUID();
    const timer = setTimeout(() => {
      quickRequests.delete(requestId);
      if (quickWindow && !quickWindow.isDestroyed()) {
        quickWindow.webContents.send("screen-grab:quick-result", { ok: false, error: "JARVIS did not receive the draft. Open the app and try again." });
      }
    }, 45000);
    quickRequests.set(requestId, timer);
    showMainWindow();
    win.webContents.send("screen-grab:quick-draft", {
      text: draft.text, image: draft.image || null, modelEndpointId: draft.modelEndpointId, requestId,
    });
  });
  ipcMain.on("screen-grab:quick-result", (event, requestId, result) => {
    if (!isMain(event) || typeof requestId !== "string" || !quickRequests.has(requestId)) return;
    clearTimeout(quickRequests.get(requestId));
    quickRequests.delete(requestId);
    if (!quickWindow || quickWindow.isDestroyed()) return;
    quickWindow.webContents.send("screen-grab:quick-result", result);
    if (result?.ok) quickWindow.hide();
  });
  ipcMain.on("screen-grab:cancel", event => {
    if (isPicker(event) || isOverlay(event)) finishCapture(null);
  });
  ipcMain.on("screen-grab:choose", async (event, id) => {
    if (!isPicker(event) || typeof id !== "string") return;
    if (id === "jarvis-browser") {
      try {
        const image = await sideBrowser.captureStill();
        if (!image || image.isEmpty()) throw new Error("The side browser is no longer open.");
        finishCapture(stillImage(image));
      } catch (error) { pickerWindow?.webContents.send("screen-grab:error", error.message); }
      return;
    }
    pickerWindow.hide();
    await new Promise(resolve => setTimeout(resolve, 110));
    try { finishCapture(stillImage(await sourceImage(id))); }
    catch (error) {
      pickerWindow?.show();
      pickerWindow?.webContents.send("screen-grab:error", error.message);
    }
  });
  ipcMain.on("screen-grab:region", async (event, sourceId) => {
    if (!isPicker(event)) return;
    try { await openRegionOverlay(sourceId); }
    catch (error) { finishCapture(null, error); }
  });
  ipcMain.on("screen-grab:selection", async (event, result) => {
    if (!isOverlay(event) || !overlayImage || !result) return;
    try {
      if (![result.x, result.y].every(Number.isFinite)) return;
      if (result.kind === "region") {
        if (![result.width, result.height].every(Number.isFinite)) return;
        const size = overlayImage.getSize();
        const x = Math.max(0, Math.floor(result.x * size.width));
        const y = Math.max(0, Math.floor(result.y * size.height));
        const width = Math.min(size.width - x, Math.ceil(result.width * size.width));
        const height = Math.min(size.height - y, Math.ceil(result.height * size.height));
        if (width < 4 || height < 4) return;
        finishCapture(stillImage(overlayImage.crop({ x, y, width, height })));
        return;
      }
      if (result.kind !== "window") return;
      const bounds = overlayWindow.getBounds();
      const physicalPoint = screen.dipToScreenPoint({
        x: Math.round(bounds.x + result.x * bounds.width),
        y: Math.round(bounds.y + result.y * bounds.height),
      });
      overlayWindow.hide();
      // The overlay must be hidden before WindowFromPoint can see the app below it.
      await new Promise(resolve => setTimeout(resolve, 90));
      const target = await windowAtPoint(physicalPoint.x, physicalPoint.y);
      if (!target?.hwnd) { finishCapture(null); return; }
      const sources = await desktopCapturer.getSources({
        types: ["window"], thumbnailSize: { width: 4096, height: 4096 },
      });
      const match = sources.find(source => Number(source.id.split(":")[1]) === target.hwnd);
      if (match && !match.thumbnail.isEmpty()) {
        finishCapture(stillImage(match.thumbnail));
        return;
      }
      if (!Array.isArray(target.rect) || target.rect.length !== 4) { finishCapture(null); return; }
      const rect = screen.screenToDipRect(overlayWindow, {
        x: target.rect[0], y: target.rect[1],
        width: target.rect[2] - target.rect[0], height: target.rect[3] - target.rect[1],
      });
      const size = overlayImage.getSize();
      const scaleX = size.width / bounds.width;
      const scaleY = size.height / bounds.height;
      const x = Math.max(0, Math.floor((rect.x - bounds.x) * scaleX));
      const y = Math.max(0, Math.floor((rect.y - bounds.y) * scaleY));
      const width = Math.min(size.width - x, Math.ceil(rect.width * scaleX));
      const height = Math.min(size.height - y, Math.ceil(rect.height * scaleY));
      finishCapture(width > 3 && height > 3 ? stillImage(overlayImage.crop({ x, y, width, height })) : null);
    } catch (error) {
      console.error("[screen-grab] selection failed", error);
      finishCapture(null);
    }
  });

  if (!claimShortcut) return { showQuickEntry };
  try {
    const saved = JSON.parse(fs.readFileSync(preferenceFile, "utf8"));
    if (typeof saved.shortcut === "string") shortcut = saved.shortcut;
  } catch { /* First run. */ }
  let registered = false;
  try { registered = globalShortcut.register(shortcut, showQuickEntry); } catch { registered = false; }
  if (!registered) {
    shortcut = DEFAULT_SHORTCUT;
    try { globalShortcut.register(shortcut, showQuickEntry); } catch { /* tray still opens Quick Entry */ }
  }
  app.on("will-quit", () => globalShortcut.unregister(shortcut));
  return { showQuickEntry };
}

module.exports = { installScreenGrab };
