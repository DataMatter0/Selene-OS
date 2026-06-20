# Selene OS — Codebase Restructure Plan

**Goal:** Clean separation of concerns, smaller files, no file doing more than one job.
**Rule:** Pure reorganization — no logic changes during restructure. Each step is independently committable.

---

## Target Structure

```
selene_os/
├── selene_server.py          # Entry point only — imports and wires handlers
├── selene_brain/
│   ├── __init__.py
│   ├── llm_chat.py           # LLMChat class (chat loop, agent swap, presence)
│   ├── llm_caller.py         # LM Studio API client
│   ├── lm_studio_manager.py  # Model load/unload/skip logic
│   ├── prompter.py           # System prompt builder
│   ├── agent_memory.py       # SQLite layer
│   ├── memory_extractor.py   # Background memory extraction
│   ├── conversation_manager.py
│   ├── trajectory_compressor.py
│   ├── mood_observer.py
│   └── tool_suggestion.py
│
├── server/                   # NEW — server logic extracted from selene_server.py
│   ├── __init__.py
│   ├── config.py             # Constants, env vars, _normalize()
│   ├── startup.py            # _init_selene(), lifespan(), background tasks
│   ├── state.py              # get_state(), _cached_emotion, broadcast()
│   ├── tool_pipeline.py      # process_message(), _execute_tool_and_respond(),
│   │                         # _generate_tool_reasoning_background(), set_last_message_status()
│   ├── utils.py              # clean_xml_tags(), split_response_chunks(),
│   │                         # _format_tool_data(), extract_presence_decision()
│   └── handlers/             # WS message router — one file per domain
│       ├── __init__.py
│       ├── chat.py           # chat, force_generate, clear_memory, rollback_last_turn
│       ├── conversations.py  # new_conversation, load_conversation, rename, list, delete
│       ├── memory.py         # get_memory, save_memory, force_memory_extract,
│       │                     # get/add/remove tool_phrases
│       ├── manifest.py       # add_task, update_task, toggle_task, delete_task,
│       │                     # reorder_tasks, update_guidelines, reorganize_manifest,
│       │                     # compile_and_push_manifest, get_manifest, todo_get, todo_clear
│       ├── knowledge.py      # knowledge_get_state, save/delete/update/sync card,
│       │                     # search_web, enrich, summarize, arxiv, rss_*
│       ├── system.py         # get_state, get_models, set_model, toggle_agent,
│       │                     # save_dashboard_layout, run_latency_test, update_gamepad_config,
│       │                     # force_memory_extract, get_discord_status, check_discord_connectivity
│       └── steam.py          # get_steam_games, launch_steam_game + get_steam_games_list()
│
├── tools/
│   ├── __init__.py
│   ├── schema.py             # BaseTool interface
│   ├── registry.py           # ToolRouter
│   ├── manifest.py           # NEW — ManifestTool (extracted from builtin.py)
│   ├── todo.py               # NEW — TodoTool (extracted from builtin.py)
│   ├── memory_tool.py        # NEW — MemoryTool + ChronicleTool (extracted from builtin.py)
│   ├── status.py             # NEW — StatusTool (extracted from builtin.py)
│   ├── meta_insight.py       # (already isolated)
│   ├── presence.py           # (already isolated)
│   ├── knowledge.py          # (already isolated)
│   ├── runereader.py         # (already isolated)
│   ├── schedule.py           # (already isolated)
│   └── file_manager.py       # (already isolated)
│
├── configs/
│   ├── selene_config.json
│   ├── sage_config.json
│   ├── selene_prompt.txt     # gitignored — personal
│   └── sage_prompt.txt       # gitignored — personal
│
├── scripts/
│   └── parse_html.js         # (patch.py and restore_script.py deleted)
│
├── .env.example
├── requirements.txt
├── README.md
├── CHANGELOG.md
└── RESTRUCTURE_PLAN.md
```

---

## Steps

Each step = one commit. Do them in order — later steps depend on earlier ones.

---

### Step 1 — Delete dead scripts
**Files:** `scripts/patch.py`, `scripts/restore_script.py`
**What:** One-time emergency scripts with no future use. Already applied.
**Commit:** `chore: remove dead one-time scripts`

---

### Step 2 — Add placeholder prompt files
**Files:** `configs/selene_prompt.txt`, `configs/sage_prompt.txt`, `configs/sage_tools_context.md` (new, gitignored)
**What:** Repo currently has no prompt file — cloning it produces import errors.
Add template versions so the project actually runs after clone.
**Commit:** `chore: add placeholder prompt templates for fresh installs`
**Note:** Real prompts stay gitignored. Templates are just instructional stubs.

---

### Step 3 — Split `tools/builtin.py` into 4 files
**Files:** Create `tools/manifest.py`, `tools/todo.py`, `tools/memory_tool.py`, `tools/status.py`
**What:** `builtin.py` contains 4 unrelated tool classes (~1167 lines).
Cut each class into its own file. Update `tools/__init__.py` imports.
Delete `tools/builtin.py`.
**Commit:** `refactor: split builtin.py into manifest, todo, memory_tool, status`

---

### Step 4 — Extract `server/` package from `selene_server.py`
Do this in sub-steps to keep diffs readable:

#### Step 4a — `server/config.py`
Move: `BASE_URL`, `DESIRED_MODEL`, `SERVER_HOST`, `SERVER_PORT`, `_normalize()`
**Commit:** `refactor: extract server config and normalize helper`

#### Step 4b — `server/utils.py`
Move: `clean_xml_tags()`, `split_response_chunks()`, `_format_tool_data()`, `extract_presence_decision()`
**Commit:** `refactor: extract server utility functions`

#### Step 4c — `server/state.py`
Move: `_cached_emotion`, `get_state()`, `broadcast()`, `_state_broadcaster()`
**Commit:** `refactor: extract state management`

#### Step 4d — `server/tool_pipeline.py`
Move: `process_message()`, `_execute_tool_and_respond()`, `_generate_tool_reasoning_background()`, `set_last_message_status()`, `update_memory_and_energy()`
**Commit:** `refactor: extract tool pipeline`

#### Step 4e — `server/startup.py`
Move: `_init_selene()`, `lifespan()`, background task starters (`_timer_poller`, `_state_broadcaster`, `_gamepad_poller_thread`)
**Commit:** `refactor: extract startup and lifespan`

#### Step 4f — `server/handlers/`
Split the 3500-line WS `if/elif` chain into domain handler files.
Each handler receives `(websocket, data, selene, loop)` and handles its own cases.
Main `websocket_endpoint` becomes a thin dispatcher.
**Commit:** `refactor: split websocket handler into domain modules`

#### Step 4g — `server/handlers/steam.py`
Move `get_steam_games_list()` (currently line 37 of selene_server.py, above imports) into its handler.
**Commit:** `refactor: move steam utilities into handlers/steam.py`

---

### Step 5 — Clean up `selene_server.py`
After extraction, `selene_server.py` becomes:
```python
# selene_server.py — entry point
from server.startup import lifespan
from server.handlers import register_all_handlers
from fastapi import FastAPI
...
app = FastAPI(lifespan=lifespan)
register_all_handlers(app)
```
~30-50 lines. Pure wiring, no logic.
**Commit:** `refactor: reduce selene_server.py to entry point`

---

### Step 6 — Add section TOC comments to remaining large files
**Files:** `selene_brain/llm_chat.py`, `selene_brain/agent_memory.py`, `tools/knowledge.py`
**What:** Not splitting these further (they're cohesive enough), but add a
`# ── SECTIONS ──` comment block at the top of each so a reader can navigate.
**Commit:** `docs: add section TOCs to large cohesive files`

---

### Step 7 — Update README and CHANGELOG
Reflect new structure in README project tree.
Move current "Unreleased" to v0.3 in CHANGELOG.
**Commit:** `docs: update README and CHANGELOG for v0.3 restructure`

---

## After restructure — Dev Manifest

The "full reasoning doc" idea is worth doing. The right form is a `CODEBASE.md` —
not generated, maintained by hand (or with Claude) as the code evolves.

Structure:
- One section per module/package
- What it does, what it owns, what it does NOT do
- Key design decisions and why
- What to read next (dependencies, callers)

This is more useful than a Gemini-style outline because it captures *intent* and
*constraints*, not just structure. A new dev (or a future Claude session) can read
it and understand why things are shaped the way they are, not just where they are.

Will be written after Step 7 as `CODEBASE.md`.

---

## What NOT to change during restructure

- No logic changes — pure file moves and import updates
- No renaming of functions or classes
- No adding features
- `selene_brain/` internal structure stays as-is (already well-organized)
- `tools/knowledge.py` stays as one file (cohesive, already isolated)

---

## Current state before restructure

| File | Lines | Problem |
|---|---|---|
| `selene_server.py` | 3541 | 6+ domains, WS chain is 2000+ lines |
| `tools/builtin.py` | 1167 | 4 unrelated tool classes |
| `selene_brain/llm_chat.py` | 886 | Clean now (CLI removed) ✅ |
| `tools/knowledge.py` | 1042 | Large but cohesive, low priority |
| `selene_brain/agent_memory.py` | 714 | Large but cohesive, add TOC only |
