from .schema import BaseTool
from typing import Any, Dict, List, Optional
import time
import uuid
import os
import json

class ScheduleTool(BaseTool):
    """A tool for managing alarms, timers, and reminders."""
    name = "schedule_manager"
    description = (
        "Manages time-based reminders and alarms for the user. "
        "Commands:\n"
        "- set_timer: {\"command\": \"set_timer\", \"duration_minutes\": 5, \"message\": \"Check the pizza\"}\n"
        "- set_alarm: {\"command\": \"set_alarm\", \"time\": \"14:30\", \"message\": \"Meeting\"} (24hr time)\n"
        "- list_timers: {\"command\": \"list_timers\"}\n"
        "- delete_timer: {\"command\": \"delete_timer\", \"id\": \"...\"}"
    )
    input_type = "json"
    output_type = "any"

    def __init__(self, agent_state: Any):
        self.agent_state = agent_state

    @property
    def state_file(self) -> str:
        return os.path.join(self.agent_state.MEMORY_DIR, "schedule_state.json")

    def load_state(self) -> dict:
        if not os.path.exists(self.state_file):
            return {"timers": []}
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {"timers": []}

    def save_state(self, state: dict) -> None:
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            print(f"[ScheduleTool] Failed to save state: {e}")

    def check_and_trigger(self, user_input: str) -> Optional[Dict[str, Any]]:
        normalized = user_input.lower()
        if "set a timer for" in normalized or "remind me in" in normalized:
            # We let the LLM extract the exact duration if we just return a partial or let it route naturally
            return None
        return None

    def execute(self, input_data: Dict[str, Any]) -> Any:
        command = input_data.get("command")
        state = self.load_state()
        
        if command == "set_timer":
            mins = float(input_data.get("duration_minutes", 0))
            msg = input_data.get("message", "Timer")
            if mins <= 0:
                return "Duration must be greater than 0."
            
            trigger_time = time.time() + (mins * 60)
            timer_id = str(uuid.uuid4())[:8]
            
            state["timers"].append({
                "id": timer_id,
                "type": "timer",
                "trigger_time": trigger_time,
                "message": msg,
                "created_at": time.time()
            })
            self.save_state(state)
            return f"Timer set for {mins} minutes. ID: {timer_id}"

        elif command == "set_alarm":
            time_str = input_data.get("time", "")
            msg = input_data.get("message", "Alarm")
            
            try:
                import datetime
                now = datetime.datetime.now()
                h, m = map(int, time_str.split(":"))
                target = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if target < now:
                    target += datetime.timedelta(days=1)
                
                trigger_time = target.timestamp()
                timer_id = str(uuid.uuid4())[:8]
                
                state["timers"].append({
                    "id": timer_id,
                    "type": "alarm",
                    "trigger_time": trigger_time,
                    "message": msg,
                    "created_at": time.time()
                })
                self.save_state(state)
                return f"Alarm set for {time_str}. ID: {timer_id}"
            except Exception as e:
                return f"Failed to set alarm. Format must be HH:MM (24-hour). Error: {e}"

        elif command == "list_timers":
            now = time.time()
            active = [t for t in state["timers"] if t["trigger_time"] > now]
            if not active:
                return "No active timers or alarms."
            
            res = []
            for t in active:
                rem = (t["trigger_time"] - now) / 60.0
                res.append(f"[{t['id']}] {t['message']} (triggers in {rem:.1f} minutes)")
            return "\n".join(res)

        elif command == "delete_timer":
            tid = input_data.get("id", "")
            initial = len(state["timers"])
            state["timers"] = [t for t in state["timers"] if t["id"] != tid]
            if len(state["timers"]) < initial:
                self.save_state(state)
                return f"Timer {tid} deleted."
            return f"Timer {tid} not found."

        return f"Unknown command for schedule_manager: {command}"
