"""
server/handlers/story.py — Infinity Sim / Infinite Story Engine handlers
"""

import json
import os
import random
import time

import server.state as _st


async def handle(websocket, data: dict, loop) -> bool:
    msg_type = data.get("type")
    selene   = _st.selene_ref

    if not msg_type.startswith("story_"):
        return False

    if msg_type == "story_get_profiles":
        from tools.story_engine.db_helper import get_db_connection
        conn = get_db_connection()
        try:
            rows     = conn.execute("SELECT * FROM profiles ORDER BY created_at ASC").fetchall()
            profiles = [dict(r) for r in rows]
            await websocket.send_json({"type": "story_profiles", "profiles": profiles})
        finally:
            conn.close()
        return True

    elif msg_type == "story_add_profile":
        from tools.story_engine.db_helper import get_db_connection
        profile_name = data.get("name", "").strip()
        profile_type = data.get("profile_type", "human").strip()
        if profile_name:
            conn = get_db_connection()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO profiles (name, profile_type, created_at) VALUES (?, ?, ?)",
                    (profile_name, profile_type, time.time())
                )
                conn.commit()
                rows     = conn.execute("SELECT * FROM profiles ORDER BY created_at ASC").fetchall()
                profiles = [dict(r) for r in rows]
                await websocket.send_json({"type": "story_profiles", "profiles": profiles})
            except Exception as e:
                await websocket.send_json({"type": "error", "message": f"Failed to add profile: {e}"})
            finally:
                conn.close()
        return True

    elif msg_type == "story_get_presets":
        from tools.story_engine.db_helper import get_db_connection
        conn = get_db_connection()
        try:
            rows    = conn.execute("SELECT * FROM presets ORDER BY created_at ASC").fetchall()
            presets = [dict(r) for r in rows]
            await websocket.send_json({"type": "story_presets", "presets": presets})
        finally:
            conn.close()
        return True

    elif msg_type == "story_save_preset":
        from tools.story_engine.db_helper import get_db_connection
        preset_name = data.get("name", "").strip()
        preset_type = data.get("preset_type", "character").strip()
        data_json   = json.dumps(data.get("data_json", {}))
        if preset_name:
            conn = get_db_connection()
            try:
                preset_id = f"preset_{int(time.time())}_{random.randint(100, 999)}"
                conn.execute(
                    "INSERT INTO presets (id, preset_type, name, data_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (preset_id, preset_type, preset_name, data_json, time.time())
                )
                conn.commit()
                await websocket.send_json({"type": "story_preset_saved", "status": "success", "id": preset_id})
            except Exception as e:
                await websocket.send_json({"type": "error", "message": f"Failed to save preset: {e}"})
            finally:
                conn.close()
        return True

    elif msg_type == "story_get_characters":
        from tools.story_engine.db_helper import get_db_connection
        profile_name = data.get("profile_name", "").strip()
        if profile_name:
            conn = get_db_connection()
            try:
                rows  = conn.execute(
                    "SELECT * FROM characters WHERE profile_name = ? AND is_active = 1", (profile_name,)
                ).fetchall()
                chars = []
                for r in rows:
                    c    = dict(r)
                    c_id = c["id"]
                    cards = conn.execute(
                        "SELECT * FROM cards WHERE character_id = ? AND is_active = 1", (c_id,)
                    ).fetchall()
                    c["cards"] = [dict(cd) for cd in cards]
                    chars.append(c)
                await websocket.send_json({"type": "story_characters", "profile_name": profile_name, "characters": chars})
            finally:
                conn.close()
        return True

    elif msg_type == "story_create_character":
        from selene_brain.story_engine import InfiniteStoryEngine
        profile_name  = data.get("profile_name", "").strip()
        char_name     = data.get("name", "").strip()
        char_class    = data.get("char_class", "Adventurer").strip()
        level_label   = data.get("level_label", "Level").strip()
        points_label  = data.get("points_label", "Points").strip()
        stats         = data.get("stats", {})
        gear_desc     = data.get("gear_description", "").strip()
        engine        = InfiniteStoryEngine()
        try:
            char_info = engine.create_character(
                profile_name, char_name, stats, gear_desc,
                char_class=char_class, level_label=level_label, points_label=points_label,
            )
            await websocket.send_json({"type": "story_character_created", "status": "success", "character": char_info})
        except Exception as e:
            await websocket.send_json({"type": "error", "message": f"Failed to create character: {e}"})
        return True

    elif msg_type == "story_generate_random_character":
        if selene is None:
            return True
        prompt = data.get("prompt", "").strip()
        if not prompt:
            genres = [
                "dark fantasy grimdark",
                "cyberpunk high-tech low-life",
                "space opera futuristic explorer",
                "eldritch steampunk occult scholar",
                "post-apocalyptic scavenger survivor",
                "modern supernatural investigator",
            ]
            chosen_genre      = random.choice(genres)
            generation_prompt = f'You are an expert character creator for the Infinity Sim tabletop RPG. Generate a random fully structured character for the genre: "{chosen_genre}".'
        else:
            generation_prompt = f'You are an expert character creator for the Infinity Sim tabletop RPG. The player has provided this concept prompt: "{prompt}". Based on this prompt, generate a fully structured character.'

        generation_prompt += """
Return ONLY a valid JSON object matching this schema. Do not include markdown code block tags or additional text, just the raw JSON:
{
    "name": "a fitting name based on the prompt/genre",
    "char_class": "a creative custom character class name",
    "level_label": "Level",
    "points_label": "Points",
    "stats": {
        "stat_1_name": "Strength (or custom renamed stat matching prompt)",
        "stat_1_val": 10,
        "stat_2_name": "Dexterity (or custom renamed stat matching prompt)",
        "stat_2_val": 10,
        "stat_3_name": "Constitution (or custom renamed stat matching prompt)",
        "stat_3_val": 10,
        "stat_4_name": "Intelligence (or custom renamed stat matching prompt)",
        "stat_4_val": 10,
        "stat_5_name": "Wisdom (or custom renamed stat matching prompt)",
        "stat_5_val": 10
    },
    "gear_description": "Starting gear weapons armor and skill details",
    "profile_flavor": "1-2 sentences of thematic background flavor"
}

RULES FOR STATS:
1. You have exactly 10 extra attribute points to distribute across the 5 stats.
2. The base for each stat is 10.
3. The sum of all "stat_X_val" values MUST be exactly 60 (since 5 stats starting at 10 sum to 50, plus 10 extra points).
4. Do not make any stat less than 8 or greater than 16.
5. Stat names can be standard or creative matching the prompt genre.
"""
        try:
            _gp      = generation_prompt
            raw_res  = await loop.run_in_executor(None, selene.llm_caller.call_llm, _gp)
            clean_json = raw_res.strip()
            if clean_json.startswith("```"):
                lines = clean_json.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                clean_json = "\n".join(lines).strip()
            start_idx = clean_json.find("{")
            end_idx   = clean_json.rfind("}")
            if start_idx != -1 and end_idx != -1:
                clean_json = clean_json[start_idx:end_idx + 1]
            parsed_json = json.loads(clean_json)
            await websocket.send_json({
                "type": "story_random_character_generated", "status": "success", "character": parsed_json,
            })
        except Exception as e:
            await websocket.send_json({"type": "error", "message": f"Failed to generate random character: {e}"})
        return True

    elif msg_type == "story_start_campaign":
        if selene is None:
            return True
        from tools.story_engine.db_helper import get_db_connection
        world_name              = data.get("world_name", "Forgotten Realm").strip()
        origin_details          = data.get("origin_details", "").strip()
        long_term               = data.get("long_term_elements", "").strip()
        world_details           = data.get("world_details", "").strip()
        ambient_elements        = data.get("ambient_elements", "").strip()
        chronological_milestones= data.get("chronological_milestones", "").strip()
        major_goal              = data.get("major_goal", "Clear the threat").strip()
        roadmap_json            = json.dumps(data.get("roadmap", []))
        character_ids           = data.get("character_ids", [])

        conn = get_db_connection()
        try:
            levels    = []
            for c_id in character_ids:
                row = conn.execute("SELECT level FROM characters WHERE id = ?", (c_id,)).fetchone()
                if row:
                    levels.append(row[0])
            avg_level  = sum(levels) // len(levels) if levels else 1
            world_level = min(10, max(1, data.get("world_level", avg_level)))
            world_id    = f"world_{int(time.time())}"
            now         = time.time()
            conn.execute("""
            INSERT INTO worlds (
                id, name, world_level, origin_details, long_term_elements,
                world_details, ambient_elements, chronological_milestones,
                major_goal, roadmap_json, created_at, last_saved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                world_id, world_name, world_level, origin_details, long_term,
                world_details, ambient_elements, chronological_milestones,
                major_goal, roadmap_json, now, now,
            ))
            for c_id in character_ids:
                conn.execute("UPDATE characters SET world_id = ? WHERE id = ?", (world_id, c_id))
            loc_id = f"loc_{int(time.time())}"
            conn.execute("""
            INSERT INTO locations (id, world_id, name, description, is_hub, is_explored)
            VALUES (?, ?, 'Origin Outpost', 'The gateway where your saga begins.', 1, 1)
            """, (loc_id, world_id))

            intro_prompt = (
                f"You are the atmospheric Dungeon Master for the Infinity Sim tabletop RPG.\n"
                f"Generate a rich, immersive, and highly atmospheric starting introduction scenario for this new campaign.\n\n"
                f"WORLD DETAILS:\nName: {world_name}\nDifficulty Level: {world_level}\n"
                f"Campaign Goal: {major_goal}\nLore Details: {world_details}\n"
                f"Starting Origin: {origin_details}\nAmbient Elements: {ambient_elements}\n"
                f"Chronological Milestones: {chronological_milestones}\n\nPARTY MEMBERS:"
            )
            for c_id in character_ids:
                c_row = conn.execute("SELECT name, char_class, profile_flavor FROM characters WHERE id = ?", (c_id,)).fetchone()
                if c_row:
                    intro_prompt += f"\n- {c_row[0]} ({c_row[1]}): {c_row[2]}"
            intro_prompt += "\n\nWrite a 2-3 paragraph introduction set in the Starting Origin, establishing the theme and launching the saga. End by welcoming the characters."

            _ip = intro_prompt
            intro_narration = await loop.run_in_executor(None, selene.llm_caller.call_llm, _ip)

            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO manifest_log (world_id, turn_number, speaker, action_type, content, timestamp)
            VALUES (?, 1, 'DM', 'speak', ?, ?)
            """, (world_id, intro_narration, time.time()))
            conn.commit()

            await websocket.send_json({
                "type": "story_campaign_started",
                "world_id":          world_id,
                "world_level":       world_level,
                "major_goal":        major_goal,
                "starting_location": "Origin Outpost",
                "intro_narration":   intro_narration,
            })
        except Exception as e:
            conn.rollback()
            await websocket.send_json({"type": "error", "message": f"Failed to start campaign: {e}"})
        finally:
            conn.close()
        return True

    elif msg_type == "story_player_action":
        if selene is None:
            return True
        from tools.story_engine.db_helper import get_db_connection
        from selene_brain.story_engine    import InfiniteStoryEngine

        char_id           = data.get("character_id")
        action_type       = data.get("action_type")
        content           = data.get("content", "").strip()
        stat_used         = data.get("stat_used", "").strip()
        opponent_level    = int(data.get("opponent_level", 1))
        difficulty_penalty= int(data.get("difficulty_penalty", 0))
        engine            = InfiniteStoryEngine()

        roll_res = {}
        if action_type == "act":
            roll_res = engine.resolve_dice_action(char_id, stat_used, opponent_level, difficulty_penalty)

        char     = engine.get_character(char_id)
        world_id = char.get("world_id")

        conn   = get_db_connection()
        cursor = conn.cursor()
        try:
            row         = conn.execute("SELECT MAX(turn_number) FROM manifest_log WHERE world_id = ?", (world_id,)).fetchone()
            turn_number = (row[0] or 0) + 1
            roll_details_str = json.dumps(roll_res) if roll_res else None
            cursor.execute("""
            INSERT INTO manifest_log (world_id, turn_number, speaker, action_type, content, dice_roll_details, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (world_id, turn_number, char["name"], action_type, content, roll_details_str, time.time()))
            conn.commit()
            world_row    = conn.execute("SELECT * FROM worlds WHERE id = ?", (world_id,)).fetchone()
            world        = dict(world_row) if world_row else {}
            history_rows = conn.execute(
                "SELECT speaker, content FROM manifest_log WHERE world_id = ? ORDER BY turn_number DESC LIMIT 10",
                (world_id,)
            ).fetchall()
            history = list(reversed(history_rows))
        finally:
            conn.close()

        await websocket.send_json({"type": "story_thinking"})
        history_text = "\n".join([f"{h['speaker']}: {h['content']}" for h in history])
        dm_prompt = (
            f"You are the atmospheric Dungeon Master for the Infinity Sim tabletop RPG.\n"
            f"You must narrate the next scene based on the established world lore, milestones, history, and player action.\n\n"
            f"WORLD CONTEXT:\n"
            f"World Name: {world.get('name', 'Forgotten Realm')}\n"
            f"World Level: {world.get('world_level', 1)}\n"
            f"Major Campaign Goal: {world.get('major_goal', 'Clear the world')}\n"
            f"World/Story Lore Details: {world.get('world_details', 'None provided.')}\n"
            f"Starting Origin: {world.get('origin_details', 'None provided.')}\n"
            f"Ambient Elements: {world.get('ambient_elements', 'None provided.')}\n"
            f"Chronological Milestones: {world.get('chronological_milestones', 'None provided.')}\n\n"
            f"ACTIVE PARTY:\n"
            f"Character: {char['name']} ({char.get('char_class', 'Adventurer')})\n"
            f"{char.get('level_label', 'Level')}: {char['level']}\n"
            f"HP: {char['current_hp']}/{char['max_hp']} | MP: {char['current_mp']}/{char['max_mp']}\n"
            f"Capabilities & Background: {char['profile_flavor']}\n\n"
            f"RECENT SAGA TIMELINE HISTORY:\n{history_text}\n\n"
            f"CURRENT PLAYER TURN ACTION:\nSpeaker: {char['name']}\nAction Type: {action_type.upper()}\nContent: {content}\n"
        )
        if roll_res:
            dm_prompt += (
                f"\nDICE CHECK RESOLVED (D20):\n"
                f"Base Roll: {roll_res['base_roll']}\n"
                f"Stat Used: {roll_res['stat_used']} (Bonus: {roll_res['stat_bonus']})\n"
                f"Final Modified Roll: {roll_res['final_roll']}\n"
                f"World Floor Required: {roll_res['world_floor']}\n"
                f"Roll Result: {'SUCCESS' if roll_res['success'] else 'FAILURE'}\n"
                f"Combat Damage Dealt: {roll_res['damage_dealt']}\n"
            )
        dm_prompt += "\nNarrate the DM outcome with high atmosphere. Keep it concise (1-3 paragraphs) to avoid flooding the chat."

        try:
            _dp      = dm_prompt
            response = await loop.run_in_executor(None, selene.llm_caller.call_llm, _dp)
            conn     = get_db_connection()
            try:
                conn.execute("""
                INSERT INTO manifest_log (world_id, turn_number, speaker, action_type, content, timestamp)
                VALUES (?, ?, 'DM', 'speak', ?, ?)
                """, (world_id, turn_number + 1, response, time.time()))
                conn.commit()

                count_row = conn.execute("SELECT COUNT(*) FROM manifest_log WHERE world_id = ?", (world_id,)).fetchone()
                if count_row and count_row[0] >= 50:
                    print(f"[Story Compactor]: Timeline has reached {count_row[0]} turns. Compacting and archiving...")
                    turns_rows = conn.execute(
                        "SELECT speaker, content FROM manifest_log WHERE world_id = ? ORDER BY turn_number ASC",
                        (world_id,)
                    ).fetchall()
                    timeline_text     = "\n".join([f"{t['speaker']}: {t['content']}" for t in turns_rows])
                    compaction_prompt = (
                        "Summarize this roleplaying campaign timeline into a highly dense, compact world state summary.\n"
                        "Include active events, explored locations, known NPCs, relationship statuses, and major character gear/achievements.\n\n"
                        f"TIMELINE HISTORY:\n{timeline_text}\n\n"
                        "Return only the compact summary, keeping it highly readable for the DM."
                    )
                    _cp             = compaction_prompt
                    compact_summary = await loop.run_in_executor(None, selene.llm_caller.call_llm, _cp)

                    archived   = False
                    notion_tool = selene.tool_router.tools.get("notion_manager")
                    if notion_tool and not notion_tool.dormant:
                        try:
                            _wid = world_id; _tl = timeline_text
                            await loop.run_in_executor(None, lambda: notion_tool.execute({
                                "command": "create_page",
                                "title":   f"Infinity Sim Archive - World {_wid}",
                                "content": _tl,
                            }))
                            archived = True
                            print("[Story Compactor]: Archive successfully synced to Notion workspace.")
                        except Exception as ne:
                            print(f"[Story Compactor] Notion sync failed: {ne}")

                    if not archived:
                        from tools.story_engine.db_helper import STORY_ENGINE_DIR
                        archive_path = os.path.join(STORY_ENGINE_DIR, f"archived_timeline_{world_id}_{int(time.time())}.txt")
                        os.makedirs(STORY_ENGINE_DIR, exist_ok=True)
                        with open(archive_path, "w", encoding="utf-8") as af:
                            af.write(timeline_text)
                        print(f"[Story Compactor]: Timeline archived locally to {archive_path}")

                    conn.execute("DELETE FROM manifest_log WHERE world_id = ?", (world_id,))
                    conn.execute("""
                    INSERT INTO manifest_log (world_id, turn_number, speaker, action_type, content, timestamp)
                    VALUES (?, 1, 'DM_Archive_Summary', 'observe', ?, ?)
                    """, (world_id, compact_summary, time.time()))
                    conn.commit()
                    print("[Story Compactor]: History log compacted.")
            finally:
                conn.close()

            await websocket.send_json({"type": "story_turn_resolved", "roll_result": roll_res, "dm_narration": response})
        except Exception as e:
            await websocket.send_json({"type": "error", "message": f"DM call failed: {e}"})
        return True

    elif msg_type == "story_merchant_inventory":
        from selene_brain.story_engine import InfiniteStoryEngine
        location_name = data.get("location_name", "Origin Outpost")
        world_level   = int(data.get("world_level", 1))
        engine        = InfiniteStoryEngine()
        inv           = engine.generate_merchant_shop(location_name, world_level)
        await websocket.send_json({"type": "story_merchant_items", "inventory": inv})
        return True

    elif msg_type == "story_buy_item":
        from tools.story_engine.db_helper import get_db_connection
        char_id   = data.get("character_id")
        item_name = data.get("item_name")
        item_desc = data.get("item_description")
        price     = int(data.get("price", 0))
        conn      = get_db_connection()
        cursor    = conn.cursor()
        try:
            cursor.execute("SELECT points FROM characters WHERE id = ?", (char_id,))
            row = cursor.fetchone()
            if row and row[0] >= price:
                new_pts = row[0] - price
                cursor.execute("UPDATE characters SET points = ? WHERE id = ?", (new_pts, char_id))
                card_id = f"card_{int(time.time())}_{random.randint(100, 999)}"
                cursor.execute(
                    "INSERT INTO cards (id, character_id, card_type, name, description, is_active) VALUES (?, ?, 'gear', ?, ?, 1)",
                    (card_id, char_id, item_name, item_desc),
                )
                conn.commit()
                await websocket.send_json({"type": "story_purchase_complete", "status": "success", "points_remaining": new_pts})
            else:
                await websocket.send_json({"type": "error", "message": "Insufficient points for purchase."})
        except Exception as e:
            conn.rollback()
            await websocket.send_json({"type": "error", "message": f"Purchase failed: {e}"})
        finally:
            conn.close()
        return True

    elif msg_type == "story_level_up":
        from selene_brain.story_engine import InfiniteStoryEngine
        char_id       = data.get("character_id")
        stat_to_boost = data.get("stat_to_boost")
        engine        = InfiniteStoryEngine()
        try:
            res = engine.spend_points_level_up(char_id, stat_to_boost)
            await websocket.send_json({"type": "story_levelled_up", "data": res})
        except Exception as e:
            await websocket.send_json({"type": "error", "message": f"Level up failed: {e}"})
        return True

    elif msg_type == "story_regenerate_dm":
        if selene is None:
            return True
        from tools.story_engine.db_helper import get_db_connection
        world_id = data.get("world_id")
        hint     = data.get("hint", "").strip()
        conn     = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT id, content FROM manifest_log WHERE world_id = ? ORDER BY id DESC LIMIT 2",
                (world_id,)
            ).fetchall()
            if len(rows) >= 2:
                player_turn = dict(rows[1])
                dm_turn     = dict(rows[0])
                dm_prompt   = (
                    f'Re-narrate the Dungeon Master outcome using this new inspiration/hint: "{hint}".\n'
                    f'Previous player choice: "{player_turn["content"]}"\n'
                    f"Narrate with high tabletop atmosphere. Keep it concise."
                )
                await websocket.send_json({"type": "story_thinking"})
                _dp      = dm_prompt
                response = await loop.run_in_executor(None, selene.llm_caller.call_llm, _dp)
                conn.execute("UPDATE manifest_log SET content = ? WHERE id = ?", (response, dm_turn["id"]))
                conn.commit()
                await websocket.send_json({"type": "story_turn_resolved", "dm_narration": response})
            else:
                await websocket.send_json({"type": "error", "message": "Cannot find previous narrative turn to regenerate."})
        except Exception as e:
            await websocket.send_json({"type": "error", "message": f"Regeneration failed: {e}"})
        finally:
            conn.close()
        return True

    elif msg_type == "story_save_game":
        from tools.story_engine.db_helper import get_db_connection
        conn = get_db_connection()
        try:
            world_row = conn.execute("SELECT id FROM worlds ORDER BY last_saved_at DESC LIMIT 1").fetchone()
            if world_row:
                conn.execute("UPDATE worlds SET last_saved_at = ? WHERE id = ?", (time.time(), world_row["id"]))
                conn.commit()
            await websocket.send_json({"type": "story_game_saved", "status": "success"})
        except Exception as e:
            await websocket.send_json({"type": "error", "message": f"Save failed: {e}"})
        finally:
            conn.close()
        return True

    elif msg_type == "story_resume_campaign":
        from tools.story_engine.db_helper import get_db_connection
        conn = get_db_connection()
        try:
            world_row = conn.execute("SELECT * FROM worlds ORDER BY last_saved_at DESC LIMIT 1").fetchone()
            if world_row:
                world_id  = world_row["id"]
                char_rows = conn.execute("SELECT * FROM characters WHERE world_id = ?", (world_id,)).fetchall()
                characters = [dict(c) for c in char_rows]
                log_rows   = conn.execute(
                    "SELECT * FROM manifest_log WHERE world_id = ? ORDER BY turn_number ASC", (world_id,)
                ).fetchall()
                timeline = []
                for row in log_rows:
                    timeline.append({
                        "speaker": row["speaker"],
                        "content": row["content"],
                        "roll":    json.loads(row["dice_roll_details"]) if row["dice_roll_details"] else None,
                    })
                loc_rows       = conn.execute("SELECT * FROM locations WHERE world_id = ?", (world_id,)).fetchall()
                locations_list = [dict(l) for l in loc_rows]
                active_loc     = "Origin Outpost"
                for l in locations_list:
                    if l.get("is_hub"):
                        active_loc = l["name"]
                        break
                await websocket.send_json({
                    "type":                    "story_campaign_loaded",
                    "status":                  "success",
                    "world_id":                world_id,
                    "world_name":              world_row["name"],
                    "world_level":             world_row["world_level"],
                    "major_goal":              world_row["major_goal"],
                    "origin_details":          world_row["origin_details"],
                    "long_term_elements":      world_row["long_term_elements"],
                    "world_details":           world_row["world_details"],
                    "ambient_elements":        world_row["ambient_elements"],
                    "chronological_milestones":world_row["chronological_milestones"],
                    "characters":              characters,
                    "timeline":                timeline,
                    "locations":               locations_list,
                    "active_location":         active_loc,
                })
            else:
                await websocket.send_json({
                    "type": "story_campaign_loaded", "status": "error",
                    "message": "No active campaign found.",
                })
        except Exception as e:
            await websocket.send_json({"type": "error", "message": f"Resume failed: {e}"})
        finally:
            conn.close()
        return True

    return False
