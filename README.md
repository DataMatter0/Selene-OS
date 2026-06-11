# Selene OS

A local-first AI companion system built on LM Studio. Not a chat wrapper — a persistent agent runtime with its own memory, emotional state, tool system, and the ability to decide whether or not to respond.

Two agents share the same inference backend but maintain separate identities, memory databases, and tool permissions: **Selene** (warm companion, daily context) and **Sage** (analytical, research and development).

---

## What makes this different

Most local AI projects are thin frontends over an LLM API. Selene OS is an operating environment:

- **Presence layer** — before every turn, the active agent runs a lightweight gating pass (RESPOND / OBSERVE / IGNORE). Agents can stay silent mid-conversation without a prompt instruction, driven by context and emotional state.
- **Two-agent group chat** — swap between Selene and Sage mid-conversation with `@sage` / `@selene`. History is attributed per-agent. Both share one loaded model, so there's no reload overhead.
- **Working memory with status** — each message chunk carries a `status` field (sent / read / observed) that persists across reloads. The model sees attributed history (`Selene: ...`, `Sage: ...`, `Ghost: ...`).
- **Tool suggestion layer** — phrase matching → binary LLM gate → confidence threshold → execute or inject a warning. Tools can be triggered by keyword, slash command, or autonomous LLM tool call via `<tool_call>` XML tags.
- **Training data pipeline** — every tool call generates a post-hoc reasoning log (why was this call necessary?) written to SQLite. The dataset is purpose-built for future fine-tuning.
- **Trajectory compressor** — long conversations are summarized and compacted into the model's history window automatically, without losing continuity.
- **Mood observer** — a rolling emotional state derived from conversation content. Injected into system context as `<emotional_state>` blocks. Only updates post-turn, not on polling.

---

## Stack

| Layer | Tech |
|---|---|
| Backend | Python 3.12 · FastAPI · Uvicorn · WebSocket |
| LLM Inference | LM Studio (OpenAI-compatible REST) |
| Current model | `google/gemma-3n-e4b` |
| Memory | SQLite · Markdown flat files |
| Frontend | Electron · React 18 (CDN) · Vanilla CSS |
| Tool system | Custom Python plugin router |

---

## Project structure

```
selene_brain/               Core agent architecture
  llm_chat.py               Main LLMChat class — chat loop, agent swap, presence gate
  llm_caller.py             LM Studio API client, reasoning_content normalization
  lm_studio_manager.py      Model load/unload, skip logic for shared-model agents
  prompter.py               System prompt builder — soul + context + emotional state
  agent_memory.py           SQLite layer — dialog, meta_insight, tool_reasoning logs
  memory_extractor.py       Background memory extraction + working memory management
  trajectory_compressor.py  Conversation compaction for long-context management
  conversation_manager.py   Conversation persistence and chunk storage
  mood_observer.py          Emotional state tracker
  tool_suggestion.py        Phrase matching + LLM gate for autonomous tool routing

tools/
  schema.py           BaseTool interface + atomic_write helper
  registry.py         ToolRouter — registration and routing
  manifest.py         ManifestTool — task graph, Obsidian sync, LLM reorganize
  todo.py             TodoTool — step-by-step plan tracker
  memory_tool.py      ChronicleTool + MemoryTool
  status.py           StatusTool — system health checks
  meta_insight.py     Self-observation — agents query their own reasoning logs
  presence.py         Presence/gating tool
  knowledge.py        Knowledge board (persistent context cards)
  runereader.py       Document synthesis tool
  file_manager.py     Local filesystem tool
  youtube.py          YouTube transcript + co-watching tool
  maps.py             Maps tool
  notion.py           Notion integration
  schedule.py         Schedule manager
  story_engine/       Infinite Story Engine (RPG campaign system)

server/                     WebSocket server package (extracted from selene_server.py)
  config.py           BASE_URL, SERVER_HOST/PORT, _normalize()
  utils.py            clean_xml_tags(), split_response_chunks(), _format_tool_data()
  state.py            selene_ref, clients, broadcast(), get_state(), _state_broadcaster()
  startup.py          _init_selene(), lifespan(), gamepad poller, timer poller
  tool_pipeline.py    process_message(), update_memory_and_energy(), tool execution
  handlers/
    chat.py           chat, force_generate, rollback, clear_memory (presence layer)
    conversations.py  new/load/rename/list/delete conversation
    memory.py         get/save memory, force extract, tool phrase management
    manifest.py       task CRUD, guidelines, reorganize, compile_and_push, todo
    knowledge.py      knowledge board, web search, arXiv, RSS
    system.py         state, models, set_model, toggle_agent, latency test, Discord
    steam.py          Steam library scan + local game launcher
    youtube.py        youtube query, search, watch_start, segment_push, co-watch chat
    story.py          Infinite Story Engine (12 handlers, auto-compaction)
    misc.py           maps, polymarket, document/RuneReader, notion, meta_insight

selene_server.py      Thin entry point — FastAPI app, REST routes, WS dispatcher (~400 lines)
configs/              Agent config JSON + prompt files (soul files gitignored)
scripts/              Utility scripts (restore, patch, dataset parsing)
```

---

## Setup

**Requirements:** Python 3.12+, Node 18+, LM Studio running locally or on LAN.

```bash
# 1. Clone
git clone https://github.com/your-username/selene-os.git
cd selene-os

# 2. Python environment
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt

# 3. Environment
cp .env.example .env
# Fill in LM_STUDIO_URL and LM_STUDIO_MODEL at minimum

# 4. Start the backend
python selene_server.py

# 5. Frontend (optional — backend runs headless too)
npm install
npm start
```

The server runs on `ws://localhost:8766`. On startup it checks LM Studio for the configured model and skips loading if it's already active.

---

## Agents

Agents are defined by a config JSON in `configs/` pointing to a prompt file, SQLite DB, and allowed tool list. Swapping agents is a hot-swap — no model reload, no restart.

```json
{
  "name": "Selene",
  "model": "google/gemma-3n-e4b",
  "prompt_path": "configs/selene_prompt.txt",
  "memory_path": "memories/selene_memory.db",
  "tools": ["memory_tool", "todo", "meta_insight", "knowledge_manager", ...]
}
```

Prompt files are plain text — edit them directly and they're picked up on the next turn.

---

## Tool system

Tools extend `BaseTool` from `tools/schema.py`. Register them in `tools/registry.py`. Each tool can implement:

- `execute(input_data)` — called by the router
- `check_and_trigger(user_input)` — keyword matching for automatic invocation
- Slash command registration in `tool_suggestion.py`

The `meta_insight` tool lets agents query their own decision and tool-use logs — a foundation for self-reflection and future fine-tuning feedback loops.

---

## Training data

Every tool execution logs to `tool_reasoning_log` in SQLite:

```
agent | tool_name | trigger_mode | input_context | tool_args | tool_result | reasoning
```

`reasoning` is generated post-hoc by a lightweight LLM call: *"Was this tool call actually necessary given what the user said?"* This produces honest training signal, not justification-after-the-fact.

---

## Status

Active development. v0.3 — codebase restructured into focused packages. `selene_server.py` reduced from 3541 → 406 lines. `tools/builtin.py` split into four single-responsibility modules. All server logic extracted into a `server/` package with one file per domain.

Planned: fine-tuning pipeline on collected tool reasoning data, inner state stream, idle/interrupt model.

---

## License

MIT — see [LICENSE](LICENSE).
