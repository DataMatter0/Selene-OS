# selene_brain/tests/test_story_engine.py
import unittest
import os
import tempfile
import shutil
import sqlite3
import random

# Monkeypatch DB_PATH before importing db_helper and story_engine to isolate unit tests
import tools.story_engine.db_helper as db_helper
TEMP_TEST_DIR = tempfile.mkdtemp()
TEST_DB_PATH = os.path.join(TEMP_TEST_DIR, "test_story_engine.db")
db_helper.STORY_ENGINE_DIR = TEMP_TEST_DIR
db_helper.DB_PATH = TEST_DB_PATH

from tools.story_engine.db_helper import initialize_database, get_db_connection
from selene_brain.story_engine import InfiniteStoryEngine

class TestInfiniteStoryEngine(unittest.TestCase):
    
    @classmethod
    def setUpClass(cls):
        # Initialize database tables once for testing
        initialize_database()
        cls.engine = InfiniteStoryEngine()

    @classmethod
    def tearDownClass(cls):
        # Cleanup temporary directory
        shutil.rmtree(TEMP_TEST_DIR)

    def setUp(self):
        # Clear specific tables before each test to maintain clean state
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cards;")
        cursor.execute("DELETE FROM characters;")
        cursor.execute("DELETE FROM worlds;")
        cursor.execute("DELETE FROM manifest_log;")
        conn.commit()
        conn.close()

    def test_default_profiles_exist(self):
        """Verify that default profiles (Ghost, Selene, Recruit) are initialized."""
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, profile_type FROM profiles ORDER BY name;")
        profiles = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        self.assertEqual(len(profiles), 3)
        self.assertEqual(profiles[0]["name"], "Ghost")
        self.assertEqual(profiles[0]["profile_type"], "human")
        self.assertEqual(profiles[1]["name"], "Recruit")
        self.assertEqual(profiles[1]["profile_type"], "ai_fillin")
        self.assertEqual(profiles[2]["name"], "Selene")
        self.assertEqual(profiles[2]["profile_type"], "ai_companion")

    def test_character_creation_and_limit(self):
        """Verify locked character creation, HP math, and strict 3-character limit per profile."""
        profile = "Ghost"
        stats = {
            "Strength": 12,
            "Dexterity": 10,
            "Constitution": 14,
            "Intelligence": 10,
            "Wisdom": 8
        }
        
        # Test character 1 creation
        char1 = self.engine.create_character(profile, "Roland", stats, "Iron sword, leather armor")
        self.assertIsNotNone(char1["id"])
        self.assertEqual(char1["name"], "Roland")
        
        # Constitution is 14. Max HP formula: (Con * 5) + 50 => (14 * 5) + 50 = 120
        self.assertEqual(char1["max_hp"], 120)
        self.assertEqual(char1["max_mp"], 50)
        
        # Check database records
        char_data = self.engine.get_character(char1["id"])
        self.assertEqual(char_data["name"], "Roland")
        self.assertEqual(char_data["stat_1_name"], "Strength")
        self.assertEqual(char_data["stat_1_val"], 12)
        self.assertEqual(char_data["stat_3_name"], "Constitution")
        self.assertEqual(char_data["stat_3_val"], 14)
        
        # Verify card creation
        self.assertEqual(len(char_data["cards"]), 1)
        self.assertEqual(char_data["cards"][0]["card_type"], "gear")
        self.assertEqual(char_data["cards"][0]["description"], "Iron sword, leather armor")
        
        # Test character 2 creation
        char2 = self.engine.create_character(profile, "Eldritch Scholar", stats, "Spellbook")
        self.assertIsNotNone(char2["id"])
        
        # Test character 3 creation
        char3 = self.engine.create_character(profile, "Swift Rogue", stats, "Daggers")
        self.assertIsNotNone(char3["id"])
        
        # Verify 3 character limit is hit, creating a 4th must raise ValueError
        with self.assertRaises(ValueError):
            self.engine.create_character(profile, "Cheater Warrior", stats, "Greatsword")

    def test_custom_character_properties(self):
        """Verify custom character class and dynamic vocabulary labels (level_label, points_label) are stored and fetched correctly."""
        profile = "Selene"
        stats = {"Strength": 10, "Dexterity": 10, "Constitution": 10, "Intelligence": 10, "Wisdom": 10}
        char = self.engine.create_character(
            profile, "Occultist Scholar", stats, "Occult books",
            char_class="Mystic Scholar", level_label="Tier", points_label="Milestones"
        )
        
        char_data = self.engine.get_character(char["id"])
        self.assertEqual(char_data["char_class"], "Mystic Scholar")
        self.assertEqual(char_data["level_label"], "Tier")
        self.assertEqual(char_data["points_label"], "Milestones")

    def test_d20_resolution_math(self):
        """Verify D20 rolls calculation, world floors, stat mods, and combat damage margins."""
        # Create a world to establish floor
        conn = get_db_connection()
        cursor = conn.cursor()
        world_id = "world_test_1"
        cursor.execute("""
        INSERT INTO worlds (id, name, world_level, origin_details, long_term_elements, major_goal, created_at, last_saved_at)
        VALUES (?, 'Rust Shallows', 6, 'Origin Outpost', 'Heavy winds', 'Destroy Baron', ?, ?)
        """, (world_id, 1.0, 1.0))
        conn.commit()
        conn.close()
        
        # Create character bound to this world
        stats = {"Strength": 14, "Dexterity": 10, "Constitution": 10, "Intelligence": 10, "Wisdom": 10}
        char = self.engine.create_character("Ghost", "Fighter", stats, "Fists")
        
        # Manually assign character's world_id to test world floor
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE characters SET world_id = ? WHERE id = ?", (world_id, char["id"]))
        conn.commit()
        conn.close()
        
        # We test resolve_dice_action with seeded randoms to verify math
        # Formula: Final Roll = D20 Base + Stat Bonus + Level Diff - Penalty
        # Base roll is randomized, so we mock random.randint to return 10
        original_randint = random.randint
        try:
            random.randint = lambda a, b: 10 if (a == 1 and b == 20) else original_randint(a, b)
            
            # Opponent is Level 1, Player is Level 1 (Diff = 0)
            # Strength is 14 (Bonus = 14 - 10 = 4)
            # Final roll = 10 + 4 + 0 - 0 = 14
            # Floor is 6 (World Level). 14 >= 6 => Success!
            # Defense Target (Opponent Level) is 1. Combat damage = 14 - 1 = 13
            result = self.engine.resolve_dice_action(char["id"], "Strength", target_defense=1)
            self.assertEqual(result["base_roll"], 10)
            self.assertEqual(result["stat_used"], "Strength")
            self.assertEqual(result["stat_bonus"], 4)
            self.assertEqual(result["final_roll"], 14)
            self.assertTrue(result["success"])
            self.assertEqual(result["damage_dealt"], 13)
            
            # Test level penalty
            # Opponent is Level 3 (Diff = 1 - 3 = -2)
            # Final roll = 10 + 4 - 2 = 12
            result2 = self.engine.resolve_dice_action(char["id"], "Strength", target_defense=3, diff_penalty=0)
            self.assertEqual(result2["final_roll"], 12)
            self.assertEqual(result2["level_diff"], -2)
            
        finally:
            random.randint = original_randint

    def test_progression_and_level_up(self):
        """Verify unified XP/Currency points, leveling mechanics, costs scale, and stat boosts."""
        # Create character
        stats = {"Strength": 10, "Dexterity": 10, "Constitution": 10, "Intelligence": 10, "Wisdom": 10}
        char = self.engine.create_character("Ghost", "Hero", stats, "Cape")
        char_id = char["id"]
        
        # Test insufficient points
        res = self.engine.spend_points_level_up(char_id, "Strength")
        self.assertEqual(res["status"], "error")
        self.assertIn("Insufficient points", res["message"])
        
        # Grant points to level up (Level 1 to 2 costs 1 * 3 = 3 points)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE characters SET points = 10 WHERE id = ?", (char_id,))
        conn.commit()
        conn.close()
        
        # Boost Strength
        res = self.engine.spend_points_level_up(char_id, "Strength")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["new_level"], 2)
        self.assertEqual(res["points_remaining"], 7) # 10 - 3 = 7
        self.assertEqual(res["boosted_stat"], "Strength")
        
        # Verify state in database
        updated_char = self.engine.get_character(char_id)
        self.assertEqual(updated_char["level"], 2)
        self.assertEqual(updated_char["stat_1_val"], 11)
        self.assertIn("capabilities have incrementally hardened", updated_char["profile_flavor"])
        
        # Level 2 to 3 costs 2 * 3 = 6 points. Points remaining is 7.
        res2 = self.engine.spend_points_level_up(char_id, "Strength")
        self.assertEqual(res2["status"], "success")
        self.assertEqual(res2["new_level"], 3)
        self.assertEqual(res2["points_remaining"], 1) # 7 - 6 = 1
        self.assertEqual(updated_char["stat_1_val"], 11) # Previously 11, now should be 12
        
        # Check DB to confirm stat is now 12
        self.assertEqual(self.engine.get_character(char_id)["stat_1_val"], 12)

    def test_procedural_merchant_shop(self):
        """Verify dynamic items generation and scale logic by World Level."""
        # World Level 1
        items_lvl1 = self.engine.generate_merchant_shop("Mud Outpost", 1)
        self.assertTrue(3 <= len(items_lvl1) <= 5)
        for item in items_lvl1:
            self.assertGreaterEqual(item["price"], 1)
            
        # World Level 10 should have higher prices than World Level 1
        items_lvl10 = self.engine.generate_merchant_shop("Endgame Spire", 10)
        
        # Map item names to compare
        lvl1_dict = {i["name"]: i["price"] for i in items_lvl1}
        lvl10_dict = {i["name"]: i["price"] for i in items_lvl10}
        
        # Ensure any overlapping item has higher price in level 10 than level 1
        for name in lvl1_dict:
            if name in lvl10_dict:
                self.assertGreater(lvl10_dict[name], lvl1_dict[name])

    def test_complete_world_goal(self):
        """Verify world cleared bonuses (2 trait points) award to characters."""
        # Create a world
        conn = get_db_connection()
        cursor = conn.cursor()
        world_id = "world_test_2"
        cursor.execute("""
        INSERT INTO worlds (id, name, world_level, origin_details, long_term_elements, major_goal, created_at, last_saved_at)
        VALUES (?, 'Neon Spire', 4, 'Bar', 'Decay', 'Assassinate Rust Baron', ?, ?)
        """, (world_id, 1.0, 1.0))
        conn.commit()
        conn.close()
        
        # Create character bound to this world
        stats = {"Strength": 10, "Dexterity": 10, "Constitution": 10, "Intelligence": 10, "Wisdom": 10}
        char = self.engine.create_character("Ghost", "WorldSavior", stats, "Sword")
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE characters SET world_id = ? WHERE id = ?", (world_id, char["id"]))
        conn.commit()
        conn.close()
        
        # Verify baseline bonus points is 0
        char_data = self.engine.get_character(char["id"])
        self.assertEqual(char_data["bonus_trait_points"], 0)
        
        # Complete goal
        res = self.engine.complete_world_goal(world_id)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["rewards"]["WorldSavior"], 2)
        
        # Verify database record
        char_data_after = self.engine.get_character(char["id"])
        self.assertEqual(char_data_after["bonus_trait_points"], 2)

if __name__ == "__main__":
    unittest.main()
