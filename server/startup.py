"""
server/startup.py — Server startup, lifespan, and background tasks
────────────────────────────────────────────────────────────────────
Owns:
  _init_selene()          — blocking LM Studio init + LLMChat construction
  lifespan()              — FastAPI lifespan context manager
  _timer_poller()         — background task: polls schedule tool every 10s
  _gamepad_poller_thread()— background thread: gamepad guide-button broadcast
"""

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from .config  import BASE_URL, DESIRED_MODEL, _normalize
from .state   import set_selene, broadcast, get_state, _state_broadcaster, clients
from selene_brain import LLMChat, LMStudioManager


global_guide_button: int = 16


def _gamepad_poller_thread(loop) -> None:
    global global_guide_button
    try:
        import pygame
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
                num_buttons      = joy.get_numbuttons()
                is_select_start  = False
                is_guide_pressed = False

                if num_buttons > 9 and joy.get_button(8) and joy.get_button(9):
                    is_select_start = True
                elif num_buttons > 7 and joy.get_button(6) and joy.get_button(7):
                    is_select_start = True

                if num_buttons > global_guide_button and joy.get_button(global_guide_button):
                    is_guide_pressed = True

                if is_select_start or is_guide_pressed:
                    asyncio.run_coroutine_threadsafe(
                        broadcast({"type": "force_focus"}), loop
                    )
                    time.sleep(1.0)
                    break
    except Exception as e:
        print("[Gamepad] Poller failed:", e)


async def _timer_poller() -> None:
    """
    Background asyncio task.
    Polls the schedule manager every 10 seconds.
    If a timer expires, broadcasts a notification and pushes it into Selene's
    working memory as an alert.
    """
    from . import state as _s
    while True:
        await asyncio.sleep(10)
        selene = _s.selene_ref
        if selene and "schedule_manager" in selene.tool_router.tools:
            try:
                tool = selene.tool_router.tools["schedule_manager"]
                if hasattr(tool, "load_state") and hasattr(tool, "save_state"):
                    state = tool.load_state()
                    now   = time.time()
                    expired = [t for t in state.get("timers", []) if t["trigger_time"] <= now]
                    active  = [t for t in state.get("timers", []) if t["trigger_time"] >  now]

                    if expired:
                        state["timers"] = active
                        tool.save_state(state)
                        for t in expired:
                            msg = f"[ALARM/TIMER EXPIRED] Title: {t.get('message', 'Timer')}"
                            await broadcast({"type": "timer_expired", "data": {"message": msg, "id": t["id"]}})
                            with selene.lock:
                                selene.working_memory.append(
                                    {"role": "user", "content": msg, "ts": time.time()}
                                )
            except Exception as e:
                print(f"[Timer Poller Error]: {e}")


def _init_selene() -> None:
    """
    Blocking initialisation: checks LM Studio, loads the desired model,
    instantiates LLMChat, and starts the autonomy monitor thread.
    Called from a thread-pool executor so it doesn't block the event loop.
    """
    from . import state as _s

    from tools.story_engine.db_helper import initialize_database
    try:
        initialize_database()
        print("[Selene Server]: Story engine database initialized and verified.")
    except Exception as db_err:
        print(f"[Selene Server Error]: Database initialization failed: {db_err}")

    print("[Selene Server]: Contacting LM Studio...")
    manager = LMStudioManager(base_url=BASE_URL)

    loaded      = manager.get_loaded_model_info()
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
            time.sleep(5)
        else:
            print(f"[Selene Server]: Failed to load desired model '{DESIRED_MODEL}'.")
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
    set_selene(selene)

    # Hook up real-time WebSocket state change broadcast for knowledge manager
    k_tool = selene.tool_router.tools.get("knowledge_manager")
    if k_tool:
        def handle_change():
            import asyncio as _aio
            from . import state as _st
            if _st.selene_ref and _st.clients:
                loop = None
                try:
                    loop = _aio.get_event_loop()
                except RuntimeError:
                    pass
                if loop:
                    loop.call_soon_threadsafe(
                        lambda: _aio.create_task(
                            broadcast({"type": "knowledge_state", "data": k_tool.load_state()})
                        )
                    )
        setattr(k_tool, "on_state_change", handle_change)

    from selene_brain.tool_suggestion import ToolSuggestionLayer
    selene.tool_suggestion           = ToolSuggestionLayer(selene)
    selene.pending_tool_confirmation = None
    print("[Selene Server]: ToolSuggestionLayer initialised.")

    autonomy_thread = threading.Thread(target=selene._autonomy_monitor, daemon=True)
    autonomy_thread.start()

    print(f"[Selene Server]: Selene is online  *  model: {active_path}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ───────────────────────────────────────────────────────────────
    loop = asyncio.get_event_loop()

    async def _start_selene_background():
        await loop.run_in_executor(None, _init_selene)
        print("[Selene Server]: Background init complete.")
        from . import state as _s
        selene = _s.selene_ref
        if selene is not None:
            try:
                from selene_discord import start_discord_bot
                from .tool_pipeline import process_message, update_memory_and_energy
                asyncio.create_task(
                    start_discord_bot(
                        selene_chat=selene,
                        process_message_fn=process_message,
                        update_memory_fn=update_memory_and_energy,
                        broadcast_fn=broadcast,
                    )
                )
            except Exception as exc:
                print(f"[Selene Server]: Discord bot failed to start — {exc}")
            await broadcast({"type": "state", "data": get_state()})

    asyncio.create_task(_start_selene_background())
    asyncio.create_task(_state_broadcaster())
    asyncio.create_task(_timer_poller())
    threading.Thread(target=_gamepad_poller_thread, args=(loop,), daemon=True).start()

    yield

    # ── shutdown ──────────────────────────────────────────────────────────────
    try:
        from selene_discord import stop_discord_bot
        await stop_discord_bot()
    except Exception as exc:
        print(f"[Selene Server]: Error stopping Discord bot -- {exc}")

    from . import state as _s
    if _s.selene_ref:
        _s.selene_ref.save_state()
        print("[Selene Server]: State saved.")
