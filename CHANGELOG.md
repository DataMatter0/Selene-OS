# Changelog — Selene OS

Format: `[vX.Y] YYYY-MM-DD — Title`
Each version lists what changed and what's safe to commit.

---

## [v0.2] 2026-06-08 — Multi-Agent Architecture + Core Pipeline

### Added
- **Two-agent system** — Selene (companion) and Sage (oracle) hot-swap via `@agent` ping or UI toggle. Separate SQLite DBs, prompt files, tool permissions, and memory per agent. No model reload — both share `google/gemma-3n-e4b`.
- **Presence layer** (`run_choice_layer`) — RESPOND / OBSERVE / IGNORE gating before every turn. Agents can stay silent based on context and emotional state. OBSERVE logs thoughts to `meta_insight_log` without responding.
- **Tool suggestion layer** (`selene_brain/tool_suggestion.py`) — phrase classifier → binary LLM gate → confidence threshold. Tools fire via slash command, keyword trigger, or autonomous `<tool_call>` XML tag. Low-confidence paths inject a suggestion warning instead of executing.
- **Tool phrase classifier** — SQLite `tool_phrases` table with hit/miss accuracy tracking per phrase.
- **`meta_insight` tool** — agents query their own decision logs, tool-use records, and emotional state traces. Sage access to Selene's logs is opt-in via `grant_sage` command.
- **Training data pipeline** — every tool call generates a post-hoc reasoning entry in `tool_reasoning_log`. Reasoning is LLM-generated: *"Was this call actually necessary?"*
- **Chunked delivery** — responses split into 2–4 sentence chunks with random inter-chunk delays. Each chunk stored separately in `working_memory` with `chunk_group` UUID so reloads preserve the original split.
- **Agent attribution** — `working_memory` and `local_history` label turns as `Selene:`, `Sage:`, `Ghost:`. Model sees attributed group-chat-style history.
- **Message status persistence** — `sent` / `read` / `observed` markers written to both SQLite and `working_memory` via `set_last_message_status()`. Survive conversation reloads.
- **Continue button** — `force_generate` WebSocket message bypasses presence layer and runs `chat()` directly. Useful when agent chose OBSERVE but you want a response.
- **Emotional state injection** — `<emotional_state>` block in system context. Cached post-turn, not on 2s poll, to avoid neutral noise in logs.
- **Model loading skip** — `_init_selene()` skips LM Studio load/unload if desired model already active. `set_model` WS handler does the same check before triggering a reload.
- **`_format_tool_data()`** — all tool result paths now use structured formatting instead of raw `str()`. List-of-dict results (e.g. meta_insight queries) render as numbered blocks the model can narrate.

### Changed
- Removed onboarding logic entirely — pipeline starts from turn 1.
- `selene_brain/` package replaces root-level `llm_chat.py`, `llm_caller.py`, `lm_studio_manager.py`.
- `tools/` package replaces root-level `tools.py` and `tool_schema.py`.
- `clean_xml_tags()` uses placeholder swap to preserve `<think>` and `<tool_reasoning>` blocks.
- `reasoning_content` from Gemma normalized into `<think>` blocks in `call_llm()`.
- System prompt race condition fixed — background threads set `_prompt_dirty = True`, never call `_refresh_system_prompt()` directly.
- `save_memory` maps "soul" key to `prompt_path`, not `SOUL_FILE` — edits now reach the file the model actually reads.
- Fallback compactor summary block changed from `role: system` to `role: user`.

### Model
- `google/gemma-3n-e4b` — set as base model for both Selene and Sage in agent config JSONs.

### Safe to commit
- `selene_brain/`, `tools/`, `selene_server.py`, `configs/` (excluding `soul.md`, `sage_soul.md`), `requirements.txt`, `.env.example`, `README.md`, `LICENSE`, `CHANGELOG.md`, `SELENE_SYSTEM.md`, `SELENE_INNER_STATE_FEATURE.md`

---

## [v0.1] 2026 — Initial Commit

### Added
- FastAPI + Uvicorn WebSocket server (`selene_server.py`)
- LM Studio OpenAI-compatible client (`llm_caller.py`)
- `LLMChat` core loop with `PromptBuilderMixin`, `ConversationManagerMixin`, `MemoryExtractorMixin`
- SQLite memory layer (`agent_memory.py`)
- Basic tool routing and tool schema
- Electron shell + React frontend
- `.env.example` with all supported integrations documented

---

## [v0.3] 2026-06-11 — Codebase Restructure

### Changed

**`tools/` split** — `tools/builtin.py` (1167 lines) deleted. Its four unrelated tool classes now live in focused files:
- `tools/manifest.py` — `ManifestTool` (task graph, Obsidian sync, LLM reorganize)
- `tools/todo.py` — `TodoTool` (step-by-step plan tracker)
- `tools/memory_tool.py` — `ChronicleTool` + `MemoryTool`
- `tools/status.py` — `StatusTool`
- `tools/registry.py` import block updated to pull from the four new modules
- `tools/__init__.py` docstring updated to match

**`server/` package extracted** — `selene_server.py` reduced from 3541 → 406 lines. All domain logic moved into:
- `server/config.py` — `BASE_URL`, `DESIRED_MODEL`, `SERVER_HOST`, `SERVER_PORT`, `_normalize()`
- `server/utils.py` — `clean_xml_tags()`, `split_response_chunks()`, `_format_tool_data()`, `extract_presence_decision()`
- `server/state.py` — `selene_ref`, `clients`, `_cached_emotion`, `set_selene()`, `get_state()`, `broadcast()`, `_state_broadcaster()`
- `server/startup.py` — `_init_selene()`, `lifespan()`, `_gamepad_poller_thread()`, `_timer_poller()`
- `server/tool_pipeline.py` — `process_message()`, `_execute_tool_and_respond()`, `update_memory_and_energy()`, `set_last_message_status()`, `_generate_tool_reasoning_background()`
- `server/handlers/chat.py` — chat, force_generate, rollback_last_turn, clear_memory (full presence layer)
- `server/handlers/conversations.py` — new/load/rename/list/delete conversation
- `server/handlers/memory.py` — get/save memory, force extract, tool phrase management
- `server/handlers/manifest.py` — full task CRUD, guidelines, reorganize, compile_and_push, todo
- `server/handlers/knowledge.py` — knowledge board, web search, arXiv, RSS
- `server/handlers/system.py` — state, models, set_model, toggle_agent, latency test, Discord, integrations
- `server/handlers/steam.py` — Steam library scan + local game launcher (moved from selene_server.py line 37)
- `server/handlers/youtube.py` — youtube_query, search, watch_start, segment_push, co-watching chat
- `server/handlers/story.py` — full Infinite Story Engine (12 handlers, auto-compaction at 50 turns)
- `server/handlers/misc.py` — maps, polymarket, document/RuneReader, notion, meta_insight

The thin `selene_server.py` now only: wires FastAPI + CORS, registers REST routes (`/yt-proxy`, `/state`, `/steam/image`, `/sounds`, `/v1/models`, `/v1/chat/completions`), and dispatches WebSocket messages through a 10-handler chain.

### Architecture notes
- Circular import risk in `tool_pipeline.py` resolved via lazy `_selene()` accessor — imports `state` inside a function, not at module level
- `global_guide_button` mutated by `server/handlers/system.py` via `_startup.global_guide_button = int(...)`
- `_cached_emotion` mutated directly in `server/handlers/chat.py` post-turn (same pattern as original)
- Handler signature: `async def handle(websocket, data, loop) -> bool` — returns `True` if handled, `False` to pass to next handler. YouTube handler has extra `yt_state` arg for per-session dormancy tracking

### Safe to commit
- `server/`, `tools/manifest.py`, `tools/todo.py`, `tools/memory_tool.py`, `tools/status.py`, `tools/registry.py`, `tools/__init__.py`, `selene_server.py`, `README.md`, `CHANGELOG.md`, `CODEBASE.md`
- Do NOT commit: `.env`, `configs/soul.md`, `configs/sage_soul.md`, `configs/selene_prompt.txt`, `configs/sage_prompt.txt`, `memories/`, `conversations/`, `selene_state.json`

---

## Unreleased

_Track in-progress work here. Move to a versioned block when committing._

