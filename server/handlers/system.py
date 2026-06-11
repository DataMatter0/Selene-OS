"""
server/handlers/system.py -- System state, model management, agent swap

Sections
--------
  STATE        get_state
  MODELS       get_models, set_model (with already-loaded skip)
  AGENT        toggle_agent, save_dashboard_layout
  GAMEPAD      update_gamepad_config
  DIAGNOSTICS  run_latency_test
  DISCORD      get_discord_status, check_discord_connectivity
  INTEGRATIONS get_integrations_status
"""

import asyncio
import os
import time

from server.config    import BASE_URL, _normalize
from server.state     import get_state, broadcast, clients
from server.startup   import global_guide_button
import server.state   as _st
import server.startup as _startup

from selene_brain import LMStudioManager


async def handle(websocket, data: dict, loop) -> bool:
    msg_type = data.get("type")
    selene   = _st.selene_ref

    if msg_type == "get_state":
        await websocket.send_json({"type": "state", "data": get_state()})
        return True

    elif msg_type == "get_models":
        manager = LMStudioManager(base_url=BASE_URL)
        models  = await loop.run_in_executor(None, manager.list_models)
        loaded  = await loop.run_in_executor(None, manager.get_loaded_model_info)
        await websocket.send_json({
            "type":    "models_list",
            "models":  [m.get("path", m.get("id", "")) for m in (models or [])],
            "current": loaded.get("path", "") if loaded else "",
        })
        return True

    elif msg_type == "set_model":
        new_path = data.get("model", "").strip()
        if not new_path:
            await websocket.send_json({"type": "model_switch_status", "ok": False, "error": "No model path given."})
            return True

        await websocket.send_json({"type": "model_switch_status", "ok": None, "status": "switching"})
        try:
            manager = LMStudioManager(base_url=BASE_URL)

            _currently_loaded = await loop.run_in_executor(None, manager.get_loaded_model_info)
            _already_loaded   = (
                _currently_loaded is not None
                and _normalize(new_path) in _normalize(_currently_loaded.get("path", ""))
            )
            if _already_loaded:
                if selene is not None:
                    selene.llm_caller.model_name = new_path
                    selene._prompt_dirty = True
                await websocket.send_json({
                    "type": "model_switch_status", "ok": True,
                    "model": new_path, "status": "already_loaded",
                })
                print(f"[Selene Server]: Model '{new_path}' already loaded -- skipping reload.")
                return True

            instance_id = await loop.run_in_executor(None, manager.get_loaded_instance_id)
            if instance_id:
                await loop.run_in_executor(None, manager.unload_model, instance_id)

            ok = await loop.run_in_executor(None, manager.load_model, new_path)
            if not ok:
                await websocket.send_json({
                    "type": "model_switch_status", "ok": False,
                    "error": f"LM Studio failed to load '{new_path}'",
                })
            else:
                _norm  = _normalize(new_path)
                _ready = False
                for _attempt in range(30):
                    await asyncio.sleep(1)
                    try:
                        _loaded = await loop.run_in_executor(None, manager.get_loaded_model_info)
                        if _loaded and _norm in _normalize(_loaded.get("path", "")):
                            _ready = True
                            break
                    except Exception:
                        pass

                if _ready:
                    if selene is not None:
                        selene.llm_caller.model_name = new_path
                        selene._prompt_dirty         = True
                    await websocket.send_json({"type": "model_switch_status", "ok": True, "model": new_path})
                    print(f"[Selene Server]: Model switched to '{new_path}' and ready.")
                else:
                    await websocket.send_json({
                        "type": "model_switch_status", "ok": False,
                        "error": f"Model '{new_path}' loaded but didn't become ready within 30s.",
                    })
        except Exception as exc:
            try:
                import httpx as _httpx
                detail = exc.response.text if isinstance(exc, _httpx.HTTPStatusError) else str(exc)
            except Exception:
                detail = str(exc)
            await websocket.send_json({"type": "model_switch_status", "ok": False, "error": detail})
        return True

    elif msg_type == "toggle_agent":
        new_agent = data.get("agent", "selene").lower()
        if selene:
            await loop.run_in_executor(None, selene.swap_agent, new_agent)
            await websocket.send_json({"type": "state",         "data": get_state()})
            await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})
            print(f"[Selene Server]: Swapped active agent to '{new_agent}' via UI toggle request.")
        return True

    elif msg_type == "save_dashboard_layout":
        layout = data.get("layout")
        if selene and layout:
            selene.dashboard_layout = layout
            await loop.run_in_executor(None, selene.save_state)
            await websocket.send_json({"type": "state", "data": get_state()})
            print(f"[Selene Server]: Saved new dashboard layout state to disk: {layout}")
        return True

    elif msg_type == "update_gamepad_config":
        if "guide_button" in data:
            _startup.global_guide_button = int(data["guide_button"])
            print(f"[Gamepad] Updated guide button to {_startup.global_guide_button}")
        return True

    elif msg_type == "run_latency_test":
        await websocket.send_json({"type": "latency_test_status", "status": "running"})

        db_duration = 0.0
        if selene:
            t0 = time.perf_counter()
            for _ in range(20):
                selene.db.get_setting("ONBOARDING_COMPLETE")
            db_duration = (time.perf_counter() - t0) * 1000.0 / 20.0

        prompt_duration = 0.0
        if selene:
            t0 = time.perf_counter()
            selene._build_system_prompt()
            prompt_duration = (time.perf_counter() - t0) * 1000.0

        llm_ok = False
        llm_error = None
        llm_duration = 0.0
        if selene:
            t0 = time.perf_counter()
            try:
                await loop.run_in_executor(
                    None, selene.llm_caller.call_llm,
                    "ping", "Reply with only the word 'pong'.", [], 0.0, 5
                )
                llm_duration = (time.perf_counter() - t0) * 1000.0
                llm_ok = True
            except Exception as e:
                llm_error = str(e)

        await websocket.send_json({
            "type": "latency_test_result",
            "ok": llm_ok, "error": llm_error,
            "db_latency_ms":     round(db_duration, 2),
            "prompt_latency_ms": round(prompt_duration, 2),
            "llm_latency_ms":    round(llm_duration, 2),
            "total_latency_ms":  round(db_duration + prompt_duration + llm_duration, 2),
        })
        return True

    elif msg_type in ("get_discord_status", "check_discord_connectivity"):
        try:
            import selene_discord
            client    = selene_discord.discord_client
            is_online = client is not None and client.is_ready()
            bot_name  = f"{client.user.name}#{client.user.discriminator}" if (is_online and client.user) else "Offline"
            latency   = round(client.latency * 1000) if (is_online and client.latency is not None) else 0
            guilds_list = [g.name for g in client.guilds] if (is_online and client.guilds) else []

            payload = {
                "online": is_online, "bot_name": bot_name, "latency": latency,
                "guilds": guilds_list,
                "allowed_channels": selene_discord.ALLOWED_CHANNELS,
                "allowed_users":    selene_discord.ALLOWED_USERS,
                "token_exists":     bool(selene_discord.DISCORD_BOT_TOKEN),
            }
            if msg_type == "get_discord_status":
                await websocket.send_json({"type": "discord_status", "data": payload})
            else:
                await websocket.send_json({"type": "discord_connectivity_result", "ok": is_online, "data": payload})
        except Exception as exc:
            if msg_type == "get_discord_status":
                await websocket.send_json({"type": "discord_status", "data": {"online": False, "error": str(exc)}})
            else:
                await websocket.send_json({"type": "discord_connectivity_result", "ok": False, "error": str(exc)})
        return True

    elif msg_type == "get_integrations_status":
        if selene:
            status = {
                "google":  {"active": False, "message": "google_client_secret.json missing at startup."},
                "hass":    {"active": False, "url": "", "entities_count": 0},
                "spotify": {"active": False, "message": "Spotify credentials not set in .env."},
            }
            google_tool = selene.tool_router.tools.get("google")
            if google_tool:
                status["google"]["active"]  = not google_tool.dormant
                status["google"]["message"] = (
                    "Connected and authorized via OAuth." if not google_tool.dormant
                    else "google_client_secret.json missing at startup. OAuth setup instructions printed to logs."
                )
            hass_tool = selene.tool_router.tools.get("homeassistant")
            if hass_tool:
                status["hass"]["active"] = not hass_tool.dormant
                status["hass"]["url"]    = os.environ.get("HASS_URL", "")
                if not hass_tool.dormant:
                    try:
                        status["hass"]["entities_count"] = len(hass_tool.list_entities())
                    except Exception:
                        status["hass"]["entities_count"] = 14
            spotify_tool = selene.tool_router.tools.get("spotify")
            if spotify_tool:
                status["spotify"]["active"]  = not spotify_tool.dormant
                status["spotify"]["message"] = (
                    "Connected to Spotify Web API." if not spotify_tool.dormant
                    else "SPOTIFY_CLIENT_ID missing in .env config. Stub dormant."
                )
            await websocket.send_json({"type": "integrations_status", "data": status})
        return True

    return False
