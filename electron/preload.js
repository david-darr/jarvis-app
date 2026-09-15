// electron/preload.js — the only bridge between the sandboxed renderer
// (contextIsolation: true, nodeIntegration: false, in main.js) and Node/OS
// capabilities. Exposes a small, fixed set of narrow functions, not a
// general IPC passthrough — the renderer can ask for exactly these specific
// things and nothing else, matching "trusted gateway, untrusted execution"
// from the harness architecture research (see JARVIS Plan / Harness
// Architecture Ideas).
//
// This file runs ONLY in the app's own window. The side browser's
// WebContentsView is created with no preload at all (electron/browser.js),
// so nothing below is reachable from a page opened in it — that separation
// is what keeps an arbitrary website from driving the app.

const { contextBridge, ipcRenderer } = require("electron");

// Each entry is a named function taking plain data, never a channel string
// supplied by the caller — a generic send(channel, ...) would hand the
// renderer the entire IPC surface and undo the point of this file.
contextBridge.exposeInMainWorld("jarvis", {
  pickVaultFolder: () => ipcRenderer.invoke("pick-vault-folder"),

  // Side browser (David's ask 2026-09-15). Controls only: nothing here can
  // read page content, and the main process re-validates every URL anyway
  // (electron/browser.js's safeUrl) rather than trusting this side.
  browser: {
    open: (url, bounds) => ipcRenderer.invoke("browser:open", url, bounds),
    navigate: (url) => ipcRenderer.invoke("browser:navigate", url),
    state: () => ipcRenderer.invoke("browser:state"),
    openExternal: () => ipcRenderer.invoke("browser:external"),
    back: () => ipcRenderer.send("browser:back"),
    forward: () => ipcRenderer.send("browser:forward"),
    reload: () => ipcRenderer.send("browser:reload"),
    close: () => ipcRenderer.send("browser:close"),
    setBounds: (rect) => ipcRenderer.send("browser:bounds", rect),
    setVisible: (visible) => ipcRenderer.send("browser:visible", !!visible),

    // Subscriptions return their own unsubscribe function. The raw event
    // object is never passed through: it carries a `sender` reference that
    // would leak an IPC handle straight into page scope.
    onState: (handler) => {
      const wrapped = (_event, payload) => handler(payload);
      ipcRenderer.on("browser:state", wrapped);
      return () => ipcRenderer.removeListener("browser:state", wrapped);
    },
    onError: (handler) => {
      const wrapped = (_event, payload) => handler(payload);
      ipcRenderer.on("browser:error", wrapped);
      return () => ipcRenderer.removeListener("browser:error", wrapped);
    },
    // Raised when an external link is activated in the app UI, so the
    // renderer can open the pane itself and keep its own layout in step.
    onOpenRequest: (handler) => {
      const wrapped = (_event, url) => handler(url);
      ipcRenderer.on("browser:open-request", wrapped);
      return () => ipcRenderer.removeListener("browser:open-request", wrapped);
    },
    onHostResized: (handler) => {
      const wrapped = () => handler();
      ipcRenderer.on("browser:host-resized", wrapped);
      return () => ipcRenderer.removeListener("browser:host-resized", wrapped);
    },
  },
});
