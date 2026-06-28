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

## [v0.4] 2026-06-15 — Presence Layer Expansion + Emotion Pipeline + Discord Parity

### Added

**Presence layer — full response mode routing**
- Presence prompt always offers all three modes: RESPOND / REFLECT / INQUIRE. No `_low_confidence` gate hiding options.
- Low-confidence nudge injected when mode is CONVERSATIONAL and `last_entropy > 1.5` — tells Selene it's okay to ask Ghost to clarify rather than guess.
- `self._last_response_mode` stored post-presence for background threads to reference.

**REFLECT sub-mode — reflective extraction**
- When presence resolves to REFLECT, `reflective_turn=True` is passed to `maybe_extract_memory` and `update_memory_and_energy`.
- `reflective_turn` flag bypasses `MIN_TRIAGE_CHARS` gate and biases triage classifier toward SELENE/INSIGHT categories.

**INSIGHT triage category** (`selene_brain/memory_extractor.py`)
- New category `"INSIGHT"` — ephemeral realizations or perspective shifts Selene works out during reflection.
- Routed to `{agent}_insights.md` alongside standard memory files.
- `_FILE_MAP["INSIGHT"]` and `_SECTION_DESC["INSIGHT"]` wired in; category included in classifier filter.

**Insight folding at manifest compilation** (`selene_brain/llm_chat.py`)
- `compile_daily_manifest` reads `{agent}_insights.md`, folds stable insights into `character_profile`, clears the insights file after folding.
- Insights that accumulate across reflect turns become permanent character knowledge at next manifest compile.

**Discord — full presence layer capacity** (`selene_discord.py`)
- `run_choice_layer` called before every `process_message` in Discord.
- IGNORE → sends `*— (no response) —*` soft notification, early return.
- OBSERVE → sends `*— (observing) —*`, commits user turn to memory, early return.
- RESPOND → full `process_message_fn` with `response_mode` forwarded.
- `maybe_extract_memory` and `update_memory_fn` both receive `reflective_turn` derived from `response_mode`.

**Discord chunking parity**
- Discord now uses `split_response_chunks` (same 2–4 sentence grouping used by the UI) instead of single-message delivery.
- Inter-chunk delay matches UI feel: `random.uniform(1.2, 2.8) + len(chunk) * 0.008`, capped at 4.5s.
- `split_message` retained as safety fallback for chunks exceeding Discord's 2000-char limit.

**Emotion pipeline — model visibility**
- `get_mood_description` (`selene_brain/mood_observer.py`) now reports three signals per turn: dominant mood, immediate reaction, and largest last-turn emotional shift (direction + magnitude).
- `last_applied` dict tracked on `MoodObserver`; shift entries with `|delta| > 0.04` surface in mood description.
- `_build_turn_context` injects `<emotional_state>` block so the model sees live mood data every turn.

**Emotion data in meta insight records**
- `_after_chat_turn` consolidates emotion scoring + meta insight into a single `run_emotion_and_insight` background thread.
- Meta insight records now carry real classified emotion data, not the previous placeholder `{"energy": ..., "status": "idle"}`.
- Discord sessions tagged with `chat_response:discord` subcategory via `session_id.startswith("discord_")` detection.

**Error recovery — working memory persistence**
- On LLM error (both UI and Discord), the failed user turn and error string are appended to `working_memory` as a `user`/`assistant` pair.
- No injection logic, no probe detection — the failed turn surfaces naturally in context when Ghost asks about it.

### Changed

- `update_memory_and_energy` signature now includes `response_mode: str = "CONVERSATIONAL"` — fixes `NameError` when called from background threads.
- `start_loop` CLI path passes `reflective_turn=False` explicitly — resolves Pylance `_response_mode` undefined warning.
- `last_entropy` comparison guarded with `(getattr(..., None) or 0.0) > 1.5` — resolves Pylance `None >` operator warning.
- Removed `pending_reprompt` attribute from `startup.py` (no longer needed; working memory handles error context).
- Removed `/reprompt` WebSocket handler from `server/handlers/chat.py`.
- Removed probe-phrase detection and context-injection block from `selene_discord.py`.

### Fixed

- `NameError: name 'response_mode' is not defined` in `update_memory_and_energy` — variable was used but not in function scope.
- `selene_discord.py` tail truncation during Edit tool use — reconstructed via bash append; mangled collision line patched via Python string replacement. Rule established: never use Edit/Write on large files; always use bash Python replacement + `ast.parse` verify.
- Unicode em-dash (`—`) in source caused `assert OLD in src` failures — fixed by inspecting actual bytes with `repr()` then matching exactly.

### Safe to commit
- `selene_brain/llm_chat.py`, `selene_brain/memory_extractor.py`, `selene_brain/mood_observer.py`
- `selene_discord.py` (gitignored — use `git add -f`)
- `server/startup.py`, `server/tool_pipeline.py`, `server/handlers/chat.py`
- `CHANGELOG.md`
- Do NOT commit: `.env`, `configs/soul.md`, `configs/sage_soul.md`, `configs/selene_prompt.txt`, `configs/sage_prompt.txt`, `memories/`, `conversations/`, `selene_state.json`

---

## [v0.5] 2026-06-19 — Pantheon Skeleton + UI Wiring

### Added

**agents/ folder — six self-contained agent cards**
- Uniform per-agent folder layout: `config.json`, `prompt.txt`, `user_profile.md`, `character_profile.md`, `tools_context.md`, `insights.md`, `manifest_state.json`, `memory.db`
- `config.json` schema drives all runtime path resolution — no agent names hardcoded in Python logic
- Six agents scaffolded: Selene, Sage, Akari, Yami, ROM, RAM

**Model stack locked**
- Selene + Sage: `google/gemma-3n-e4b` (Gemma family)
- Akari (The Saintess, Frontend): `DevQuasar/Tesslate.UIGEN-T2-7B-GGUF:Q4_K_M` — Qwen2.5-Coder fine-tuned on 50k UI samples with design-reasoning traces. Q4_K_M = 4.68GB, fits 6GB VRAM
- Yami (The Pharaoh, Backend): `mistralai/Ministral-8B-Instruct-2410` — Mistral family, structural and precise
- ROM (The Dreamer, Creative/VL): `WarlordHermes/Huihui-Qwen3-VL-8B-Instruct-Creative-v0.4` — Qwen VL fine-tuned for creative writing + visual interpretation
- RAM (The Creative, Image Gen): `black-forest-labs/FLUX.1-schnell` — diffusion model, no LLM slot
- Three distinct LLM families across five LLM agents: Gemma, Qwen, Mistral

**`swap_agent` rewrite — fully path-agnostic**
- All file paths resolved via `_ap(key, fallback)` helper from `agents/{slug}/config.json`
- `self.MEMORY_DIR = agent_dir` — manifest, memory_extractor, all tools inherit correct paths automatically
- Per-agent `AgentMemoryStore`; Selene DB optionally opened read-only for cross-agent reference
- `FileNotFoundError` on missing config replaces old `("selene", "sage")` allowlist

**`agent_meta` state broadcast**
- `server/state.py` `get_state()` includes `agent_meta`: name, title, domain, color_primary, slug
- Frontend reads live agent identity on every state poll — no hardcoded names in renderer

**UI — dynamic Pantheon wiring**
- `TopBar.jsx`: `PANTHEON` array drives dropdown — data-driven, no hardcoded agent logic
- `renderer/index.css`: theme blocks for all six agents (`.theme-selene` through `.theme-ram`), full CSS variable overrides per agent
- `renderer/index.html`: toggle cycles all six slugs; bottom bar shows live agent name; `MEM_TABS` converted to function taking `agentName`
- `Dashboard.jsx`: passes `agentMeta` to TopBar, `agentName` to MemoryView
- `MemoryView.jsx`: single dynamic `manifest` tab replaces hardcoded `manifest_selene`/`manifest_sage`
- `server/handlers/memory.py`: all file paths resolve through active agent's `MEMORY_DIR`; returns `agent_name` for tab labeling

### Changed
- `memory_extractor.py`: removed `{agent_name}_` prefixes — paths derive from `self.MEMORY_DIR`
- `tools/manifest.py`: `load_state_json`/`save_state_json` use `self.agent_state.MEMORY_DIR`
- `.gitignore`: added `agents/*/memory.db`, `agents/*/prompt.txt`, `agents/*/soul.md`
- `CODEBASE.md`: agents/ section added — folder layout, config schema, swap_agent pattern, model stack table

### Safe to commit
- `agents/` (configs, profiles, example files — excludes `memory.db`, `prompt.txt`, `soul.md`)
- `selene_brain/llm_chat.py`, `selene_brain/memory_extractor.py`
- `tools/manifest.py`
- `server/state.py`, `server/handlers/memory.py`
- `renderer/index.css`, `renderer/index.html`, `renderer/components/TopBar.jsx`, `renderer/components/Dashboard.jsx`, `renderer/components/MemoryView.jsx`
- `CODEBASE.md`, `CHANGELOG.md`
- Do NOT commit: `.env`, `agents/*/memory.db`, `agents/*/prompt.txt`, `agents/*/soul.md`, `memories/`, `conversations/`, `selene_state.json`

---

## [v0.6] 2026-06-19 — Boot Select, Model Swap, UI Polish

### Added

**Boot agent select screen**
- Full-screen PANTHEON grid on every launch — pick your agent before the UI loads
- Skips model swap only if `seleneState.active_agent` already matches the selection
- All other selections trigger `toggle_agent` + swap overlay immediately

**Model swap overlay**
- Full-screen blur overlay with animated spinner while LM Studio loads a model
- 6 orbiting dots, one per Pantheon member, each in their primary color
- Overlay holds until `seleneState.active_agent` confirms the swap (not just `ok: true`)
- 12s safety timeout clears overlay if state update never arrives
- Error path clears after 5s with toast

**`model_path` field in agent configs**
- Separates LM Studio chat endpoint name (`model`) from load API path (`model_path`)
- Selene/Sage use `Selene/Sage` endpoint name, `google/gemma-3n-e4b` load path
- Akari, Yami, ROM use their actual LM Studio display names for both fields
- `toggle_agent` checks if target model already loaded — skips unload/load cycle if so

**Akari theme overhaul**
- Dark background (`#0a0510`), deep plum atmo blobs, pink/yellow accents, light text
- Fixes white/light background that made the OS unreadable when Akari was active
- `LeftPanel` now reads `agent_meta.color_primary` from live state — all agents render in their configured color instead of only Selene/Sage being recognized

**Branding**
- All `SELENE_OS` references renamed to `THE PANTHEON` throughout the UI

### Changed

- `system.py` `get_integrations_status` — was missing `return True`, causing every call to fall through to "Unknown message type" error. Fixed + completed truncated handler block (Spotify tool check + `send_json`)
- `server/handlers/system.py` `toggle_agent` — uses `model_path` for LM Studio load API, `model` for chat completions payload. Already-loaded check compares both `id` and `path` fields from LM Studio response
- `startup.py` — broadcasts `ready` + `conversations` after `_init_selene` completes so frontend re-fetches all state after the boot race window
- `index.html` — `prevSeleneStatusRef` starts `null` (was `"offline"`); `null → idle` transition now correctly triggers `fetchAllState`
- `TopBar.jsx` — NAV dropdown button removed entirely
- `ToolsView.jsx` — `story` tab gated on `runereader` tool (was always visible); only Selene, Sage, ROM see it
- All six agent `character_profile.md` files reset — old codenames (Forge, Pixel, Echo) removed, clean stubs with correct agent names
- All six agent `tools_context.md` files updated from Selene's real version — replaces placeholder stubs
- `agents/akari/soul.md`, `yami/soul.md`, `rom/soul.md`, `ram/soul.md` — new functional stubs: domain, autonomy, purpose
- SWAP button in Dashboard slot headers: uniform `36×18px` matching adjacent size-step buttons
- `agents/selene/memory.db` — was corrupt (disk image malformed, likely from move). Deleted; will regenerate clean on next boot

### Fixed

- Boot race condition: UI received `status: offline` on connect, sent data-fetch requests before `_init_selene` completed, all handlers returned "Selene not initialised" errors. Fixed via: 1.5s delay on `connState` effect, `ready` broadcast from server, `prevSeleneStatusRef` transition hook, silent no-op in all handlers during init
- `get_integrations_status` missing `return True` — every call fell through to dispatcher's unknown-type error handler
- LM Studio model name mismatch — agent configs had HuggingFace paths, LM Studio endpoint is named `Selene/Sage`. Added `model`/`model_path` split
- `schedule_manager` registration warning (`MEMORY_DIR not set`) — fires before `swap_agent` sets paths. Non-blocking but noted
- Swap overlay dismissed too early — now waits for `seleneState.active_agent` to confirm identity before clearing

### Safe to commit
- `agents/*/config.json`, `agents/*/soul.md`, `agents/*/character_profile.md`, `agents/*/tools_context.md`, `agents/*/user_profile.md`
- `server/handlers/system.py`, `server/startup.py`, `server/state.py`
- `renderer/index.html`, `renderer/index.css`
- `renderer/components/TopBar.jsx`, `renderer/components/LeftPanel.jsx`, `renderer/components/Dashboard.jsx`, `renderer/components/ToolsView.jsx`
- `CHANGELOG.md`
- Do NOT commit: `.env`, `agents/*/memory.db`, `agents/*/prompt.txt`, `agents/*/soul.md` (private), `memories/`, `conversations/`, `selene_state.json`

---

## [v0.7] 2026-06-20 — Conversation Participants, Notifications, Agent Strip Fix

### Added

**Conversation participant system**
- Every conversation now has a `participants` list — seeded with the creating agent's slug on `new_conversation()`
- `/invite @agent` command in chat: adds agent as participant, grants full conversation history access, sends confirmation message
- `@agent` pings remain unchanged — one-shot response, no participant written
- `add_participant(conv_id, slug)` and `get_participants(conv_id)` methods on `ConversationManagerMixin`
- `invite_agent` WS handler in `server/handlers/conversations.py` — broadcasts `participant_added` event
- `participant_added` WS message handled in frontend — updates conversation list + active participants
- All 6 `@agent` pings now recognized (`selene`, `sage`, `akari`, `yami`, `rom`, `ram`) — previous version only handled `@selene` / `@sage`

**Participant filter UI in ConvList**
- Agent color dots on each conversation row showing who's participating
- Filter row: ALL button + per-agent colored dot buttons, filters conversation list by participant
- Inline invite panel: shows uninvited agents as dashed-border dots, clicking adds them to current conversation
- `ConvList.jsx` fully rewritten to support participants, filtering, and invites

**Notification system**
- Persistent store at `agents/shared/notifications.json` — survives restarts, max 200 entries
- `server/handlers/notifications.py` — `get_notifications`, `mark_notification_read`, `mark_all_notifications_read`, `clear_notifications`
- `add_notification(title, body, page, source_agent)` — callable from any tool or handler. Writes to disk + broadcasts WS event to all clients
- `server.handlers.add_notification` re-exported from `__init__` for internal callers
- Bell icon in TopBar with animated unread count badge (red dot, count, 99+ cap)
- `NotificationPanel.jsx` — slides in below TopBar, shows title/body/time-ago/source agent dot/page link. Click → marks read + navigates. Mark all / Clear buttons
- `server/state.py` — `event_loop = None` slot; set by `lifespan()` so sync helpers can schedule async broadcasts
- `notification` + `notifications_data` WS cases added to frontend handler

### Fixed

- `llm_caller.py` strip regex — was only stripping `Selene|Sage|Ghost` from model output. Now covers all six agents: `Selene|Sage|Akari|Yami|ROM|RAM|Ghost`
- `conversation_loaded` WS response now includes `participants` field
- `new_conversation` WS response now includes `participants` field

### Safe to commit
- `selene_brain/conversation_manager.py`, `selene_brain/llm_caller.py`
- `server/handlers/conversations.py`, `server/handlers/chat.py`, `server/handlers/notifications.py`
- `server/handlers/__init__.py`, `server/state.py`, `server/startup.py`, `selene_server.py`
- `renderer/index.html`
- `renderer/components/ConvList.jsx`, `renderer/components/TopBar.jsx`, `renderer/components/NotificationPanel.jsx`
- `CHANGELOG.md`
- Do NOT commit: `.env`, `agents/*/memory.db`, `agents/*/prompt.txt`, `agents/*/soul.md` (private), `memories/`, `conversations/`, `selene_state.json`, `agents/shared/notifications.json`

---

## [v0.8] 2026-06-20 — Roster-Driven Agent System

### Added

**`server/roster.py` — dynamic agent roster**
- Scans `agents/*/config.json` at startup — no central agent list anywhere else in the system
- `get_roster()` / `get_agent(slug)` / `agent_has_cap(slug, cap)` / `default_agent_slug()` / `agents_with_cap(cap)` / `build_ping_map()`
- `_derive_glow(hex)` — auto-generates RGBA glow from `color_primary` so new agents don't need to specify it
- `reload_roster()` hot-reload WS handler exposed via `server/handlers/system.py`
- `"roster": get_roster()` added to live WS state payload

**Capability system in `agents/*/config.json`**
- `capabilities` array replaces all `if slug == "sage"` / `if slug == "selene"` guards in the backend
- Defined capabilities: `default_boot`, `grant_access`, `dev_manifest`, `idea_routing`, `write_agent_manifest`, `agent_creation`
- Current assignments: selene → `["default_boot","grant_access"]`; sage → `["grant_access","dev_manifest","idea_routing","write_agent_manifest"]`; akari/yami → `["agent_creation"]`; rom/ram → `[]`
- All six `config.json` files updated with `color_glow`, `role`, `capabilities`, `display_name` fields
- Yami `color_primary` corrected from near-black `#1c1917` to amber `#f59e0b`

**Backend roster wiring**
- `tools/manifest.py` — all `== "sage"` checks replaced with `agent_has_cap()` / `agents_with_cap()` calls
- `tools/meta_insight.py` — `is_sage` replaced with `agent_has_cap(slug, "grant_access")`; fallback slugs use `default_agent_slug()`
- `selene_brain/agent_memory.py` — access control uses `agent_has_cap(requesting_agent, "grant_access")`
- `selene_brain/llm_chat.py` — boot and saved-agent fallback use `default_agent_slug()` from roster
- `selene_brain/conversation_manager.py` — default agent fallback uses `default_agent_slug()`
- `server/handlers/chat.py` — `_PING_MAP` and `_AGENT_SLUGS` built from `build_ping_map()` / `get_roster()`; `/invite` now routes to any roster agent, not a fixed six
- `server/handlers/system.py` — `toggle_agent` default uses `default_agent_slug()`
- `server/state.py` — offline fallback uses `_default_agent_slug_safe()` helper (avoids import-time disk I/O errors)
- `server/startup.py` — `reload_roster()` called before `_init_selene()`

**Frontend roster wiring**
- `renderer/components/RosterUtils.js` — new shared helper loaded before all components. `window.RosterUtils`: `getColor()`, `getName()`, `getGlow()`, `getTitle()`, `hasCap()`, `defaultSlug()`, `allSlugs()`
- All six hardcoded `PANTHEON_COLORS` / `AGENT_COLORS` / `ALL_AGENTS` / `PANTHEON` dicts removed from every component
- `renderer/components/TypingIndicator.jsx` — uses `RosterUtils.getColor()` / `getName()`
- `renderer/components/ChatView.jsx` — uses `RosterUtils.getName()` / `getColor()`; passes `roster` to ConvList and TypingIndicator
- `renderer/components/ConvList.jsx` — `ParticipantDots` and `ConvList` accept `roster` prop; all color/slug lookups via RosterUtils
- `renderer/components/NotificationPanel.jsx` — agent dot color via `RosterUtils.getColor()`
- `renderer/components/TopBar.jsx` — `PANTHEON` array derived from `roster` prop; falls back to selene/sage if roster not yet loaded
- `renderer/components/ToolsView.jsx` — manifest panel label/badge/description driven by `agent_has_cap("dev_manifest")` from roster; no more `activeAgent === "sage"` conditionals
- `renderer/index.html` — `const roster = seleneState?.roster || []`; `BOOT_PANTHEON` derived from roster (falls back to selene/sage); swap overlay orbit dots driven by roster colors and count; `roster` prop threaded to TopBar, ChatView, ToolsView, NotificationPanel

### Impact

Adding a new agent now requires **only**:
1. Create `agents/<slug>/config.json` with name, color, title, capabilities, tools, model
2. Restart server — roster auto-discovers the new folder

No other system file needs to change. Frontend adapts automatically on next WS state push.

### Safe to commit
- `server/roster.py`, `server/state.py`, `server/startup.py`
- `server/handlers/system.py`, `server/handlers/chat.py`
- `tools/manifest.py`, `tools/meta_insight.py`
- `selene_brain/agent_memory.py`, `selene_brain/llm_chat.py`, `selene_brain/conversation_manager.py`, `selene_brain/llm_caller.py`
- `agents/selene/config.json`, `agents/sage/config.json`, `agents/akari/config.json`, `agents/yami/config.json`, `agents/rom/config.json`, `agents/ram/config.json`
- `renderer/components/RosterUtils.js`, `renderer/components/TypingIndicator.jsx`, `renderer/components/ChatView.jsx`
- `renderer/components/ConvList.jsx`, `renderer/components/NotificationPanel.jsx`, `renderer/components/TopBar.jsx`, `renderer/components/ToolsView.jsx`
- `renderer/index.html`
- `CHANGELOG.md`
- Do NOT commit: `.env`, `agents/*/memory.db`, `agents/*/prompt.txt`, `agents/*/soul.md`, `memories/`, `conversations/`, `selene_state.json`, `agents/shared/notifications.json`

---

## [v0.9] 2026-06-28 — Multi-Agent Ping, Emotion Tab, Bug Fixes, Cleanup

### Added

**Multi-agent ping system (full rewrite)**
- `@mention` parsing collects ALL slugs from a single message in order — `@Sage @Akari` triggers both in sequence
- Each pinged agent: swap config → presence gate → respond → tag response with agent name → swap to next
- After all pings complete, swaps back to origin agent (whoever was active when message arrived)
- Group conversations (agents added via `/invite`): all participants respond in succession when no explicit `@` ping is used
- Explicit pings in group chats override the succession order — only named agents respond
- Skip-swap optimization: if the next agent in the sequence is already active (same slug), `swap_agent` is skipped
- User message logged once; each agent response tagged with `"agent"` field on the WS message

**Emotion tab in MetaInsightView**
- `run_emotion_and_insight` now logs a dedicated `category="emotion"` entry per turn alongside the existing `category="output"` entry
- Subcategory is the emotional arc: `"curious → content"`, intensity as confidence score, shift magnitude in reasoning field
- EMOTION tab in MetaInsightView now populates from real data

### Fixed

- **Critical: `return True` at wrong indentation in `chat.py`** — stray `return True` was outside the `if is_keep or is_yes or _named_agent` block but inside `if pending_idea_routing`. Fired after every single chat message, short-circuiting before the presence layer or LLM were ever reached. Every agent appeared to receive messages (typing indicator) but never responded.
- **`llm_chat.py` truncation** — file was truncated at `if __name__ ==` with no continuation. Python ran stale `.pyc` from before truncation, masking the SyntaxError. Fixed by completing the `if __name__ == "__main__": main()` block and clearing `.pyc` cache.
- **Wrong model name** — `agents/selene/config.json` and `agents/sage/config.json` had `"model": "Selene/Sage"` (a legacy LM Studio display name). Updated to `"google/gemma-3n-e4b"` to match the actual loaded model identifier.
- `notifications.py` type annotation — `page: "str | None" = None` (string-quoted for Python 3.9 compat)
- `tools/schedule.py` — `state_file` converted to `@property` to avoid accessing `MEMORY_DIR` at `__init__` time before `swap_agent` sets paths
- `server/handlers/system.py` — `default_agent_slug` was missing from roster import, causing `NameError` on boot when no saved state existed
- Debug step-prints (`[Chat]: step 1`, etc.) removed from `chat.py`

### Changed

- `.gitignore` — added `agents/*/manifest_state.json` and `agents/shared/` (runtime data, should never be tracked)
- Deleted stale root-level docs: `RESTRUCTURE_PLAN.md` (restructure complete), `SELENE_INNER_STATE_FEATURE.md` (superseded by meta_insight reasoning), `SKILLS_PROFILE*.md`, `SKILLS_ADDENDUM_UNITY_VRM.md`
- Deleted superseded configs: `configs/sage_config.json`, `configs/selene_config.json` (replaced by `agents/*/config.json`)
- Deleted dead dormant modules: `selene_brain/dormant/curator.py`, `selene_brain/dormant/subdirectory_hints.py` (foreign codebase, zero relation to Selene)
- Deleted leftover log files and stale runtime JSONs from `memories/`
- `README.md` — full rewrite reflecting current roster system, project structure, and setup flow
- `CODEBASE.md` → `ARCHITECTURE.md` — renamed, updated with identity packet design spec and v0.9 roadmap

### Safe to commit
- `server/handlers/chat.py`, `server/roster.py`
- `selene_brain/llm_chat.py`, `tools/meta_insight.py`, `tools/schedule.py`
- `server/handlers/system.py`, `server/handlers/notifications.py`
- `agents/selene/config.json`, `agents/sage/config.json`
- `README.md`, `ARCHITECTURE.md`, `CHANGELOG.md`, `.gitignore`
- Do NOT commit: `.env`, `agents/*/memory.db`, `agents/*/prompt.txt`, `agents/*/soul.md`, `agents/*/manifest_state.json`, `agents/shared/`, `memories/`, `conversations/`, `selene_state.json`

---


