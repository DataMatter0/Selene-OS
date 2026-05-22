/**
 * electron/preload.js
 * Exposes a minimal, safe IPC bridge to the renderer.
 * contextIsolation is ON — never expose require() directly.
 *
 * Available as window.seleneBridge in the renderer.
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("seleneBridge", {
  // ── Window controls ─────────────────────────────────────────────────────
  closeWindow:    () => ipcRenderer.send("window-close"),
  minimizeWindow: () => ipcRenderer.send("window-minimize"),
  maximizeWindow: () => ipcRenderer.send("window-maximize"),

  // ── Notes (file system) ─────────────────────────────────────────────────
  // Returns Promise<{ ok, filename }>
  saveNote:   (filename, content)  => ipcRenderer.invoke("note:save",   { filename, content }),
  // Returns Promise<{ ok, files: [{filename, modified}] }>
  listNotes:  ()                   => ipcRenderer.invoke("note:list"),
  // Returns Promise<{ ok, content }>
  loadNote:   (filename)           => ipcRenderer.invoke("note:load",   filename),
  // Returns Promise<{ ok }>
  deleteNote: (filename)           => ipcRenderer.invoke("note:delete", filename),
});
