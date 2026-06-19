"""
server/handlers/memory.py — Memory file access and tool phrase management
"""

import datetime
import os

import server.state as _st


async def handle(websocket, data: dict, loop) -> bool:
    msg_type = data.get("type")
    selene   = _st.selene_ref

    if msg_type == "get_memory":
        if selene:
            today     = datetime.date.today().isoformat()
            soul_path = getattr(selene, "prompt_path", getattr(selene, "SOUL_FILE", selene.SOUL_FILE))

            # Active agent's daily manifest — always from the currently loaded DB
            try:
                row      = selene.db.get_daily_manifest(today)
                manifest = (row.get("summary", "") if row else "") or "(No manifest compiled for today yet.)"
            except Exception:
                manifest = "(No manifest compiled for today yet.)"

            await websocket.send_json({
                "type": "memory_files",
                "data": {
                    "soul":              selene._read_file_safe(soul_path),
                    "tools_context":     selene._read_file_safe(getattr(selene, "TOOLS_CONTEXT_FILE", os.path.join(selene.MEMORY_DIR, "tools_context.md"))),
                    "user_profile":      selene._read_file_safe(getattr(selene, "USER_PROFILE_FILE",  os.path.join(selene.MEMORY_DIR, "user_profile.md"))),
                    "character_profile": selene._read_file_safe(getattr(selene, "CHARACTER_PROFILE_FILE", os.path.join(selene.MEMORY_DIR, "character_profile.md"))),
                    "manifest":          manifest,
                    "agent_name":        getattr(selene, "active_agent_name", "Selene"),
                }
            })
        else:
            await websocket.send_json({"type": "error", "message": "Selene not initialised."})
        return True

    elif msg_type == "save_memory":
        file_key = data.get("file", "").strip()
        content  = data.get("content", "")
        if selene and file_key:
            soul_path = getattr(selene, "prompt_path", getattr(selene, "SOUL_FILE", selene.SOUL_FILE))
            _file_map = {
                "soul":              soul_path,
                "tools_context":     getattr(selene, "TOOLS_CONTEXT_FILE", selene.TOOLS_CONTEXT_FILE),
                "user_profile":      getattr(selene, "USER_PROFILE_FILE",  os.path.join(selene.MEMORY_DIR, "user_profile.md")),
                "character_profile": getattr(selene, "CHARACTER_PROFILE_FILE", os.path.join(selene.MEMORY_DIR, "character_profile.md")),
            }
            target = _file_map.get(file_key)
            if target:
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
                    with open(os.path.abspath(target), 'w', encoding='utf-8') as fh:
                        fh.write(content)
                    selene._prompt_dirty = True
                    print(f"[Selene Server]: Memory file '{file_key}' saved -> {os.path.abspath(target)}")
                    await websocket.send_json({"type": "memory_saved", "file": file_key, "ok": True})
                except Exception as exc:
                    await websocket.send_json({"type": "memory_saved", "file": file_key, "ok": False, "error": str(exc)})
            else:
                await websocket.send_json({"type": "error", "message": f"Unknown file key: {file_key}"})
        return True

    elif msg_type == "force_memory_extract":
        if selene:
            await loop.run_in_executor(None, selene.force_extract_memory)
            await websocket.send_json({"type": "memory_extract_started"})
        return True

    elif msg_type == "get_tool_phrases":
        if selene:
            phrases = selene.db.get_tool_phrases()
            await websocket.send_json({"type": "tool_phrases", "data": phrases})
        return True

    elif msg_type == "add_tool_phrase":
        tool_name = data.get("tool_name", "").strip().lower()
        phrase    = data.get("phrase", "").strip().lower()
        if selene and tool_name and phrase:
            ok = selene.db.add_tool_phrase(tool_name, phrase)
            if ok and hasattr(selene, "tool_suggestion") and selene.tool_suggestion:
                selene.tool_suggestion._seed_default_phrases()
            await websocket.send_json({"type": "tool_phrase_added", "ok": ok,
                                       "tool_name": tool_name, "phrase": phrase})
        return True

    elif msg_type == "remove_tool_phrase":
        tool_name = data.get("tool_name", "").strip().lower()
        phrase    = data.get("phrase", "").strip().lower()
        if selene and tool_name and phrase:
            ok = selene.db.remove_tool_phrase(tool_name, phrase)
            await websocket.send_json({"type": "tool_phrase_removed", "ok": ok,
                                       "tool_name": tool_name, "phrase": phrase})
        return True

    return False
