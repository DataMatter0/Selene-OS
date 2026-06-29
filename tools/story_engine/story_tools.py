import uuid
import time
import json
import logging
from ..schema import BaseTool
from .db_helper import get_db_connection

logger = logging.getLogger(__name__)

class StoryAddLocationTool(BaseTool):
    """Adds a new location to the persistent world manifest."""
    name = "story_add_location"
    description = (
        "Adds a new, explored location to the active world. "
        "Inputs must be JSON containing:\n"
        "- world_id: The ID of the active world\n"
        "- name: The unique name of the location\n"
        "- description: A detailed narrative description of the area\n"
        "- is_hub: Optional boolean (1 = Hub, 0 = Minor area, defaults to 0)"
    )
    input_type = "json"
    output_type = "text"

    def __init__(self, agent_state):
        self.agent_state = agent_state

    def execute(self, input_data: dict) -> str:
        world_id = input_data.get("world_id")
        name = input_data.get("name")
        description = input_data.get("description")
        is_hub = int(input_data.get("is_hub", 0))

        if not world_id or not name or not description:
            return "[story_add_location] Error: 'world_id', 'name', and 'description' are required fields."

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # 1 Major event limit per area unless hub
            # Check if there is already a location with same name
            loc_id = f"loc_{int(time.time())}_{uuid.uuid4().hex[:4]}"
            cursor.execute(
                "INSERT INTO locations (id, world_id, name, description, is_hub, is_explored) VALUES (?, ?, ?, ?, ?, 1)",
                (loc_id, world_id, name, description, is_hub)
            )
            conn.commit()
            return f"Location '{name}' added successfully under ID {loc_id}."
        except Exception as e:
            conn.rollback()
            return f"[story_add_location] DB Error: {e}"
        finally:
            conn.close()


class StoryAddNPCTool(BaseTool):
    """Generates minor character cards and populates a location with NPCs."""
    name = "story_add_npc"
    description = (
        "Adds a new NPC character card to a location. "
        "Inputs must be JSON containing:\n"
        "- world_id: The ID of the active world\n"
        "- location_id: The ID of the location where the NPC resides\n"
        "- name: The NPC's name\n"
        "- description: A 1-2 sentence character card describing their appearance and personality\n"
        "- psycho_profile: Optional narrative psychoanalytical description"
    )
    input_type = "json"
    output_type = "text"

    def __init__(self, agent_state):
        self.agent_state = agent_state

    def execute(self, input_data: dict) -> str:
        world_id = input_data.get("world_id")
        location_id = input_data.get("location_id")
        name = input_data.get("name")
        description = input_data.get("description")
        psycho_profile = input_data.get("psycho_profile", "Normal demeanor.")

        if not world_id or not name or not description:
            return "[story_add_npc] Error: 'world_id', 'name', and 'description' are required."

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # Check limit: maximum of 5 NPCs per location to prevent narrative bloat
            if location_id:
                cursor.execute("SELECT COUNT(*) FROM npcs WHERE current_location_id = ? AND is_active = 1", (location_id,))
                count = cursor.fetchone()[0]
                if count >= 5:
                    return f"[story_add_npc] Error: Location {location_id} has reached its maximum of 5 active NPCs."

            npc_id = f"npc_{int(time.time())}_{uuid.uuid4().hex[:4]}"
            cursor.execute(
                "INSERT INTO npcs (id, world_id, current_location_id, name, description, psycho_profile, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
                (npc_id, world_id, location_id, name, description, psycho_profile)
            )
            conn.commit()
            return f"NPC '{name}' successfully created at location {location_id} with ID {npc_id}."
        except Exception as e:
            conn.rollback()
            return f"[story_add_npc] DB Error: {e}"
        finally:
            conn.close()


class StoryTriggerEventTool(BaseTool):
    """Triggers dynamic events around the player or at locations."""
    name = "story_trigger_event"
    description = (
        "Triggers a dynamic narrative event at a location. "
        "Inputs must be JSON containing:\n"
        "- world_id: The active world ID\n"
        "- location_id: The location ID where the event occurs\n"
        "- name: Event title\n"
        "- description: Action-packed narrative of the event\n"
        "- is_major: 1 if it is a major roadmap event, 0 if minor encounter"
    )
    input_type = "json"
    output_type = "text"

    def __init__(self, agent_state):
        self.agent_state = agent_state

    def execute(self, input_data: dict) -> str:
        world_id = input_data.get("world_id")
        location_id = input_data.get("location_id")
        name = input_data.get("name")
        description = input_data.get("description")
        is_major = int(input_data.get("is_major", 0))

        if not world_id or not location_id or not name or not description:
            return "[story_trigger_event] Error: All fields are required."

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # 1 Major event limit per area unless Hub
            cursor.execute("SELECT is_hub FROM locations WHERE id = ?", (location_id,))
            loc = cursor.fetchone()
            if loc and not loc["is_hub"] and is_major:
                cursor.execute(
                    "SELECT COUNT(*) FROM events WHERE location_id = ? AND is_major = 1 AND status = 'completed'",
                    (location_id,)
                )
                completed_majors = cursor.fetchone()[0]
                if completed_majors >= 1:
                    return f"[story_trigger_event] Error: Area {location_id} is not a Hub and already completed its 1 major event."

            event_id = f"ev_{int(time.time())}_{uuid.uuid4().hex[:4]}"
            cursor.execute(
                "INSERT INTO events (id, world_id, location_id, name, description, is_major, status, turn_spacing) VALUES (?, ?, ?, ?, ?, ?, 'active', 3)",
                (event_id, world_id, location_id, name, description, is_major)
            )
            conn.commit()
            return f"Event '{name}' successfully triggered and marked active in area {location_id}."
        except Exception as e:
            conn.rollback()
            return f"[story_trigger_event] DB Error: {e}"
        finally:
            conn.close()


class StoryToggleCardTool(BaseTool):
    """Enables or disables cards (gear, skills) if they become inactive/lost."""
    name = "story_toggle_card"
    description = (
        "Toggles the active state of a skill or gear card (e.g. disabling broken gear or exhausted skills). "
        "Inputs must be JSON containing:\n"
        "- card_id: The target card ID\n"
        "- is_active: 1 to activate, 0 to disable"
    )
    input_type = "json"
    output_type = "text"

    def __init__(self, agent_state):
        self.agent_state = agent_state

    def execute(self, input_data: dict) -> str:
        card_id = input_data.get("card_id")
        is_active = int(input_data.get("is_active", 1))

        if not card_id:
            return "[story_toggle_card] Error: 'card_id' is required."

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE cards SET is_active = ? WHERE id = ?", (is_active, card_id))
            conn.commit()
            status_str = "activated" if is_active else "disabled"
            return f"Card {card_id} successfully {status_str}."
        except Exception as e:
            conn.rollback()
            return f"[story_toggle_card] DB Error: {e}"
        finally:
            conn.close()


class StoryOpenMerchantTool(BaseTool):
    """Prompts DM to open merchant inventory. Triggers procedural generation."""
    name = "story_open_merchant"
    description = (
        "Instructs the DM to generate merchant shop inventory. "
        "Inputs must be JSON containing:\n"
        "- location_name: The name of the safe zone/hub\n"
        "- world_level: The difficulty level of the active world (1-10)"
    )
    input_type = "json"
    output_type = "any"

    def __init__(self, agent_state):
        self.agent_state = agent_state

    def execute(self, input_data: dict) -> dict:
        location_name = input_data.get("location_name", "Wilderness Hub")
        world_level = int(input_data.get("world_level", 1))

        # Import dynamically from core engine to prevent circular import locks
        from pantheon_brain.story_engine import InfiniteStoryEngine
        engine = InfiniteStoryEngine()
        
        try:
            inventory = engine.generate_merchant_shop(location_name, world_level)
            return {"status": "success", "location": location_name, "inventory": inventory}
        except Exception as e:
            return {"status": "error", "message": str(e)}
