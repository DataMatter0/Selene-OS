# Selene OS

A local-first AI companion desktop application. Not a chat wrapper — a persistent agent runtime that runs on your own hardware, remembers who you are over time, decides whether or not to respond, and routes tasks through a modular tool system.

The **Pantheon** is a roster of six agents sharing one inference backend (LM Studio), each with their own identity, memory database, tool permissions, and reasoning style. Swapping agents is a hot-swap — no model reload, no restart.

---

## What makes this different

- **Presence layer** — before every turn, the active agent runs a gating pass (RESPOND / OBSERVE / IGNORE). Agents stay silent when the moment calls for it, driven by emotional state and conversation context — not a prompt instruction.
- **Roster-driven agent system** — agents are self-describing `config.json` files in `agents/<slug>/`. Adding a new agent requires no changes to Python or frontend code. The roster auto-discovers and the UI adapts on the next state push.
- **Multi-agent group chat** — `@ping` any agent mid-conversation. Multiple pings respond in succession. Group conversations with `/invite`d agents respond to every message in order. All swap back to the origin agent when done.
- **Tool plugin system** — tools extend `BaseTool`, register in `tools/registry.py`, and are assigned per-agent via `config.json`. Keyword triggers, slash commands, and autonomous `<tool_call>` XML tags all route through the same pipeline.
- **Self-observation** — every turn logs a reasoning entry to `meta_insight_log`. Agents can query their own decision history, emotional arc, and tool-use patterns. Reasoning is a rolling window — agents see their past few turns of thought.
- **Trajectory compressor** — long conversations are summarized and compacted into the model's history window automatically without losing continuity.

---

## Stack

| Layer | Tech |
|---|---|
| Backend | Python 3.12 · FastAPI · Uvicorn · WebSocket |
| LLM Inference | LM Studio (OpenAI-compatible REST) |
| Memory | SQLite · Markdown flat files |
| Frontend | Electron · React 18 (CDN) · Vanilla CSS |
| Tool system | Custom Python plugin router |

---

## Project structure

```
selene_server.py        Entry point — FastAPI app wiring, REST routes, WS dispatcher

selene_brain/           Core agent runtime
  llm_chat.py           LLMChat — chat loop, swap_agent, presence gate, mixin composition
  llm_caller.py         LM Studio API client, reasoning_content normalization
  lm_studio_manager.py  Model load/unload, skip logic for shared-model agents
  prompter.py           System prompt builder — soul + injected context + emotional state
  agent_memory.py       SQLite layer — dialog, meta_insight, tool_reasoning, emotional history
  memory_extractor.py   Background long-term memory extraction
  trajectory_compressor.py  Conversation compaction for long-context management
  conversation_manager.py   Conversation persistence, participants, chunk storage
  mood_observer.py      Emotional state tracker — moodlets, dominant mood, shift detection
  tool_suggestion.py    Phrase match → LLM gate → confidence threshold pipeline

server/                 WebSocket server package
  roster.py             Agent roster — scans agents/*/config.json, capability checks
  config.py             BASE_URL, SERVER_HOST/PORT, _normalize()
  state.py              selene_ref, clients, broadcast(), get_state()
  startup.py            _init_selene(), lifespan(), background task starters
  tool_pipeline.py      process_message(), update_memory_and_energy(), tool execution
  utils.py              clean_xml_tags(), split_response_chunks(), _format_tool_data()
  handlers/
    chat.py             chat, force_generate, rollback, clear_memory + presence layer
    conversations.py    new/load/rename/list/delete, invite_agent
    memory.py           get/save memory, force extract, tool phrase management
    manifest.py         task CRUD, guidelines, reorganize, compile_and_push
    knowledge.py        knowledge board, web search, arXiv, RSS
    system.py           state, models, toggle_agent, latency test, Discord, roster reload
    notifications.py    notification store, mark read, clear
    steam.py            Steam library scan + local game launcher
    youtube.py          YouTube transcript, co-watching, segment push
    story.py            Infinite Story Engine (12 handlers)
    misc.py             maps, document/RuneReader, notion, meta_insight

tools/                  Tool plugins — one file per tool
  schema.py             BaseTool interface + atomic_write
  registry.py           ToolRouter — registration and routing
  manifest.py           ManifestTool — task graph, daily planner, LLM reorganize
  todo.py               TodoTool — multi-step autonomous plan tracker
  memory_tool.py        ChronicleTool + MemoryTool
  status.py             StatusTool — system health checks
  meta_insight.py       Self-observation — agents query their own reasoning logs
  knowledge.py          Knowledge board (persistent context cards)
  runereader.py         Document synthesis tool
  file_manager.py       Local filesystem tool
  schedule.py           Schedule manager
  [+ integration tools: youtube, notion, maps, spotify, hass — dormant if unconfigured]

agents/                 Agent definitions — one folder per agent
  <slug>/
    config.json         Identity, model, capabilities, tools, colors, file paths
    character_profile.md  Who this agent is
    user_profile.md     What this agent knows about Ghost
    tools_context.md    How this agent thinks about its tools
    insights.md         Accumulated reflections (runtime, gitignored)
    prompt.txt          System prompt (gitignored — personal)
    soul.md             Character soul file (gitignored — personal)
    memory.db           SQLite memory store (gitignored — runtime)
  shared/               Cross-agent shared state (gitignored)
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
# Fill in LM_STUDIO_URL at minimum. Other keys (Notion, Google, Exa) enable optional tools.

# 4. Agent prompts
# Copy configs/templates/prompt.txt.example to agents/selene/prompt.txt
# Edit to define Selene's voice and behavior

# 5. Start
python selene_server.py
npm install && npm start
```

The server runs on `ws://localhost:8766`. On startup it checks LM Studio for the configured model and skips loading if it's already active.

---

## Adding an agent

Create `agents/<slug>/config.json`:

```json
{
  "name": "Akari",
  "title": "The Saintess",
  "domain": "Frontend, UI, Design Systems",
  "model": "google/gemma-3n-e4b",
  "model_path": "google/gemma-3n-e4b",
  "color_primary": "#f472b6",
  "color_glow": "rgba(244,114,182,0.08)",
  "role": "engineer",
  "capabilities": ["agent_creation"],
  "tools": ["manifest_manager", "todo", "file_manager", "knowledge_manager"],
  "memory_db": "memory.db",
  "prompt_file": "prompt.txt"
}
```

Restart the server. The roster auto-discovers the new folder. The frontend adapts on the next state push. No other file needs to change.

---

## Tool system

Tools extend `BaseTool` from `tools/schema.py` and register via `tools/registry.py`. Each tool implements:

- `execute(input_data)` — called by the router
- `check_and_trigger(user_input)` — optional keyword matching for automatic invocation

Tools are assigned to agents in `config.json` under `"tools"`. An agent only has access to the tools in its list.

---

## Status

Active development. See [CHANGELOG.md](CHANGELOG.md) for version history and [ARCHITECTURE.md](ARCHITECTURE.md) for design decisions and the roadmap for v0.9.

---

## License

MIT — see [LICENSE](LICENSE).
