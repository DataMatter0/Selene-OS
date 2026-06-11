"""
server/handlers/misc.py — Maps, Polymarket, Document/RuneReader, Notion, MetaInsight
"""

from server.state  import get_state
from server.tool_pipeline import update_memory_and_energy
import server.state as _st


async def handle(websocket, data: dict, loop) -> bool:
    msg_type = data.get("type")
    selene   = _st.selene_ref

    if msg_type == "maps_query":
        if selene:
            maps = selene.tool_router.tools.get("maps")
            if maps:
                await websocket.send_json({"type": "maps_thinking"})
                tool_input = data.get("input", {})
                res = await loop.run_in_executor(None, maps.execute, tool_input)
                await websocket.send_json({"type": "maps_result", "data": res})
            else:
                await websocket.send_json({"type": "maps_result", "data": {"error": "Maps tool not loaded."}})
        return True

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
        return True

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
                        user_msg     = res.get("user_message", "")
                        resp_content = res.get("response", "")
                        session_id   = selene.active_conversation_id or "default"
                        await loop.run_in_executor(None, selene.db.log_dialog, session_id, "user",      user_msg,     "", "read")
                        await loop.run_in_executor(None, selene.db.log_dialog, session_id, "assistant", resp_content, "[RuneReader Analysis Synthesis]", "read")
                        update_memory_and_energy(user_msg, resp_content)
                        await loop.run_in_executor(None, selene.save_current_conversation)
                        await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})
                        await websocket.send_json({"type": "response", "content": resp_content})
                        await websocket.send_json({"type": "state",    "data": get_state()})
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
                    await websocket.send_json({
                        "type": "document_result",
                        "data": {"error": "Document tool not loaded. Run: pip install pymupdf pymupdf4llm"},
                    })
        return True

    elif msg_type == "notion_query":
        if selene:
            notion_tool = selene.tool_router.tools.get("notion")
            if notion_tool:
                await websocket.send_json({"type": "notion_thinking"})
                tool_input = {k: v for k, v in data.items() if k != "type"}
                res        = await loop.run_in_executor(None, notion_tool.execute, tool_input)
                cmd        = data.get("command", "")
                await websocket.send_json({"type": "notion_result", "command": cmd, "data": res})
            else:
                await websocket.send_json({"type": "notion_result", "data": {"error": "Notion tool not loaded."}})
        return True

    elif msg_type == "meta_insight_query":
        if selene:
            mi_tool = selene.tool_router.tools.get("meta_insight")
            if mi_tool:
                args   = {k: v for k, v in data.items() if k != "type"}
                result = await loop.run_in_executor(None, mi_tool.execute, args)
                await websocket.send_json({"type": "meta_insight_result", "data": result})
            else:
                await websocket.send_json({"type": "meta_insight_result", "data": {"status": "error", "message": "MetaInsight tool not loaded."}})
        else:
            await websocket.send_json({"type": "meta_insight_result", "data": {"status": "error", "message": "Selene not initialised."}})
        return True

    elif msg_type == "meta_insight_promote_card":
        if selene:
            entry_id  = data.get("entry_id")
            card_data = data.get("card")
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
                    from server.state import clients
                    for client in clients:
                        await client.send_json({"type": "knowledge_state", "data": state})
            await websocket.send_json({"type": "meta_insight_promoted", "ok": True})
        return True

    return False
