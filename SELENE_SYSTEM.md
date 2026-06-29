# Selene OS — Frontend Reference

This document covers the Electron/React frontend — why it's built the way it is, and what's non-obvious. For backend architecture, agent runtime, and data flow see `ARCHITECTURE.md`.

---

## Stack

| Layer | Technology | Why |
|---|---|---|
| Shell | Electron | Native desktop, system access, custom titlebar |
| UI Runtime | React 18 via CDN + Babel Standalone | No build step, fast iteration, JSX in-browser |
| Styling | Vanilla CSS with CSS variables | Theme-aware, no framework overhead |

### Why no bundler

React and Babel are loaded from cdnjs at runtime. Components are separate `.jsx` files loaded as `<script type="text/babel">` tags. This means:

- **Zero build step** — edit a file, reload Electron, see the change
- **No bundler config to maintain**
- Tradeoff: Babel transpiles in-browser (slightly slower initial parse), no tree-shaking

Pragmatic choice for a solo-developed personal system where iteration speed matters more than bundle size.

---

## Two Modes

**Dashboard mode** (`view === "menu"`) — the home screen. Three-column widget grid where each column holds an independently selectable widget.

**View mode** — every other screen (chat, board, tools, memory, settings) renders as a full-panel view. Navigation is a string `view` state variable plus a menu index.

---

## State Management

No Redux, no Zustand. Everything is React `useState` and `useRef` at the `SeleneOS` root component, passed down as props. The WebSocket message handler (`handleWsMessage`) is the single point where server events update React state. The component tree is shallow enough that prop drilling is manageable.

---

## WebSocket Protocol

One persistent WebSocket to `ws://127.0.0.1:8765/ws`. Exponential backoff retry on disconnect (3s → 6s → 12s → 24s → 30s cap). All messages are JSON with a `type` discriminator field. No request/response correlation — the server can push events at any time (state broadcasts, autonomy events, agent chunks, timer expiries).

Story engine messages (`story_*`) are routed via a custom event bus: `handleWsMessage` dispatches them as `new CustomEvent("story_event", { detail: msg })` on `window`, and `StoryEngineView` subscribes with `window.addEventListener("story_event", ...)`. Keeps ~1300 lines of RPG UI isolated from the main WS handler.

---

## Dashboard Widget System

Widgets can be dragged between columns, resized via `¼ ½ ¾ ■` snapper buttons (sets CSS `fr` values on the grid), and expanded to fullscreen. Widget state (e.g. YouTube chat history, Notion navigation) lives in the Dashboard component and survives position swaps — only the widget ID moves, not the state. Layout and active tab are persisted per agent in `pantheon_state.json` via the backend.

### Drag system

Built from scratch to fix flicker and cross-column failures:

- **`setPointerCapture`** on the grip handle — all `pointermove` / `pointerup` events keep firing on that element even when the mouse leaves the column. This is what makes cross-column drops reliable.
- **Direct DOM manipulation** for the ghost position — `ghostElRef.current.style.left = x` bypasses React's render cycle. Ghost moves at native speed with zero React overhead per frame.
- **`useRef` for mutable drag state**, `useState` only for what needs a re-render (drop target highlight). Prevents the full Dashboard from re-rendering 60× per second during a drag.

---

## Roster-Driven UI

The frontend has no hardcoded agent names. On boot, `GET /api/roster` returns the full agent list from `server/roster.py` (which scans `agents/*/config.json`). Components that need agent identity read from `seleneState.roster`:

- Boot screen — dynamically renders agent cards from roster
- Tab visibility in `ToolsView` — driven by `allowedTools` from the active agent's config
- Widget filtering — agent capabilities gate which widgets appear
- Dashboard layout and active tab — persisted and restored per agent slug
- Ping map — `@mention` routing built at server startup from roster slugs and display names

Adding an agent is adding a folder. No frontend code changes required.

---

## Emotional Visualizer (Left Panel)

Replaced the VRM avatar placeholder. Two tabs:

**NOW** — Live gauges for `creative_energy` and `mood_index` (0–100 bars), plus a state field grid showing available telemetry (status, memory count, active conversation, emotion label, context token count). Pulsing `GENERATING...` indicator when the active agent is writing.

**OVER TIME** — 60-point rolling ring buffer sampled every 5 seconds. Rendered as inline SVG sparklines — raw `<polyline>` with computed point coordinates, no charting library. Last 8 snapshots logged in a timestamped list below the charts. Buffer stored in `useRef` (not `useState`) so sample updates don't trigger re-renders.

---

## Scaling and Monitor Handling

The viewport scaling script caps scale at `1.0` — it only scales down for sub-1280×720 windows, never up. Prevents the fullscreen-on-secondary-monitor zoom problem where the UI would stretch to fill a large display.

The server binds explicitly to `127.0.0.1` (IPv4). On Windows, `localhost` can resolve to `::1` (IPv6) while the browser connects to `127.0.0.1` (IPv4) — two different sockets. Explicit IPv4 on both sides removes the ambiguity.

---

## What's Not Refactored Yet

`index.html` is the monolithic frontend file — all components, state, and handlers in one file. This is intentional for now; the iteration speed benefit outweighs the organizational cost at the current scale. The plan is to extract components into separate `.jsx` files once the feature set stabilizes. Nothing in the backend depends on the frontend file structure.
