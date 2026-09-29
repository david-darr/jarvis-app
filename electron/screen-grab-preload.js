const { contextBridge, ipcRenderer } = require("electron");

function subscribe(channel, handler) {
  const wrapped = (_event, value) => handler(value);
  ipcRenderer.on(channel, wrapped);
  return () => ipcRenderer.removeListener(channel, wrapped);
}

contextBridge.exposeInMainWorld("screenGrab", {
  capture: () => ipcRenderer.invoke("screen-grab:capture"),
  shortcut: () => ipcRenderer.invoke("screen-grab:shortcut"),
  setShortcut: value => ipcRenderer.invoke("screen-grab:set-shortcut", value),
  models: () => ipcRenderer.invoke("screen-grab:models"),
  closeQuick: () => ipcRenderer.send("screen-grab:quick-close"),
  quickSize: height => ipcRenderer.send("screen-grab:quick-size", height),
  sendQuick: draft => ipcRenderer.send("screen-grab:quick-send", draft),
  onQuickResult: handler => subscribe("screen-grab:quick-result", handler),
  onShown: handler => subscribe("screen-grab:shown", handler),
  cancel: () => ipcRenderer.send("screen-grab:cancel"),
  choose: id => ipcRenderer.send("screen-grab:choose", id),
  region: id => ipcRenderer.send("screen-grab:region", id),
  selection: value => ipcRenderer.send("screen-grab:selection", value),
  onSources: handler => subscribe("screen-grab:sources", handler),
  onOverlayImage: handler => subscribe("screen-grab:overlay-image", handler),
  onError: handler => subscribe("screen-grab:error", handler),
});
