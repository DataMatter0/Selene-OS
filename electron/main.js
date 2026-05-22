/**
 * electron/main.js — Selene OS Electron main process
 *
 * Start sequence (Option B — separate processes):
 *   1. python selene_server.py    ← run this first in a terminal
 *   2. npm start                  ← then launch Electron
 */

const { app, BrowserWindow, ipcMain, shell } = require("electron");
const path = require("path");
const fs   = require("fs");

const isDev    = process.argv.includes("--dev");
const NOTES_DIR = path.join(__dirname, "../notes");

// Ensure notes directory exists on startup
if (!fs.existsSync(NOTES_DIR)) {
  fs.mkdirSync(NOTES_DIR, { recursive: true });
}

// ── Window creation ───────────────────────────────────────────────────────────

function createWindow() {
  const win = new BrowserWindow({
    width:           1360,
    height:          820,
    minWidth:        960,
    minHeight:       640,
    backgroundColor: "#07051a",
    title:           "SELENE OS · v0.1 · 2026",
    frame:           false,
    titleBarStyle:   "hidden",
    webPreferences: {
      nodeIntegration:  false,
      contextIsolation: true,
      preload:          path.join(__dirname, "preload.js"),
    },
  });

  win.loadFile(path.join(__dirname, "../renderer/index.html"));

  if (isDev) {
    win.webContents.openDevTools({ mode: "detach" });
  }

  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
}

// ── IPC: window controls ──────────────────────────────────────────────────────

ipcMain.on("window-close",    () => {
  const win = BrowserWindow.getFocusedWindow();
  if (win) win.close();
});
ipcMain.on("window-minimize", () => BrowserWindow.getFocusedWindow()?.minimize());
ipcMain.on("window-maximize", () => {
  const win = BrowserWindow.getFocusedWindow();
  if (win) win.isMaximized() ? win.unmaximize() : win.maximize();
});

// ── IPC: notes (file system) ──────────────────────────────────────────────────

/**
 * Save a note.
 * filename is sanitised — only alphanumerics, spaces, hyphens and underscores allowed.
 * Files are stored as plain .txt in the notes/ folder.
 */
ipcMain.handle("note:save", async (_event, { filename, content }) => {
  try {
    const safe = filename
      .replace(/[^a-zA-Z0-9 _\-]/g, "")
      .trim()
      .replace(/\s+/g, "_")
      .slice(0, 80) || `note_${Date.now()}`;

    const filepath = path.join(NOTES_DIR, `${safe}.txt`);
    fs.writeFileSync(filepath, content, "utf8");
    return { ok: true, filename: `${safe}.txt` };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

/**
 * List all saved notes (filename + last-modified timestamp).
 */
ipcMain.handle("note:list", async () => {
  try {
    const files = fs.readdirSync(NOTES_DIR)
      .filter(f => f.endsWith(".txt"))
      .map(f => {
        const stat = fs.statSync(path.join(NOTES_DIR, f));
        return { filename: f, modified: stat.mtimeMs };
      })
      .sort((a, b) => b.modified - a.modified);   // newest first
    return { ok: true, files };
  } catch (err) {
    return { ok: false, files: [], error: err.message };
  }
});

/**
 * Load the content of a specific note.
 */
ipcMain.handle("note:load", async (_event, filename) => {
  try {
    const safe    = path.basename(filename);        // prevent path traversal
    const content = fs.readFileSync(path.join(NOTES_DIR, safe), "utf8");
    return { ok: true, content };
  } catch (err) {
    return { ok: false, content: "", error: err.message };
  }
});

/**
 * Delete a note.
 */
ipcMain.handle("note:delete", async (_event, filename) => {
  try {
    const safe = path.basename(filename);
    fs.unlinkSync(path.join(NOTES_DIR, safe));
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

// ── App lifecycle ─────────────────────────────────────────────────────────────

app.whenReady().then(createWindow);

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
