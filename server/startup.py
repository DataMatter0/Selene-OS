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

from .config  import BASE_URL
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
    Blocking initialisation: instantiates LLMChat (which calls swap_agent("selene")
    and reads the model from agents/selene/config.json), then starts supporting systems.
    No longer attempts to pre-load a model — LM Studio should have Selene's model
    ready, or the first chat call will surface a clear error. Model loading is
    triggered by toggle_agent when the user switches agents from the UI.
    Called from a thread-pool executor so it doesn't block the event loop.
    """
    from . import state as _s

    from tools.story_engine.db_helper import initialize_database
    try:
        initialize_database()
        print("[Selene Server]: Story engine database initialized and verified.")
    except Exception as db_err:
        print(f"[Selene Server Error]: Database initialization failed: {db_err}")

    # Check LM Studio reachability (informational only — don't block boot)
    print("[Selene Server]: Contacting LM Studio...")
    manager = LMStudioManager(base_url=BASE_URL)
    loaded  = manager.get_loaded_model_info()
    if loaded:
        print(f"[Selene Server]: LM Studio online — loaded model: {loaded.get('path', loaded.get('id', '?'))}")
    else:
        print("[Selene Server]: LM Studio offline or no model loaded — booting in degraded mode.")

    # LLMChat.__init__ calls swap_agent("selene"), which reads
    # agents/selene/config.json and sets llm_caller.model_name automatically.
    selene = LLMChat(base_url=BASE_URL, model_name="")
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
    setattr(selene, "tool_suggestion",           ToolSuggestionLayer(selene))
    setattr(selene, "pending_tool_confirmation", None)
    print("[Selene Server]: ToolSuggestionLayer initialised.")

    autonomy_thread = threading.Thread(target=selene._autonomy_monitor, daemon=True)
    autonomy_thread.start()

    print(f"[Selene Server]: Selene is online  *  model: {selene.llm_caller.model_name}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ───────────────────────────────────────────────────────────────
    loop = asyncio.get_event_loop()
    # Store loop on state so internal helpers (e.g. add_notification) can broadcast
    import server.state as _st_ref
    _st_ref.event_loop = loop

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
            # Broadcast a "ready" event so the frontend knows init is complete
        # even if it already received an "offline" state on first connect.
        await broadcast({"type": "state",  "data": get_state()})
        await broadcast({"type": "ready",  "data": get_state()})
        if selene is not None:
            await broadcast({"type": "conversations", "data": selene.list_conversations()})

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
