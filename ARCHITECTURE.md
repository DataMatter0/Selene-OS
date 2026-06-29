# Selene OS — Architecture Reference

This document explains *why* the code is structured the way it is, and where it's going. Read this before touching anything non-trivial. Keep it updated as the system evolves.

---

## Package map

```
pantheon_brain/    Core agent runtime — LLM calls, memory, presence, prompting
tools/           Tool plugins — one file per tool, loaded at startup via registry
server/          WebSocket + REST server — one file per domain concern
selene_server.py Thin entry point (~400 lines) — app wiring + WS dispatcher
agents/          Self-contained agent folders — config, profiles, memory (per-agent)
```

---

## pantheon_brain/

The agent runtime. Everything inside here is agent-centric — it only knows about the model, memory, and conversation state. It has no FastAPI imports and no WebSocket handling.

**`llm_chat.py` — `LLMChat`**
The main class. Composes several mixins:
- `PromptBuilderMixin` — builds the system prompt from soul file + injected context + emotional state
- `ConversationManagerMixin` — new/load/save/rename/delete conversations, chunk persistence, participants
- `MemoryExtractorMixin` — background extraction of long-term memories from turn pairs
- `TrajectoryCompressorMixin` — compacts conversation history when working memory grows too large

Key methods used by the server:
- `chat(user_input)` — full turn: presence gate → tool routing → LLM → chunked delivery
- `swap_agent(slug)` — hot-swaps identity, memory DB, prompt, and tool access from `agents/{slug}/config.json`
- `run_choice_layer(user_input)` — RESPOND / OBSERVE / IGNORE presence gate
- `maybe_extract_memory(user, response)` — fires background memory extraction
- `compile_daily_manifest()` — aggregates task state into a daily summary

**`tool_suggestion.py` — `ToolSuggestionLayer`**
Sits between user input and the main LLM call. Pipeline:
1. Phrase match against `tool_phrases` SQLite table
2. If match → binary LLM gate (is this actually relevant?)
3. Confidence threshold — execute if high, inject warning if low
4. Falls through to normal chat if no match

**`mood_observer.py`**
Rolls emotional state from turn content. Updates `_cached_emotion` post-turn only — never on polling — to avoid neutral noise flooding meta_insight logs and state broadcasts.

---

## tools/

Each tool extends `BaseTool` from `tools/schema.py`. Tools are registered in `tools/registry.py` via `ToolRouter`, which handles both keyword routing and `<tool_call>` XML dispatch.

**Registration pattern:**
```python
router.register("manifest_manager", ManifestTool(agent_state=selene))
```

**Execution pattern:**
```python
result = selene.tool_router.route_and_execute("manifest_manager", {"command": "add_task", ...})
# Returns: {"status": "success"|"error", "data": ..., "message": ...}
```

**`tools/schema.py`**
Defines `BaseTool` and `atomic_write()` (write-then-rename for safe file updates). Used by any tool writing JSON state.

**`tools/story_engine/`**
Subpackage for the Infinite Story Engine RPG system. `db_helper.py` owns the SQLite schema. `InfiniteStoryEngine` handles dice resolution, character creation, merchant generation, and level-up.

---

## server/

Extracted from `selene_server.py` in v0.3. The design principle: `selene_server.py` should only wire things together — no business logic.

**`server/roster.py`**
Scans `agents/*/config.json` at startup. No central agent list anywhere else in the system.
- `get_roster()` — full list as dicts
- `get_agent(slug)` — single agent entry
- `agent_has_cap(slug, cap)` — capability check (replaces all `if slug == "sage"` guards)
- `default_agent_slug()` — whoever has `"default_boot"` capability
- `build_ping_map()` — `{slug: display_name}` for `@mention` routing
- `reload_roster()` — hot-reload without restart

**`server/state.py`**
Mutable globals shared across handler files:
- `selene_ref` — live `LLMChat` instance. `None` until init completes — all handlers guard with `if selene:`
- `clients` — set of connected WebSocket clients for broadcast
- `_cached_emotion` — post-turn emotion snapshot
- `broadcast()` — sends a dict to all connected clients

**`server/tool_pipeline.py`**
Core message routing:
- `process_message(user_input, response_mode)` — routes via tool_suggestion → keyword fallback → `LLMChat.chat()`
- `update_memory_and_energy(user, response)` — commits turn to working_memory
- `set_last_message_status(status)` — stamps SQLite + working_memory status field
- `_generate_tool_reasoning_background()` — post-hoc "was this call necessary?" reasoning for training dataset

**Circular import mitigation:**
`tool_pipeline.py` uses a lazy accessor to avoid import-time circular dependency:
```python
def _selene():
    from . import state as _s
    return _s.selene_ref
```

**`server/handlers/`**
Each file exposes `async def handle(websocket, data, loop) -> bool`. Returns `True` if handled, `False` to fall through. Dispatch order matters — `chat.py` is first (highest frequency), `misc.py` last.

---

## agents/

The Pantheon — six self-contained agent folders. No agent names are hardcoded in Python logic.

**`config.json` schema:**
```json
{
  "name": "Selene",
  "title": "The Voice",
  "domain": "Communication, Emotional Continuity, Daily Habits",
  "model": "google/gemma-3n-e4b",
  "model_path": "google/gemma-3n-e4b",
  "color_primary": "#2dd4bf",
  "color_glow": "rgba(45,212,191,0.08)",
  "role": "companion",
  "capabilities": ["default_boot", "grant_access"],
  "tools": ["memory_tool", "manifest_manager", "todo", ...],
  "memory_db": "memory.db",
  "prompt_file": "prompt.txt"
}
```

**`swap_agent(slug)` in `llm_chat.py`:**
All paths resolved from `config.json`. No hardcoded names.
```python
agent_dir        = os.path.join(_AGENTS_DIR, slug)
self.MEMORY_DIR  = agent_dir          # all tools inherit correct paths
self.db          = AgentMemoryStore(_ap("memory_db", "memory.db"))
self.prompt_path = _ap("prompt_file", "prompt.txt")
self.llm_caller.model_name = config.get("model", "")
```

**Model stack:**
| Agent | Model | Family |
|-------|-------|--------|
| Selene | `google/gemma-3n-e4b` | Gemma |
| Sage | `google/gemma-3n-e4b` | Gemma |
| Akari | `DevQuasar/Tesslate.UIGEN-T2-7B-GGUF:Q4_K_M` | Qwen (UI fine-tune) |
| Yami | `mistralai/Ministral-8B-Instruct-2410` | Mistral |
| ROM | `WarlordHermes/Huihui-Qwen3-VL-8B-Instruct-Creative-v0.4` | Qwen VL |
| RAM | `black-forest-labs/FLUX.1-schnell` | Diffusion (no LLM) |

---

## Data flow — a normal chat turn

```
UI  →  WS  →  handlers/chat.py
                 │
                 ├─ @mention parse → multi-agent response loop (if pings present)
                 │     swap → presence gate → respond → swap → ... → swap back to origin
                 │
                 ├─ presence gate (IGNORE / OBSERVE / RESPOND)
                 │
                 └─ tool_pipeline.process_message()
                       │
                       ├─ ToolSuggestionLayer.check(input)
                       │     ├─ phrase match → LLM gate → execute or warn
                       │
                       ├─ keyword fallback (tool_router.check_and_trigger)
                       │
                       └─ LLMChat.chat() → chunked response
                             │
                             ├─ update_memory_and_energy()
                             ├─ run_emotion_and_insight() [background thread]
                             │     ├─ log to meta_insight_log (category: "output")
                             │     └─ log to meta_insight_log (category: "emotion")
                             └─ broadcast chunks → UI
```

---

## Key invariants

- **`selene_ref` is None until `_init_selene()` completes.** Every handler guards with `if selene:`.
- **Emotion cache updates post-turn only.** The 2s state broadcaster reads `_cached_emotion` but never triggers an LLM call.
- **Soul files are never read by the server directly.** `_build_system_prompt()` owns prompt assembly. Handlers set `selene._prompt_dirty = True` when something changes.
- **Tool results always go through `_format_tool_data()`.** Raw `str()` on dicts produces unreadable model output.
- **`atomic_write` for any JSON state file.** Write to `.tmp`, then `os.replace()`. Prevents corruption on crash.
- **Capabilities, not slug names.** `agent_has_cap(slug, "grant_access")` — never `if slug == "sage"`.

---

## What's gitignored (never commit)

```
.env                       Live API keys
agents/*/memory.db         Per-agent SQLite memory stores
agents/*/prompt.txt        Per-agent system prompts
agents/*/soul.md           Per-agent soul docs
agents/*/manifest_state.json  Runtime task state
agents/shared/             Cross-agent runtime data
memories/                  Extracted long-term memory files
conversations/             Saved conversation JSON
pantheon_state.json          Runtime state snapshot
```

---

## v0.9 — Identity Packet + Reasoning Chain (Next session)

This section is the design spec for the next major development phase. Read this before writing any code.

---

### Problem

Currently `swap_agent()` sets ~12 separate properties on `self` (`active_agent_name`, `active_agent_slug`, `active_agent_title`, `allowed_tools`, `MEMORY_DIR`, `db`, `prompt_path`, etc.). Every tool that needs agent identity has to call `getattr(self.agent_state, "active_agent_name", "selene")` and similar scattered lookups. There's no single clean object a tool can receive that tells it everything it needs to know about who is currently active.

---

### Solution: `AgentIdentity` dataclass

Define in `pantheon_brain/agent_protocol.py` (already exists as a Protocol — extend it):

```python
from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class AgentIdentity:
    slug: str
    name: str
    title: str
    domain: str
    role: str
    color_primary: str
    color_glow: str
    capabilities: List[str]
    allowed_tools: List[str]
    model: str                     # LM Studio chat completions identifier
    model_path: str                # LM Studio load/unload API path
    memory_dir: str                # agents/{slug}/ absolute path
    prompt_path: str
    user_profile_path: str
    character_profile_path: str
    tools_context_path: str
    db: object                     # AgentMemoryStore instance
    prompt_text: str = ""          # loaded at swap time, cached here
```

**`swap_agent` becomes:**
```python
def swap_agent(self, slug: str) -> None:
    config = json.load(open(f"agents/{slug}/config.json"))
    self.identity = AgentIdentity(
        slug=slug, name=config["name"], ...
        db=AgentMemoryStore(_ap("memory_db", "memory.db")),
    )
    self.llm_caller.model_name = self.identity.model
    # The dozen self.active_* properties become self.identity.* reads
```

**Backwards compat:** Keep `active_agent_name`, `active_agent_slug` etc. as `@property` shims reading from `self.identity` so existing callers don't break immediately. Migrate gradually.

**Tools receive identity:** The `BaseTool.__init__` already takes `agent_state`. Add a property:
```python
@property
def identity(self) -> AgentIdentity:
    return self.agent_state.identity
```
Every tool then does `self.identity.name` instead of `getattr(self.agent_state, "active_agent_name", "selene")`.

**Matches roster structure:** `AgentIdentity` fields mirror `config.json` keys and `get_roster()` dict keys exactly. `AgentIdentity.from_config(slug, config_dict)` is a clean factory.

---

### Reasoning chain — rolling train of thought

**The vision (from SELENE_INNER_STATE_FEATURE.md, updated):**
Each agent maintains a rolling window of their own reasoning — not dialogue, not tool results, but first-person internal thought relative to their identity. Selene thinks as Selene. Sage thinks as Sage.

**Implementation:**

1. **Storage:** `meta_insight_log` already exists with `category="reasoning"` available. Each turn, the `run_emotion_and_insight` background thread logs a third entry:
   ```python
   self.db.log_meta_insight(
       agent=slug,
       category="reasoning",
       subcategory="turn_thought",
       input_context=user_input[:300],
       reasoning=thought_content,     # extracted from <think> block
       result="",
       confidence_score=_conf,
       trigger_mode="reasoning_chain",
       session_id=session_id,
   )
   ```

2. **Prompt injection:** `_build_system_prompt()` (in `prompter.py`) queries the last 3–5 `category="reasoning"` entries for the active agent and injects them between the soul anchor and memory profiles:
   ```
   ══════════════════════════════════════
   YOUR RECENT TRAIN OF THOUGHT
   ══════════════════════════════════════
   [3 most recent reasoning entries, most-recent-first, first-person]
   ```

3. **First-person constraint:** Each agent's `prompt.txt` should instruct: *"When you reason or reflect internally, do so in first person as [Name]. Your thoughts are your own. Write them the way you actually think, not as an observer."* This is per-agent in the prompt file, not hardcoded in Python.

4. **Rolling window:** Only the past N turns of reasoning are injected — agents don't carry the full reasoning history in context, just recent thought. Full history is queryable via `meta_insight` tool.

5. **Write-back:** Agents can write back to their `insights.md` and `character_profile.md` via existing `memory_tool` and `manifest_manager`. The reasoning chain creates the raw material; the memory extractor and manifest compiler do the distillation.

---

### Surgical edit plan for v0.9

**Step 1 — `AgentIdentity` dataclass** (no behavior change)
- Add `AgentIdentity` to `pantheon_brain/agent_protocol.py`
- Add `AgentIdentity.from_config(slug, config)` factory classmethod
- Update `swap_agent` to construct `self.identity` and set shim properties
- Add `identity` property to `BaseTool`
- **Files:** `pantheon_brain/agent_protocol.py`, `pantheon_brain/llm_chat.py`, `tools/schema.py`
- **Test:** boot, swap agents, verify all tools still work

**Step 2 — Migrate tool identity lookups** (cosmetic, gradual)
- Replace `getattr(self.agent_state, "active_agent_name", ...)` with `self.identity.name` across all tools
- **Files:** all files in `tools/`
- **Test:** no behavior change expected — property shims handle it

**Step 3 — Reasoning chain logging**
- In `run_emotion_and_insight` (in `llm_chat.py`), extract `<think>` block from `_reasoning_snap` and log as `category="reasoning"` entry
- **Files:** `pantheon_brain/llm_chat.py`
- **Test:** check `meta_insight` REASONING category populates in MetaInsightView

**Step 4 — Reasoning chain prompt injection**
- In `prompter.py` `_build_system_prompt()`, query last 5 `category="reasoning"` entries for active agent
- Inject as `YOUR RECENT TRAIN OF THOUGHT` block between soul anchor and memory profiles
- **Files:** `pantheon_brain/prompter.py`, `pantheon_brain/agent_memory.py` (ensure query method supports category filter)
- **Test:** verify injected block appears in system prompt, verify no prompt length explosion

**Step 5 — First-person reasoning instruction**
- Add reasoning instruction to each agent's `prompt.txt` (not Python) — *"Reason in first person as [Name]..."*
- **Files:** `agents/*/prompt.txt` (gitignored — do this locally)

**Step 6 — Integrated code editor tool**
*(Separate planning session — design the tool interface and file scope before writing code)*
- Tool plugin in `tools/code_editor.py`
- Capability-gated (Sage, Akari likely) via `config.json`
- Reads/writes files in `MEMORY_DIR` or a defined workspace scope
- Tool call interface: `read_file`, `write_file`, `diff`, `run_snippet`
- UI panel in `ToolsView.jsx` gated on `code_editor` capability

---

---

## Data migration — `memories/` cleanup (do before v0.9)

The `memories/` folder is gitignored legacy content from before the per-agent folder system existed. It contains real data that should be migrated before deleting. Do this manually — it's personal content, not code.

### What to migrate and where

| `memories/` file | Migrate to | Notes |
|---|---|---|
| `character_profile.md` | `agents/selene/character_profile.md` | Merge with existing stub — keep the richer content |
| `sage_character_profile.md` | `agents/sage/character_profile.md` | Same |
| `user_profile.md` | `agents/selene/user_profile.md` | Merge |
| `sage_user_profile.md` | `agents/sage/user_profile.md` | Merge |
| `tools_context.md` | `agents/selene/tools_context.md` | Merge |
| `selene_insights.md` | `agents/selene/insights.md` | Append — insights are additive |
| `selene_notes.md` | `agents/selene/insights.md` | Append anything still relevant |

### What to delete (no migration needed)

| File | Reason |
|---|---|
| `memories/development_manifest.md` | Superseded by manifest tool + `agents/*/manifest_state.json` |
| `memories/philosophy_manifest.md` | Superseded |
| `memories/prioritization_guidelines.md` | Superseded |
| `memories/build_context.md` | Superseded by `ARCHITECTURE.md` |
| `memories/world_knowledge.md` | Move to knowledge board via the tool, not a flat file |
| `memories/sage_memory.db` | Dead copy — live DB is `agents/sage/memory.db` |
| `memories/selene_memory.db` | Dead copy — live DB is `agents/selene/memory.db` |
| `memories/manifest_state.json` | Dead copy |
| `memories/sage_manifest_state.json` | Dead copy |
| `memories/selene_manifest_state.json` | Dead copy |
| `memories/knowledge_board_state.json` | Dead copy — live is `agents/shared/knowledge_board_state.json` |
| `memories/runereader_notes/` | Generated synthesis output, not source data |
| `memories/story_engine/story_engine.db` | Verify the live story DB path in `pantheon_brain/story_engine.py` first — if it reads from here, update the path to `agents/shared/story_engine.db` and move the file |

### After migration

Once content is merged into `agents/*/`, the `memories/` folder can be emptied. It stays in `.gitignore` so any new runtime files written there won't be tracked. Nothing in Python currently reads from `memories/` — all paths resolve through `self.MEMORY_DIR` (which is `agents/{slug}/`) after the v0.5 migration.

**Verify before deleting:** grep for `memories/` in `*.py` to confirm nothing still hardcodes that path.

```cmd
grep -r "memories/" pantheon_brain/ server/ tools/ selene_server.py
```

If any hits come back that aren't comments, fix the path reference before deleting the file.

---

## Other cleanup before v0.9

**`models/` at project root** — LM Studio model cache that landed in the wrong place. Added to `.gitignore`. Never commit.

**`pantheon_state.json`** — OS-level runtime state: `creative_energy`, `last_conversation_id`, `active_agent`, `agent_layouts` (dashboard slot config per agent). This is intentionally a single shared file — it tracks the state of the OS session, not any individual agent. Already gitignored. Path is hardcoded in `pantheon_brain/llm_chat.py` as `_SCRIPT_DIR/pantheon_state.json` (project root). Keep as-is. Regenerates on boot if deleted.

**`CODEBASE.md`** — replaced by `ARCHITECTURE.md`. Added to `.gitignore`. Run `git rm --cached CODEBASE.md` to remove from index.

**`dataset/`** — private fine-tuning conversations (`.jsonl`). Already gitignored. Never stage.

**`conversations/`** — private session JSON. Already gitignored. Never stage.

**`selene_discord.py`** — gitignored (personal config). Low priority — could move to `server/discord.py` eventually.

---

## `pantheon_brain/` rename → `agent_runtime/`

**Why:** `pantheon_brain` encodes a specific agent name into the package that runs all agents. The package contains the runtime for the entire Pantheon — LLM calling, memory, presence, prompting, conversation management. `agent_runtime` is accurate and agent-neutral.

**Scope — exactly 11 import lines to update:**

| File | Current import |
|---|---|
| `server/startup.py` | `from pantheon_brain import LLMChat, LMStudioManager` |
| `server/startup.py` | `from pantheon_brain.tool_suggestion import ToolSuggestionLayer` |
| `server/handlers/system.py` | `from pantheon_brain import LMStudioManager` |
| `server/handlers/story.py` | `from pantheon_brain.story_engine import InfiniteStoryEngine` (×4) |
| `tools/manifest.py` | `from pantheon_brain.agent_protocol import AgentState` |
| `tools/memory_tool.py` | `from pantheon_brain.agent_protocol import AgentState` |
| `tools/status.py` | `from pantheon_brain.agent_protocol import AgentState` |
| `tools/todo.py` | `from pantheon_brain.agent_protocol import AgentState` |
| `tools/story_engine/story_tools.py` | `from pantheon_brain.story_engine import InfiniteStoryEngine` |

**Steps:**
1. Rename folder: `pantheon_brain/` → `agent_runtime/`
2. Update all 11 import lines: `pantheon_brain` → `agent_runtime`
3. Delete `pantheon_brain/__pycache__/` if present
4. Commit: `refactor: rename pantheon_brain to agent_runtime`

**Nothing else changes** — no logic, no file moves within the package, no class renames. Pure find-and-replace on the package name.

**CMD rename:**
```cmd
move "pantheon_brain" "agent_runtime"
```
Then update imports. Then clear pycache:
```cmd
rmdir /s /q agent_runtime\__pycache__ 2>nul
```

---

## Commit checklist for v0.9

Before committing any v0.9 work:
- `agents/*/manifest_state.json` is in `.gitignore` — verify `git status` doesn't show these
- `agents/shared/` is in `.gitignore`
- `models/` is in `.gitignore`
- No `agents/*/prompt.txt` or `agents/*/soul.md` staged
- `server/roster.py` is tracked (was untracked in v0.8)
- `CODEBASE.md` removed from git index (`git rm --cached CODEBASE.md`)
- No files from `memories/`, `conversations/`, `dataset/` staged
- Run `git status` and read every line before committing
