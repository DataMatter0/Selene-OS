"""
selene_server.py — WebSocket + REST API bridge for Selene OS UI
────────────────────────────────────────────────────────────────
Run:   python selene_server.py
Then:  npm start  (in this same folder, to launch Electron)
       — or open renderer/index.html in a browser for quick testing.

WebSocket protocol  ws://localhost:8765/ws
  Client → Server:
    {"type": "chat",         "content": "user message"}
    {"type": "clear_memory"}
    {"type": "get_state"}

  Server → Client:
    {"type": "connected",     "data": <state>}
    {"type": "thinking"}
    {"type": "response",      "content": "selene reply"}
    {"type": "state",         "data": <state>}
    {"type": "autonomy_start"}
    {"type": "autonomy_end"}
    {"type": "error",         "message": "..."}
"""

import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional, Set

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from llm_chat import LLMChat
from lm_studio_manager import LMStudioManager

load_dotenv()   # reads .env if present; silently no-ops if not

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL      = os.environ.get("LM_STUDIO_URL",   "http://localhost:1234")
DESIRED_MODEL = os.environ.get("LM_STUDIO_MODEL",  "nvidia/nemotron-3-nano-4b")
SERVER_HOST        = "localhost"
SERVER_PORT        = 8765

# ── Globals ───────────────────────────────────────────────────────────────────

selene: Optional[LLMChat] = None
# Track connected WebSocket clients
clients: Set[WebSocket] = set()
_prev_writing: bool = False   # tracks last broadcast state

# ── Utilities ─────────────────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    """Case/separator-insensitive model name comparison."""
    return name.lower().replace(" ", "").replace("-", "").replace("_", "").replace(".", "").replace("/", "")


def get_state() -> dict:
    """JSON-serialisable snapshot of Selene's live state."""
    if selene is None:
        return {
            "status":          "offline",
            "creative_energy": 0,
            "memory_count":    0,
            "is_running":      False,
        }
    with selene.lock:
        return {
            "status":          "writing" if selene.is_writing_autonomously else "idle",
            "creative_energy": selene.creative_energy,
            "memory_count":    len(selene.working_memory) // 2,
            "is_running":      selene.is_running,
        }


def process_message(user_input: str) -> str:
    """
    Mirrors the get_final_response() logic inside LLMChat.start_loop().
    Routes through tools first; falls back to direct chat.
    """
    if selene is None:
        return "[System Error]: Selene is not initialised."

    triggered_args = None
    triggered_name = None

    for tool in selene.tool_router.tools.values():
        if hasattr(tool, "check_and_trigger"):
            triggered_args = tool.check_and_trigger(user_input)
            if triggered_args:
                triggered_name = tool.name
                break

    if triggered_name and triggered_args is not None:
        print(f"[Selene Server]: Tool triggered — {triggered_name}")
        result = selene.tool_router.route_and_execute(triggered_name, triggered_args)
        if result.get("status") == "success":
            data = result.get("data")
            return str(data) if data is not None else ""
        else:
            return f"I tried to use the {triggered_name} tool, but something went wrong: {result.get('message')}"

    return selene.chat(user_input)


def update_memory_and_energy(user_input: str, response: str):
    """Persists a turn to working memory and refreshes creative energy / idle timer."""
    if selene is None:
        return
    selene.creative_energy       = min(100, selene.creative_energy + 10)
    selene.last_interaction_time = time.time()
    with selene.lock:
        selene.working_memory.append({"role": "user",      "content": user_input})
        selene.working_memory.append({"role": "assistant", "content": response})
        window = selene.memory_window * 2
        if len(selene.working_memory) > window:
            selene.working_memory = selene.working_memory[-window:]

# ── Broadcast helpers ─────────────────────────────────────────────────────────

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

# ── Startup / shutdown ────────────────────────────────────────────────────────

def _init_selene():
    """
    Blocking initialisation: checks LM Studio, loads the desired model,
    instantiates LLMChat, and starts the autonomy monitor thread.
    Called from a thread-pool executor so it doesn't block the event loop.
    """
    global selene

    print("[Selene Server]: Contacting LM Studio…")
    manager = LMStudioManager(base_url=BASE_URL)

    loaded = manager.get_loaded_model_info()

    norm_target = _normalize(DESIRED_MODEL)
    loaded_path = loaded.get("path", "") if loaded else ""
    active_path: Optional[str] = None

    if loaded and norm_target in _normalize(loaded_path):
        print(f"[Selene Server]: Desired model already loaded — {loaded_path}")
        active_path = loaded_path
    else:
        if loaded:
            print(f"[Selene Server]: A different model is loaded ('{loaded_path}').")
        elif loaded is None:
            print("[Selene Server]: Server is offline or no model is loaded.")

        print(f"[Selene Server]: Attempting to load model — {DESIRED_MODEL}")
        if manager.load_model(DESIRED_MODEL):
            active_path = DESIRED_MODEL
            print(f"[Selene Server]: Model '{DESIRED_MODEL}' loaded successfully.")
            time.sleep(5)   # give LM Studio time to warm up
        else:
            print(f"[Selene Server]: Failed to load desired model '{DESIRED_MODEL}'.")
            # Fall back to whatever is currently loaded, if anything.
            if loaded_path:
                print(f"[Selene Server]: Using already loaded model as fallback — {loaded_path}")
                active_path = loaded_path
            else:
                print("[Selene Server]: No model available. Chat disabled.")
                return

    if not active_path:
        print("[Selene Server]: Could not determine an active model. Chat disabled.")
        return

    selene = LLMChat(base_url=BASE_URL, model_name=active_path)
    selene.is_running = True

    # Start Selene's internal autonomy monitor in a background daemon thread.
    # Its print() output goes to this terminal — handy for debugging.
    autonomy_thread = threading.Thread(target=selene._autonomy_monitor, daemon=True)
    autonomy_thread.start()

    print(f"[Selene Server]: Selene is online  ✦  model: {active_path}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ───────────────────────────────────────────────────────────────
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _init_selene)
    asyncio.create_task(_state_broadcaster())
    yield
    # ── shutdown ──────────────────────────────────────────────────────────────
    if selene:
        selene.save_state()
        print("[Selene Server]: State saved.")

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Selene OS Server", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/state")
async def state_endpoint():
    """Quick health + state check — useful for debugging."""
    return get_state()

# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    print(f"[Selene Server]: UI connected  ({len(clients)} client(s))")

    # Greet with current state so the UI can render immediately
    await websocket.send_json({"type": "connected", "data": get_state()})

    try:
        while True:
            data     = await websocket.receive_json()
            msg_type = data.get("type")

            # ── Chat message ──────────────────────────────────────────────────
            if msg_type == "chat":
                user_input = data.get("content", "").strip()
                if not user_input:
                    continue

                # Tell the UI Selene is thinking
                await websocket.send_json({"type": "thinking"})

                # Run blocking LLM call in thread pool
                loop     = asyncio.get_event_loop()
                response = await loop.run_in_executor(None, process_message, user_input)

                # Commit to memory
                update_memory_and_energy(user_input, response)

                await websocket.send_json({"type": "response",  "content": response})
                await websocket.send_json({"type": "state",     "data": get_state()})

            # ── Clear conversation memory ─────────────────────────────────────
            elif msg_type == "clear_memory":
                if selene:
                    with selene.lock:
                        selene.working_memory.clear()
                    print("[Selene Server]: Memory cleared by UI.")
                await websocket.send_json({"type": "state", "data": get_state()})

            # ── Manual state poll ─────────────────────────────────────────────
            elif msg_type == "get_state":
                await websocket.send_json({"type": "state", "data": get_state()})

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

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("╔══════════════════════════════════════╗")
    print("║   S E L E N E   O S   S E R V E R    ║")
    print(f"║   ws://{SERVER_HOST}:{SERVER_PORT}/ws            ║")
    print("╚══════════════════════════════════════╝")
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")
