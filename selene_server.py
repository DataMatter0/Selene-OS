"""
selene_server.py -- WebSocket + REST API bridge for Selene OS UI
----------------------------------------------------------------
Run:   python selene_server.py
Then:  npm start  (in this same folder, to launch Electron)
       -- or open renderer/index.html in a browser for quick testing.

WebSocket protocol  ws://localhost:8765/ws
  Client -> Server:
    {"type": "chat",         "content": "user message"}
    {"type": "clear_memory"}
    {"type": "get_state"}

  Server -> Client:
    {"type": "connected",     "data": <state>}
    {"type": "thinking"}
    {"type": "response",      "content": "selene reply"}
    {"type": "state",         "data": <state>}
    {"type": "autonomy_start"}
    {"type": "autonomy_end"}
    {"type": "error",         "message": "..."}
"""

import asyncio
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Set

import json
import re
import winreg

def get_steam_games_list():
    games = []
    
    # 1. Load local games from C:\Games
    local_dir = r"C:\Games"
    if os.path.exists(local_dir):
        try:
            for folder_name in os.listdir(local_dir):
                folder_path = os.path.join(local_dir, folder_name)
                if os.path.isdir(folder_path):
                    # Look for executables in the root of the folder
                    exe_path = None
                    try:
                        for filename in os.listdir(folder_path):
                            if filename.lower().endswith(".exe") and not "unity" in filename.lower() and not "crash" in filename.lower():
                                # Prefer filename matching folder name
                                if filename.lower().startswith(folder_name.lower()):
                                    exe_path = os.path.normpath(os.path.join(folder_path, filename))
                                    break
                                if not exe_path:
                                    exe_path = os.path.normpath(os.path.join(folder_path, filename))
                    except Exception:
                        pass
                    
                    # If no exe found, fallback to folder path
                    if not exe_path:
                        exe_path = os.path.normpath(folder_path)
                        
                    games.append({
                        "appid": f"local_{folder_name}",
                        "name": folder_name,
                        "exe_path": exe_path,
                        "is_local": True
                    })
        except Exception as e:
            print("[Local Games Parser] Error:", e)

    # 2. Load Steam games
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        steam_path, _ = winreg.QueryValueEx(key, "SteamPath")
        apps_dir = os.path.join(steam_path, "steamapps")
        if os.path.exists(apps_dir):
            for f in os.listdir(apps_dir):
                if f.startswith("appmanifest_") and f.endswith(".acf"):
                    try:
                        with open(os.path.join(apps_dir, f), "r", encoding="utf-8") as file:
                            content = file.read()
                            appid_match = re.search(r'"appid"\s+"(\d+)"', content)
                            name_match = re.search(r'"name"\s+"([^"]+)"', content)
                            if appid_match and name_match:
                                games.append({"appid": appid_match.group(1), "name": name_match.group(1)})
                    except Exception:
                        pass
    except Exception as e:
        print("[Steam Parser] Error:", e)
        
    games.sort(key=lambda x: x['name'].lower())
    return games

logger = logging.getLogger("selene_server")

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, FileResponse

from selene_brain import LLMChat, LMStudioManager

load_dotenv()   # reads .env if present; silently no-ops if not

# -- Config --------------------------------------------------------------------

BASE_URL      = os.environ.get("LM_STUDIO_URL") or os.environ.get("LM_BASE_URL") or "http://10.0.0.35:1234"
DESIRED_MODEL = os.environ.get("LM_STUDIO_MODEL") or os.environ.get("LM_MODEL") or "nvidia/nemotron-3-nano-4b"
SERVER_HOST        = "127.0.0.1"   # explicit IPv4 — avoids localhost→::1 ambiguity on Windows
SERVER_PORT        = 8766

# -- Globals -------------------------------------------------------------------

selene: Optional[LLMChat] = None
# Track connected WebSocket clients
clients: Set[WebSocket] = set()
_prev_writing: bool = False   # tracks last broadcast state
main_loop = None               # main asyncio event loop for threadsafe broadcasts

# Cached emotion state — only updates after a turn completes, not every 2s poll.
# Prevents neutral noise flooding meta_insight and state broadcasts.
_cached_emotion: dict = {"mood_index": 0, "emotion": ""}

def _normalize(name: str) -> str:
    """Case/separator-insensitive model name comparison."""
    return name.lower().replace(" ", "").replace("-", "").replace("_", "").replace(".", "").replace("/", "")


def clean_xml_tags(text: str) -> str:
    if not text:
        return ""
    import re
    # Preserve <think>...</think> intact — ThoughtBubble in ChatView parses it.
    # Step 1: pull out preserved blocks so subsequent regexes can't touch them.
    # tool_reasoning — training data block shown as ThoughtBubble in UI
    # think — legacy reasoning block (backward compat)
    tool_block = ""
    tool_match = re.search(r'<tool_reasoning[^>]*>[\s\S]*?</tool_reasoning>', text, flags=re.IGNORECASE)
    if tool_match:
        tool_block = tool_match.group(0)
        text = text[:tool_match.start()] + "\x00TOOL\x00" + text[tool_match.end():]

    think_block = ""
    think_match = re.search(r'<think>[\s\S]*?</think>', text, flags=re.IGNORECASE)
    if think_match:
        think_block = think_match.group(0)
        text = text[:think_match.start()] + "\x00THINK\x00" + text[think_match.end():]

    # Step 2: strip all other XML tags/blocks
    cleaned = re.sub(r'<(?!think\b)([a-zA-Z0-9_\-]+)[^>]*>([\s\S]*?)</\1>', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'<[^>]+/>', '', cleaned)
    cleaned = re.sub(r'<(?!/?think\b)[^>]+>', '', cleaned)
    cleaned = re.sub(r'</?think>', '', cleaned, flags=re.IGNORECASE)

    # Step 3: restore preserved blocks
    cleaned = cleaned.replace("\x00TOOL\x00", tool_block)
    cleaned = cleaned.replace("\x00THINK\x00", think_block)

    return cleaned.strip()


def split_response_chunks(text: str) -> list:
    """
    Splits Selene's response into conversational message chunks.

    Groups sentences into chunks of 2-4 sentences each (random per chunk)
    so the delivery feels like natural typed thought rather than one wall
    or one-sentence-at-a-time staccato.

    Sage does NOT use this — she sends one complete structured response.
    """
    import re as _re
    import random as _random

    if not text:
        return []

    # Split into individual sentences on punctuation boundaries
    sentences = [s.strip() for s in _re.split(r'(?<=[.!?…])\s+', text) if s.strip()]

    if len(sentences) <= 2:
        # Short response — send as one chunk
        return [text.strip()]

    chunks: list = []
    i = 0
    while i < len(sentences):
        # Pick a random group size of 2-4 sentences
        group_size = _random.randint(2, 4)
        group = sentences[i:i + group_size]
        chunks.append(" ".join(group))
        i += group_size

    return [c for c in chunks if c.strip()]


def extract_presence_decision(text: str) -> Optional[str]:
    """Return observe/ignore when the model chose a silent presence tool."""
    if not text:
        return None
    match = re.search(r'<presence_decision\b[^>]*\bmode=["\']?(observe|ignore)["\']?', text, flags=re.IGNORECASE)
    return match.group(1).lower() if match else None


def get_state() -> dict:
    """JSON-serialisable snapshot of Selene's live state."""
    if selene is None:
        return {
            "status":            "offline",
            "creative_energy":   0,
            "memory_count":      0,
            "is_running":        False,
            "conversation_id":   None,
            "conversation_name": "New Conversation",
            "active_agent":      "selene",
            "tools": [
                "chronicle_manager",
                "memory_tool",
                "status_checker",
                "manifest_manager",
                "todo",
                "schedule_manager",
                "knowledge_manager",
                "file_manager",
                "document_reader",
                "runereader",
                "maps",
                "notion",
                "youtube"
            ],
            "dashboard_layout": {
                "left": "fused_manifest",
                "center": "main_chat",
                "right": "status_panel"
            }
        }
    with selene.lock:
        active_agent = getattr(selene, "active_agent_name", "Selene").lower()
        layout = getattr(selene, "dashboard_layout", {
            "left": "fused_manifest",
            "center": "main_chat",
            "right": "status_panel"
        })

        return {
            "status":            "writing" if selene.is_writing_autonomously else "idle",
            "creative_energy":   selene.creative_energy,
            "memory_count":      len(selene.working_memory) // 2,
            "is_running":        selene.is_running,
            "conversation_id":   selene.active_conversation_id,
            "conversation_name": selene.active_conversation_name,
            "active_agent":      active_agent,
            "tools":             getattr(selene, "allowed_tools", []),
            "dashboard_layout":  layout,
            # Use cached emotion — only updates after a turn, not every 2s poll
            "mood_index":        _cached_emotion["mood_index"],
            "emotion":           _cached_emotion["emotion"],
        }


def _generate_tool_reasoning_background(
    selene_ref, tool_name: str, trigger_mode: str,
    user_input: str, tool_args: str, tool_result: str,
    session_id: str, turn_id: str, chain_id: str = ""
):
    """
    Background thread: generates post-hoc validation reasoning for a tool call
    and writes it to tool_reasoning_log.

    Post-hoc reasoning validates the necessity of the call after seeing the
    result — wrong calls are obvious here, making the training data honest.
    """
    try:
        agent = getattr(selene_ref, "active_agent_name", "selene").lower()

        if trigger_mode == "direct":
            # Keyword-triggered — Ghost explicitly asked for it
            reasoning = f"Direct user request. Ghost's message contained a keyword that maps to {tool_name}. Result confirmed the call was appropriate."
        else:
            # LLM-initiated — generate one-sentence validation
            reasoning_prompt = (
                f"You called the '{tool_name}' tool during this conversation turn.\n\n"
                f"Ghost said: {user_input[:300]}\n"
                f"Tool args: {tool_args[:200]}\n"
                f"Tool result: {tool_result[:300]}\n\n"
                f"In one sentence, explain why calling this tool was or was not necessary "
                f"given what Ghost said. Be honest — if the call was unnecessary, say so."
            )
            reasoning = selene_ref.llm_caller.call_llm(
                input_data="/no_think\n" + reasoning_prompt,
                system_prompt="Reply with exactly one sentence of honest post-hoc reasoning. No preamble.",
                history=[],
                temperature=0.3,
                max_tokens=80,
            )
            # Strip think blocks if model produced them despite /no_think
            import re as _re
            reasoning = _re.sub(r'<think>[\s\S]*?</think>', '', reasoning, flags=_re.IGNORECASE).strip()

        selene_ref.db.log_tool_reasoning(
            agent=agent,
            session_id=session_id,
            turn_id=turn_id,
            tool_name=tool_name,
            trigger_mode=trigger_mode,
            input_context=user_input,
            tool_args=tool_args,
            tool_result=tool_result,
            reasoning=reasoning,
            chain_id=chain_id,
        )
        print(f"[Tool Reasoning]: Logged for {tool_name} ({trigger_mode})")
    except Exception as e:
        print(f"[Tool Reasoning]: Failed — {e}")


def _format_tool_data(data: Any) -> str:
    """
    Convert raw tool result data to a clean, model-readable string.
    Avoids Python repr (single-quoted dicts/lists) by using JSON or
    structured text for list-of-dict results like meta_insight queries.
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        if not data:
            return "No results found."
        # List of dicts — format each as a mini block the model can narrate
        if isinstance(data[0], dict):
            lines = []
            for i, entry in enumerate(data, 1):
                # Build a human-readable block per entry
                parts = [f"[{i}]"]
                for k, v in entry.items():
                    if k in ("id",):
                        continue  # skip internal IDs from narration
                    if isinstance(v, dict):
                        v = json.dumps(v)
                    parts.append(f"  {k}: {v}")
                lines.append("\n".join(parts))
            return "\n\n".join(lines)
        return json.dumps(data, ensure_ascii=False)
    if isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False, indent=2)
    return str(data)


def _execute_tool_and_respond(triggered_name: str, triggered_args: Any,
                               user_input: str, trigger_mode: str = "direct") -> str:
    """Shared tool execution path used by both keyword and suggestion routes."""
    if hasattr(selene, "thought_callback") and selene.thought_callback:
        selene.thought_callback("tool_call", f"Tool: {triggered_name}", f"Args: {triggered_args}")

    _emotion_before = {"energy": selene.creative_energy, "status": "idle"}
    result          = selene.tool_router.route_and_execute(triggered_name, triggered_args)
    result_data     = _format_tool_data(result.get("data", ""))
    _emotion_after  = {"energy": selene.creative_energy, "status": "idle"}

    session_id = selene.active_conversation_id or "default"
    turn_id    = str(uuid.uuid4())

    try:
        selene.db.log_meta_insight(
            agent=getattr(selene, "active_agent_name", "selene").lower(),
            category="tool_use", subcategory=triggered_name,
            input_context=user_input[:500],
            reasoning=f"Trigger: {trigger_mode}. Args: {str(triggered_args)[:400]}",
            result=result_data[:1000],
            emotional_state_before=_emotion_before,
            emotional_state_after=_emotion_after,
            confidence_score=1.0 if trigger_mode in ("direct", "slash") else 0.85,
            trigger_mode=trigger_mode, session_id=session_id,
        )
    except Exception:
        pass

    threading.Thread(
        target=_generate_tool_reasoning_background,
        args=(selene, triggered_name, trigger_mode, user_input,
              str(triggered_args), result_data, session_id, turn_id),
        daemon=True
    ).start()

    if hasattr(selene, "thought_callback") and selene.thought_callback:
        selene.thought_callback("tool_response", f"Tool Completed: {triggered_name}",
                                f"Status: {result.get('status')}\nResult: {result_data[:300]}")

    thought_log = (f"[Tool: {triggered_name} | trigger: {trigger_mode}]\n"
                   f"Args: {triggered_args}\nResult: {result_data}")
    selene.db.log_dialog(session_id, "assistant", result_data, thought_log, "read")

    final_reply = _format_tool_data(result.get("data")) if result.get("status") == "success" \
                  else f"I tried to use {triggered_name} but something went wrong: {result.get('message')}"

    return f"<tool_reasoning turn_id=\"{turn_id}\">\n{thought_log}\n</tool_reasoning>\n{final_reply}"


def process_message(user_input: str, disable_tools: bool = False,
                    suggestion_warning: str = "") -> str:
    """
    Routes a user message: slash command → phrase suggestion gate → normal chat.
    suggestion_warning is injected when the suggestion layer flagged low confidence.
    """
    if selene is None:
        return "[System Error]: Selene is not initialised."

    if not disable_tools and hasattr(selene, "tool_suggestion") and selene.tool_suggestion:
        decision = selene.tool_suggestion.process(user_input)

        if decision["decision"] == "execute":
            triggered_name = decision["tool_name"]
            triggered_args = decision["args"]
            trigger_mode   = decision.get("trigger", "direct")
            return _execute_tool_and_respond(triggered_name, triggered_args,
                                              user_input, trigger_mode)

        elif decision["decision"] == "suggest":
            # Low confidence — pass warning to chat() via turn context injection
            # The warning is returned to the WS handler which prepends it
            # to the user message before calling chat()
            return selene.chat(user_input, disable_tools=True,
                                _suggestion_warning=decision.get("warning", ""))

        # decision == "pass" — fall through to normal chat

    # Legacy path: no suggestion layer attached yet (e.g. on boot)
    # Keep old keyword trigger loop as fallback
    triggered_args = None
    triggered_name = None
    if not disable_tools:
        allowed = getattr(selene, "allowed_tools", None)
        for tool in selene.tool_router.tools.values():
            if allowed is not None and tool.name not in allowed:
                continue
            if hasattr(tool, "check_and_trigger"):
                triggered_args = tool.check_and_trigger(user_input)
                if triggered_args:
                    triggered_name = tool.name
                    break

    if triggered_name and triggered_args is not None:
        print(f"[Selene Server]: Tool triggered -- {triggered_name}")
        if hasattr(selene, "thought_callback") and selene.thought_callback:
            selene.thought_callback("tool_call", f"Keyword Triggered Tool: {triggered_name}", f"Trigger args: {triggered_args}")

        _emotion_before = {"energy": selene.creative_energy, "status": "idle"}
        result = selene.tool_router.route_and_execute(triggered_name, triggered_args)
        result_data = _format_tool_data(result.get("data", ""))
        _emotion_after  = {"energy": selene.creative_energy, "status": "idle"}

        session_id = selene.active_conversation_id or "default"
        turn_id = str(uuid.uuid4())

        # MetaInsight: log keyword-triggered tool use
        try:
            selene.db.log_meta_insight(
                agent=getattr(selene, "active_agent_name", "selene").lower(),
                category="tool_use",
                subcategory=triggered_name,
                input_context=user_input[:500],
                reasoning=f"Keyword triggered. Args: {str(triggered_args)[:400]}",
                result=result_data[:1000],
                emotional_state_before=_emotion_before,
                emotional_state_after=_emotion_after,
                confidence_score=1.0,
                trigger_mode="keyword",
                session_id=session_id,
            )
        except Exception:
            pass

        # Post-hoc reasoning logging — runs in background, never blocks response
        threading.Thread(
            target=_generate_tool_reasoning_background,
            args=(selene, triggered_name, "direct", user_input,
                  str(triggered_args), result_data, session_id, turn_id),
            daemon=True
        ).start()

        if hasattr(selene, "thought_callback") and selene.thought_callback:
            selene.thought_callback("tool_response", f"Tool Completed: {triggered_name}", f"Status: {result.get('status')}\nResult: {result_data[:300]}{'...' if len(result_data) > 300 else ''}")

        thought_log = f"[Keyword Triggered Tool: {triggered_name}] Trigger args: {triggered_args}\n[Tool Completed: {triggered_name}] Status: {result.get('status')}\nResult: {result_data}"

        # Log keyword tool turns to DB
        selene.db.log_dialog(session_id, "assistant", result_data, thought_log, "read")

        if result.get("status") == "success":
            final_reply = _format_tool_data(result.get("data"))
            return f"<tool_reasoning turn_id=\"{turn_id}\">\n{thought_log}\n</tool_reasoning>\n{final_reply}"
        else:
            final_reply = f"I tried to use the {triggered_name} tool, but something went wrong: {result.get('message')}"
            return f"<tool_reasoning turn_id=\"{turn_id}\">\n{thought_log}\n</tool_reasoning>\n{final_reply}"

    return selene.chat(user_input, disable_tools=disable_tools)


def set_last_message_status(session_id: str, status: str):
    """
    Update the status of the last user message in both:
      1. SQLite dialog_history (for logging/query)
      2. working_memory (so save_current_conversation persists it)
    This ensures read/observed/ignored markers survive conversation reloads.
    """
    if selene is None:
        return
    # Update SQLite
    try:
        selene.db.update_last_message_status(session_id, status)
    except Exception:
        pass
    # Update working_memory — find last user entry and stamp status
    with selene.lock:
        for i in range(len(selene.working_memory) - 1, -1, -1):
            if selene.working_memory[i].get("role") == "user":
                selene.working_memory[i]["status"] = status
                break


# selene_server.py  update_memory_and_energy()
def update_memory_and_energy(user_input: str, response: str, chunks: list = None):
    """
    Commit a turn to working_memory.

    chunks — if provided (Selene's split delivery), each chunk is stored as a
    separate assistant entry sharing a chunk_group ID. On conversation reload
    they render as individual bubbles exactly as originally sent.
    The model sees them merged back into one turn via the chunk_group logic
    in chat()'s local_history builder.

    Without chunks (Sage, tool responses) a single entry is stored as before.
    """
    if selene is None:
        return
    selene.creative_energy       = min(100, selene.creative_energy + 10)
    selene.last_interaction_time = time.time()

    # Strip internal tags before writing to working_memory
    clean = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL | re.IGNORECASE)
    clean = clean_xml_tags(clean).strip()
    if not clean:
        clean = response.strip()

    _agent = getattr(selene, "active_agent_name", "selene").lower()

    with selene.lock:
        _ts = time.time()
        selene.working_memory.append({"role": "user", "content": user_input, "ts": _ts})

        if chunks and len(chunks) > 1:
            _group_id = str(uuid.uuid4())[:8]
            for chunk in chunks:
                selene.working_memory.append({
                    "role":        "assistant",
                    "content":     chunk,
                    "ts":          _ts,
                    "chunk_group": _group_id,
                    "agent":       _agent,
                })
        else:
            selene.working_memory.append({
                "role":  "assistant",
                "content": clean,
                "ts":    _ts,
                "agent": _agent,
            })

        window = selene.memory_window * 2
        if len(selene.working_memory) > window:
            selene.working_memory = selene.working_memory[-window:]

    selene.maybe_extract_memory(user_input, clean)

# -- Broadcast helpers ---------------------------------------------------------

async def broadcast(message: dict):
    """Send a JSON payload to every connected client, pruning dead sockets."""
    # Use a plain set at runtime; typing.Set is only for annotations.
    dead = set()
    for ws in list(clients):
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    # Remove any dead sockets from the global clients set.
    for d in dead:
        clients.discard(d)


global_guide_button = 16

def _gamepad_poller_thread(loop):
    global global_guide_button
    try:
        import pygame
        import time
        pygame.init()
        pygame.joystick.init()
        
        joysticks = []
        for x in range(pygame.joystick.get_count()):
            j = pygame.joystick.Joystick(x)
            j.init()
            joysticks.append(j)
            
        while True:
            time.sleep(0.05)
            pygame.event.pump()
            
            if pygame.joystick.get_count() != len(joysticks):
                joysticks = []
                for x in range(pygame.joystick.get_count()):
                    j = pygame.joystick.Joystick(x)
                    j.init()
                    joysticks.append(j)

            for joy in joysticks:
                num_buttons = joy.get_numbuttons()
                is_select_start = False
                is_guide_pressed = False
                
                # Check common Select + Start dual button mappings:
                if num_buttons > 9 and joy.get_button(8) and joy.get_button(9):
                    is_select_start = True
                elif num_buttons > 7 and joy.get_button(6) and joy.get_button(7):
                    is_select_start = True
                
                # Check if custom guide button is pressed
                if num_buttons > global_guide_button and joy.get_button(global_guide_button):
                    is_guide_pressed = True

                if is_select_start or is_guide_pressed:
                    asyncio.run_coroutine_threadsafe(
                        broadcast({"type": "force_focus"}),
                        loop
                    )
                    time.sleep(1.0) # Debounce
                    break
    except Exception as e:
        print("[Gamepad] Poller failed:", e)


async def _timer_poller():
    """
    Background asyncio task.
    Polls the schedule manager every 10 seconds.
    If a timer expires, broadcasts a notification and pushes it into Selene's working memory as an alert.
    """
    import time
    while True:
        await asyncio.sleep(10)
        if selene and "schedule_manager" in selene.tool_router.tools:
            try:
                tool = selene.tool_router.tools["schedule_manager"]
                if hasattr(tool, "load_state") and hasattr(tool, "save_state"):
                    state = tool.load_state()
                    now = time.time()
                    expired = []
                    active = []
                    for t in state.get("timers", []):
                        if t["trigger_time"] <= now:
                            expired.append(t)
                        else:
                            active.append(t)
                    
                    if expired:
                        state["timers"] = active
                        tool.save_state(state)
                        
                        for t in expired:
                            msg = f"[ALARM/TIMER EXPIRED] Title: {t.get('message', 'Timer')}"
                            # 1. Alert UI
                            await broadcast({"type": "timer_expired", "data": {"message": msg, "id": t["id"]}})
                            # 2. Add to Selene's working memory
                            with selene.lock:
                                selene.working_memory.append({"role": "user", "content": msg, "ts": time.time()}) # type: ignore
                            
                        # If she's idle, maybe she can respond? Let's just leave it in memory for her next turn.
            except Exception as e:
                print(f"[Timer Poller Error]: {e}")


async def _state_broadcaster():
    """
    Background asyncio task.
    Every 2 s: broadcasts current state and fires autonomy_start / autonomy_end
    events when Selene's writing status changes.
    """
    global _prev_writing
    while True:
        await asyncio.sleep(2)
        if not clients:
            continue

        state      = get_state()
        is_writing = state["status"] == "writing"

        if is_writing != _prev_writing:
            _prev_writing = is_writing
            await broadcast({"type": "autonomy_start" if is_writing else "autonomy_end"})

        await broadcast({"type": "state", "data": state})

# -- Startup / shutdown --------------------------------------------------------

def _init_selene():
    """
    Blocking initialisation: checks LM Studio, loads the desired model,
    instantiates LLMChat, and starts the autonomy monitor thread.
    Called from a thread-pool executor so it doesn't block the event loop.
    """
    global selene

    # Perform story engine database checks and migrations on startup
    from tools.story_engine.db_helper import initialize_database
    try:
        initialize_database()
        print("[Selene Server]: Story engine database initialized and verified.")
    except Exception as db_err:
        print(f"[Selene Server Error]: Database initialization failed: {db_err}")

    print("[Selene Server]: Contacting LM Studio...")
    manager = LMStudioManager(base_url=BASE_URL)

    loaded = manager.get_loaded_model_info()

    norm_target = _normalize(DESIRED_MODEL)
    loaded_path = loaded.get("path", "") if loaded else ""
    active_path: Optional[str] = None

    if loaded and norm_target in _normalize(loaded_path):
        print(f"[Selene Server]: Desired model already loaded -- {loaded_path}")
        active_path = loaded_path
    else:
        if loaded:
            print(f"[Selene Server]: A different model is loaded ('{loaded_path}').")
        elif loaded is None:
            print("[Selene Server]: Server is offline or no model is loaded.")

        print(f"[Selene Server]: Attempting to load model -- {DESIRED_MODEL}")
        if manager.load_model(DESIRED_MODEL):
            active_path = DESIRED_MODEL
            print(f"[Selene Server]: Model '{DESIRED_MODEL}' loaded successfully.")
            time.sleep(5)   # give LM Studio time to warm up
        else:
            print(f"[Selene Server]: Failed to load desired model '{DESIRED_MODEL}'.")
            # Fall back to whatever is currently loaded, if anything.
            if loaded_path:
                print(f"[Selene Server]: Using already loaded model as fallback -- {loaded_path}")
                active_path = loaded_path
            else:
                print("[Selene Server]: No model available. Chat disabled.")
                return

    if not active_path:
        print("[Selene Server]: Could not determine an active model. Chat disabled.")
        return

    selene = LLMChat(base_url=BASE_URL, model_name=active_path)
    selene.is_running = True

    # Hook up real-time WebSocket state change broadcast for knowledge manager card changes
    k_tool = selene.tool_router.tools.get("knowledge_manager")
    if k_tool:
        def handle_change():
            if main_loop and clients:
                main_loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(broadcast({"type": "knowledge_state", "data": k_tool.load_state()}))
                )
        setattr(k_tool, "on_state_change", handle_change)

    # Initialise ToolSuggestionLayer and attach to selene instance
    from selene_brain.tool_suggestion import ToolSuggestionLayer
    selene.tool_suggestion = ToolSuggestionLayer(selene)
    selene.pending_tool_confirmation = None
    print("[Selene Server]: ToolSuggestionLayer initialised.")

    # Start Selene's internal autonomy monitor in a background daemon thread.
    autonomy_thread = threading.Thread(target=selene._autonomy_monitor, daemon=True)
    autonomy_thread.start()

    print(f"[Selene Server]: Selene is online  *  model: {active_path}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # -- startup ---------------------------------------------------------------
    global main_loop
    loop = asyncio.get_event_loop()
    main_loop = loop

    # Background tasks start immediately so the WebSocket is available right away.
    # _init_selene contacts LM Studio (potentially slow) — run it in the background
    # so clients can connect while Selene warms up. selene stays None until ready;
    # the WS handler already guards every selene usage with `if selene:`.
    async def _start_selene_background():
        await loop.run_in_executor(None, _init_selene)
        print("[Selene Server]: Background init complete.")
        # Start Discord bot after Selene is ready
        if selene is not None:
            from selene_discord import start_discord_bot
            asyncio.create_task(
                start_discord_bot(
                    selene_chat=selene,
                    process_message_fn=process_message,
                    update_memory_fn=update_memory_and_energy,
                    broadcast_fn=broadcast,
                )
            )
            await broadcast({"type": "state", "data": get_state()})

    asyncio.create_task(_start_selene_background())
    asyncio.create_task(_state_broadcaster())
    asyncio.create_task(_timer_poller())
    threading.Thread(target=_gamepad_poller_thread, args=(loop,), daemon=True).start()

    yield
    # -- shutdown --------------------------------------------------------------
    from selene_discord import stop_discord_bot
    try:
        await stop_discord_bot()
    except Exception as exc:
        print(f"[Selene Server]: Error stopping Discord bot -- {exc}")

    if selene:
        selene.save_state()
        print("[Selene Server]: State saved.")

# -- App -----------------------------------------------------------------------

app = FastAPI(title="Selene OS Server", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# -- YouTube proxy endpoint ---------------------------------------------------

@app.get("/yt-proxy", response_class=HTMLResponse)
async def yt_proxy_endpoint(v: str = ""):
    """
    Serves the YouTube IFrame API wrapper.
    Why this fixes Error 152/153:
      YouTube's embed player requires a valid HTTP/HTTPS Referer.
      Electron's file:// origin sends no referer. Serving from localhost/127.0.0.1
      provides a legitimate HTTP origin.
    """
    if not v:
        return HTMLResponse(content="Missing ?v=VIDEO_ID", status_code=400)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="referrer" content="strict-origin-when-cross-origin">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100%;background:#000;overflow:hidden}}
#yt-player{{width:100%;height:100%}}
#yt-player iframe{{width:100%!important;height:100%!important;border:none!important}}
</style>
</head>
<body>
<div id="yt-player"></div>
<script>
// Load YouTube IFrame API
(function(){{
  var tag = document.createElement('script');
  tag.src = 'https://www.youtube.com/iframe_api';
  document.head.appendChild(tag);
}})();

var player, tickTimer;

function onYouTubeIframeAPIReady() {{
  player = new YT.Player('yt-player', {{
    width: '100%', height: '100%',
    videoId: '{v}',
    playerVars: {{ autoplay: 1, rel: 0, modestbranding: 1, playsinline: 1, enablejsapi: 1 }},
    events: {{
      onReady: function() {{
        window.parent.postMessage({{ event: 'onStateChange', info: -1 }}, '*');
      }},
      onStateChange: function(e) {{
        // Forward state to parent
        window.parent.postMessage({{ event: 'onStateChange', info: e.data }}, '*');
        
        clearInterval(tickTimer);
        if (e.data === 1) {{
          tickTimer = setInterval(function() {{
            if (!player || player.getPlayerState() !== 1) return;
            window.parent.postMessage({{
              event: 'onTimeTick',
              info:  player.getCurrentTime()
            }}, '*');
          }}, 2000);
        }}
      }}
    }}
  }});
}}
</script>
</body>
</html>"""
    return HTMLResponse(
        content=html,
        headers={
            "Referrer-Policy": "strict-origin-when-cross-origin",
        }
    )

# -- REST endpoints ------------------------------------------------------------

@app.get("/state")
async def state_endpoint():
    """Quick health + state check -- useful for debugging."""
    return get_state()

@app.get("/steam/image/{appid}")
async def steam_image_endpoint(appid: str):
    """Serve local Steam library cache or C:\\Games custom cover images, with a premium SVG cartridge fallback."""
    logger.info(f"[Steam Image Endpoint] Request received for AppID: {appid}")
    
    def make_svg_fallback(appid_str: str):
        from fastapi import Response
        name_text = appid_str.replace("local_", "").replace("_", " ").replace("-", " ").title()
        if len(name_text) > 20:
            name_text = name_text[:17] + "..."
            
        svg_data = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 450" width="100%" height="100%">
            <!-- Background -->
            <rect width="300" height="450" rx="15" fill="#0c0a21" stroke="#4dd9f7" stroke-width="3" />
            
            <!-- Cartridge Grips / Lines -->
            <line x1="30" y1="40" x2="270" y2="40" stroke="rgba(77,217,247,0.15)" stroke-width="4" />
            <line x1="30" y1="50" x2="270" y2="50" stroke="rgba(77,217,247,0.15)" stroke-width="4" />
            <line x1="30" y1="60" x2="270" y2="60" stroke="rgba(77,217,247,0.15)" stroke-width="4" />
            
            <!-- Inner Border / Screen style -->
            <rect x="25" y="80" width="250" height="280" rx="8" fill="#14103c" stroke="rgba(224,64,160,0.4)" stroke-width="2" />
            
            <!-- Decorative Grid -->
            <path d="M 25,180 L 275,180 M 25,280 L 275,280 M 100,80 L 100,360 M 200,80 L 200,360" stroke="rgba(77,217,247,0.06)" stroke-width="1.5" />
            
            <!-- Core Retro Chip Shape -->
            <rect x="90" y="150" width="120" height="120" rx="12" fill="rgba(45,212,191,0.08)" stroke="#2dd4bf" stroke-width="2.5" />
            <circle cx="150" cy="210" r="35" fill="none" stroke="#fbbf24" stroke-width="2" stroke-dasharray="6,4" />
            <circle cx="150" cy="210" r="10" fill="#fbbf24" />
            
            <!-- Text details -->
            <text x="150" y="325" font-family="'Share Tech Mono', 'Courier New', monospace" font-size="15" fill="#ffffff" font-weight="bold" text-anchor="middle" letter-spacing="1">
                {name_text.upper()}
            </text>
            <text x="150" y="342" font-family="'Share Tech Mono', 'Courier New', monospace" font-size="8.5" fill="#4dd9f7" text-anchor="middle" letter-spacing="2" opacity="0.8">
                SEGA SYSTEM CORE
            </text>
            
            <!-- Technical Label -->
            <rect x="40" y="390" width="220" height="36" rx="4" fill="#1e1a4a" stroke="rgba(77,217,247,0.2)" stroke-width="1" />
            <text x="150" y="412" font-family="'Share Tech Mono', 'Courier New', monospace" font-size="9" fill="#a5b4fc" text-anchor="middle" letter-spacing="4">
                * PERSISTENT MEMORY *
            </text>
        </svg>"""
        return Response(content=svg_data, media_type="image/svg+xml")

    if appid.startswith("local_"):
        folder_name = appid.replace("local_", "")
        local_path = os.path.join(r"C:\Games", folder_name)
        if os.path.exists(local_path):
            try:
                # Look for common image names in the game folder
                for filename in os.listdir(local_path):
                    if filename.lower().endswith((".jpg", ".jpeg", ".png")):
                        img_path = os.path.normpath(os.path.join(local_path, filename))
                        if os.path.isfile(img_path):
                            return FileResponse(img_path)
            except Exception as e:
                logger.error(f"Error scanning local folder images for {appid}: {e}")
        return make_svg_fallback(appid)
        
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        steam_path, _ = winreg.QueryValueEx(key, "SteamPath")
        cache_dir = os.path.normpath(os.path.join(steam_path, "appcache", "librarycache", appid))
        if os.path.exists(cache_dir):
            # 1. Prefer standard vertical/horizontal library images
            for filename in ["library_600x900.jpg", "header.jpg", "library_header.jpg", "library_hero.jpg"]:
                img_path = os.path.normpath(os.path.join(cache_dir, filename))
                if os.path.isfile(img_path):
                    return FileResponse(img_path)
            
            # 2. Fall back to any image ending with .jpg/.jpeg/.png (excluding blur)
            files = os.listdir(cache_dir)
            candidates = []
            for f in files:
                if f.endswith(("_blur.jpg", "logo.png")):
                    continue
                if f.lower().endswith((".jpg", ".jpeg", ".png")):
                    candidates.append(f)
            
            if candidates:
                candidates.sort()
                return FileResponse(os.path.normpath(os.path.join(cache_dir, candidates[0])))
            
            # If no candidates, but logo exists, use logo
            if "logo.png" in files:
                return FileResponse(os.path.normpath(os.path.join(cache_dir, "logo.png")))
            
            # Otherwise, just return any image file
            for f in files:
                if f.lower().endswith((".jpg", ".jpeg", ".png")):
                    return FileResponse(os.path.normpath(os.path.join(cache_dir, f)))
    except Exception as e:
        logger.error(f"Error fetching steam image for {appid}: {e}")
    # Return beautiful fallback cartridge image
    return make_svg_fallback(appid)


@app.get("/sounds/{filename}")
async def sounds_endpoint(filename: str):
    """Serve local UI sounds safely from the sounds directory."""
    try:
        from urllib.parse import unquote
        safe_filename = os.path.basename(unquote(filename))
        sound_path = os.path.join(os.path.dirname(__file__), "sounds", safe_filename)
        if os.path.isfile(sound_path):
            return FileResponse(sound_path)
    except Exception as e:
        logger.error(f"Error fetching sound {filename}: {e}")
    return JSONResponse(status_code=404, content={"error": "Sound not found"})


# -- OpenAI-compatible API  (for Hermes Agent and any other tool framework) ----
#
# Hermes Agent (and most agent frameworks) expect an OpenAI-compatible base URL.
# Point Hermes at http://localhost:8765 -- it will call /v1/chat/completions and
# /v1/models just like it would any OpenAI-compatible provider, but every request
# flows through Selene's full pipeline: soul.md, memory injection, tool routing.

@app.get("/v1/models")
async def list_models_openai():
    """
    Returns a minimal OpenAI-compatible model list so Hermes can confirm the
    endpoint is live and discover the model name to use in requests.
    """
    selene_instance = selene
    model_id = "selene"
    if selene_instance is not None:
        model_id = selene_instance.llm_caller.model_name
    return JSONResponse({
        "object": "list",
        "data": [{
            "id":       model_id,
            "object":   "model",
            "created":  0,
            "owned_by": "selene-os",
        }]
    })


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI-compatible chat completions endpoint for Hermes Agent and other
    tool frameworks.  Handles both streaming (stream=true) and non-streaming
    requests -- Hermes defaults to stream=true, so SSE support is required.

    Every request flows through Selene's full pipeline: soul.md injection,
    memory, and tool routing all apply exactly as in the WebSocket chat path.
    """
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse(
            {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
            status_code=400,
        )

    messages: List[Dict[str, Any]] = body.get("messages", [])
    # DEBUG -- log the first request from Hermes so we can see exactly what
    # it passes (username format, system prompt content, etc.).
    # Remove this block once confirmed.
    import pathlib as _pl
    _dbg = _pl.Path("hermes_message_debug.json")
    if not _dbg.exists():
        try:
            _dbg.write_text(__import__("json").dumps(body, indent=2, default=str))
            print(f"[Selene Debug]: Wrote first Hermes payload -> {_dbg.resolve()}")
        except Exception:
            pass
    stream: bool                   = body.get("stream", False)
    tools: Optional[List]          = body.get("tools")
    tool_choice: Any               = body.get("tool_choice", "auto")
    temperature: float             = float(body.get("temperature", 0.7))
    max_tokens: int                = int(body.get("max_tokens", 4096))

    if not messages:
        return JSONResponse(
            {"error": {"message": "messages array is empty", "type": "invalid_request_error"}},
            status_code=400,
        )

    selene_instance = selene
    if selene_instance is None:
        return JSONResponse(
            {"error": {"message": "Selene is not initialised -- server may still be loading.",
                       "type": "server_error"}},
            status_code=503,
        )

    loop = asyncio.get_event_loop()

    # -- Call LM Studio via call_with_messages ---------------------------------
    # DeepHermes handles tool calls natively -- tools pass through unchanged.
    # The XML stripper in call_with_messages remains as a safety net but won't
    # fire on proper tool_calls responses.
    try:
        assistant_message = await loop.run_in_executor(
            None,
            lambda: selene_instance.llm_caller.call_with_messages(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice=tool_choice,
            )
        )
    except Exception as exc:
        return JSONResponse(
            {"error": {"message": f"Inference error: {exc}", "type": "server_error"}},
            status_code=500,
        )

    # Selene always returns text; tool_calls will never be set with tools=None
    is_tool_call = bool(assistant_message.get("tool_calls"))
    if not is_tool_call:
        # Extract the last user message for memory persistence
        user_content = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                raw = msg.get("content", "")
                user_content = (
                    " ".join(p.get("text","") for p in raw if isinstance(p, dict))
                    if isinstance(raw, list) else raw
                )
                break
        if user_content:
            response_text = assistant_message.get("content", "")
            # Hermes manages its own per-platform conversation history, so we
            # do NOT write external turns into selene.working_memory -- that
            # would contaminate UI sessions.  Long-term extraction still runs
            # so things Ghost says via Discord inform Selene's persistent memory.
            selene_instance.maybe_extract_memory(user_content, response_text)

    model_id      = selene_instance.llm_caller.model_name
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_ts    = int(time.time())
    finish_reason = "tool_calls" if is_tool_call else "stop"

    # -- Streaming response (SSE) ----------------------------------------------
    if stream:
        async def sse_generator():
            if is_tool_call:
                # Tool-call response -- send as a single delta chunk
                chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created_ts, "model": model_id,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "role":       "assistant",
                            "content":    None,
                            "tool_calls": assistant_message["tool_calls"],
                        },
                        "finish_reason": "tool_calls",
                    }],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
            else:
                content = assistant_message.get("content", "")
                # Role chunk
                yield f"data: {json.dumps({'id':completion_id,'object':'chat.completion.chunk','created':created_ts,'model':model_id,'choices':[{'index':0,'delta':{'role':'assistant','content':''},'finish_reason':None}]})}\n\n"
                # Content chunk
                yield f"data: {json.dumps({'id':completion_id,'object':'chat.completion.chunk','created':created_ts,'model':model_id,'choices':[{'index':0,'delta':{'content':content},'finish_reason':None}]})}\n\n"
                # Stop chunk
                yield f"data: {json.dumps({'id':completion_id,'object':'chat.completion.chunk','created':created_ts,'model':model_id,'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- Non-streaming response ------------------------------------------------
    choice: Dict[str, Any] = {
        "index":         0,
        "message":       assistant_message,
        "finish_reason": finish_reason,
    }
    return JSONResponse({
        "id":      completion_id,
        "object":  "chat.completion",
        "created": created_ts,
        "model":   model_id,
        "choices": [choice],
        "usage":   {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })




# -- WebSocket endpoint --------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):  # type: ignore[reportGeneralTypeIssues]
    await websocket.accept()
    clients.add(websocket)
    print(f"[Selene Server]: UI connected  ({len(clients)} client(s))")

    # Per-session YouTube presence / dormancy state
    yt_state: dict = {
        "awaiting_ghost_reply": False,  # True after Selene speaks autonomously
        "absence_prompted":     False,  # True once the "you still there?" ping is sent
        "dormant":              False,  # Suppresses auto-reactions until Ghost replies
    }

    # Greet with current state + conversation list so the UI can render immediately
    await websocket.send_json({"type": "connected", "data": get_state()})
    if selene:
        await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})

    try:
        while True:
            data     = await websocket.receive_json()
            msg_type = data.get("type")
            loop     = asyncio.get_event_loop()

            # -- Chat message --------------------------------------------------
            if msg_type == "chat":
                import time
                t_start = time.perf_counter()

                user_input = data.get("content", "").strip()
                if not user_input:
                    continue

                # ── Pending tool confirmation intercept ──────────────────────
                # If the suggestion layer is waiting for Ghost's YES/NO reply,
                # catch it here before the presence layer runs.
                if selene and hasattr(selene, "tool_suggestion") and selene.tool_suggestion:
                    conf_result = selene.tool_suggestion.check_pending_confirmation(user_input)
                    if conf_result is not None:
                        if conf_result["action"] == "execute":
                            # Ghost confirmed — execute tool now
                            _tool_resp = await loop.run_in_executor(
                                None,
                                lambda: _execute_tool_and_respond(
                                    conf_result["tool_name"],
                                    conf_result["args"],
                                    conf_result["context"],
                                    "confirmed"
                                )
                            )
                            update_memory_and_energy(user_input, _tool_resp)
                            cleaned = clean_xml_tags(_tool_resp)
                            active_agent_name = getattr(selene, "active_agent_name", "Selene").lower()
                            await websocket.send_json({
                                "type": "response", "content": cleaned, "agent": active_agent_name
                            })
                            await websocket.send_json({"type": "state", "data": get_state()})
                            continue
                        elif conf_result["action"] == "cancel":
                            # Ghost declined — fall through to normal chat with no tool
                            pass
                        # None return means ambiguous — treat as normal message

                # Check for agent pings and automatically swap active agent profile
                target_agent = None
                if "@selene" in user_input.lower():
                    target_agent = "selene"
                elif "@sage" in user_input.lower():
                    target_agent = "sage"

                if target_agent and selene:
                    current_agent = getattr(selene, "active_agent_name", "Selene").lower()
                    if current_agent != target_agent:
                        print(f"[Selene Server]: Intercepted ping to @{target_agent}. Swapping active agent profile.")
                        await loop.run_in_executor(None, selene.swap_agent, target_agent)
                        await broadcast({"type": "state", "data": get_state()})
                        await broadcast({"type": "conversations", "data": selene.list_conversations()})
                        manifest_res = selene.tool_router.route_and_execute("manifest_manager", {"command": "get_manifest"})
                        await broadcast({"type": "manifest_data", "data": manifest_res.get("data")})

                # Strip the @agent tag from the message so the model never sees it —
                # it's a routing directive, not part of Ghost's actual words.
                if target_agent:
                    import re as _re
                    user_input = _re.sub(rf'@{target_agent}\b', '', user_input, flags=_re.IGNORECASE).strip()

                # On a clean boot there's no active conversation yet.
                # Create one now so saves and auto-naming work correctly.
                if selene and selene.active_conversation_id is None:
                    await loop.run_in_executor(None, selene.new_conversation)

                is_first_message = (
                    selene is not None and
                    len(selene.working_memory) == 0 and
                    selene.active_conversation_name == "New Conversation"
                )

                # 1. Log the user message to SQLite with status "sent"
                session_id = selene.active_conversation_id or "default"
                if selene:
                    await loop.run_in_executor(
                        None, 
                        selene.db.log_dialog, 
                        session_id, 
                        "user", 
                        user_input, 
                        "", 
                        "sent"
                    )

                # 2. Run Presence Layer to decide RESPOND / OBSERVE / IGNORE
                gating = "RESPOND"
                choice_latency = 0.0
                if selene:
                    t_choice_start = time.perf_counter()
                    choice         = await loop.run_in_executor(None, selene.run_choice_layer, user_input)
                    choice_latency = (time.perf_counter() - t_choice_start) * 1000.0
                    gating         = choice.get("gating", "RESPOND")
                    print(f"[Selene Server]: Presence Layer → {gating} ({choice_latency:.0f}ms)")
                
                # 3. Handle Gating Decisions
                if gating == "IGNORE":
                    if selene:
                        set_last_message_status(session_id, "ignored")
                    print(f"[Selene Server]: Gating IGNORE - discarding message.")
                    await websocket.send_json({"type": "read_receipt", "status": "ignored"})
                    await websocket.send_json({"type": "state", "data": get_state()})
                    continue
                    
                elif gating == "OBSERVE":
                    if selene:
                        set_last_message_status(session_id, "observed")
                    print(f"[Selene Server]: Gating OBSERVE - running silent think pass.")
                    await websocket.send_json({"type": "read_receipt", "status": "observed"})

                    # Silent observation pass — extract reasoning_content directly from
                    # the raw API response so we get Gemma's inner monologue without
                    # letting her spoken reply through. call_llm normalizes reasoning_content
                    # into a <think> block — we extract that and discard everything else.
                    _obs_thoughts = ""
                    if selene:
                        _observe_system = (
                            f"{selene.system_prompt}\n\n"
                            "[OBSERVE MODE] You are silently witnessing this moment. "
                            "Think about what you're observing — your reactions, feelings, "
                            "and internal impressions. Do NOT speak or reply aloud."
                        )
                        try:
                            _obs_resp = await loop.run_in_executor(
                                None,
                                lambda: selene.llm_caller.call_llm(
                                    input_data=user_input,
                                    system_prompt=_observe_system,
                                    history=[
                                        {k: v for k, v in m.items() if k != "ts"}
                                        for m in selene.working_memory[-6:]
                                    ],
                                    temperature=0.8,
                                    max_tokens=512,
                                )
                            )
                            import re as _re
                            # Extract <think> block — call_llm wraps reasoning_content here
                            _tm = _re.search(r'<think>([\s\S]*?)</think>', _obs_resp or "", _re.DOTALL | _re.IGNORECASE)
                            if _tm and _tm.group(1).strip():
                                _obs_thoughts = _tm.group(1).strip()
                            elif _obs_resp and not _tm:
                                # Gemma returned reasoning without tags — use the whole response
                                # but strip any content that looks like a spoken reply
                                _stripped = _re.sub(r'\n{2,}.*', '', _obs_resp, flags=_re.DOTALL).strip()
                                _obs_thoughts = _stripped or _obs_resp.strip()

                            if _obs_thoughts:
                                await websocket.send_json({
                                    "type": "thought",
                                    "step": "reasoning",
                                    "title": "Silent Observation",
                                    "content": _obs_thoughts
                                })
                        except Exception as _obs_err:
                            print(f"[Selene Server]: Observe think pass failed — {_obs_err}")

                    # Persist the observed turn
                    if selene:
                        # Append user message to working_memory (no assistant reply)
                        with selene.lock:
                            selene.working_memory.append({
                                "role": "user", "content": user_input, "ts": time.time()
                            })
                            window = selene.memory_window * 2
                            if len(selene.working_memory) > window:
                                selene.working_memory = selene.working_memory[-window:]
                        # Log assistant turn as OBSERVED in dialog_history
                        selene.db.log_dialog(
                            session_id, "assistant",
                            "[OBSERVED — no spoken reply]",
                            _obs_thoughts,
                            "observed"
                        )
                        # Still run memory extraction — observed context is valuable
                        selene.maybe_extract_memory(user_input, _obs_thoughts or "[observed]")
                        # Log observation to meta_insight so it's queryable and feeds training data
                        if _obs_thoughts:
                            try:
                                selene.db.log_meta_insight(
                                    agent=getattr(selene, "active_agent_name", "selene").lower(),
                                    category="observation",
                                    subcategory="silent_observe",
                                    input_context=user_input[:500],
                                    reasoning=_obs_thoughts[:3000],
                                    result="[no spoken reply — observe mode]",
                                    emotional_state_before={"energy": selene.creative_energy, "status": "idle"},
                                    emotional_state_after={"energy": selene.creative_energy,  "status": "idle"},
                                    confidence_score=0.9,
                                    trigger_mode="observe",
                                    session_id=session_id,
                                )
                            except Exception:
                                pass

                    await websocket.send_json({"type": "state", "data": get_state()})
                    continue
                    
                else: # RESPOND
                    if selene:
                        set_last_message_status(session_id, "read")
                    await websocket.send_json({"type": "read_receipt", "status": "read"})
                    await websocket.send_json({"type": "thinking"})

                # Register thread-safe thought callback for real-time streaming
                def handle_thought(step, title, content):
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({
                            "type": "thought",
                            "step": step,
                            "title": title,
                            "content": content
                        }),
                        loop
                    )
                if selene:
                    selene.thought_callback = handle_thought

                # Run blocking LLM call in thread pool -- catch errors here so
                # the WebSocket stays alive and the UI gets an error message.
                t_llm_start = time.perf_counter()
                try:
                    response = await loop.run_in_executor(None, process_message, user_input)
                except Exception as exc:
                    err_msg = f"[Selene Error]: LM Studio call failed -- {type(exc).__name__}: {exc}"
                    print(err_msg)
                    await websocket.send_json({"type": "response", "content": err_msg})
                    await websocket.send_json({"type": "state",    "data": get_state()})
                    if selene:
                        selene.thought_callback = None
                    continue
                llm_latency = (time.perf_counter() - t_llm_start) * 1000.0

                if selene:
                    selene.thought_callback = None

                presence_mode = extract_presence_decision(response)
                if presence_mode in ("observe", "ignore"):
                    receipt_status = "observed" if presence_mode == "observe" else "ignored"
                    if selene:
                        set_last_message_status(session_id, receipt_status)
                    total_latency = (time.perf_counter() - t_start) * 1000.0
                    await websocket.send_json({"type": "read_receipt", "status": receipt_status})
                    await websocket.send_json({
                        "type": "latency_metrics",
                        "choice_latency_ms": round(choice_latency, 2),
                        "llm_latency_ms": round(llm_latency, 2),
                        "total_latency_ms": round(total_latency, 2)
                    })
                    await websocket.send_json({"type": "state", "data": get_state()})
                    continue

                total_latency = (time.perf_counter() - t_start) * 1000.0

                # Clean non-think XML blocks from the returned prose
                cleaned_response = clean_xml_tags(response)

                active_agent_name = getattr(selene, "active_agent_name", "Selene").lower()

                if active_agent_name == "selene":
                    # ── Selene: chunked conversational delivery ───────────────
                    chunks = split_response_chunks(cleaned_response)
                    for i, chunk in enumerate(chunks):
                        if i > 0:
                            await websocket.send_json({"type": "thinking", "inter_chunk": True})
                            import random as _rand
                            base_delay = _rand.uniform(1.2, 2.8)
                            char_delay = len(chunk) * 0.008
                            delay      = min(4.5, base_delay + char_delay)
                            await asyncio.sleep(delay)
                        await websocket.send_json({
                            "type": "response",
                            "content": chunk,
                            "agent": active_agent_name
                        })
                    # Commit chunks to memory after sending — each stored separately
                    # so reloading the conversation renders the same bubble layout.
                    update_memory_and_energy(user_input, response, chunks=chunks)
                else:
                    # ── Sage: single complete structured response ─────────────
                    await websocket.send_json({
                        "type": "response",
                        "content": cleaned_response,
                        "agent": active_agent_name
                    })
                    update_memory_and_energy(user_input, response)

                # Refresh emotion cache after turn — only point where moodlets change
                if selene:
                    try:
                        _mo = selene.emotion_classifier.mood_observer
                        _dom, _int = _mo.get_dominant_mood()
                        _cached_emotion["mood_index"] = int(_int * 100)
                        _cached_emotion["emotion"]    = _dom if _dom != "neutral" else ""
                    except Exception:
                        pass

                # Auto-name the conversation from its first user message
                if is_first_message and selene and selene.active_conversation_id:
                    conv_id   = selene.active_conversation_id
                    auto_name = selene.auto_name_from_message(user_input)
                    selene.rename_conversation(conv_id, auto_name)
                    await websocket.send_json({
                        "type": "conversation_renamed",
                        "id":   conv_id,
                        "name": auto_name,
                    })

                # Persist after every turn
                if selene:
                    await loop.run_in_executor(None, selene.save_current_conversation)
                    await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})

                await websocket.send_json({
                    "type": "latency_metrics",
                    "choice_latency_ms": round(choice_latency, 2),
                    "llm_latency_ms": round(llm_latency, 2),
                    "total_latency_ms": round(total_latency, 2)
                })

                await websocket.send_json({"type": "state", "data": get_state()})

            # -- New conversation ----------------------------------------------
            elif msg_type == "new_conversation":
                if selene:
                    conv_info = await loop.run_in_executor(None, selene.new_conversation)
                    await websocket.send_json({
                        "type":     "conversation_loaded",
                        "id":       conv_info["id"],
                        "name":     conv_info["name"],
                        "messages": [],
                    })
                    await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})
                await websocket.send_json({"type": "state", "data": get_state()})

            # -- Load conversation ---------------------------------------------
            elif msg_type == "load_conversation":
                conv_id = data.get("id", "").strip()
                if selene and conv_id:
                    result = await loop.run_in_executor(None, selene.load_conversation, conv_id)
                    if result:
                        await websocket.send_json({
                            "type":     "conversation_loaded",
                            "id":       result["id"],
                            "name":     result["name"],
                            "messages": result["messages"],
                        })
                        await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})
                        await websocket.send_json({"type": "state", "data": get_state()})
                    else:
                        await websocket.send_json({"type": "error", "message": f"Conversation not found: {conv_id}"})

            # -- Rename conversation -------------------------------------------
            elif msg_type == "rename_conversation":
                conv_id  = data.get("id", "").strip()
                new_name = data.get("name", "").strip()
                if selene and conv_id and new_name:
                    ok = selene.rename_conversation(conv_id, new_name)
                    if ok:
                        await websocket.send_json({
                            "type": "conversation_renamed",
                            "id":   conv_id,
                            "name": new_name,
                        })
                        await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})

            # -- List conversations --------------------------------------------
            elif msg_type == "list_conversations":
                if selene:
                    await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})

            # -- Legacy: clear memory (kept for backward compat) ---------------
            elif msg_type == "clear_memory":
                if selene:
                    conv_info = await loop.run_in_executor(None, selene.new_conversation)
                    await websocket.send_json({
                        "type":     "conversation_loaded",
                        "id":       conv_info["id"],
                        "name":     conv_info["name"],
                        "messages": [],
                    })
                    print("[Selene Server]: Memory cleared by UI (legacy).")
                await websocket.send_json({"type": "state", "data": get_state()})

            # -- Read memory / soul files --------------------------------------
            elif msg_type == "get_memory":
                if selene:
                    import datetime as _dt
                    today = _dt.date.today().isoformat()

                    soul_path = getattr(selene, "prompt_path", getattr(selene, "SOUL_FILE", selene.SOUL_FILE))

                    # Fetch today's manifest for Selene from her DB
                    def _get_manifest(db, date_str):
                        try:
                            row = db.get_daily_manifest(date_str)
                            return row.get("summary", "") if row else ""
                        except Exception:
                            return ""

                    manifest_selene = _get_manifest(selene.db, today)

                    # Fetch Sage's manifest — open Sage's DB read-only if it exists
                    manifest_sage = ""
                    try:
                        from selene_brain.agent_memory import AgentMemoryStore as _AMS
                        _sage_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memories", "sage_memory.db")
                        if os.path.exists(_sage_db_path):
                            _sage_db = _AMS(_sage_db_path, is_readonly=True)
                            manifest_sage = _get_manifest(_sage_db, today)
                            _sage_db.close()
                    except Exception:
                        pass

                    await websocket.send_json({
                        "type": "memory_files",
                        "data": {
                            "soul":              selene._read_file_safe(soul_path),
                            "tools_context":     selene._read_file_safe(getattr(selene, "TOOLS_CONTEXT_FILE", selene.TOOLS_CONTEXT_FILE)),
                            "user_profile":      selene._read_file_safe(getattr(selene, "USER_PROFILE_FILE", os.path.join(selene.MEMORY_DIR, "user_profile.md"))),
                            "character_profile": selene._read_file_safe(getattr(selene, "CHARACTER_PROFILE_FILE", os.path.join(selene.MEMORY_DIR, "character_profile.md"))),
                            "manifest_selene":   manifest_selene or "(No manifest compiled for today yet.)",
                            "manifest_sage":     manifest_sage   or "(No manifest compiled for today yet.)",
                        }
                    })
                else:
                    await websocket.send_json({"type": "error", "message": "Selene not initialised."})

            # -- Write a memory / soul file ------------------------------------
            elif msg_type == "save_memory":
                file_key = data.get("file", "").strip()
                content  = data.get("content", "")
                if selene and file_key:
                    # "soul" writes to the active agent's prompt_path — the file
                    # _build_system_prompt actually reads. soul.md is a legacy orphan.
                    soul_path = getattr(selene, "prompt_path", getattr(selene, "SOUL_FILE", selene.SOUL_FILE))
                    _file_map = {
                        "soul":              soul_path,
                        "tools_context":     getattr(selene, "TOOLS_CONTEXT_FILE", selene.TOOLS_CONTEXT_FILE),
                        "user_profile":      getattr(selene, "USER_PROFILE_FILE", os.path.join(selene.MEMORY_DIR, "user_profile.md")),
                        "character_profile": getattr(selene, "CHARACTER_PROFILE_FILE", os.path.join(selene.MEMORY_DIR, "character_profile.md")),
                    }
                    target = _file_map.get(file_key)
                    if target:
                        try:
                            _dir = os.path.dirname(os.path.abspath(target))
                            os.makedirs(_dir, exist_ok=True)
                            with open(os.path.abspath(target), 'w', encoding='utf-8') as fh:
                                fh.write(content)
                            selene._prompt_dirty = True
                            print(f"[Selene Server]: Memory file '{file_key}' saved -> {os.path.abspath(target)}")
                            await websocket.send_json({"type": "memory_saved", "file": file_key, "ok": True})
                        except Exception as exc:
                            print(f"[Selene Server]: Memory save error for '{file_key}': {exc}")
                            await websocket.send_json({
                                "type": "memory_saved", "file": file_key,
                                "ok": False, "error": str(exc),
                            })
                    else:
                        await websocket.send_json({"type": "error", "message": f"Unknown file key: {file_key}"})

            # -- Get available models ------------------------------------------
            elif msg_type == "get_models":
                manager = LMStudioManager(base_url=BASE_URL)
                models  = await loop.run_in_executor(None, manager.list_models)
                loaded  = await loop.run_in_executor(None, manager.get_loaded_model_info)
                await websocket.send_json({
                    "type":    "models_list",
                    "models":  [m.get("path", m.get("id", "")) for m in (models or [])],
                    "current": loaded.get("path", "") if loaded else "",
                })

            # -- Gamepad Config ------------------------------------------------
            elif msg_type == "update_gamepad_config":
                global global_guide_button
                if "guide_button" in data:
                    global_guide_button = int(data["guide_button"])
                    print(f"[Gamepad] Updated guide button to {global_guide_button}")

            # -- Switch active model -------------------------------------------
            elif msg_type == "set_model":
                new_path = data.get("model", "").strip()
                if not new_path:
                    await websocket.send_json({
                        "type": "model_switch_status", "ok": False,
                        "error": "No model path given.",
                    })
                else:
                    # Ack immediately so UI can show a spinner
                    await websocket.send_json({
                        "type": "model_switch_status", "ok": None, "status": "switching"
                    })
                    try:
                        manager = LMStudioManager(base_url=BASE_URL)

                        # Skip unload/reload if the requested model is already loaded.
                        # Both Selene and Sage share the same base model (google/gemma-3n-e4b),
                        # so agent swaps never need a model reload.
                        _currently_loaded = await loop.run_in_executor(
                            None, manager.get_loaded_model_info
                        )
                        _already_loaded = (
                            _currently_loaded is not None
                            and _normalize(new_path) in _normalize(_currently_loaded.get("path", ""))
                        )
                        if _already_loaded:
                            if selene is not None:
                                selene.llm_caller.model_name = new_path
                                selene._prompt_dirty = True
                            await websocket.send_json({
                                "type": "model_switch_status", "ok": True, "model": new_path,
                                "status": "already_loaded"
                            })
                            print(f"[Selene Server]: Model '{new_path}' already loaded — skipping reload.")
                            continue

                        # Unload current model first
                        instance_id = await loop.run_in_executor(
                            None, manager.get_loaded_instance_id
                        )
                        if instance_id:
                            await loop.run_in_executor(None, manager.unload_model, instance_id)

                        ok = await loop.run_in_executor(None, manager.load_model, new_path)
                        if not ok:
                            await websocket.send_json({
                                "type": "model_switch_status", "ok": False,
                                "error": f"LM Studio failed to load '{new_path}'",
                            })
                        else:
                            # Poll until the model is actually serving — load_model returns
                            # True as soon as the HTTP request succeeds, but the model isn't
                            # ready to serve completions until LM Studio finishes loading it.
                            # Without this, the first chat turn after a switch hits a loading
                            # model and gets an error or empty response.
                            _norm = _normalize(new_path)
                            _ready = False
                            for _attempt in range(30):   # up to ~30s
                                await asyncio.sleep(1)
                                try:
                                    _loaded = await loop.run_in_executor(
                                        None, manager.get_loaded_model_info
                                    )
                                    if _loaded and _norm in _normalize(_loaded.get("path", "")):
                                        _ready = True
                                        break
                                except Exception:
                                    pass

                            if _ready:
                                selene_instance = selene
                                if selene_instance is not None:
                                    selene_instance.llm_caller.model_name = new_path
                                    # Invalidate system prompt so next turn rebuilds cleanly
                                    selene_instance._prompt_dirty = True
                                await websocket.send_json({
                                    "type": "model_switch_status", "ok": True, "model": new_path
                                })
                                print(f"[Selene Server]: Model switched to '{new_path}' and ready.")
                            else:
                                await websocket.send_json({
                                    "type": "model_switch_status", "ok": False,
                                    "error": f"Model '{new_path}' loaded but didn't become ready within 30s.",
                                })
                    except Exception as exc:
                        import httpx as _httpx
                        detail = exc.response.text if isinstance(exc, _httpx.HTTPStatusError) else str(exc)
                        await websocket.send_json({
                            "type": "model_switch_status", "ok": False, "error": detail
                        })

            # -- Delete conversation -------------------------------------------
            elif msg_type == "delete_conversation":
                conv_id = data.get("id", "").strip()
                if selene and conv_id:
                    ok = selene.delete_conversation(conv_id)
                    await websocket.send_json({"type": "conversation_deleted", "id": conv_id, "ok": ok})
                    # If the active conversation was just deleted, start a new one
                    if ok and selene.active_conversation_id is None:
                        conv_info = await loop.run_in_executor(None, selene.new_conversation)
                        await websocket.send_json({
                            "type": "conversation_loaded",
                            "id":       conv_info["id"],
                            "name":     conv_info["name"],
                            "messages": [],
                        })
                    await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})
                    await websocket.send_json({"type": "state", "data": get_state()})

            # -- Force generation (continue button) ---------------------------
            elif msg_type == "force_generate":
                # Bypass presence layer entirely — Ghost explicitly asked for a response.
                # Uses current context as-is. Attributed to whichever agent is active.
                if selene:
                    _continue_prompt = data.get("prompt", "").strip() or "Continue."
                    active_agent_name = getattr(selene, "active_agent_name", "Selene").lower()
                    session_id = selene.active_conversation_id or "default"

                    await websocket.send_json({"type": "thinking"})

                    def handle_continue_thought(step, title, content):
                        asyncio.run_coroutine_threadsafe(
                            websocket.send_json({"type": "thought", "step": step, "title": title, "content": content}),
                            loop
                        )
                    selene.thought_callback = handle_continue_thought

                    try:
                        _resp = await loop.run_in_executor(
                            None, lambda: selene.chat(_continue_prompt, disable_tools=False)
                        )
                    except Exception as _exc:
                        _resp = f"[Generation error: {_exc}]"

                    selene.thought_callback = None

                    _cleaned = clean_xml_tags(_resp)
                    if active_agent_name == "selene":
                        _chunks = split_response_chunks(_cleaned)
                        for i, chunk in enumerate(_chunks):
                            if i > 0:
                                await websocket.send_json({"type": "thinking", "inter_chunk": True})
                                import random as _r
                                await asyncio.sleep(_r.uniform(1.2, 2.5))
                            await websocket.send_json({"type": "response", "content": chunk, "agent": active_agent_name})
                        update_memory_and_energy(_continue_prompt, _resp, chunks=_chunks)
                    else:
                        await websocket.send_json({"type": "response", "content": _cleaned, "agent": active_agent_name})
                        update_memory_and_energy(_continue_prompt, _resp)

                    await websocket.send_json({"type": "state", "data": get_state()})

            # -- Manual memory extraction --------------------------------------
            elif msg_type == "force_memory_extract":
                if selene:
                    await loop.run_in_executor(None, selene.force_extract_memory)
                    await websocket.send_json({"type": "memory_extract_started"})

            # -- Rollback last turn (for reprompt) -----------------------------
            elif msg_type == "rollback_last_turn":
                # Strips the last user+assistant pair from working_memory so the
                # follow-up reprompt doesn't reuse the bad/error exchange.
                if selene:
                    selene.rollback_last_turn()
                    await websocket.send_json({"type": "rollback_ack"})

            # -- Tool phrase management ----------------------------------------
            elif msg_type == "get_tool_phrases":
                if selene:
                    phrases = selene.db.get_tool_phrases()
                    await websocket.send_json({"type": "tool_phrases", "data": phrases})

            elif msg_type == "add_tool_phrase":
                tool_name = data.get("tool_name", "").strip().lower()
                phrase    = data.get("phrase", "").strip().lower()
                if selene and tool_name and phrase:
                    ok = selene.db.add_tool_phrase(tool_name, phrase)
                    # Re-seed the suggestion layer so it picks up the new phrase
                    if ok and hasattr(selene, "tool_suggestion") and selene.tool_suggestion:
                        selene.tool_suggestion._seed_default_phrases()
                    await websocket.send_json({"type": "tool_phrase_added", "ok": ok,
                                               "tool_name": tool_name, "phrase": phrase})

            elif msg_type == "remove_tool_phrase":
                tool_name = data.get("tool_name", "").strip().lower()
                phrase    = data.get("phrase", "").strip().lower()
                if selene and tool_name and phrase:
                    ok = selene.db.remove_tool_phrase(tool_name, phrase)
                    await websocket.send_json({"type": "tool_phrase_removed", "ok": ok,
                                               "tool_name": tool_name, "phrase": phrase})

            # -- Toggle Agent swapper ------------------------------------------
            elif msg_type == "toggle_agent":
                new_agent = data.get("agent", "selene").lower()
                if selene:
                    await loop.run_in_executor(None, selene.swap_agent, new_agent)
                    await websocket.send_json({"type": "state", "data": get_state()})
                    await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})
                    print(f"[Selene Server]: Swapped active agent to '{new_agent}' via UI toggle request.")

            # -- Run Latency Pipeline Diagnostics Benchmark Test --------------
            elif msg_type == "run_latency_test":
                await websocket.send_json({"type": "latency_test_status", "status": "running"})
                
                # Step 1: Database Query Speed (SQLite get_setting baseline)
                db_duration = 0.0
                if selene:
                    t0 = time.perf_counter()
                    for _ in range(20):
                        selene.db.get_setting("ONBOARDING_COMPLETE")
                    db_duration = (time.perf_counter() - t0) * 1000.0 / 20.0
                
                # Step 2: System Prompt Compiler / Builder Speed
                prompt_duration = 0.0
                if selene:
                    t0 = time.perf_counter()
                    selene._build_system_prompt()
                    prompt_duration = (time.perf_counter() - t0) * 1000.0
                
                # Step 3: LLM Inference Roundtrip Speed (Fast Mock Call)
                llm_ok = False
                llm_error = None
                llm_duration = 0.0
                if selene:
                    t0 = time.perf_counter()
                    try:
                        await loop.run_in_executor(
                            None,
                            selene.llm_caller.call_llm,
                            "ping",
                            "Reply with only the word 'pong'.",
                            [],
                            0.0,
                            5
                        )
                        llm_duration = (time.perf_counter() - t0) * 1000.0
                        llm_ok = True
                    except Exception as e:
                        llm_error = str(e)
                        
                
                await websocket.send_json({
                    "type": "latency_test_result",
                    "ok": llm_ok,
                    "error": llm_error,
                    "db_latency_ms": round(db_duration, 2),
                    "prompt_latency_ms": round(prompt_duration, 2),
                    "llm_latency_ms": round(llm_duration, 2),
                    "total_latency_ms": round(db_duration + prompt_duration + llm_duration, 2)
                })

            # -- Save Dashboard layout configuration state ----------------------
            elif msg_type == "save_dashboard_layout":
                layout = data.get("layout")
                if selene and layout:
                    selene.dashboard_layout = layout
                    await loop.run_in_executor(None, selene.save_state)
                    await websocket.send_json({"type": "state", "data": get_state()})
                    print(f"[Selene Server]: Saved new dashboard layout state to disk: {layout}")

            # -- Compile & Push daily manifest to Notion ----------------------
            elif msg_type == "compile_and_push_manifest":
                if selene:
                    # Compile manifest
                    res = await loop.run_in_executor(None, selene.compile_daily_manifest)
                    if res.get("status") == "success":
                        # Push to Notion if notion tool is registered and not dormant
                        notion_tool = selene.tool_router.tools.get("notion")
                        notion_pushed = False
                        notion_error = None
                        if notion_tool and not notion_tool.dormant:
                            try:
                                push_res = await loop.run_in_executor(
                                    None, 
                                    notion_tool.execute, 
                                    {
                                        "command": "append_blocks",
                                        "page_id": selene.notion_page_id,
                                        "content": f"### Daily Manifest - {res['date']}\n\n{res['summary']}"
                                    }
                                )
                                if isinstance(push_res, dict) and "error" not in push_res:
                                    notion_pushed = True
                                else:
                                    # Fallback: try create_page under parent_id
                                    push_res = await loop.run_in_executor(
                                        None, 
                                        notion_tool.execute, 
                                        {
                                            "command": "create_page",
                                            "parent_id": selene.notion_page_id,
                                            "title": f"Daily Manifest - {res['date']}",
                                            "content": res['summary']
                                        }
                                    )
                                    if isinstance(push_res, dict) and "error" not in push_res:
                                        notion_pushed = True
                                    else:
                                        notion_error = push_res.get("error") if isinstance(push_res, dict) else "Failed to push page"
                            except Exception as ex:
                                notion_error = str(ex)
                        else:
                            notion_error = "Notion integration is dormant (missing NOTION_API_KEY in .env)."

                        await websocket.send_json({
                            "type": "manifest_compiled",
                            "ok": True,
                            "date": res["date"],
                            "summary": res["summary"],
                            "notion_pushed": notion_pushed,
                            "notion_error": notion_error
                        })
                    else:
                        await websocket.send_json({
                            "type": "manifest_compiled",
                            "ok": False,
                            "error": res.get("summary", "No data to compile today.")
                        })

            # -- Manual state poll ---------------------------------------------
            elif msg_type == "get_state":
                await websocket.send_json({"type": "state", "data": get_state()})

            # -- Obsidian Journal & Manifest endpoints -------------------------
            elif msg_type == "get_manifest":
                if selene:
                    res = selene.tool_router.route_and_execute("manifest_manager", {"command": "get_manifest"})
                    if res.get("status") == "success":
                        await websocket.send_json({
                            "type": "manifest_data",
                            "data": res.get("data")
                        })
                    else:
                        await websocket.send_json({"type": "error", "message": res.get("message")})
                else:
                    await websocket.send_json({"type": "error", "message": "Selene not initialised."})

            elif msg_type == "add_task":
                if selene:
                    title = data.get("title", "")
                    desc = data.get("description", "")
                    cat = data.get("category", "Feature")
                    prio = data.get("priority", "B")
                    deps = data.get("dependencies", [])
                    subs = data.get("subtasks", [])
                    res = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                        "command": "add_task",
                        "title": title,
                        "description": desc,
                        "category": cat,
                        "priority": prio,
                        "dependencies": deps,
                        "subtasks": subs
                    })
                    if res.get("status") == "success":
                        await websocket.send_json({"type": "task_added", "ok": True, "message": res.get("data")})
                        manifest_res = selene.tool_router.route_and_execute("manifest_manager", {"command": "get_manifest"})
                        for client in clients:
                            await client.send_json({"type": "manifest_data", "data": manifest_res.get("data")})
                    else:
                        await websocket.send_json({"type": "task_added", "ok": False, "error": res.get("message")})
                else:
                    await websocket.send_json({"type": "error", "message": "Selene not initialised."})

            elif msg_type == "update_task_full":
                if selene:
                    tid = data.get("id", "").strip().upper()
                    title = data.get("title", "")
                    desc = data.get("description", "")
                    cat = data.get("category", "Feature")
                    prio = data.get("priority", "B")
                    deps = data.get("dependencies", [])
                    subs = data.get("subtasks", [])
                    if tid:
                        res = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                            "command": "update_task_full",
                            "id": tid,
                            "title": title,
                            "description": desc,
                            "category": cat,
                            "priority": prio,
                            "dependencies": deps,
                            "subtasks": subs
                        })
                        manifest_res = selene.tool_router.route_and_execute("manifest_manager", {"command": "get_manifest"})
                        for client in clients:
                            await client.send_json({"type": "manifest_data", "data": manifest_res.get("data")})
                else:
                    await websocket.send_json({"type": "error", "message": "Selene not initialised."})

            elif msg_type == "toggle_task":
                if selene:
                    tid = data.get("id", "")
                    status = data.get("status")
                    res = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                        "command": "toggle_task",
                        "id": tid,
                        "status": status
                    })
                    if res.get("status") == "success":
                        await websocket.send_json({"type": "task_toggled", "ok": True, "message": res.get("data")})
                        manifest_res = selene.tool_router.route_and_execute("manifest_manager", {"command": "get_manifest"})
                        for client in clients:
                            await client.send_json({"type": "manifest_data", "data": manifest_res.get("data")})
                    else:
                        await websocket.send_json({"type": "task_toggled", "ok": False, "error": res.get("message")})
                else:
                    await websocket.send_json({"type": "error", "message": "Selene not initialised."})

            elif msg_type == "delete_task":
                if selene:
                    tid = data.get("id", "")
                    res = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                        "command": "delete_task",
                        "id": tid
                    })
                    if res.get("status") == "success":
                        await websocket.send_json({"type": "task_deleted", "ok": True, "message": res.get("data")})
                        manifest_res = selene.tool_router.route_and_execute("manifest_manager", {"command": "get_manifest"})
                        for client in clients:
                            await client.send_json({"type": "manifest_data", "data": manifest_res.get("data")})
                    else:
                        await websocket.send_json({"type": "task_deleted", "ok": False, "error": res.get("message")})
                else:
                    await websocket.send_json({"type": "error", "message": "Selene not initialised."})

            elif msg_type == "update_task":
                if selene:
                    tid  = data.get("id", "").strip().upper()
                    desc = data.get("description", "").strip()
                    if tid and desc:
                        res = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                            "command": "update_task",
                            "id": tid,
                            "description": desc,
                        })
                        manifest_res = selene.tool_router.route_and_execute("manifest_manager", {"command": "get_manifest"})
                        for client in clients:
                            await client.send_json({"type": "manifest_data", "data": manifest_res.get("data")})
                else:
                    await websocket.send_json({"type": "error", "message": "Selene not initialised."})

            elif msg_type == "reorder_tasks":
                if selene:
                    order = data.get("task_order", [])
                    res = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                        "command": "reorder_tasks",
                        "task_order": order
                    })
                    if res.get("status") == "success":
                        await websocket.send_json({"type": "tasks_reordered", "ok": True, "message": res.get("data")})
                        manifest_res = selene.tool_router.route_and_execute("manifest_manager", {"command": "get_manifest"})
                        for client in clients:
                            await client.send_json({"type": "manifest_data", "data": manifest_res.get("data")})
                    else:
                        await websocket.send_json({"type": "tasks_reordered", "ok": False, "error": res.get("message")})
                else:
                    await websocket.send_json({"type": "error", "message": "Selene not initialised."})

            elif msg_type == "update_guidelines":
                if selene:
                    content = data.get("content", "")
                    selene.tool_router.tools["manifest_manager"].update_guidelines(content)
                    await websocket.send_json({"type": "guidelines_updated", "ok": True})
                    manifest_res = selene.tool_router.route_and_execute("manifest_manager", {"command": "get_manifest"})
                    for client in clients:
                        await client.send_json({"type": "manifest_data", "data": manifest_res.get("data")})
                else:
                    await websocket.send_json({"type": "error", "message": "Selene not initialised."})

            elif msg_type == "reorganize_manifest":
                if selene:
                    prompt_text = data.get("prompt", "")
                    await websocket.send_json({"type": "thinking"})
                    explanation = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                        "command": "reorganize",
                        "prompt": prompt_text
                    })
                    if explanation.get("status") == "success":
                        await websocket.send_json({"type": "response", "content": explanation.get("data")})
                        manifest_res = selene.tool_router.route_and_execute("manifest_manager", {"command": "get_manifest"})
                        for client in clients:
                            await client.send_json({"type": "manifest_data", "data": manifest_res.get("data")})
                    else:
                        await websocket.send_json({"type": "error", "message": explanation.get("message")})
                else:
                    await websocket.send_json({"type": "error", "message": "Selene not initialised."})

            elif msg_type == "get_discord_status":
                try:
                    import selene_discord
                    client = selene_discord.discord_client
                    is_online = client is not None and client.is_ready()
                    bot_name = f"{client.user.name}#{client.user.discriminator}" if (is_online and client.user) else "Offline"
                    latency = round(client.latency * 1000) if (is_online and client.latency is not None) else 0
                    guilds_list = [g.name for g in client.guilds] if (is_online and client.guilds) else []
                    
                    await websocket.send_json({
                        "type": "discord_status",
                        "data": {
                            "online": is_online,
                            "bot_name": bot_name,
                            "latency": latency,
                            "guilds": guilds_list,
                            "allowed_channels": selene_discord.ALLOWED_CHANNELS,
                            "allowed_users": selene_discord.ALLOWED_USERS,
                            "token_exists": bool(selene_discord.DISCORD_BOT_TOKEN)
                        }
                    })
                except Exception as exc:
                    await websocket.send_json({"type": "discord_status", "data": {"online": False, "error": str(exc)}})

            elif msg_type == "check_discord_connectivity":
                try:
                    import selene_discord
                    client = selene_discord.discord_client
                    is_online = client is not None and client.is_ready()
                    bot_name = f"{client.user.name}#{client.user.discriminator}" if (is_online and client.user) else "Offline"
                    latency = round(client.latency * 1000) if (is_online and client.latency is not None) else 0
                    guilds_list = [g.name for g in client.guilds] if (is_online and client.guilds) else []
                    
                    await websocket.send_json({
                        "type": "discord_connectivity_result",
                        "ok": is_online,
                        "data": {
                            "online": is_online,
                            "bot_name": bot_name,
                            "latency": latency,
                            "guilds": guilds_list,
                            "allowed_channels": selene_discord.ALLOWED_CHANNELS,
                            "allowed_users": selene_discord.ALLOWED_USERS,
                            "token_exists": bool(selene_discord.DISCORD_BOT_TOKEN)
                        }
                    })
                except Exception as exc:
                    await websocket.send_json({"type": "discord_connectivity_result", "ok": False, "error": str(exc)})

            elif msg_type == "knowledge_get_state":
                if selene:
                    k_tool = selene.tool_router.tools.get("knowledge_manager")
                    if k_tool:
                        state = k_tool.load_state()
                        await websocket.send_json({"type": "knowledge_state", "data": state})

            elif msg_type == "knowledge_save_card":
                if selene:
                    k_tool = selene.tool_router.tools.get("knowledge_manager")
                    if k_tool:
                        title = data.get("title", "Untitled Card")
                        content = data.get("content", "")
                        card_type = data.get("card_type") or data.get("type") or "manual_note"
                        if card_type == "knowledge_save_card":
                            card_type = "manual_note"
                        source_url = data.get("source_url")
                        new_card = k_tool.add_card(title, content, card_type, source_url)
                        
                        # Broadcast fresh state to all active client streams
                        state = k_tool.load_state()
                        for client in clients:
                            await client.send_json({"type": "knowledge_state", "data": state})

            elif msg_type == "knowledge_delete_card":
                if selene:
                    k_tool = selene.tool_router.tools.get("knowledge_manager")
                    if k_tool:
                        card_id = data.get("id", "")
                        k_tool.delete_card(card_id)
                        
                        state = k_tool.load_state()
                        for client in clients:
                            await client.send_json({"type": "knowledge_state", "data": state})

            elif msg_type == "knowledge_update_card":
                if selene:
                    k_tool = selene.tool_router.tools.get("knowledge_manager")
                    if k_tool:
                        card_id  = data.get("id", "")
                        title    = data.get("title", "")
                        content  = data.get("content", "")
                        category = data.get("category", "")
                        if card_id and title:
                            k_tool.update_card(card_id, title, content, category)
                            state = k_tool.load_state()
                            for client in clients:
                                await client.send_json({"type": "knowledge_state", "data": state})

            elif msg_type == "get_steam_games":
                games = await loop.run_in_executor(None, get_steam_games_list)
                await websocket.send_json({"type": "steam_games_list", "data": games})

            elif msg_type == "launch_steam_game":
                appid = data.get("appid", "")
                if appid:
                    if appid.startswith("local_"):
                        games_list = await loop.run_in_executor(None, get_steam_games_list)
                        game_entry = next((g for g in games_list if g.get("appid") == appid), None)
                        if game_entry and game_entry.get("exe_path"):
                            try:
                                exe_path = game_entry["exe_path"]
                                logger.info(f"[Local Launcher] Launching: {exe_path}")
                                os.startfile(exe_path)
                            except Exception as e:
                                logger.error(f"[Local Launcher] Error launching local game: {e}")
                    else:
                        os.startfile(f"steam://rungameid/{appid}")

            elif msg_type == "knowledge_sync_board":
                if selene:
                    k_tool = selene.tool_router.tools.get("knowledge_manager")
                    if k_tool:
                        cards_list = data.get("cards", [])
                        k_tool.sync_board(cards_list)
                        
                        state = k_tool.load_state()
                        for client in clients:
                            await client.send_json({"type": "knowledge_state", "data": state})

            elif msg_type == "knowledge_search_web":
                if selene:
                    k_tool = selene.tool_router.tools.get("knowledge_manager")
                    if k_tool:
                        query = data.get("query", "")
                        await websocket.send_json({"type": "knowledge_searching", "query": query})
                        res_dict = await loop.run_in_executor(None, k_tool.unified_search, query)
                        results = res_dict.get("results", []) if isinstance(res_dict, dict) else []
                        await websocket.send_json({"type": "knowledge_search_results", "data": results})

            elif msg_type == "knowledge_enrich_card":
                if selene:
                    k_tool = selene.tool_router.tools.get("knowledge_manager")
                    if k_tool:
                        card_id = data.get("id", "")
                        await loop.run_in_executor(None, k_tool.enrich_card, card_id)
                        state = k_tool.load_state()
                        for client in clients:
                            await client.send_json({"type": "knowledge_state", "data": state})

            elif msg_type == "knowledge_summarize_and_save":
                # Large-text card creation: Selene summarizes input, saves summary as card
                # content, full text stored as extended_content.
                if selene:
                    k_tool = selene.tool_router.tools.get("knowledge_manager")
                    if k_tool:
                        title    = data.get("title", "Untitled Card")
                        raw_text = data.get("content", data.get("text", ""))
                        category = data.get("category", "")
                        card_type = data.get("card_type", "manual_note")
                        source_url = data.get("source_url")
                        if not raw_text.strip():
                            await websocket.send_json({"type": "knowledge_save_error", "error": "No text provided."})
                        else:
                            await websocket.send_json({"type": "knowledge_summarizing"})
                            word_count = len(raw_text.split())
                            if word_count <= 80:
                                # Short enough -- use as-is
                                summary = raw_text.strip()
                            else:
                                # Run LLM summarization
                                def _summarize():
                                    prompt = (
                                        f"Summarize the following into 2--4 concise sentences "
                                        f"suitable as a knowledge card. Capture the core idea.\n\n"
                                        f"{raw_text[:4000]}"
                                    )
                                    selene_instance = selene
                                    if selene_instance is not None:
                                        return selene_instance.llm_caller.call_llm(
                                            input_data=prompt,
                                            system_prompt="Output only the summary. No preamble.",
                                            history=[],
                                            temperature=0.3,
                                            max_tokens=200,
                                        )
                                    return raw_text[:400]
                                summary = await loop.run_in_executor(None, _summarize)
                                summary = summary.strip() or raw_text[:400]

                            new_card = k_tool.add_card(
                                title=title,
                                content=summary,
                                card_type=card_type,
                                source_url=source_url,
                                category=category,
                                extended_content=raw_text if word_count > 80 else None,
                            )
                            state = k_tool.load_state()
                            for client in clients:
                                await client.send_json({"type": "knowledge_state", "data": state})
                            await websocket.send_json({
                                "type": "knowledge_summarized",
                                "card": new_card,
                            })

            # -- Arxiv search --------------------------------------------------
            elif msg_type == "knowledge_arxiv_search":
                if selene:
                    k_tool = selene.tool_router.tools.get("knowledge_manager")
                    if k_tool:
                        query       = data.get("query", "")
                        max_results = int(data.get("max_results", 6))
                        await websocket.send_json({"type": "knowledge_searching", "query": query, "source": "arxiv"})
                        results = await loop.run_in_executor(None, k_tool.search_arxiv, query, max_results)
                        await websocket.send_json({"type": "knowledge_arxiv_results", "data": results, "query": query})

            # -- RSS / Blogwatcher ---------------------------------------------
            elif msg_type == "knowledge_rss_add":
                if selene:
                    k_tool = selene.tool_router.tools.get("knowledge_manager")
                    if k_tool:
                        name = data.get("name", "")
                        url  = data.get("url", "")
                        res  = await loop.run_in_executor(None, k_tool.rss_add, name, url)
                        await websocket.send_json({"type": "knowledge_rss_result", "data": res})

            elif msg_type == "knowledge_rss_list":
                if selene:
                    k_tool = selene.tool_router.tools.get("knowledge_manager")
                    if k_tool:
                        res = await loop.run_in_executor(None, k_tool.rss_list)
                        await websocket.send_json({"type": "knowledge_rss_list", "data": res})

            elif msg_type == "knowledge_rss_scan":
                if selene:
                    k_tool = selene.tool_router.tools.get("knowledge_manager")
                    if k_tool:
                        blog_name = data.get("blog_name")
                        await websocket.send_json({"type": "knowledge_searching", "query": "RSS feeds", "source": "rss"})
                        res = await loop.run_in_executor(None, k_tool.rss_scan, blog_name)
                        # New articles become cards on the board
                        added = []
                        for art in res:
                            if art.get("url"):
                                card = k_tool.add_card(
                                    title=art.get("title", "RSS Article"),
                                    content=f"Blog: {art.get('blog','')}\nPublished: {art.get('published','')}",
                                    card_type="rss_article",
                                    source_url=art.get("url"),
                                )
                                added.append(card)
                        state = k_tool.load_state()
                        for client in clients:
                            await client.send_json({"type": "knowledge_state", "data": state})
                        await websocket.send_json({
                            "type": "knowledge_rss_scan_result",
                            "articles_found": len(res),
                            "cards_added": len(added),
                        })

            # -- Todo / Step Tracker -------------------------------------------
            # Selene owns the plan -- the UI sends read/clear requests only.
            elif msg_type == "todo_get":
                if selene:
                    todo = selene.tool_router.tools.get("todo")
                    if todo:
                        await websocket.send_json({"type": "todo_state", "data": todo.get_plan()})

            elif msg_type == "todo_clear":
                if selene:
                    todo = selene.tool_router.tools.get("todo")
                    if todo:
                        todo.execute({"command": "clear"})
                        for client in clients:
                            await client.send_json({"type": "todo_state", "data": todo.get_plan()})

            # -- Maps ---------------------------------------------------------
            elif msg_type == "maps_query":
                if selene:
                    maps = selene.tool_router.tools.get("maps")
                    if maps:
                        await websocket.send_json({"type": "maps_thinking"})
                        tool_input = data.get("input", {})
                        res = await loop.run_in_executor(None, maps.execute, tool_input)
                        await websocket.send_json({"type": "maps_result", "data": res})
                    else:
                        await websocket.send_json({"type": "maps_result", "data": {"error": "Maps tool not loaded."}})

            # -- Polymarket ---------------------------------------------------
            elif msg_type == "polymarket_query":
                if selene:
                    pm = selene.tool_router.tools.get("polymarket")
                    if pm:
                        await websocket.send_json({"type": "polymarket_thinking"})
                        tool_input = {k: v for k, v in data.items() if k != "type"}
                        res = await loop.run_in_executor(None, pm.execute, tool_input)
                        await websocket.send_json({"type": "polymarket_result", "data": res})
                    else:
                        await websocket.send_json({"type": "polymarket_result", "data": {"error": "Polymarket tool not loaded."}})

            # -- YouTube ------------------------------------------------------
            elif msg_type == "youtube_query":
                if selene:
                    yt = selene.tool_router.tools.get("youtube")
                    if yt:
                        await websocket.send_json({"type": "youtube_thinking"})
                        tool_input = {k: v for k, v in data.items() if k != "type"}
                        res = await loop.run_in_executor(None, yt.execute, tool_input)
                        await websocket.send_json({"type": "youtube_result", "data": res})
                    else:
                        await websocket.send_json({"type": "youtube_result", "data": {"error": "YouTube tool not loaded. Run: pip install youtube-transcript-api"}})

            # -- YouTube: search (dedicated fast path) ------------------------
            elif msg_type == "youtube_search":
                query = data.get("query", "").strip()
                limit = int(data.get("limit", 8))
                sort_type = data.get("sort", "relevance")
                if not query:
                    await websocket.send_json({"type": "youtube_search_results", "results": []})
                elif selene:
                    yt = selene.tool_router.tools.get("youtube")
                    if yt:
                        await websocket.send_json({"type": "youtube_searching"})
                        try:
                            results = await loop.run_in_executor(None, yt.search_youtube, query, limit, sort_type)
                        except Exception as e:
                            logger.error(f"[YouTube Search Error]: {e}")
                            results = []
                        await websocket.send_json({"type": "youtube_search_results", "results": results})
                    else:
                        await websocket.send_json({"type": "youtube_search_results", "results": [], "error": "YouTube tool not loaded."})

            # -- YouTube: watch start -- fetch segments for co-watching ---------
            elif msg_type == "youtube_watch_start":
                video_id  = data.get("video_id", "")
                seg_dur_s = int(data.get("segment_duration_s", 60))
                if not video_id:
                    await websocket.send_json({"type": "youtube_segments", "error": "No video_id provided."})
                elif selene:
                    yt = selene.tool_router.tools.get("youtube")
                    if yt:
                        await websocket.send_json({"type": "youtube_segments_loading"})
                        try:
                            result = await loop.run_in_executor(None, yt.get_segments, video_id, seg_dur_s)
                        except Exception as e:
                            logger.error(f"[YouTube Segments Error]: {e}")
                            result = {"ok": False, "error": str(e), "segments": []}
                        await websocket.send_json({"type": "youtube_segments", "data": result})
                    else:
                        await websocket.send_json({"type": "youtube_segments", "error": "YouTube tool not loaded."})

            # -- YouTube: segment push -- Selene reacts to current video moment --
            elif msg_type == "youtube_segment_push":
                selene_instance = selene
                if selene_instance is not None:
                    video_title = data.get("video_title", "this video")
                    seg_idx     = data.get("segment_idx", 0)
                    timestamp   = data.get("timestamp_label", "??:??")
                    seg_text    = data.get("segment_text", "").strip()
                    video_id    = data.get("video_id", "")
                    watch_mode  = data.get("watch_mode", "normal").lower()

                    if watch_mode == "ignore":
                        continue

                    if seg_text:
                        # ── Absence check: Selene spoke, Ghost hasn't replied ─────────────
                        if yt_state["awaiting_ghost_reply"] and not yt_state["dormant"]:
                            if not yt_state["absence_prompted"]:
                                await websocket.send_json({
                                    "type":            "youtube_reaction",
                                    "video_id":        video_id,
                                    "segment_idx":     seg_idx,
                                    "timestamp_label": timestamp,
                                    "reaction":        "Still watching? 👀",
                                })
                                yt_state["absence_prompted"] = True
                            else:
                                yt_state["dormant"] = True
                            continue

                        if yt_state["dormant"]:
                            continue

                        # ── Raw LLM call — presence tools available via XML mechanism ─────
                        seg_prompt = (
                            f"[Co-watching: {video_title} @ {timestamp}] "
                            f"Transcript: {seg_text}"
                        )
                        _sp = seg_prompt
                        try:
                            import json as _json, re as _re
                            
                            system_prompt = selene_instance.system_prompt
                            if watch_mode == "observe":
                                system_prompt += "\nRecord your observations in a <think>...</think> block. Stay silent and do not speak or use tools."

                            raw_out = await loop.run_in_executor(
                                None,
                                lambda: selene_instance.llm_caller.call_llm(
                                    input_data=_sp,
                                    system_prompt=system_prompt,
                                    history=[],
                                    temperature=0.7,
                                    max_tokens=256,
                                )
                            )
                            
                            # Extract thoughts (reasoning) from the output if present
                            think_match = _re.search(r'<think>([\s\S]*?)</think>', raw_out or "", _re.DOTALL | _re.IGNORECASE)
                            thoughts_text = think_match.group(1).strip() if think_match else ""

                            # Parse any <tool_call name="...">...</tool_call> from response
                            reaction = ""
                            if watch_mode != "observe":
                                tc_match = _re.search(
                                    r'<tool_call\s+name=["\']?([^"\' \t>]+)["\']?\s*>(.*?)</tool_call>',
                                    raw_out or "", _re.DOTALL | _re.IGNORECASE
                                )
                                if tc_match:
                                    tc_name = tc_match.group(1).lower()
                                    tc_args_raw = tc_match.group(2).strip()
                                    if tc_name == "chat":
                                        try:
                                            args = _json.loads(tc_args_raw) if tc_args_raw else {}
                                            reaction = args.get("message", tc_args_raw).strip()
                                        except Exception:
                                            reaction = tc_args_raw
                                    elif tc_name in ("observe", "ignore"):
                                        reaction = ""  # stay silent
                                    else:
                                        # Any other tool — route normally and surface result
                                        _tn = tc_name
                                        _ta = tc_args_raw
                                        result = await loop.run_in_executor(
                                            None,
                                            lambda: selene_instance.tool_router.route_and_execute(_tn, _ta)
                                        )
                                        reaction = _format_tool_data(result.get("data", "")).strip()
                                else:
                                    # No tool call — plain text response means she's speaking
                                    cleaned = _re.sub(r'<think>.*?</think>', '', raw_out or '', flags=_re.DOTALL | _re.IGNORECASE)
                                    reaction = cleaned.strip()

                            if reaction or thoughts_text:
                                if reaction:
                                    update_memory_and_energy(_sp, reaction)
                                    selene_instance.maybe_extract_memory(_sp, reaction)
                                
                                await websocket.send_json({
                                    "type":            "youtube_reaction",
                                    "video_id":        video_id,
                                    "segment_idx":     seg_idx,
                                    "timestamp_label": timestamp,
                                    "reaction":        reaction,
                                    "thoughts":        thoughts_text if thoughts_text else None,
                                })
                                
                                if reaction:
                                    yt_state["awaiting_ghost_reply"] = True
                                    yt_state["absence_prompted"]     = False
                        except Exception as e:
                            logger.error(f"[YouTube Reaction Error]: {e}")

            # -- YouTube: in-player chat ---------------------------------------
            elif msg_type == "youtube_chat":
                selene_instance = selene
                if selene_instance is not None:
                    video_title  = data.get("video_title", "")
                    user_message = data.get("message", "").strip()
                    context_segs = data.get("context_segments", [])
                    no_video     = data.get("no_video", False)
                    if user_message:
                        # Ghost is engaging — lift dormancy / absence state
                        yt_state["awaiting_ghost_reply"] = False
                        yt_state["absence_prompted"]     = False
                        yt_state["dormant"]              = False

                        # Auto-create a conversation if none is active yet
                        if selene_instance.active_conversation_id is None:
                            await loop.run_in_executor(None, selene_instance.new_conversation)
                            await websocket.send_json({
                                "type": "conversation_loaded",
                                "id":   selene_instance.active_conversation_id,
                                "name": selene_instance.active_conversation_name,
                            })

                        # Build video context for the system prompt
                        if no_video or not video_title:
                            video_context = (
                                "\n\n-- CURRENT CONTEXT --\n"
                                "Ghost has the YouTube co-watching panel open but has NOT selected any video yet. "
                                "There is no active video playing. Do not assume what video is playing or reference "
                                "any specific content. If he is asking about a video, let him know he needs to "
                                "search for and select one first."
                                "\n\nThis is casual chat. Reply in plain conversational text only."
                            )
                        else:
                            context_text = ""
                            if context_segs:
                                seg_lines = [f"[{s.get('start_label', s.get('timestamp_label','?'))}] {s.get('text','')}" for s in context_segs[-5:]]
                                context_text = "\n".join(seg_lines)
                            video_context = (
                                f"\n\n-- CURRENT CONTEXT --\n"
                                f"You and Ghost are co-watching \"{video_title}\" right now."
                                + (f"\n\nRecent transcript:\n{context_text}" if context_text else "")
                                + "\n\nThis is casual co-watching chat. Reply in plain conversational text only."
                            )
                        chat_system = selene_instance.system_prompt + video_context

                        # ── Raw LLM call — presence tools available via XML mechanism ─────
                        try:
                            import json as _json, re as _re
                            _um = user_message
                            _cs = chat_system
                            raw_out = await loop.run_in_executor(
                                None,
                                lambda: selene_instance.llm_caller.call_llm(
                                    input_data=_um,
                                    system_prompt=_cs,
                                    history=selene_instance.working_memory,
                                    temperature=0.6,
                                    max_tokens=4096,
                                )
                            )
                            # Parse any <tool_call> from response
                            clean_response = ""
                            tc_match = _re.search(
                                r'<tool_call\s+name=["\']?([^"\' \t>]+)["\']?\s*>(.*?)</tool_call>',
                                raw_out or "", _re.DOTALL | _re.IGNORECASE
                            )
                            if tc_match:
                                tc_name = tc_match.group(1).lower()
                                tc_args_raw = tc_match.group(2).strip()
                                if tc_name == "chat":
                                    try:
                                        args = _json.loads(tc_args_raw) if tc_args_raw else {}
                                        clean_response = args.get("message", tc_args_raw).strip()
                                    except Exception:
                                        clean_response = tc_args_raw
                                elif tc_name in ("observe", "ignore"):
                                    clean_response = ""  # stay silent
                                else:
                                    # Any other tool — route and surface result
                                    _tn = tc_name
                                    _ta = tc_args_raw
                                    result = await loop.run_in_executor(
                                        None,
                                        lambda: selene_instance.tool_router.route_and_execute(_tn, _ta)
                                    )
                                    clean_response = _format_tool_data(result.get("data", "")).strip()
                            else:
                                # No tool call — plain text
                                raw = _re.sub(r'<tool_call[^>]*>.*?</tool_call>', '', raw_out or '', flags=_re.DOTALL | _re.IGNORECASE)
                                raw = _re.sub(r'<think>.*?</think>', '', raw, flags=_re.DOTALL | _re.IGNORECASE)
                                clean_response = raw.strip()

                            if clean_response:
                                with selene_instance.lock:
                                    _ts = time.time()
                                    selene_instance.working_memory.append({"role": "user",      "content": user_message,    "ts": _ts})
                                    selene_instance.working_memory.append({"role": "assistant", "content": clean_response,  "ts": _ts})
                                await loop.run_in_executor(None, selene_instance.save_current_conversation)
                            await websocket.send_json({
                                "type":    "youtube_chat_response",
                                "message": clean_response,
                            })
                        except Exception as e:
                            logger.error(f"[YouTube Chat Error]: {e}")
                            await websocket.send_json({"type": "youtube_chat_response", "message": f"(Error: {e})"})

            # -- Documents & RuneReader ---------------------------------------
            elif msg_type == "document_query":
                if selene:
                    cmd = data.get("command")
                    if cmd == "runereader_process":
                        runereader_tool = selene.tool_router.tools.get("runereader")
                        if runereader_tool:
                            await websocket.send_json({"type": "document_thinking"})
                            tool_input = {k: v for k, v in data.items() if k != "type"}
                            res = await loop.run_in_executor(None, runereader_tool.execute, tool_input)
                            await websocket.send_json({"type": "document_result", "data": res})
                            
                            if res.get("ok"):
                                user_msg = res.get("user_message", "")
                                resp_content = res.get("response", "")
                                session_id = selene.active_conversation_id or "default"
                                
                                # Log turns to dialog history DB
                                await loop.run_in_executor(None, selene.db.log_dialog, session_id, "user", user_msg, "", "read")
                                await loop.run_in_executor(None, selene.db.log_dialog, session_id, "assistant", resp_content, "[RuneReader Analysis Synthesis]", "read")
                                
                                # Update active working memory and refresh energy
                                update_memory_and_energy(user_msg, resp_content)
                                
                                # Save conversation and broadcast refreshed conversation state
                                await loop.run_in_executor(None, selene.save_current_conversation)
                                await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})
                                
                                # Send response event so UI instantly renders assistant's turn in ChatView
                                await websocket.send_json({"type": "response", "content": resp_content})
                                await websocket.send_json({"type": "state", "data": get_state()})
                        else:
                            await websocket.send_json({"type": "document_result", "data": {"error": "Rune Reader tool not loaded."}})
                    else:
                        doc_tool = selene.tool_router.tools.get("document_reader")
                        if doc_tool:
                            await websocket.send_json({"type": "document_thinking"})
                            tool_input = {k: v for k, v in data.items() if k != "type"}
                            res = await loop.run_in_executor(None, doc_tool.execute, tool_input)
                            await websocket.send_json({"type": "document_result", "data": res})
                        else:
                            await websocket.send_json({"type": "document_result", "data": {"error": "Document tool not loaded. Run: pip install pymupdf pymupdf4llm"}})

            elif msg_type == "notion_query":
                if selene:
                    notion_tool = selene.tool_router.tools.get("notion")
                    if notion_tool:
                        await websocket.send_json({"type": "notion_thinking"})
                        tool_input = {k: v for k, v in data.items() if k != "type"}
                        res = await loop.run_in_executor(None, notion_tool.execute, tool_input)
                        cmd = data.get("command", "")
                        await websocket.send_json({"type": "notion_result", "command": cmd, "data": res})
                    else:
                        await websocket.send_json({"type": "notion_result", "data": {"error": "Notion tool not loaded."}})

            elif msg_type == "get_integrations_status":
                if selene:
                    status = {
                        "google": {
                            "active": False,
                            "message": "google_client_secret.json missing at startup."
                        },
                        "hass": {
                            "active": False,
                            "url": "",
                            "entities_count": 0
                        },
                        "spotify": {
                            "active": False,
                            "message": "Spotify credentials not set in .env."
                        }
                    }
                    google_tool = selene.tool_router.tools.get("google")
                    if google_tool:
                        status["google"]["active"] = not google_tool.dormant
                        if not google_tool.dormant:
                            status["google"]["message"] = "Connected and authorized via OAuth."
                        else:
                            status["google"]["message"] = "google_client_secret.json missing at startup. OAuth setup instructions printed to logs."
                    hass_tool = selene.tool_router.tools.get("homeassistant")
                    if hass_tool:
                        status["hass"]["active"] = not hass_tool.dormant
                        status["hass"]["url"] = os.environ.get("HASS_URL", "")
                        if not hass_tool.dormant:
                            try:
                                entities = hass_tool.list_entities()
                                status["hass"]["entities_count"] = len(entities)
                            except Exception:
                                status["hass"]["entities_count"] = 14
                    spotify_tool = selene.tool_router.tools.get("spotify")
                    if spotify_tool:
                        status["spotify"]["active"] = not spotify_tool.dormant
                        if not spotify_tool.dormant:
                            status["spotify"]["message"] = "Connected to Spotify Web API."
                        else:
                            status["spotify"]["message"] = "SPOTIFY_CLIENT_ID missing in .env config. Stub dormant."
                    await websocket.send_json({"type": "integrations_status", "data": status})


            # -- Infinite Story Engine ----------------------------------------
            elif msg_type == "story_get_profiles":
                from tools.story_engine.db_helper import get_db_connection
                conn = get_db_connection()
                try:
                    rows = conn.execute("SELECT * FROM profiles ORDER BY created_at ASC").fetchall()
                    profiles = [dict(r) for r in rows]
                    await websocket.send_json({"type": "story_profiles", "profiles": profiles})
                finally:
                    conn.close()

            elif msg_type == "story_add_profile":
                from tools.story_engine.db_helper import get_db_connection
                profile_name = data.get("name", "").strip()
                profile_type = data.get("profile_type", "human").strip()
                if profile_name:
                    conn = get_db_connection()
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO profiles (name, profile_type, created_at) VALUES (?, ?, ?)",
                            (profile_name, profile_type, time.time())
                        )
                        conn.commit()
                        rows = conn.execute("SELECT * FROM profiles ORDER BY created_at ASC").fetchall()
                        profiles = [dict(r) for r in rows]
                        await websocket.send_json({"type": "story_profiles", "profiles": profiles})
                    except Exception as e:
                        await websocket.send_json({"type": "error", "message": f"Failed to add profile: {e}"})
                    finally:
                        conn.close()

            elif msg_type == "story_get_presets":
                from tools.story_engine.db_helper import get_db_connection
                conn = get_db_connection()
                try:
                    rows = conn.execute("SELECT * FROM presets ORDER BY created_at ASC").fetchall()
                    presets = [dict(r) for r in rows]
                    await websocket.send_json({"type": "story_presets", "presets": presets})
                finally:
                    conn.close()

            elif msg_type == "story_save_preset":
                import random
                from tools.story_engine.db_helper import get_db_connection
                preset_name = data.get("name", "").strip()
                preset_type = data.get("preset_type", "character").strip()
                data_json = json.dumps(data.get("data_json", {}))
                if preset_name:
                    conn = get_db_connection()
                    try:
                        preset_id = f"preset_{int(time.time())}_{random.randint(100, 999)}"
                        conn.execute(
                            "INSERT INTO presets (id, preset_type, name, data_json, created_at) VALUES (?, ?, ?, ?, ?)",
                            (preset_id, preset_type, preset_name, data_json, time.time())
                        )
                        conn.commit()
                        await websocket.send_json({"type": "story_preset_saved", "status": "success", "id": preset_id})
                    except Exception as e:
                        await websocket.send_json({"type": "error", "message": f"Failed to save preset: {e}"})
                    finally:
                        conn.close()

            elif msg_type == "story_get_characters":
                from tools.story_engine.db_helper import get_db_connection
                profile_name = data.get("profile_name", "").strip()
                if profile_name:
                    conn = get_db_connection()
                    try:
                        rows = conn.execute("SELECT * FROM characters WHERE profile_name = ? AND is_active = 1", (profile_name,)).fetchall()
                        chars = []
                        for r in rows:
                            c = dict(r)
                            c_id = c["id"]
                            cards = conn.execute("SELECT * FROM cards WHERE character_id = ? AND is_active = 1", (c_id,)).fetchall()
                            c["cards"] = [dict(cd) for cd in cards]
                            chars.append(c)
                        await websocket.send_json({"type": "story_characters", "profile_name": profile_name, "characters": chars})
                    finally:
                        conn.close()

            elif msg_type == "story_create_character":
                from selene_brain.story_engine import InfiniteStoryEngine
                profile_name = data.get("profile_name", "").strip()
                char_name = data.get("name", "").strip()
                char_class = data.get("char_class", "Adventurer").strip()
                level_label = data.get("level_label", "Level").strip()
                points_label = data.get("points_label", "Points").strip()
                stats = data.get("stats", {})
                gear_desc = data.get("gear_description", "").strip()
                
                engine = InfiniteStoryEngine()
                try:
                    char_info = engine.create_character(
                        profile_name, char_name, stats, gear_desc,
                        char_class=char_class,
                        level_label=level_label,
                        points_label=points_label
                    )
                    await websocket.send_json({"type": "story_character_created", "status": "success", "character": char_info})
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": f"Failed to create character: {e}"})

            elif msg_type == "story_generate_random_character":
                prompt = data.get("prompt", "").strip()
                
                # If prompt is empty, select a random genre to give LM Studio context
                if not prompt:
                    genres = [
                        "dark fantasy grimdark",
                        "cyberpunk high-tech low-life",
                        "space opera futuristic explorer",
                        "eldritch steampunk occult scholar",
                        "post-apocalyptic scavenger survivor",
                        "modern supernatural investigator"
                    ]
                    chosen_genre = random.choice(genres)
                    generation_prompt = f'You are an expert character creator for the Infinity Sim tabletop RPG. Generate a random fully structured character for the genre: "{chosen_genre}".'
                else:
                    generation_prompt = f'You are an expert character creator for the Infinity Sim tabletop RPG. The player has provided this concept prompt: "{prompt}". Based on this prompt, generate a fully structured character.'

                generation_prompt += """
                Return ONLY a valid JSON object matching this schema. Do not include markdown code block tags or additional text, just the raw JSON:
                {
                    "name": "a fitting name based on the prompt/genre",
                    "char_class": "a creative custom character class name",
                    "level_label": "Level",
                    "points_label": "Points",
                    "stats": {
                        "stat_1_name": "Strength (or custom renamed stat matching prompt)",
                        "stat_1_val": 10,
                        "stat_2_name": "Dexterity (or custom renamed stat matching prompt)",
                        "stat_2_val": 10,
                        "stat_3_name": "Constitution (or custom renamed stat matching prompt)",
                        "stat_3_val": 10,
                        "stat_4_name": "Intelligence (or custom renamed stat matching prompt)",
                        "stat_4_val": 10,
                        "stat_5_name": "Wisdom (or custom renamed stat matching prompt)",
                        "stat_5_val": 10
                    },
                    "gear_description": "Starting gear weapons armor and skill details",
                    "profile_flavor": "1-2 sentences of thematic background flavor"
                }

                RULES FOR STATS:
                1. You have exactly 10 extra attribute points to distribute across the 5 stats.
                2. The base for each stat is 10.
                3. The sum of all "stat_X_val" values MUST be exactly 60 (since 5 stats starting at 10 sum to 50, plus 10 extra points).
                4. Do not make any stat less than 8 or greater than 16.
                5. Stat names can be standard (Strength, Dexterity, Constitution, Intelligence, Wisdom) or creative matching the prompt genre (e.g. Cybernetics, Biomarkers, Biotech, Biotech, Biotech).
                """

                try:
                    raw_res = await loop.run_in_executor(None, selene.llm_caller.call_llm, generation_prompt)
                    clean_json = raw_res.strip()
                    
                    # Clean out markdown code blocks if any
                    if clean_json.startswith("```"):
                        lines = clean_json.split("\n")
                        if lines[0].startswith("```"):
                            lines = lines[1:]
                        if lines and lines[-1].strip() == "```":
                            lines = lines[:-1]
                        clean_json = "\n".join(lines).strip()
                    
                    # Exact curly braces regex match to ensure no trailing junk
                    start_idx = clean_json.find("{")
                    end_idx = clean_json.rfind("}")
                    if start_idx != -1 and end_idx != -1:
                        clean_json = clean_json[start_idx:end_idx+1]
                    
                    parsed_json = json.loads(clean_json)
                    await websocket.send_json({
                        "type": "story_random_character_generated",
                        "status": "success",
                        "character": parsed_json
                    })
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": f"Failed to generate random character: {e}"})

            elif msg_type == "story_start_campaign":
                from tools.story_engine.db_helper import get_db_connection
                world_name = data.get("world_name", "Forgotten Realm").strip()
                origin_details = data.get("origin_details", "").strip()
                long_term = data.get("long_term_elements", "").strip()
                world_details = data.get("world_details", "").strip()
                ambient_elements = data.get("ambient_elements", "").strip()
                chronological_milestones = data.get("chronological_milestones", "").strip()
                major_goal = data.get("major_goal", "Clear the threat").strip()
                roadmap_json = json.dumps(data.get("roadmap", []))
                character_ids = data.get("character_ids", [])
                
                conn = get_db_connection()
                try:
                    levels = []
                    for c_id in character_ids:
                        row = conn.execute("SELECT level FROM characters WHERE id = ?", (c_id,)).fetchone()
                        if row:
                            levels.append(row[0])
                    
                    avg_level = sum(levels) // len(levels) if levels else 1
                    world_level = min(10, max(1, data.get("world_level", avg_level)))
                    
                    world_id = f"world_{int(time.time())}"
                    now = time.time()
                    conn.execute("""
                    INSERT INTO worlds (
                        id, name, world_level, origin_details, long_term_elements,
                        world_details, ambient_elements, chronological_milestones,
                        major_goal, roadmap_json, created_at, last_saved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        world_id, world_name, world_level, origin_details, long_term,
                        world_details, ambient_elements, chronological_milestones,
                        major_goal, roadmap_json, now, now
                    ))
                    
                    for c_id in character_ids:
                        conn.execute("UPDATE characters SET world_id = ? WHERE id = ?", (world_id, c_id))
                    
                    # Create starting location
                    loc_id = f"loc_{int(time.time())}"
                    conn.execute("""
                    INSERT INTO locations (id, world_id, name, description, is_hub, is_explored)
                    VALUES (?, ?, 'Origin Outpost', 'The gateway where your saga begins.', 1, 1)
                    """, (loc_id, world_id))

                    # Generate starting narration using LLM
                    intro_prompt = f"""
                    You are the atmospheric Dungeon Master for the Infinity Sim tabletop RPG.
                    Generate a rich, immersive, and highly atmospheric starting introduction scenario for this new campaign.
                    
                    WORLD DETAILS:
                    Name: {world_name}
                    Difficulty Level: {world_level}
                    Campaign Goal: {major_goal}
                    Lore Details: {world_details}
                    Starting Origin: {origin_details}
                    Ambient Elements (Environmental factors): {ambient_elements}
                    Chronological Milestones (Story roadmap): {chronological_milestones}
                    
                    PARTY MEMBERS:
                    """
                    for c_id in character_ids:
                        c_row = conn.execute("SELECT name, char_class, profile_flavor FROM characters WHERE id = ?", (c_id,)).fetchone()
                        if c_row:
                            intro_prompt += f"\n- {c_row[0]} ({c_row[1]}): {c_row[2]}"
                            
                    intro_prompt += "\n\nWrite a 2-3 paragraph introduction set in the Starting Origin, establishing the theme and launching the saga. End by welcoming the characters."
                    
                    intro_narration = await loop.run_in_executor(None, selene.llm_caller.call_llm, intro_prompt)
                    
                    # Insert intro into log
                    cursor = conn.cursor()
                    cursor.execute("""
                    INSERT INTO manifest_log (world_id, turn_number, speaker, action_type, content, timestamp)
                    VALUES (?, 1, 'DM', 'speak', ?, ?)
                    """, (world_id, intro_narration, time.time()))
                    
                    conn.commit()
                    
                    await websocket.send_json({
                        "type": "story_campaign_started",
                        "world_id": world_id,
                        "world_level": world_level,
                        "major_goal": major_goal,
                        "starting_location": "Origin Outpost",
                        "intro_narration": intro_narration
                    })
                except Exception as e:
                    conn.rollback()
                    await websocket.send_json({"type": "error", "message": f"Failed to start campaign: {e}"})
                finally:
                    conn.close()

            elif msg_type == "story_player_action":
                from tools.story_engine.db_helper import get_db_connection
                char_id = data.get("character_id")
                action_type = data.get("action_type")
                content = data.get("content", "").strip()
                stat_used = data.get("stat_used", "").strip()
                opponent_level = int(data.get("opponent_level", 1))
                difficulty_penalty = int(data.get("difficulty_penalty", 0))
                
                from selene_brain.story_engine import InfiniteStoryEngine
                engine = InfiniteStoryEngine()
                
                roll_res = {}
                if action_type == "act":
                    roll_res = engine.resolve_dice_action(char_id, stat_used, opponent_level, difficulty_penalty)
                
                char = engine.get_character(char_id)
                world_id = char.get("world_id")
                
                conn = get_db_connection()
                cursor = conn.cursor()
                try:
                    row = conn.execute("SELECT MAX(turn_number) FROM manifest_log WHERE world_id = ?", (world_id,)).fetchone()
                    turn_number = (row[0] or 0) + 1
                    
                    roll_details_str = json.dumps(roll_res) if roll_res else None
                    cursor.execute("""
                    INSERT INTO manifest_log (world_id, turn_number, speaker, action_type, content, dice_roll_details, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (world_id, turn_number, char["name"], action_type, content, roll_details_str, time.time()))
                    conn.commit()

                    # Fetch world context
                    world_row = conn.execute("SELECT * FROM worlds WHERE id = ?", (world_id,)).fetchone()
                    world = dict(world_row) if world_row else {}
                    
                    # Fetch recent saga history (last 10 turns)
                    history_rows = conn.execute(
                        "SELECT speaker, content FROM manifest_log WHERE world_id = ? ORDER BY turn_number DESC LIMIT 10",
                        (world_id,)
                    ).fetchall()
                    history = list(reversed(history_rows))
                finally:
                    conn.close()
                    
                await websocket.send_json({"type": "story_thinking"})
                
                history_text = "\n".join([f"{h['speaker']}: {h['content']}" for h in history])

                dm_prompt = f"""
                You are the atmospheric Dungeon Master for the Infinity Sim tabletop RPG.
                You must narrate the next scene based on the established world lore, milestones, history, and player action.
                
                WORLD CONTEXT:
                World Name: {world.get("name", "Forgotten Realm")}
                World Level: {world.get("world_level", 1)}
                Major Campaign Goal: {world.get("major_goal", "Clear the world")}
                World/Story Lore Details: {world.get("world_details", "None provided.")}
                Starting Origin: {world.get("origin_details", "None provided.")}
                Ambient Elements (Environmental factors): {world.get("ambient_elements", "None provided.")}
                Chronological Milestones (Story roadmap): {world.get("chronological_milestones", "None provided.")}
                
                ACTIVE PARTY:
                Character: {char["name"]} ({char.get("char_class", "Adventurer")})
                {char.get("level_label", "Level")}: {char["level"]}
                HP: {char["current_hp"]}/{char["max_hp"]} | MP: {char["current_mp"]}/{char["max_mp"]}
                Capabilities & Background: {char["profile_flavor"]}
                
                RECENT SAGA TIMELINE HISTORY:
                {history_text}
                
                CURRENT PLAYER TURN ACTION:
                Speaker: {char["name"]}
                Action Type: {action_type.upper()}
                Content: {content}
                """
                if roll_res:
                    dm_prompt += f"""
                    DICE CHECK RESOLVED (D20):
                    Base Roll: {roll_res["base_roll"]}
                    Stat Used: {roll_res["stat_used"]} (Bonus: {roll_res["stat_bonus"]})
                    Final Modified Roll: {roll_res["final_roll"]}
                    World Floor Required: {roll_res["world_floor"]}
                    Roll Result: {"SUCCESS" if roll_res["success"] else "FAILURE"}
                    Combat Damage Dealt: {roll_res["damage_dealt"]}
                    """
                dm_prompt += "\nNarrate the DM outcome with high atmosphere. Keep it concise (1-3 paragraphs) to avoid flooding the chat."
                
                try:
                    response = await loop.run_in_executor(None, selene.llm_caller.call_llm, dm_prompt)
                    conn = get_db_connection()
                    try:
                        conn.execute("""
                        INSERT INTO manifest_log (world_id, turn_number, speaker, action_type, content, timestamp)
                        VALUES (?, ?, 'DM', 'speak', ?, ?)
                        """, (world_id, turn_number + 1, response, time.time()))
                        conn.commit()
                        
                        # --- Auto-compaction & Notion Archiving Trigger ---
                        count_row = conn.execute("SELECT COUNT(*) FROM manifest_log WHERE world_id = ?", (world_id,)).fetchone()
                        if count_row and count_row[0] >= 50:
                            print(f"[Story Compactor]: Timeline has reached {count_row[0]} turns. Compacting and archiving...")
                            turns_rows = conn.execute("SELECT speaker, content FROM manifest_log WHERE world_id = ? ORDER BY turn_number ASC", (world_id,)).fetchall()
                            timeline_text = "\n".join([f"{t['speaker']}: {t['content']}" for t in turns_rows])
                            
                            compaction_prompt = f"""
                            Summarize this roleplaying campaign timeline into a highly dense, compact world state summary.
                            Include active events, explored locations, known NPCs, relationship statuses, and major character gear/achievements.
                            
                            TIMELINE HISTORY:
                            {timeline_text}
                            
                            Return only the compact summary, keeping it highly readable for the DM.
                            """
                            compact_summary = await loop.run_in_executor(None, selene.llm_caller.call_llm, compaction_prompt)
                            
                            # Archive to Notion if available, otherwise fallback locally
                            archived = False
                            notion_tool = selene.tool_router.tools.get("notion_manager")
                            if notion_tool and not notion_tool.dormant:
                                try:
                                    await loop.run_in_executor(None, lambda: notion_tool.execute({
                                        "command": "create_page",
                                        "title": f"Infinity Sim Archive - World {world_id}",
                                        "content": timeline_text
                                    }))
                                    archived = True
                                    print("[Story Compactor]: Archive successfully synced to Notion workspace.")
                                except Exception as ne:
                                    logger.error(f"[Story Compactor] Notion sync failed: {ne}")
                            
                            if not archived:
                                from tools.story_engine.db_helper import STORY_ENGINE_DIR
                                archive_path = os.path.join(STORY_ENGINE_DIR, f"archived_timeline_{world_id}_{int(time.time())}.txt")
                                os.makedirs(STORY_ENGINE_DIR, exist_ok=True)
                                with open(archive_path, 'w', encoding='utf-8') as af:
                                    af.write(timeline_text)
                                print(f"[Story Compactor]: Timeline archived locally to {archive_path}")
                                
                            # Clear history log and set compact summary as turn 1
                            conn.execute("DELETE FROM manifest_log WHERE world_id = ?", (world_id,))
                            conn.execute("""
                            INSERT INTO manifest_log (world_id, turn_number, speaker, action_type, content, timestamp)
                            VALUES (?, 1, 'DM_Archive_Summary', 'observe', ?, ?)
                            """, (world_id, compact_summary, time.time()))
                            conn.commit()
                            print("[Story Compactor]: History log compacted.")
                    finally:
                        conn.close()
                        
                    await websocket.send_json({
                        "type": "story_turn_resolved",
                        "roll_result": roll_res,
                        "dm_narration": response
                    })
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": f"DM call failed: {e}"})

            elif msg_type == "story_merchant_inventory":
                location_name = data.get("location_name", "Origin Outpost")
                world_level = int(data.get("world_level", 1))
                from selene_brain.story_engine import InfiniteStoryEngine
                engine = InfiniteStoryEngine()
                inv = engine.generate_merchant_shop(location_name, world_level)
                await websocket.send_json({"type": "story_merchant_items", "inventory": inv})

            elif msg_type == "story_buy_item":
                import random
                from tools.story_engine.db_helper import get_db_connection
                char_id = data.get("character_id")
                item_name = data.get("item_name")
                item_desc = data.get("item_description")
                price = int(data.get("price", 0))
                
                conn = get_db_connection()
                cursor = conn.cursor()
                try:
                    cursor.execute("SELECT points FROM characters WHERE id = ?", (char_id,))
                    row = cursor.fetchone()
                    if row and row[0] >= price:
                        new_pts = row[0] - price
                        cursor.execute("UPDATE characters SET points = ? WHERE id = ?", (new_pts, char_id))
                        card_id = f"card_{int(time.time())}_{random.randint(100, 999)}"
                        cursor.execute(
                            "INSERT INTO cards (id, character_id, card_type, name, description, is_active) VALUES (?, ?, 'gear', ?, ?, 1)",
                            (card_id, char_id, item_name, item_desc)
                        )
                        conn.commit()
                        await websocket.send_json({"type": "story_purchase_complete", "status": "success", "points_remaining": new_pts})
                    else:
                        await websocket.send_json({"type": "error", "message": "Insufficient points for purchase."})
                except Exception as e:
                    conn.rollback()
                    await websocket.send_json({"type": "error", "message": f"Purchase failed: {e}"})
                finally:
                    conn.close()

            elif msg_type == "story_level_up":
                from selene_brain.story_engine import InfiniteStoryEngine
                char_id = data.get("character_id")
                stat_to_boost = data.get("stat_to_boost")
                engine = InfiniteStoryEngine()
                try:
                    res = engine.spend_points_level_up(char_id, stat_to_boost)
                    await websocket.send_json({"type": "story_levelled_up", "data": res})
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": f"Level up failed: {e}"})

            elif msg_type == "story_regenerate_dm":
                from tools.story_engine.db_helper import get_db_connection
                world_id = data.get("world_id")
                hint = data.get("hint", "").strip()
                
                conn = get_db_connection()
                try:
                    rows = conn.execute("SELECT id, content FROM manifest_log WHERE world_id = ? ORDER BY id DESC LIMIT 2", (world_id,)).fetchall()
                    if len(rows) >= 2:
                        player_turn = dict(rows[1])
                        dm_turn = dict(rows[0])
                        
                        dm_prompt = f"""
                        Re-narrate the Dungeon Master outcome using this new inspiration/hint: "{hint}".
                        Previous player choice: "{player_turn['content']}"
                        Narrate with high tabletop atmosphere. Keep it concise.
                        """
                        await websocket.send_json({"type": "story_thinking"})
                        response = await loop.run_in_executor(None, selene.llm_caller.call_llm, dm_prompt)
                        
                        conn.execute("UPDATE manifest_log SET content = ? WHERE id = ?", (response, dm_turn["id"]))
                        conn.commit()
                        
                        await websocket.send_json({
                            "type": "story_turn_resolved",
                            "dm_narration": response
                        })
                    else:
                        await websocket.send_json({"type": "error", "message": "Cannot find previous narrative turn to regenerate."})
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": f"Regeneration failed: {e}"})
                finally:
                    conn.close()

            elif msg_type == "story_save_game":
                from tools.story_engine.db_helper import get_db_connection
                conn = get_db_connection()
                try:
                    # Get active world
                    world_row = conn.execute("SELECT id FROM worlds ORDER BY last_saved_at DESC LIMIT 1").fetchone()
                    if world_row:
                        conn.execute("UPDATE worlds SET last_saved_at = ? WHERE id = ?", (time.time(), world_row["id"]))
                        conn.commit()
                    await websocket.send_json({"type": "story_game_saved", "status": "success"})
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": f"Save failed: {e}"})
                finally:
                    conn.close()

            elif msg_type == "story_resume_campaign":
                from tools.story_engine.db_helper import get_db_connection
                conn = get_db_connection()
                try:
                    # Find most recent world
                    world_row = conn.execute("SELECT * FROM worlds ORDER BY last_saved_at DESC LIMIT 1").fetchone()
                    if world_row:
                        world_id = world_row["id"]
                        
                        # Load characters
                        char_rows = conn.execute("SELECT * FROM characters WHERE world_id = ?", (world_id,)).fetchall()
                        characters = [dict(c) for c in char_rows]
                        
                        # Load log
                        log_rows = conn.execute("SELECT * FROM manifest_log WHERE world_id = ? ORDER BY turn_number ASC", (world_id,)).fetchall()
                        timeline = []
                        for row in log_rows:
                            timeline.append({
                                "speaker": row["speaker"],
                                "content": row["content"],
                                "roll": json.loads(row["dice_roll_details"]) if row["dice_roll_details"] else None
                            })
                        
                        # Load locations
                        loc_rows = conn.execute("SELECT * FROM locations WHERE world_id = ?", (world_id,)).fetchall()
                        locations_list = [dict(l) for l in loc_rows]
                        
                        # Active location
                        active_loc = "Origin Outpost"
                        for l in locations_list:
                            if l.get("is_hub"):
                                active_loc = l["name"]
                                break
                        
                        # Send campaign state
                        await websocket.send_json({
                            "type": "story_campaign_loaded",
                            "status": "success",
                            "world_id": world_id,
                            "world_name": world_row["name"],
                            "world_level": world_row["world_level"],
                            "major_goal": world_row["major_goal"],
                            "origin_details": world_row["origin_details"],
                            "long_term_elements": world_row["long_term_elements"],
                            "world_details": world_row["world_details"],
                            "ambient_elements": world_row["ambient_elements"],
                            "chronological_milestones": world_row["chronological_milestones"],
                            "characters": characters,
                            "timeline": timeline,
                            "locations": locations_list,
                            "active_location": active_loc
                        })
                    else:
                        await websocket.send_json({
                            "type": "story_campaign_loaded",
                            "status": "error",
                            "message": "No active campaign found."
                        })
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": f"Resume failed: {e}"})
                finally:
                    conn.close()


            # -- MetaInsight --------------------------------------------------
            elif msg_type == "meta_insight_query":
                if selene:
                    mi_tool = selene.tool_router.tools.get("meta_insight")
                    if mi_tool:
                        args = {k: v for k, v in data.items() if k != "type"}
                        result = await loop.run_in_executor(None, mi_tool.execute, args)
                        await websocket.send_json({"type": "meta_insight_result", "data": result})
                    else:
                        await websocket.send_json({"type": "meta_insight_result", "data": {"status": "error", "message": "MetaInsight tool not loaded."}})
                else:
                    await websocket.send_json({"type": "meta_insight_result", "data": {"status": "error", "message": "Selene not initialised."}})

            elif msg_type == "meta_insight_promote_card":
                if selene:
                    entry_id  = data.get("entry_id")
                    card_data = data.get("card")   # {title, content, category} for knowledge board
                    if entry_id:
                        selene.db.mark_meta_insight_promoted(int(entry_id))
                    if card_data:
                        k_tool = selene.tool_router.tools.get("knowledge_manager")
                        if k_tool:
                            new_card = k_tool.add_card(
                                title=card_data.get("title", "MetaInsight Pattern"),
                                content=card_data.get("content", ""),
                                card_type="meta_insight",
                                source_url=None,
                                category=card_data.get("category", "INSIGHT"),
                            )
                            state = k_tool.load_state()
                            for client in clients:
                                await client.send_json({"type": "knowledge_state", "data": state})
                    await websocket.send_json({"type": "meta_insight_promoted", "ok": True})

            # -- Catch-all ----------------------------------------------------
            else:
                await websocket.send_json({
                    "type":    "error",
                    "message": f"Unknown message type: {msg_type}",
                })

    except WebSocketDisconnect:
        clients.discard(websocket)
        print(f"[Selene Server]: UI disconnected  ({len(clients)} client(s))")
    except Exception as exc:
        clients.discard(websocket)
        print(f"[Selene Server]: Client error — {exc}")


# -- Entry point --------------------------------------------------------------

if __name__ == "__main__":
    print("+" + "-"*38 + "+")
    print("|   S E L E N E   O S   S E R V E R    |")
    addr_str = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws"
    padded_addr = addr_str.center(36)
    print(f"| {padded_addr} |")
    print("+" + "-"*38 + "+")
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")
