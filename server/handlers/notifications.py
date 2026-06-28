"""
server/handlers/notifications.py — Persistent, system-wide notification store.

Any agent or tool can call add_notification() to push a notification.
The UI subscribes via WS: bell icon shows unread count, panel shows full list.
"""

import json
import os
import time
import uuid
import server.state as _st

NOTIF_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "agents", "shared", "notifications.json"
)
NOTIF_PATH = os.path.normpath(NOTIF_PATH)


def _load() -> list:
    try:
        with open(NOTIF_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(notifications: list) -> None:
    os.makedirs(os.path.dirname(NOTIF_PATH), exist_ok=True)
    with open(NOTIF_PATH, "w", encoding="utf-8") as f:
        json.dump(notifications, f, indent=2, ensure_ascii=False)


def add_notification(title: str, body: str, page: "str | None" = None,
                     source_agent: str = "system") -> dict:
    """Write a notification and broadcast it to all connected clients."""
    notif = {
        "id":           str(uuid.uuid4()),
        "title":        title,
        "body":         body,
        "source_agent": source_agent,
        "page":         page,          # e.g. "board", "tools", "mem"
        "ts":           time.time(),
        "read":         False,
    }
    notifications = _load()
    notifications.insert(0, notif)
    # Keep max 200
    if len(notifications) > 200:
        notifications = notifications[:200]
    _save(notifications)

    # Broadcast to all WS clients
    import asyncio
    try:
        loop = _st.event_loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(
                _st.broadcast({"type": "notification", "data": notif,
                               "unread_count": sum(1 for n in notifications if not n["read"])}),
                loop
            )
    except Exception as e:
        print(f"[Notifications]: broadcast failed — {e}")

    return notif


async def handle(websocket, data: dict, loop) -> bool:
    msg_type = data.get("type")

    if msg_type == "get_notifications":
        notifications = _load()
        unread = sum(1 for n in notifications if not n["read"])
        await websocket.send_json({
            "type": "notifications_data",
            "data": notifications,
            "unread_count": unread,
        })
        return True

    elif msg_type == "mark_notification_read":
        notif_id = data.get("id", "").strip()
        notifications = _load()
        for n in notifications:
            if n["id"] == notif_id:
                n["read"] = True
                break
        _save(notifications)
        unread = sum(1 for n in notifications if not n["read"])
        await websocket.send_json({
            "type": "notifications_data",
            "data": notifications,
            "unread_count": unread,
        })
        return True

    elif msg_type == "mark_all_notifications_read":
        notifications = _load()
        for n in notifications:
            n["read"] = True
        _save(notifications)
        await websocket.send_json({
            "type": "notifications_data",
            "data": notifications,
            "unread_count": 0,
        })
        return True

    elif msg_type == "clear_notifications":
        _save([])
        await websocket.send_json({
            "type": "notifications_data",
            "data": [],
            "unread_count": 0,
        })
        return True

    return False
