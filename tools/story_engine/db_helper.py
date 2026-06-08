import os
import sqlite3
import logging

logger = logging.getLogger(__name__)

# Base project directories
_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STORY_ENGINE_DIR = os.path.join(_SCRIPT_DIR, "memories", "story_engine")
DB_PATH = os.path.join(STORY_ENGINE_DIR, "story_engine.db")

def get_db_connection():
    """Establish and return a connection to the SQLite database."""
    os.makedirs(STORY_ENGINE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def initialize_database():
    """Initializes all required tables in the SQLite database and loads default profiles."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # 1. Worlds table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS worlds (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            world_level INTEGER DEFAULT 1 CHECK(world_level BETWEEN 1 AND 10),
            origin_details TEXT NOT NULL,
            long_term_elements TEXT NOT NULL,
            world_details TEXT DEFAULT '',
            ambient_elements TEXT DEFAULT '',
            chronological_milestones TEXT DEFAULT '',
            major_goal TEXT NOT NULL,
            roadmap_json TEXT,
            created_at REAL NOT NULL,
            last_saved_at REAL NOT NULL
        );
        """)

        # 2. Player Profiles table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            name TEXT PRIMARY KEY,
            profile_type TEXT CHECK(profile_type IN ('human', 'ai_companion', 'ai_fillin')),
            created_at REAL NOT NULL
        );
        """)

        # 3. Presets table (Character & World configs)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS presets (
            id TEXT PRIMARY KEY,
            preset_type TEXT CHECK(preset_type IN ('character', 'world')),
            name TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        """)

        # 4. Player Characters (Strictly locked once created, 3 active limit per profile)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS characters (
            id TEXT PRIMARY KEY,
            world_id TEXT REFERENCES worlds(id) ON DELETE SET NULL,
            profile_name TEXT REFERENCES profiles(name) ON DELETE CASCADE,
            name TEXT NOT NULL,
            char_class TEXT DEFAULT 'Adventurer',
            level_label TEXT DEFAULT 'Level',
            points_label TEXT DEFAULT 'Points',
            level INTEGER DEFAULT 1,
            points INTEGER DEFAULT 0,
            stat_1_name TEXT DEFAULT 'Strength',
            stat_1_val INTEGER DEFAULT 10,
            stat_2_name TEXT DEFAULT 'Dexterity',
            stat_2_val INTEGER DEFAULT 10,
            stat_3_name TEXT DEFAULT 'Constitution',
            stat_3_val INTEGER DEFAULT 10,
            stat_4_name TEXT DEFAULT 'Intelligence',
            stat_4_val INTEGER DEFAULT 10,
            stat_5_name TEXT DEFAULT 'Wisdom',
            stat_5_val INTEGER DEFAULT 10,
            bonus_trait_points INTEGER DEFAULT 0,
            current_hp INTEGER DEFAULT 100,
            max_hp INTEGER DEFAULT 100,
            current_mp INTEGER DEFAULT 50,
            max_mp INTEGER DEFAULT 50,
            profile_flavor TEXT NOT NULL,
            is_active INTEGER DEFAULT 1
        );
        """)

        # 5. Cards (Equipment & Skills)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS cards (
            id TEXT PRIMARY KEY,
            character_id TEXT REFERENCES characters(id) ON DELETE CASCADE,
            card_type TEXT CHECK(card_type IN ('skill', 'gear')),
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            mp_cost INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1
        );
        """)

        # 6. Locations table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS locations (
            id TEXT PRIMARY KEY,
            world_id TEXT REFERENCES worlds(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            is_hub INTEGER DEFAULT 0,
            is_explored INTEGER DEFAULT 0
        );
        """)

        # 7. NPCs table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS npcs (
            id TEXT PRIMARY KEY,
            world_id TEXT REFERENCES worlds(id) ON DELETE CASCADE,
            current_location_id TEXT REFERENCES locations(id) ON DELETE SET NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            psycho_profile TEXT,
            is_active INTEGER DEFAULT 1
        );
        """)

        # 8. Dynamic Events table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            world_id TEXT REFERENCES worlds(id) ON DELETE CASCADE,
            location_id TEXT REFERENCES locations(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            is_major INTEGER DEFAULT 0,
            status TEXT CHECK(status IN ('pending', 'active', 'completed', 'failed')),
            turn_spacing INTEGER DEFAULT 3
        );
        """)

        # 9. Narrative Manifest Log
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS manifest_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            world_id TEXT REFERENCES worlds(id) ON DELETE CASCADE,
            turn_number INTEGER NOT NULL,
            speaker TEXT NOT NULL,
            action_type TEXT CHECK(action_type IN ('speak', 'act', 'observe', 'dice_roll')),
            content TEXT NOT NULL,
            dice_roll_details TEXT,
            timestamp REAL NOT NULL
        );
        """)

        # Load default profiles if they do not exist
        import time
        now = time.time()
        cursor.execute("INSERT OR IGNORE INTO profiles (name, profile_type, created_at) VALUES ('Ghost', 'human', ?)", (now,))
        cursor.execute("INSERT OR IGNORE INTO profiles (name, profile_type, created_at) VALUES ('Selene', 'ai_companion', ?)", (now,))
        cursor.execute("INSERT OR IGNORE INTO profiles (name, profile_type, created_at) VALUES ('Recruit', 'ai_fillin', ?)", (now,))

        # Upgrade columns for existing databases dynamically
        try:
            cursor.execute("ALTER TABLE characters ADD COLUMN char_class TEXT DEFAULT 'Adventurer';")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE characters ADD COLUMN level_label TEXT DEFAULT 'Level';")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE characters ADD COLUMN points_label TEXT DEFAULT 'Points';")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE worlds ADD COLUMN world_details TEXT DEFAULT '';")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE worlds ADD COLUMN ambient_elements TEXT DEFAULT '';")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE worlds ADD COLUMN chronological_milestones TEXT DEFAULT '';")
        except sqlite3.OperationalError:
            pass

        conn.commit()
        logger.info("[Story DB]: Database successfully initialized and migrated.")
    except Exception as e:
        conn.rollback()
        logger.error(f"[Story DB]: Database initialization failed: {e}")
        raise e
    finally:
        conn.close()

if __name__ == "__main__":
    initialize_database()
