# Selene OS — Codebase Design Reference

This document explains *why* the code is structured the way it is. It's the thing you'd want before touching anything non-trivial.

---

## Package map

```
selene_brain/    Core agent runtime — LLM calls, memory, presence, prompting
tools/           Tool plugins — one file per tool, loaded at startup via registry
server/          WebSocket + REST server — one file per domain concern
selene_server.py Thin entry point (~400 lines) — app wiring + WS dispatcher
```

---

## selene_brain/

The agent runtime. Everything inside here is agent-centric — it only knows about the model, memory, and conversation state. It has no FastAPI imports and no WebSocket handling.

**`llm_chat.py` — `LLMChat`**
The main class. Composes several mixins:
- `PromptBuilderMixin` — builds the system prompt from soul file + injected context + emotional state
- `ConversationManagerMixin` — new/load/save/rename/delete conversations, chunk persistence
- `MemoryExtractorMixin` — background extraction of long-term memories from turn pairs
- `TrajectoryCompressorMixin` — compacts conversation history when working memory grows too large

Key methods used by the server:
- `chat(user_input)` — full turn: presence gate → tool routing → LLM → chunked delivery
- `swap_agent(name)` — hot-swap between Selene and Sage without model reload
- `maybe_extract_memory(user, response)` — fires background memory extraction
- `compile_daily_manifest()` — aggregates task state into a daily summary

**`tool_suggestion.py` — `ToolSuggestionLayer`**
Sits between user input and the main LLM call. Pipeline:
1. Phrase match against `tool_phrases` SQLite table
2. If match found → binary LLM gate (is this actually relevant?)
3. Confidence threshold — execute if high, inject warning if low
4. Falls through to normal chat if no match

**`mood_observer.py`**
Rolls emotional state from turn content. Updates `_cached_emotion` post-turn only — never on polling — to avoid neutral noise flooding meta_insight logs and state broadcasts.

---

## tools/

Each tool extends `BaseTool` from `tools/schema.py`. Tools are registered in `tools/registry.py` via `ToolRouter`, which handles both keyword routing and `<tool_call>` XML dispatch.

**Registration pattern:**
```python
# tools/registry.py
router.register("manifest_manager", ManifestTool(db=selene.db))
```

**Execution pattern:**
```python
result = selene.tool_router.route_and_execute("manifest_manager", {"command": "add_task", ...})
# Returns: {"status": "success"|"error", "data": ..., "message": ...}
```

**Why one file per tool (v0.3 split):**
`tools/builtin.py` was a 1167-line grab-bag of unrelated classes. The split was motivated by:
- `ManifestTool` is ~800 lines on its own and touches Obsidian, SQLite, and LLM calls
- `TodoTool` has nothing to do with `MemoryTool` or `StatusTool`
- Each tool now has its own import surface — new tools don't touch existing files

**`tools/schema.py`**
Defines `BaseTool` (abstract base with `execute()`, `check_and_trigger()`) and `atomic_write()` (write-then-rename for safe file updates). `atomic_write` is used by `TodoTool._save()` and any tool writing JSON state.

**`tools/story_engine/`**
Subpackage for the Infinite Story Engine RPG system. `db_helper.py` owns the SQLite schema (profiles, characters, worlds, manifest_log, locations, cards, presets). `InfiniteStoryEngine` class handles dice resolution, character creation, merchant generation, and level-up.

---

## server/

Extracted from `selene_server.py` in v0.3. The design principle: `selene_server.py` should only wire things together — no business logic.

**`server/config.py`**
Constants only. `BASE_URL`, `DESIRED_MODEL`, `SERVER_HOST`, `SERVER_PORT`. `_normalize()` for case/separator-insensitive model name comparison (used in set_model skip logic).

**`server/state.py`**
Mutable globals that multiple handler files need to share:
- `selene_ref` — the live `LLMChat` instance. Set by `startup._init_selene()` via `set_selene()`. `None` until init completes — all handlers guard with `if selene`.
- `clients` — set of connected WebSocket clients for broadcast
- `_cached_emotion` — post-turn emotion snapshot, mutated directly by `handlers/chat.py`
- `_prev_writing` — debounce flag for writing-state broadcasts
- `broadcast()` — sends a dict to all connected clients
- `_state_broadcaster()` — async task, polls writing state every 2s and broadcasts diffs

**`server/tool_pipeline.py`**
The core message routing logic. Imported by `handlers/chat.py`.

- `process_message(user_input, websocket, loop)` — routes via tool_suggestion → keyword fallback → normal LLM chat. Handles chunked Selene delivery vs. single Sage response.
- `_execute_tool_and_respond()` — shared execution path for both keyword and suggestion routes
- `update_memory_and_energy(user, response)` — commits turn to working_memory, assigns chunk_group UUID
- `set_last_message_status(status)` — stamps SQLite + working_memory status field
- `_generate_tool_reasoning_background()` — background thread that generates post-hoc "was this call necessary?" reasoning for the training dataset

**Circular import mitigation:**
`tool_pipeline.py` needs `selene_ref` from `state.py`, but `state.py` is also imported by handlers at module level, and handlers import `tool_pipeline`. To break the cycle, `tool_pipeline` uses a lazy accessor:
```python
def _selene():
    from . import state as _s
    return _s.selene_ref
```
This defers the import to call time, not module load time.

**`server/startup.py`**
Everything that happens at boot and shutdown.

- `_init_selene()` — blocking: story DB init, LM Studio contact, model load/skip check, `LLMChat` construction, `set_selene()`, knowledge tool hook, `ToolSuggestionLayer` init, autonomy thread start. Runs in a background executor so the WebSocket is available immediately while Selene warms up.
- `lifespan(app)` — FastAPI `asynccontextmanager`: starts `_init_selene` + state broadcaster + timer poller + gamepad thread, handles Discord bot startup/shutdown, saves state on exit.
- `_gamepad_poller_thread(loop)` — pygame gamepad polling in a daemon thread, broadcasts `force_focus` events to the WS.
- `global_guide_button` — module-level int, updated by `handlers/system.py` via `_startup.global_guide_button = int(...)`.

**`server/utils.py`**
Pure functions with no imports from the rest of `server/`:
- `clean_xml_tags(text)` — strips all XML except `<think>` and `<tool_reasoning>` blocks, which the UI parses for ThoughtBubble rendering
- `split_response_chunks(text)` — groups sentences into 2–4 per chunk for Selene's conversational delivery. Sage does not use this.
- `_format_tool_data(data)` — converts list-of-dicts, plain dicts, or lists into readable numbered blocks for the model to narrate
- `extract_presence_decision(text)` — detects `observe`/`ignore` in model output

---

## server/handlers/

Each handler file exposes:
```python
async def handle(websocket, data: dict, loop) -> bool
```
Returns `True` if it handled the message, `False` to fall through to the next handler. The dispatcher in `selene_server.py` calls them in order.

**Dispatch order matters.** `chat.py` is first because it handles the highest-frequency messages. `misc.py` is last as a catch-all for lower-frequency tool queries.

**`handlers/chat.py`**
The most complex handler. Full flow:
1. Check for `force_generate` / `rollback_last_turn` / `clear_memory` — handle and return
2. Run presence layer (IGNORE → return, OBSERVE → silent think pass, RESPOND → continue)
3. Auto-create conversation if none active
4. Call `process_message()` from `tool_pipeline.py`
5. Post-turn: refresh emotion cache, auto-name conversation if still "New Conversation"

Selene and Sage deliver differently:
- Selene: `split_response_chunks()` + per-chunk delays via `asyncio.sleep`
- Sage: single complete response, no chunking

**`handlers/youtube.py`**
Takes an extra `yt_state` dict argument (per-session) for the co-watching dormancy system:
- `awaiting_ghost_reply` — True after Selene reacts to a segment autonomously
- `absence_prompted` — True once the "still watching?" ping was sent
- `dormant` — suppresses all auto-reactions until Ghost sends a `youtube_chat` message

**`handlers/story.py`**
All 12 `story_*` handlers. The auto-compaction logic in `story_player_action` fires at 50 turns: summarizes the full timeline via LLM, archives to Notion or local file, then replaces the log with a single compact entry.

---

## selene_server.py (entry point)

After v0.3 this file only:
1. Imports from `server.*` and `server.handlers.*`
2. Creates the FastAPI app with the `lifespan` context manager from `server/startup.py`
3. Registers REST routes: `/yt-proxy`, `/state`, `/steam/image/{appid}`, `/sounds/{filename}`, `/v1/models`, `/v1/chat/completions`
4. Defines the `/ws` WebSocket endpoint — accepts connections, initializes `yt_state`, dispatches incoming messages through the 10 handler chain

The `/v1/chat/completions` and `/v1/models` endpoints provide OpenAI API compatibility for Hermes Agent and other tool frameworks. Every request flows through Selene's full pipeline — soul prompt, memory, tool routing all apply. The endpoint does NOT write to `working_memory` to avoid contaminating UI sessions.

---

## Data flow — a normal chat turn

```
UI  →  WS  →  handlers/chat.py
                 │
                 ├─ presence gate (IGNORE / OBSERVE / RESPOND)
                 │
                 └─ tool_pipeline.process_message()
                       │
                       ├─ ToolSuggestionLayer.check(input)
                       │     ├─ phrase match
                       │     ├─ LLM gate
                       │     └─ execute or warn
                       │
                       ├─ keyword fallback (tool_router.check_and_trigger)
                       │
                       └─ LLMChat.chat() → chunked response
                             │
                             ├─ update_memory_and_energy()
                             ├─ set_last_message_status()
                             ├─ _generate_tool_reasoning_background()
                             └─ broadcast chunks → UI
```

---

## Key invariants

- **`selene_ref` is None until `_init_selene()` completes.** Every handler guards with `if selene:` or `if selene is None: return True`. The WS connection is accepted before init finishes so the UI can connect immediately.
- **Emotion cache updates post-turn only.** The 2s state broadcaster reads `_cached_emotion` but never triggers an LLM call. Only `handlers/chat.py` mutates it, after a full turn completes.
- **Soul files are never read by the server directly.** `LLMChat._build_system_prompt()` owns prompt assembly. Handlers set `selene._prompt_dirty = True` when something changes (model swap, save_memory) — the next turn picks up the rebuild.
- **Tool results always go through `_format_tool_data()`.** Raw `str()` on a list-of-dicts produces unreadable output for the model. `_format_tool_data` produces numbered blocks the model can narrate naturally.
- **`atomic_write` for any JSON state file.** Write to a `.tmp` file, then `os.replace()`. Prevents corruption on crash mid-write.

---

## What's gitignored (never commit)

```
.env                          Live API keys (Anthropic, Notion, Google, Exa, Spotify)
configs/soul.md               Selene's personal identity prompt
configs/sage_soul.md          Sage's personal identity prompt
configs/selene_prompt.txt     Full Selene system prompt
configs/sage_prompt.txt       Full Sage system prompt
memories/                     Extracted long-term memory files
conversations/                Saved conversation JSON
selene_state.json             Runtime state snapshot
```
