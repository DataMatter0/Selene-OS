"""
server/handlers/manifest.py — Task manifest and todo step tracker
"""

from server.state import clients
import server.state as _st


async def _broadcast_manifest(selene) -> None:
    """Push fresh manifest state to all clients."""
    res = selene.tool_router.route_and_execute("manifest_manager", {"command": "get_manifest"})
    for client in clients:
        await client.send_json({"type": "manifest_data", "data": res.get("data")})


async def handle(websocket, data: dict, loop) -> bool:
    msg_type = data.get("type")
    selene   = _st.selene_ref

    if msg_type == "get_manifest":
        if selene:
            res = selene.tool_router.route_and_execute("manifest_manager", {"command": "get_manifest"})
            if res.get("status") == "success":
                await websocket.send_json({"type": "manifest_data", "data": res.get("data")})
            else:
                await websocket.send_json({"type": "error", "message": res.get("message")})
        else:
            pass  # server still initialising — frontend will retry on "ready" event
        return True

    elif msg_type == "add_task":
        if selene:
            res = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                "command": "add_task",
                "title": data.get("title", ""),
                "description": data.get("description", ""),
                "category": data.get("category", "Feature"),
                "priority": data.get("priority", "B"),
                "dependencies": data.get("dependencies", []),
                "subtasks": data.get("subtasks", []),
            })
            if res.get("status") == "success":
                await websocket.send_json({"type": "task_added", "ok": True, "message": res.get("data")})
                await _broadcast_manifest(selene)
            else:
                await websocket.send_json({"type": "task_added", "ok": False, "error": res.get("message")})
        else:
            pass  # server still initialising — frontend will retry on "ready" event
        return True

    elif msg_type == "update_task_full":
        if selene:
            tid = data.get("id", "").strip().upper()
            if tid:
                await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                    "command": "update_task_full",
                    "id": tid,
                    "title": data.get("title", ""),
                    "description": data.get("description", ""),
                    "category": data.get("category", "Feature"),
                    "priority": data.get("priority", "B"),
                    "dependencies": data.get("dependencies", []),
                    "subtasks": data.get("subtasks", []),
                })
                await _broadcast_manifest(selene)
        else:
            pass  # server still initialising — frontend will retry on "ready" event
        return True

    elif msg_type == "toggle_task":
        if selene:
            res = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                "command": "toggle_task",
                "id": data.get("id", ""),
                "status": data.get("status"),
            })
            if res.get("status") == "success":
                await websocket.send_json({"type": "task_toggled", "ok": True, "message": res.get("data")})
                await _broadcast_manifest(selene)
            else:
                await websocket.send_json({"type": "task_toggled", "ok": False, "error": res.get("message")})
        else:
            pass  # server still initialising — frontend will retry on "ready" event
        return True

    elif msg_type == "delete_task":
        if selene:
            res = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                "command": "delete_task",
                "id": data.get("id", ""),
            })
            if res.get("status") == "success":
                await websocket.send_json({"type": "task_deleted", "ok": True, "message": res.get("data")})
                await _broadcast_manifest(selene)
            else:
                await websocket.send_json({"type": "task_deleted", "ok": False, "error": res.get("message")})
        else:
            pass  # server still initialising — frontend will retry on "ready" event
        return True

    elif msg_type == "update_task":
        if selene:
            tid  = data.get("id", "").strip().upper()
            desc = data.get("description", "").strip()
            if tid and desc:
                await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                    "command": "update_task", "id": tid, "description": desc,
                })
                await _broadcast_manifest(selene)
        else:
            pass  # server still initialising — frontend will retry on "ready" event
        return True

    elif msg_type == "reorder_tasks":
        if selene:
            order = data.get("task_order", [])
            res   = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                "command": "reorder_tasks", "task_order": order,
            })
            if res.get("status") == "success":
                await websocket.send_json({"type": "tasks_reordered", "ok": True, "message": res.get("data")})
                await _broadcast_manifest(selene)
            else:
                await websocket.send_json({"type": "tasks_reordered", "ok": False, "error": res.get("message")})
        else:
            pass  # server still initialising — frontend will retry on "ready" event
        return True

    elif msg_type == "update_guidelines":
        if selene:
            selene.tool_router.tools["manifest_manager"].update_guidelines(data.get("content", ""))
            await websocket.send_json({"type": "guidelines_updated", "ok": True})
            await _broadcast_manifest(selene)
        else:
            pass  # server still initialising — frontend will retry on "ready" event
        return True

    elif msg_type == "reorganize_manifest":
        if selene:
            await websocket.send_json({"type": "thinking"})
            explanation = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                "command": "reorganize", "prompt": data.get("prompt", ""),
            })
            if explanation.get("status") == "success":
                await websocket.send_json({"type": "response", "content": explanation.get("data")})
                await _broadcast_manifest(selene)
            else:
                await websocket.send_json({"type": "error", "message": explanation.get("message")})
        else:
            pass  # server still initialising — frontend will retry on "ready" event
        return True

    elif msg_type == "compile_and_push_manifest":
        if selene:
            res = await loop.run_in_executor(None, selene.compile_daily_manifest)
            if res.get("status") == "success":
                notion_tool   = selene.tool_router.tools.get("notion")
                notion_pushed = False
                notion_error  = None
                if notion_tool and not notion_tool.dormant:
                    try:
                        push_res = await loop.run_in_executor(
                            None, notion_tool.execute,
                            {"command": "append_blocks", "page_id": selene.notion_page_id,
                             "content": f"### Daily Manifest - {res['date']}\n\n{res['summary']}"}
                        )
                        if isinstance(push_res, dict) and "error" not in push_res:
                            notion_pushed = True
                        else:
                            push_res = await loop.run_in_executor(
                                None, notion_tool.execute,
                                {"command": "create_page", "parent_id": selene.notion_page_id,
                                 "title": f"Daily Manifest - {res['date']}", "content": res["summary"]}
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
                    "type": "manifest_compiled", "ok": True,
                    "date": res["date"], "summary": res["summary"],
                    "notion_pushed": notion_pushed, "notion_error": notion_error,
                })
            else:
                await websocket.send_json({
                    "type": "manifest_compiled", "ok": False,
                    "error": res.get("summary", "No data to compile today."),
                })
        return True

    elif msg_type == "todo_get":
        if selene:
            todo = selene.tool_router.tools.get("todo")
            if todo:
                await websocket.send_json({"type": "todo_state", "data": todo.get_plan()})
        return True

    elif msg_type == "todo_clear":
        if selene:
            todo = selene.tool_router.tools.get("todo")
            if todo:
                todo.execute({"command": "clear"})
                for client in clients:
                    await client.send_json({"type": "todo_state", "data": todo.get_plan()})
        return True


    elif msg_type == "save_idea":
        if selene:
            text = data.get("text", "").strip()
            if not text:
                await websocket.send_json({"type": "error", "message": "save_idea requires 'text'."})
            else:
                res = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                    "command": "save_idea", "text": text,
                })
                inner = res.get("data")
                # Sage routing intercept — the tool returned a routing_pending dict
                if isinstance(inner, dict) and inner.get("routing_pending"):
                    suggestion = inner["suggestion"]
                    distilled  = inner["distilled"]
                    # Store pending routing on the selene instance
                    selene.pending_idea_routing = {
                        "distilled":     distilled,
                        "original_text": inner.get("original_text", text),
                        "suggestion":    suggestion,
                    }
                    agent_name = suggestion.get("name", suggestion.get("agent", "another agent"))
                    reason     = suggestion.get("reason", "")
                    domain     = suggestion.get("domain", "")
                    # Build a natural routing suggestion message
                    route_msg = (
                        f"Hey, this idea feels like it belongs with **{agent_name}** — "
                        f"{reason} Their domain covers {domain}. "
                        f"Want me to route it there? You can also say *'keep it'* to save it here, "
                        f"or name a specific agent."
                    )
                    await websocket.send_json({
                        "type":       "idea_routing_suggested",
                        "suggestion": suggestion,
                        "distilled":  distilled,
                        "message":    route_msg,
                    })
                elif res.get("status") == "success":
                    await websocket.send_json({"type": "idea_saved", "ok": True, "message": res.get("data")})
                    await _broadcast_manifest(selene)
                else:
                    await websocket.send_json({"type": "idea_saved", "ok": False, "error": res.get("message")})
        return True

    elif msg_type == "confirm_idea_routing":
        if selene:
            pending = getattr(selene, "pending_idea_routing", None)
            if not pending:
                await websocket.send_json({"type": "error", "message": "No pending idea routing."})
                return True
            choice = data.get("choice", "").strip().lower()  # agent slug, "keep", or "sage"
            distilled    = pending["distilled"]
            original_text = pending["original_text"]
            suggestion   = pending["suggestion"]
            selene.pending_idea_routing = None  # always clear

            if choice in ("keep", "sage", "me", "here"):
                # Save to Sage's own manifest
                res = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                    "command": "save_idea", "text": original_text,
                })
                inner = res.get("data")
                if isinstance(inner, dict) and inner.get("routing_pending"):
                    # Force-save by calling with a bypass flag via save_idea_to_agent to sage
                    res2 = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                        "command": "save_idea_to_agent", "agent": "sage",
                        "distilled": distilled, "original_text": original_text,
                    })
                    await websocket.send_json({"type": "idea_saved", "ok": True, "message": res2.get("data", "Idea kept with Sage.")})
                else:
                    await websocket.send_json({"type": "idea_saved", "ok": True, "message": res.get("data", "Idea saved.")})
                await _broadcast_manifest(selene)
            else:
                # Route to specified agent (use suggestion slug if choice is "yes"/"yeah"/empty)
                _YES = {"yes", "yeah", "yep", "sure", "ok", "okay", "go ahead", "do it", "y"}
                target = suggestion["agent"] if choice in _YES or not choice else choice
                res = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                    "command": "save_idea_to_agent",
                    "agent": target,
                    "distilled": distilled,
                    "original_text": original_text,
                })
                if res.get("status") == "success":
                    await websocket.send_json({"type": "idea_routed", "ok": True, "agent": target, "message": res.get("data")})
                else:
                    await websocket.send_json({"type": "idea_routed", "ok": False, "error": res.get("message")})
        return True

    elif msg_type == "get_ideas":
        if selene:
            res = selene.tool_router.route_and_execute("manifest_manager", {"command": "get_ideas"})
            await websocket.send_json({"type": "ideas_list", "data": res.get("data")})
        return True

    elif msg_type == "promote_idea":
        if selene:
            res = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                "command": "promote_idea", "idea_id": data.get("idea_id", ""),
            })
            if res.get("status") == "success":
                await websocket.send_json({"type": "idea_promoted", "ok": True, "message": res.get("data")})
                await _broadcast_manifest(selene)
            else:
                await websocket.send_json({"type": "idea_promoted", "ok": False, "error": res.get("message")})
        return True

    elif msg_type == "write_agent_manifest":
        if selene:
            target = data.get("agent", "").strip().lower()
            tasks  = data.get("tasks", [])
            ideas  = data.get("ideas", None)
            if not target:
                await websocket.send_json({"type": "error", "message": "write_agent_manifest requires 'agent' slug."})
            else:
                try:
                    res = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                        "command": "write_agent_manifest",
                        "agent":   target,
                        "tasks":   tasks,
                        "ideas":   ideas,
                    })
                    ok  = "failed" not in str(res.get("data", "")).lower() and res.get("status") == "success"
                    msg = res.get("data") or res.get("message", "")
                    await websocket.send_json({"type": "agent_manifest_written", "ok": ok, "agent": target, "message": msg})
                except Exception as ex:
                    await websocket.send_json({"type": "error", "message": str(ex)})
        else:
            pass  # server still initialising — frontend will retry on "ready" event
        return True

    elif msg_type == "reorganize_agent_manifest":
        if selene:
            target = data.get("agent", "").strip().lower()
            if not target:
                await websocket.send_json({"type": "error", "message": "reorganize_agent_manifest requires 'agent' slug."})
            else:
                await websocket.send_json({"type": "thinking"})
                res = await loop.run_in_executor(None, selene.tool_router.route_and_execute, "manifest_manager", {
                    "command": "reorganize_agent_manifest",
                    "agent":   target,
                    "prompt":  data.get("prompt", ""),
                })
                if res.get("status") == "success":
                    await websocket.send_json({"type": "response", "content": res.get("data")})
                else:
                    await websocket.send_json({"type": "error", "message": res.get("message")})
        else:
            pass  # server still initialising — frontend will retry on "ready" event
        return True


    return False
