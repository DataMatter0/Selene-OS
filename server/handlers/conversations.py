"""
server/handlers/conversations.py — Conversation lifecycle management
"""

from server.state import get_state
import server.state as _st


async def handle(websocket, data: dict, loop) -> bool:
    msg_type = data.get("type")
    selene   = _st.selene_ref

    if msg_type == "new_conversation":
        if selene:
            conv_info = await loop.run_in_executor(None, selene.new_conversation)
            await websocket.send_json({
                "type": "conversation_loaded",
                "id": conv_info["id"], "name": conv_info["name"], "messages": [],
            })
            await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})
        await websocket.send_json({"type": "state", "data": get_state()})
        return True

    elif msg_type == "load_conversation":
        conv_id = data.get("id", "").strip()
        if selene and conv_id:
            result = await loop.run_in_executor(None, selene.load_conversation, conv_id)
            if result:
                await websocket.send_json({
                    "type": "conversation_loaded",
                    "id": result["id"], "name": result["name"], "messages": result["messages"],
                })
                await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})
                await websocket.send_json({"type": "state", "data": get_state()})
            else:
                await websocket.send_json({"type": "error", "message": f"Conversation not found: {conv_id}"})
        return True

    elif msg_type == "rename_conversation":
        conv_id  = data.get("id", "").strip()
        new_name = data.get("name", "").strip()
        if selene and conv_id and new_name:
            ok = selene.rename_conversation(conv_id, new_name)
            if ok:
                await websocket.send_json({"type": "conversation_renamed", "id": conv_id, "name": new_name})
                await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})
        return True

    elif msg_type == "list_conversations":
        if selene:
            await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})
        return True

    elif msg_type == "delete_conversation":
        conv_id = data.get("id", "").strip()
        if selene and conv_id:
            ok = selene.delete_conversation(conv_id)
            await websocket.send_json({"type": "conversation_deleted", "id": conv_id, "ok": ok})
            if ok and selene.active_conversation_id is None:
                conv_info = await loop.run_in_executor(None, selene.new_conversation)
                await websocket.send_json({
                    "type": "conversation_loaded",
                    "id": conv_info["id"], "name": conv_info["name"], "messages": [],
                })
            await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})
            await websocket.send_json({"type": "state", "data": get_state()})
        return True

    return False
