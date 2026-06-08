# SELENE OS — Technical System Summary
*Compiled June 2026 from full codebase inspection*

---

## What It Is

Selene OS is a **local-first AI companion desktop application**. It is not a chat wrapper around a cloud API — it is a full operating environment for a persistent AI agent that runs on your own hardware, remembers who you are over time, routes tasks through a modular tool system, and presents itself through a custom retro-futurist UI. The second agent, **Sage**, shares the same infrastructure but maintains a separate identity, manifest, and purpose.

---

## The Stack at a Glance

| Layer | Technology | Why |
|---|---|---|
| UI Shell | Electron | Native desktop, system access, custom titlebar |
| UI Runtime | React 18 via CDN + Babel Standalone | No build step, fast iteration, JSX in-browser |
| UI Styling | Vanilla CSS with CSS variables | Theme-aware, no framework overhead |
| Backend | Python FastAPI + Uvicorn (async) | Async WebSocket + REST on a single process |
| LLM Inference | LM Studio REST API (OpenAI-compatible) | Local inference, no cloud dependency |
| Conversation DB | SQLite (via custom `db` layer) | Persistent, zero-config, fast |
| Memory Files | Markdown flat files | Human-readable, directly editable, git-friendly |
| Tool System | Custom Python plugin router | Modular, keyword-triggered, swappable |
| Game Engine | SQLite + custom RPG server handlers | Stateful, persistent campaign data |
| Voice/Gamepad | pynput + pygame (background threads) | Low-latency hardware input without a UI event loop |

---

## Architecture: Two Halves

The system splits cleanly into a **renderer process** (the UI) and a **server process** (the brain). They communicate exclusively over a local WebSocket.

```
┌─────────────────────────────┐        ┌──────────────────────────────────┐
│      Electron Renderer       │        │      selene_server.py (Uvicorn)  │
│                             │        │                                  │
│  React Component Tree        │◄──────►│  WebSocket /ws                  │
│  ├── Dashboard (widgets)     │  WS    │  REST endpoints (/state, /v1/…) │
│  ├── ChatView                │        │                                  │
│  ├── ToolsView               │        │  LLMChat ──► LM Studio API      │
│  ├── KnowledgeBoard          │        │  ToolRouter ──► 14 tools        │
│  ├── StoryEngineView         │        │  SQLite DBs (convos, story)     │
│  ├── LeftPanel (emotion viz) │        │  Markdown memory files          │
│  └── RightPanel (diagnostics)│        │  Discord bot (background)       │
└─────────────────────────────┘        └──────────────────────────────────┘
```

---

## The Frontend

### Why Electron + CDN React

The choice to skip a build system (no webpack, no Vite, no npm run dev) was intentional. React and Babel are loaded from cdnjs at runtime. Components are separate `.jsx` files loaded as `<script type="text/babel">` tags. This means:

- **Zero build step** — edit a file, reload Electron, see the change
- **No bundler configuration to maintain**
- Tradeoff: slightly slower initial parse (Babel transpiles in-browser), and no tree-shaking

This is a pragmatic choice for a solo-developed personal system where iteration speed matters more than production bundle size.

### Component Architecture

The UI has two primary **modes**:

**Dashboard mode** (`view === "menu"`) — The home screen. A three-column widget grid where each column holds an independently selectable widget. Widgets can be dragged to swap positions (pointer-capture based drag, not HTML5 drag API), resized via `¼ ½ ¾ ■` snapper buttons that set CSS `fr` values on the grid, and expanded to fullscreen. Widget state (YouTube chat history, Notion navigation, etc.) lives in the Dashboard component and survives position swaps — only the widget ID moves, not the state.

**View mode** — Every other screen (chat, board, tools, memory, settings) renders as a full-panel view. Navigation between views uses a string state variable (`view`) and a menu index.

### State Management

There is no Redux, no Zustand, no external state library. Everything is React `useState` and `useRef` at the `SeleneOS` root component level, passed down as props. This is intentional for a system this size — the component tree is shallow enough that prop drilling is manageable and the simplicity is worth it. The WebSocket message handler (`handleWsMessage`) is the single point where server events update React state.

### WebSocket Protocol

The client maintains one persistent WebSocket to `ws://127.0.0.1:8765/ws`. The connection uses exponential backoff retry (3s → 6s → 12s → 24s → 30s cap) so the console isn't spammed when the server is down. All messages are JSON with a `type` discriminator field. The client sends typed action objects; the server responds with typed event objects. There is no request/response correlation — the server can push events at any time (state broadcasts, autonomy events, timer expiries).

Story engine messages (`story_*`) are routed via a custom event bus: the WebSocket handler dispatches them as `new CustomEvent("story_event", { detail: msg })` on `window`, and `StoryEngineView` subscribes with `window.addEventListener("story_event", ...)`. This decouples the story engine from the main WS handler cleanly.

### The Dashboard Drag System

The drag system was rebuilt from scratch to fix flicker and cross-column failures. Key decisions:

- **`setPointerCapture`** on the grip handle element — all `pointermove` / `pointerup` events continue firing on that element even when the mouse moves to another column. This is what makes cross-column drops reliable.
- **Direct DOM manipulation** for ghost position — `ghostElRef.current.style.left = x` bypasses React's render cycle entirely. The ghost moves at native speed (no React overhead per frame).
- **`useRef` for mutable drag state**, `useState` only for what needs to trigger a re-render (which column is highlighted as a drop target). This prevents the entire Dashboard from re-rendering 60 times per second during a drag.

---

## The Backend

### Server Process

`selene_server.py` is a single FastAPI application run by Uvicorn. It serves:

- `WebSocket /ws` — the primary UI communication channel
- `GET /state` — health check / state snapshot
- `GET /steam/image/{appid}` — serves Steam library artwork or SVG fallback cartridges
- `GET /yt-proxy` — YouTube iframe proxy (provides a valid HTTP origin so YouTube embeds work in Electron's `file://` context)
- `GET /sounds/{filename}` — serves local audio assets
- `POST /v1/chat/completions` + `GET /v1/models` — OpenAI-compatible API so Hermes Agent and other tool frameworks can point at Selene as their inference backend

The lifespan context manager starts `_init_selene` as a **background task** (non-blocking). This means the WebSocket endpoint is available immediately when the server starts, even while Selene is contacting LM Studio. The UI shows `OFFLINE` and flips to `ONLINE` when init completes and broadcasts state.

### The LLM Layer

`LMStudioManager` handles model lifecycle — listing loaded models, loading a desired model by path, unloading. `LLMCaller` is a thin httpx client that calls LM Studio's OpenAI-compatible `/v1/chat/completions` endpoint with Selene's assembled prompt.

`LLMChat` is the core agent class. It:
1. Builds the system prompt from `soul.md` + tool documentation + user/character profiles
2. Maintains a rolling `working_memory` (the recent conversation window)
3. Routes tool calls from the LLM's output
4. Runs a background `_autonomy_monitor` thread that can trigger autonomous writing when Ghost has been quiet for 120 seconds

The LLM is treated as a text-in / text-out oracle. Tool use is implemented via XML tags in the response (`<tool_call name="tool_name">args</tool_call>`) that the server parses out and routes, rather than relying on OpenAI function-calling format. This works with any instruction-following model regardless of whether it supports native function calling.

### Choice Layer

Before every user message is processed by the LLM, it passes through `run_choice_layer`. This is a fast secondary LLM call (or heuristic) that returns one of three gating decisions:

- `RESPOND` — process normally, generate a reply
- `OBSERVE` — acknowledge internally, stay silent (the message is logged but not replied to)
- `IGNORE` — discard entirely

This allows Selene to have contextually appropriate social behavior — she doesn't respond to every ambient thing Ghost says if he's clearly talking to someone else or just thinking aloud.

### Memory Architecture

Memory is split across three layers by persistence and granularity:

**Working memory** — in-RAM list of recent message pairs. Capped at `memory_window * 2` entries. Lost on server restart. Used for conversational coherence within a session.

**Conversation database** — SQLite. Every turn is logged with role, content, thought trace, and a read status (`sent` → `read` / `observed` / `ignored`). Conversations are named, loadable, and deletable from the UI.

**Long-term memory files** — Markdown files in `memories/`:
- `soul.md` — Selene's core identity, authored by Ghost, stable
- `tools_context.md` — Tool documentation injected into every system prompt
- `user_profile.md` — Ghost's behavioral profile, auto-grown by background extraction
- `character_profile.md` — Selene's internalized insights about Ghost

The extraction pipeline (`maybe_extract_memory`, `force_extract_memory`) runs after every N turns as a background LLM call that reads the recent conversation and distills persistent facts into the profile files. This is how Selene "learns" over time without fine-tuning.

### Tool Router

`ToolRouter` is a simple registry. Each tool is a Python class with:
- A `name` string
- A `check_and_trigger(user_input)` method — keyword/pattern matching that returns args if triggered
- An `execute(args)` method — does the actual work, returns `{"status": "success", "data": ...}`
- An optional `dormant` flag — tools that lack credentials (Notion without API key, Spotify without client ID) go dormant and return polite errors rather than crashing

Tools registered: `manifest_manager`, `todo`, `schedule_manager`, `knowledge_manager`, `file_manager`, `document_reader`, `runereader`, `maps`, `notion`, `youtube`, `presence`, `homeassistant`, `spotify`, `story_engine`.

The router checks all tools on every message (looking for keyword triggers) before falling back to the raw LLM call. This gives deterministic tool use for well-defined commands without relying on the LLM to decide.

---

## The Dual-Agent System

Selene and Sage are two distinct agent profiles backed by the same infrastructure:

| | Selene | Sage |
|---|---|---|
| Identity | Personal AI companion, emotional, curious | Oracle/developer analyst, structured, precise |
| Manifest | Daily habits, todos, personal planning | Dev backlog, Obsidian vault, technical priorities |
| Tools | All tools including YouTube co-watch | All tools including arXiv research tab |
| Color | Cyan (`#4dd9f7`) | Purple (`#c084fc`) |
| Knowledge cards | Can save, cannot see Sage's cards | Can see and filter both Selene's and own cards |

Switching agents calls `selene.swap_agent(name)` which hot-swaps the system prompt, soul file reference, and active manifest without restarting the server. The UI reads `seleneState.active_agent` to re-color itself, relabel diagnostics, and gate features.

---

## The Knowledge System

Cards are the atomic unit of external knowledge. Each card has a title, content, type, optional extended content (full text if it was summarized on save), position on the spatial board, and now a `creator` tag (`ghost`, `selene`, or `sage`).

The `KnowledgeBoard` component renders cards on a 4000×3000 pixel infinite canvas with zoom/pan (scroll wheel + pointer drag). Cards can be dragged to position, snapped into clusters when placed near each other, and promoted to the Kanban view where they align to lane columns by status.

The catalog sidebar lists cards not currently on the board (backlog). The web search integration clips search results directly into the catalog. The arXiv tool (Sage-only tab in ToolsView) finds research papers and clips them as typed cards.

Access control: Selene's view filters out any card with `creator === "sage"`. Sage sees everything and gets a creator filter strip to isolate cards by origin.

---

## The Story Engine (Infinity Sim)

A self-contained tabletop RPG engine built on top of the same LLM infrastructure. Persistent state lives in a dedicated SQLite database (`story_engine.db`) separate from conversation history.

Architecture:
- **Profiles** — persistent player identities (Ghost = human, Selene = AI companion, fill-in slots for AI-controlled NPCs)
- **Characters** — locked stat sheets bound to profiles. Stats are custom-named (not hardcoded D&D — you can make it "Cyber-Ninja with Agility/Interface/Resolve"). Once created, stats only change via explicit Level Up point spends.
- **Worlds** — campaign containers with name, difficulty level (1-10), major goal, lore, ambient events, and chronological milestone roadmap
- **Timeline** — append-only log of all DM narrations and player actions with dice roll details
- **Dice resolution** — D20 + stat modifier vs. world floor (world difficulty × 2). Results determine success/failure and damage. The DM is the LLM with a structured system prompt containing the full world state, active party stats, and recent timeline

The StoryEngineView listens for server events via a custom `window` event bus rather than the main WS handler — the `story_*` message types are dispatched as `CustomEvent("story_event")` and the component handles them internally. This keeps 1300 lines of RPG UI logic completely isolated from the main app.

---

## The Emotional Visualizer (Left Panel)

Replaced the VRM avatar placeholder. Two tabs:

**NOW** — Live gauges for `creative_energy` and `mood_index` (0-100 bars), plus a state field grid showing all available telemetry (status, memory count, active conversation, emotion label, context token count if provided). Shows a pulsing "GENERATING..." indicator when Selene is writing.

**OVER TIME** — A 60-point rolling ring buffer samples state every 5 seconds. Rendered as inline SVG sparklines (no charting library — raw `<polyline>` with computed point coordinates). The last 8 snapshots are logged in a timestamped list below the charts.

The buffer is stored in a `useRef` (not `useState`) so updates don't trigger re-renders — only the tab switch triggers a re-render of the history view.

---

## Scaling and Monitor Handling

The viewport scaling script caps scale at `1.0` — it only scales DOWN for sub-1280×720 windows, never up. This prevents the fullscreen-on-secondary-monitor zoom problem where the UI would stretch to fill a large display. On a large or high-DPI monitor the UI fills the window normally using CSS viewport units. On unusually small windows it letterboxes to fit.

The server explicitly binds to `127.0.0.1` (IPv4) rather than `localhost`. On Windows, `localhost` can resolve to `::1` (IPv6 loopback) while the browser connects to `127.0.0.1` (IPv4 loopback) — two different sockets that never meet. Explicit IPv4 on both server and client removes this ambiguity entirely.

---

## What Makes This Different

Most "personal AI" systems are thin wrappers: API key in, chat UI out. Selene is a local operating environment with:

1. **No cloud dependency** — inference runs on your LAN via LM Studio. The only outbound calls are optional integrations (Notion, Discord, YouTube metadata).
2. **Persistent identity** — soul.md, user_profile.md, and character_profile.md survive server restarts and accumulate over time. Selene remembers Ghost because the extraction pipeline writes facts to disk.
3. **Modular capability** — tools are Python files dropped in a folder. Adding a new tool means writing a class with `check_and_trigger` and `execute`. No framework, no decorators, no config.
4. **Dual-agent architecture** — two agents, two worldviews, one infrastructure. Sage handles development and research; Selene handles daily life and companionship. They share memory infrastructure but maintain separate identities, manifests, and knowledge access.
5. **A real UI** — not a terminal, not a web dashboard. A scanline aesthetic desktop environment with gamepad support, spatial knowledge boards, co-watching YouTube with reactions, an RPG engine, and a drag-and-drop widget system. The interface is part of the experience.
