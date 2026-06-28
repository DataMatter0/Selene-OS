"""
server/handlers/system.py — System state, model management, agent swap
"""

import asyncio
import json
import os
import time

from server.config    import BASE_URL, _normalize
from server.state     import get_state, broadcast, clients
from server.startup   import global_guide_button
from server.roster    import get_roster, reload_roster, default_agent_slug
import server.state   as _st
import server.startup as _startup

from selene_brain import LMStudioManager

_AGENTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "agents")


async def _swap_model_on_lmstudio(manager: LMStudioManager, new_path: str, loop) -> dict:
    """
    Unload the current model, load new_path, poll until ready (30s timeout).
    Returns {"ok": True, "model": new_path} or {"ok": False, "error": "..."}.
    Skips unload/load if new_path is already the loaded model.
    """
    import httpx as _httpx

    # Check if already loaded — skip the unload/load cycle
    _currently_loaded = await loop.run_in_executor(None, manager.get_loaded_model_info)
    _cl_id = _normalize((_currently_loaded or {}).get("id", "") or (_currently_loaded or {}).get("path", ""))
    _already_loaded = bool(_currently_loaded and _cl_id and (
        _normalize(new_path) in _cl_id or _cl_id in _normalize(new_path)
    ))
    if _already_loaded:
        print(f"[Selene Server]: Model '{new_path}' already loaded — skipping reload.")
        return {"ok": True, "model": new_path, "status": "already_loaded"}

    # Unload current
    instance_id = await loop.run_in_executor(None, manager.get_loaded_instance_id)
    if instance_id:
        await loop.run_in_executor(None, manager.unload_model, instance_id)

    # Load new
    try:
        ok = await loop.run_in_executor(None, manager.load_model, new_path)
    except _httpx.HTTPStatusError as exc:
        return {"ok": False, "error": exc.response.text}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    if not ok:
        return {"ok": False, "error": f"LM Studio failed to load '{new_path}'"}

    # Poll until ready (30s)
    _norm = _normalize(new_path)
    for _ in range(30):
        await asyncio.sleep(1)
        try:
            _loaded = await loop.run_in_executor(None, manager.get_loaded_model_info)
            if _loaded:
                _lid = _normalize(_loaded.get("id", "") or _loaded.get("path", ""))
                if _norm in _lid or _lid in _norm:
                    return {"ok": True, "model": new_path}
        except Exception:
            pass

    return {"ok": False, "error": f"Model '{new_path}' didn't become ready within 30s."}


async def handle(websocket, data: dict, loop) -> bool:
    msg_type = data.get("type")
    selene   = _st.selene_ref

    if msg_type == "get_state":
        await websocket.send_json({"type": "state", "data": get_state()})
        return True

    elif msg_type == "get_roster":
        await websocket.send_json({"type": "roster", "data": get_roster()})
        return True

    elif msg_type == "reload_roster":
        roster = reload_roster()
        await websocket.send_json({"type": "roster", "data": roster})
        await websocket.send_json({"type": "state",  "data": get_state()})
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
        manager = LMStudioManager(base_url=BASE_URL)
        result  = await _swap_model_on_lmstudio(manager, new_path, loop)

        if result["ok"] and selene is not None:
            selene.llm_caller.model_name = new_path
            selene._prompt_dirty         = True

        await websocket.send_json({"type": "model_switch_status", **result})
        if result["ok"]:
            print(f"[Selene Server]: Model switched to '{new_path}'.")
        return True

    elif msg_type == "toggle_agent":
        new_agent = data.get("agent", default_agent_slug()).lower()
        if not selene:
            return True

        # Read target model from agents/{slug}/config.json
        # model      = chat completions endpoint name (e.g. "Selene/Sage")
        # model_path = real LM Studio load path (e.g. "google/gemma-3n-e4b")
        config_path = os.path.join(_AGENTS_DIR, new_agent, "config.json")
        target_model:      str = ""   # used in chat completions payload
        target_model_path: str = ""   # used for LM Studio load/unload API
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as _f:
                    _cfg = json.load(_f)
                target_model      = _cfg.get("model", "")
                # Fall back to model if model_path not set (real-path agents)
                target_model_path = _cfg.get("model_path", target_model)
            except Exception as _ce:
                print(f"[Selene Server]: Could not read config for '{new_agent}': {_ce}")

        # Trigger model swap only if a load path is defined
        if target_model_path:
            manager = LMStudioManager(base_url=BASE_URL)
            # Check if the required model is already loaded — skip load if so
            _loaded_info = await loop.run_in_executor(None, manager.get_loaded_model_info)
            _loaded_id   = (_loaded_info or {}).get("id", "") or (_loaded_info or {}).get("path", "")
            _already_ok  = (
                _loaded_id and (
                    _normalize(target_model_path) in _normalize(_loaded_id) or
                    _normalize(target_model)      in _normalize(_loaded_id)
                )
            )
            if _already_ok:
                print(f"[Selene Server]: Model '{target_model}' already loaded — skipping LM Studio swap.")
                await broadcast({"type": "model_switch_status", "ok": True,
                                 "status": "already_loaded", "agent": new_agent,
                                 "model": target_model_path})
            else:
                await broadcast({"type": "model_switch_status", "ok": None, "status": "switching", "agent": new_agent})
                result = await _swap_model_on_lmstudio(manager, target_model_path, loop)
                await broadcast({"type": "model_switch_status", **result, "agent": new_agent})
                if not result["ok"]:
                    err = result.get("error", "Unknown error")
                    print(f"[Selene Server]: Model swap failed for '{new_agent}': {err}")
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Cannot switch to {new_agent} — model failed to load: {err}"
                    })
                    return True  # Abort — don't swap identity to an unloaded model
        else:
            print(f"[Selene Server]: No model defined for '{new_agent}' — skipping model swap.")
            await broadcast({"type": "model_switch_status", "ok": True,
                             "status": "no_model", "agent": new_agent, "model": ""})

        # Swap identity, memory, prompt
        try:
            await loop.run_in_executor(None, selene.swap_agent, new_agent)
            await websocket.send_json({"type": "state",         "data": get_state()})
            await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})
            # Re-push manifest + memory for the newly active agent
            _manifest_res = await loop.run_in_executor(
                None,
                lambda: selene.tool_router.route_and_execute("manifest_manager", {"command": "get_manifest"})
            )
            await websocket.send_json({"type": "manifest_data", "data": _manifest_res.get("data")})
            # Re-push memory cards for the newly active agent
            import datetime as _dt
            _today     = _dt.date.today().isoformat()
            _soul_path = getattr(selene, "prompt_path", None) or getattr(selene, "SOUL_FILE", "")
            try:
                _row      = selene.db.get_daily_manifest(_today)
                _manifest = (_row.get("summary", "") if _row else "") or ""
            except Exception:
                _manifest = ""
            await websocket.send_json({
                "type": "memory_files",
                "data": {
                    "soul":              selene._read_file_safe(_soul_path),
                    "tools_context":     selene._read_file_safe(getattr(selene, "TOOLS_CONTEXT_FILE", "")),
                    "user_profile":      selene._read_file_safe(getattr(selene, "USER_PROFILE_FILE",  "")),
                    "character_profile": selene._read_file_safe(getattr(selene, "CHARACTER_PROFILE_FILE", "")),
                    "manifest":          _manifest,
                    "agent_name":        getattr(selene, "active_agent_name", "Selene"),
                    "agent_slug":        getattr(selene, "active_agent_slug",  new_agent),
                },
            })
            print(f"[Selene Server]: Swapped active agent to '{new_agent}'.")
        except Exception as exc:
            import traceback; traceback.print_exc()
            await websocket.send_json({"type": "error", "message": f"Agent swap failed: {exc}"})
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
        if not selene:
            return True   # still initialising — silent no-op
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
                if hasattr(hass_tool, "url"):
                    status["hass"]["url"] = hass_tool.url

            spotify_tool = selene.tool_router.tools.get("spotify")
            if spotify_tool:
                status["spotify"]["active"]  = not getattr(spotify_tool, "dormant", True)
                status["spotify"]["message"] = (
                    "Connected." if not getattr(spotify_tool, "dormant", True)
                    else "Spotify credentials not set in .env."
                )

            await websocket.send_json({"type": "integrations_status", "data": status})
        return True
