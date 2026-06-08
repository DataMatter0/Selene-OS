# selene_brain/conversation_manager.py
import os
import json
import uuid
import time
from typing import Optional, TYPE_CHECKING
import threading

class ConversationManagerMixin:
    if TYPE_CHECKING:
        CONVERSATIONS_DIR: str
        active_conversation_id: Optional[str]
        working_memory: list
        active_conversation_name: str
        lock: threading.RLock
    def _conv_path(self, conv_id: str) -> str:
        return os.path.join(self.CONVERSATIONS_DIR, f"{conv_id}.json")

    def _write_conversation(self, conv_id: str, name: str, messages: list,
                            created_at: float, updated_at: float) -> None:
        try:
            with open(self._conv_path(conv_id), 'w', encoding='utf-8') as f:
                json.dump({
                    "id":         conv_id,
                    "name":       name,
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "messages":   messages,
                }, f, indent=2, ensure_ascii=False)
        except IOError as e:
            print(f"[System Error]: Could not write conversation '{conv_id}' — {e}")

    def new_conversation(self) -> dict:
        """Start a fresh conversation. The file is only written after the first save."""
        if self.active_conversation_id and self.working_memory:
            self.save_current_conversation()
        conv_id = str(uuid.uuid4())
        self.active_conversation_id   = conv_id
        self.active_conversation_name = "New Conversation"
        with self.lock:
            self.working_memory = []
        print(f"[System]: New conversation started — {conv_id[:8]}")
        return {"id": conv_id, "name": "New Conversation"}

    def save_current_conversation(self) -> None:
        """Persist the active conversation's working_memory to disk."""
        if not self.active_conversation_id:
            return
        with self.lock:
            messages = list(self.working_memory)
        if not messages:
            return   # don't create empty files
        conv_path = self._conv_path(self.active_conversation_id)
        now = time.time()
        created_at = now
        if os.path.exists(conv_path):
            try:
                with open(conv_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
                created_at = existing.get("created_at", now)
            except Exception:
                pass
        self._write_conversation(
            self.active_conversation_id,
            self.active_conversation_name,
            messages, created_at, now,
        )

    def list_conversations(self) -> list:
        """Return conversation summaries sorted by most-recently updated."""
        convs = []
        try:
            for fname in os.listdir(self.CONVERSATIONS_DIR):
                if not fname.endswith('.json'):
                    continue
                fpath = os.path.join(self.CONVERSATIONS_DIR, fname)
                try:
                    with open(fpath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    msgs = data.get("messages", [])
                    convs.append({
                        "id":            data.get("id", fname[:-5]),
                        "name":          data.get("name", "Untitled"),
                        "created_at":    data.get("created_at", 0),
                        "updated_at":    data.get("updated_at", 0),
                        "message_count": len(msgs) // 2,
                    })
                except Exception:
                    pass
        except Exception as e:
            print(f"[System Error]: Could not list conversations — {e}")
        return sorted(convs, key=lambda c: c["updated_at"], reverse=True)

    def load_conversation(self, conv_id: str) -> Optional[dict]:
        """Load a conversation into working_memory. Returns info dict or None."""
        conv_path = self._conv_path(conv_id)
        if not os.path.exists(conv_path):
            return None
        try:
            # Save whatever is in memory first
            if self.active_conversation_id and self.active_conversation_id != conv_id:
                self.save_current_conversation()
            with open(conv_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            with self.lock:
                self.working_memory = data.get("messages", [])
            self.active_conversation_id   = data.get("id", conv_id)
            self.active_conversation_name = data.get("name", "Untitled")
            print(f"[System]: Loaded conversation '{self.active_conversation_name}'")
            return {
                "id":       self.active_conversation_id,
                "name":     self.active_conversation_name,
                "messages": list(self.working_memory),
            }
        except Exception as e:
            print(f"[System Error]: Could not load conversation {conv_id} — {e}")
            return None

    def rename_conversation(self, conv_id: str, name: str) -> bool:
        """Rename a conversation on disk. Returns True on success."""
        conv_path = self._conv_path(conv_id)
        # Update in memory if it's the active one
        if self.active_conversation_id == conv_id:
            self.active_conversation_name = name
        if not os.path.exists(conv_path):
            # May not be written yet — that's OK, name is already updated in memory
            return True
        try:
            with open(conv_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data["name"] = name
            with open(conv_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"[System Error]: Could not rename conversation — {e}")
            return False

    def delete_conversation(self, conv_id: str) -> bool:
        """Delete a conversation file. If it was active, clears working memory."""
        conv_path = self._conv_path(conv_id)
        was_active = (self.active_conversation_id == conv_id)
        try:
            if os.path.exists(conv_path):
                os.remove(conv_path)
            if was_active:
                self.active_conversation_id   = None
                self.active_conversation_name = "New Conversation"
                with self.lock:
                    self.working_memory = []
            print(f"[System]: Deleted conversation {conv_id[:8]}")
            return True
        except Exception as e:
            print(f"[System Error]: Could not delete conversation {conv_id} — {e}")
            return False

    def rollback_last_turn(self) -> bool:
        """Remove the last user+assistant pair from working_memory.
        Called before a reprompt so the edited message starts fresh."""
        with self.lock:
            if len(self.working_memory) >= 2:
                self.working_memory = self.working_memory[:-2]
                return True
        return False

    def auto_name_from_message(self, message: str) -> str:
        """Generate a short conversation name from the first user message."""
        cleaned = message.strip()
        if len(cleaned) <= 42:
            return cleaned
        truncated = cleaned[:42]
        last_space = truncated.rfind(' ')
        if last_space > 18:
            return truncated[:last_space] + "…"
        return truncated[:42] + "…"
