# selene_brain/agent_memory.py
import sqlite3
from server.roster import agent_has_cap, default_agent_slug as _roster_default
import os
import json
import time
from typing import Dict, List, Optional, Any

class AgentMemoryStore:
    def __init__(self, db_path: str, is_readonly: bool = False):
        self.db_path = os.path.normpath(db_path)
        self.is_readonly = is_readonly
        self.conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        db_dir = os.path.dirname(os.path.abspath(self.db_path))
        if not self.is_readonly:
            os.makedirs(db_dir, exist_ok=True)
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self._create_tables()
        else:
            # Open in read-only mode if the file exists
            if os.path.exists(self.db_path):
                # URI mode for read-only
                db_uri = f"file:{self.db_path}?mode=ro"
                self.conn = sqlite3.connect(db_uri, uri=True, check_same_thread=False)
                self.conn.row_factory = sqlite3.Row
            else:
                # Fallback to in-memory temporary database if database does not exist yet to prevent crashes
                self.conn = sqlite3.connect(":memory:", check_same_thread=False)
                self.conn.row_factory = sqlite3.Row

    def _create_tables(self):
        if self.conn is None:
            return
        with self.conn:
            # Settings and flags (e.g. ONBOARDING_COMPLETE)
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """)
            
            # Dialog history per channel/session
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS dialog_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                session_id TEXT,
                role TEXT,
                content TEXT,
                thoughts TEXT,
                status TEXT
            )
            """)
            
            # Medium memory cards/facts
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS medium_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT,
                key TEXT UNIQUE,
                value TEXT,
                created_at REAL,
                metadata_json TEXT
            )
            """)
            
            # Daily manifest documents
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_manifests (
                date TEXT PRIMARY KEY,
                metadata_json TEXT,
                emotional_summary_json TEXT,
                choice_log_json TEXT,
                task_log_json TEXT,
                active_threads_json TEXT,
                summary TEXT
            )
            """)
            
            # Emotional history tracking
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS emotional_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                thought_emotion TEXT,
                thought_intensity REAL,
                response_emotion TEXT,
                response_intensity REAL,
                gap REAL,
                action_details TEXT
            )
            """)

            # Confidence log table
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS confidence_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                turn_id TEXT,
                topic_category TEXT,
                raw_entropy REAL,
                confidence_score REAL,
                confidence_tier TEXT,
                follow_up_triggered INTEGER,
                is_flagged INTEGER
            )
            """)

            # Tool phrase suggestion store — keyphrase classifier training data.
            # Each phrase maps to a tool. hit/miss counts update on YES/NO outcomes.
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_phrases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT NOT NULL,
                phrase TEXT NOT NULL,
                hit_count INTEGER DEFAULT 0,
                miss_count INTEGER DEFAULT 0,
                created_at REAL,
                UNIQUE(tool_name, phrase)
            )
            """)

            # Tool reasoning log — purpose-built training data.
            # Each row is one complete example: context → tool call → result → reasoning.
            # Todo chains share a chain_id and are written as one entry when complete.
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_reasoning_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                agent TEXT,
                session_id TEXT,
                turn_id TEXT,
                tool_name TEXT,
                trigger_mode TEXT,
                input_context TEXT,
                tool_args TEXT,
                tool_result TEXT,
                reasoning TEXT,
                chain_id TEXT,
                quality_flag TEXT DEFAULT 'unreviewed'
            )
            """)

            # MetaInsight self-observation log
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS meta_insight_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                agent TEXT,
                category TEXT,
                subcategory TEXT,
                input_context TEXT,
                reasoning TEXT,
                result TEXT,
                emotional_state_before TEXT,
                emotional_state_after TEXT,
                confidence_score REAL,
                trigger_mode TEXT,
                session_id TEXT,
                promoted_to_card INTEGER DEFAULT 0,
                sage_accessible INTEGER DEFAULT 0
            )
            """)

    # ── Settings Interface ───────────────────────────────────────────────────
    def get_setting(self, key: str, default: str = "") -> str:
        try:
            if self.conn is None:
                return default
            cursor = self.conn.cursor()
            row = cursor.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default
        except Exception:
            return default

    def set_setting(self, key: str, value: str):
        if self.is_readonly:
            return
        try:
            if self.conn is None:
                return
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, str(value))
                )
        except Exception as e:
            print(f"[AgentMemoryStore]: Failed to write setting: {e}")

    # ── Dialogue History Interface ──────────────────────────────────────────
    def log_dialog(self, session_id: str, role: str, content: str, thoughts: str = "", status: str = "read"):
        if self.is_readonly:
            return
        try:
            if self.conn is None:
                return
            with self.conn:
                self.conn.execute("""
                INSERT INTO dialog_history (timestamp, session_id, role, content, thoughts, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """, (time.time(), session_id, role, content, thoughts, status))
        except Exception as e:
            print(f"[AgentMemoryStore]: Failed to log dialog: {e}")

    def update_last_message_status(self, session_id: str, status: str):
        if self.is_readonly:
            return
        try:
            if self.conn is None:
                return
            with self.conn:
                # Find last user message in session and update its status
                self.conn.execute("""
                UPDATE dialog_history 
                SET status = ? 
                WHERE id = (
                    SELECT MAX(id) FROM dialog_history 
                    WHERE session_id = ? AND role = 'user'
                )
                """, (status, session_id))
        except Exception as e:
            print(f"[AgentMemoryStore]: Failed to update message status: {e}")

    def get_dialog_history(self, session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            if self.conn is None:
                return []
            cursor = self.conn.cursor()
            rows = cursor.execute("""
            SELECT role, content, thoughts, status, timestamp FROM dialog_history 
            WHERE session_id = ? 
            ORDER BY id ASC LIMIT ?
            """, (session_id, limit)).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ── Medium Memory Interface ──────────────────────────────────────────────
    def save_fact(self, key: str, value: str, category: str = "general", metadata: Optional[Dict] = None):
        if self.is_readonly:
            return
        meta_str = json.dumps(metadata) if metadata else None
        try:
            if self.conn is None:
                return
            with self.conn:
                self.conn.execute("""
                INSERT OR REPLACE INTO medium_memory (category, key, value, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                """, (category, key, value, time.time(), meta_str))
        except Exception as e:
            print(f"[AgentMemoryStore]: Failed to save fact: {e}")

    def get_fact(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            if self.conn is None:
                return None
            cursor = self.conn.cursor()
            row = cursor.execute("SELECT * FROM medium_memory WHERE key = ?", (key,)).fetchone()
            if row:
                res = dict(row)
                res["metadata"] = json.loads(res["metadata_json"]) if res["metadata_json"] else None
                return res
            return None
        except Exception:
            return None

    # ── Emotional History Interface ──────────────────────────────────────────
    def log_emotion(self, thought_emotion: str, thought_intensity: float, 
                    response_emotion: str, response_intensity: float, 
                    action_details: str = ""):
        if self.is_readonly:
            return
        gap = abs(thought_intensity - response_intensity) if thought_emotion == response_emotion else (thought_intensity + response_intensity) / 2
        try:
            if self.conn is None:
                return
            with self.conn:
                self.conn.execute("""
                INSERT INTO emotional_history (timestamp, thought_emotion, thought_intensity, response_emotion, response_intensity, gap, action_details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (time.time(), thought_emotion, thought_intensity, response_emotion, response_intensity, gap, action_details))
        except Exception as e:
            print(f"[AgentMemoryStore]: Failed to log emotion: {e}")

    def get_recent_emotions(self, limit: int = 10) -> List[Dict[str, Any]]:
        try:
            if self.conn is None:
                return []
            cursor = self.conn.cursor()
            rows = cursor.execute("""
            SELECT * FROM emotional_history 
            ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ── Confidence Logs Interface ────────────────────────────────────────────
    def log_confidence(self, turn_id: str, category: str, raw_entropy: float, 
                       confidence_score: float, confidence_tier: str, 
                       follow_up_triggered: bool, is_flagged: bool):
        if self.is_readonly:
            return
        try:
            if self.conn is None:
                return
            with self.conn:
                self.conn.execute("""
                INSERT INTO confidence_log (timestamp, turn_id, topic_category, raw_entropy, confidence_score, confidence_tier, follow_up_triggered, is_flagged)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (time.time(), turn_id, category, raw_entropy, confidence_score, confidence_tier, 1 if follow_up_triggered else 0, 1 if is_flagged else 0))
        except Exception as e:
            print(f"[AgentMemoryStore]: Failed to log confidence: {e}")

    # ── Daily Manifest Interface ─────────────────────────────────────────────
    def save_daily_manifest(self, date_str: str, metadata: Dict, emotions: Dict, choices: Dict, tasks: Dict, threads: Dict, summary: str):
        if self.is_readonly:
            return
        try:
            if self.conn is None:
                return
            with self.conn:
                self.conn.execute("""
                INSERT OR REPLACE INTO daily_manifests (date, metadata_json, emotional_summary_json, choice_log_json, task_log_json, active_threads_json, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    date_str,
                    json.dumps(metadata),
                    json.dumps(emotions),
                    json.dumps(choices),
                    json.dumps(tasks),
                    json.dumps(threads),
                    summary
                ))
        except Exception as e:
            print(f"[AgentMemoryStore]: Failed to save daily manifest: {e}")

    def get_daily_manifest(self, date_str: str) -> Optional[Dict[str, Any]]:
        try:
            if self.conn is None:
                return None
            cursor = self.conn.cursor()
            row = cursor.execute("SELECT * FROM daily_manifests WHERE date = ?", (date_str,)).fetchone()
            if row:
                res = dict(row)
                res["metadata"] = json.loads(res["metadata_json"]) if res["metadata_json"] else {}
                res["emotional_summary"] = json.loads(res["emotional_summary_json"]) if res["emotional_summary_json"] else {}
                res["choice_log"] = json.loads(res["choice_log_json"]) if res["choice_log_json"] else {}
                res["task_log"] = json.loads(res["task_log_json"]) if res["task_log_json"] else {}
                res["active_threads"] = json.loads(res["active_threads_json"]) if res["active_threads_json"] else {}
                return res
            return None
        except Exception:
            return None

    # ── MetaInsight Interface ────────────────────────────────────────────────
    def log_meta_insight(
        self,
        agent: str,
        category: str,
        subcategory: str = "",
        input_context: str = "",
        reasoning: str = "",
        result: str = "",
        emotional_state_before: Optional[Dict] = None,
        emotional_state_after: Optional[Dict] = None,
        confidence_score: float = 0.0,
        trigger_mode: str = "",
        session_id: str = "",
        sage_accessible: bool = False,
    ) -> Optional[int]:
        """Write a single self-observation entry. Returns the new row id."""
        if self.is_readonly:
            return None
        try:
            if self.conn is None:
                return None
            with self.conn:
                cur = self.conn.execute("""
                INSERT INTO meta_insight_log (
                    timestamp, agent, category, subcategory,
                    input_context, reasoning, result,
                    emotional_state_before, emotional_state_after,
                    confidence_score, trigger_mode, session_id,
                    promoted_to_card, sage_accessible
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)
                """, (
                    time.time(), agent, category, subcategory,
                    input_context[:2000], reasoning[:4000], result[:2000],
                    json.dumps(emotional_state_before or {}),
                    json.dumps(emotional_state_after or {}),
                    confidence_score, trigger_mode, session_id,
                    1 if sage_accessible else 0,
                ))
                return cur.lastrowid
        except Exception as e:
            print(f"[AgentMemoryStore]: Failed to log meta_insight: {e}")
            return None

    def query_meta_insight(
        self,
        agent: Optional[str] = None,
        category: Optional[str] = None,
        subcategory: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        time_window_hours: Optional[float] = None,
        sage_requesting: bool = False,
        requesting_agent: str = None,
    ) -> List[Dict[str, Any]]:
        """
        Flexible query over meta_insight_log.
        sage_requesting=True restricts results to sage_accessible=1 rows
        for entries belonging to the default agent.
        """
        if requesting_agent is None:
            requesting_agent = _roster_default()
        try:
            conditions = []
            params: list = []

            # Access control: grant_access agents can read the default agent's
            # accessible entries in addition to their own
            _default = _roster_default()
            if sage_requesting and agent_has_cap(requesting_agent, "grant_access"):
                conditions.append(f"(agent = ? OR (agent = '{_default}' AND sage_accessible = 1))")
                params.append(requesting_agent)

            if agent:
                conditions.append("agent = ?")
                params.append(agent)
            if category:
                conditions.append("category = ?")
                params.append(category)
            if subcategory:
                conditions.append("subcategory LIKE ?")
                params.append(f"%{subcategory}%")
            if keyword:
                conditions.append("(input_context LIKE ? OR reasoning LIKE ? OR result LIKE ?)")
                params.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])
            if time_window_hours is not None:
                cutoff = time.time() - (time_window_hours * 3600)
                conditions.append("timestamp >= ?")
                params.append(cutoff)

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            params.extend([limit, offset])

            if self.conn is None:
                return []

            rows = self.conn.execute(f"""
                SELECT * FROM meta_insight_log
                {where}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """, params).fetchall()

            result = []
            for r in rows:
                d = dict(r)
                d["emotional_state_before"] = json.loads(d["emotional_state_before"] or "{}")
                d["emotional_state_after"]  = json.loads(d["emotional_state_after"]  or "{}")
                result.append(d)
            return result
        except Exception as e:
            print(f"[AgentMemoryStore]: Failed to query meta_insight: {e}")
            return []

    def pattern_mode_meta_insight(
        self,
        agent: str,
        category: Optional[str] = None,
        time_window_hours: float = 168.0,
        min_confidence: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Aggregate patterns: frequency counts per subcategory, average confidence,
        most common results, and trigger mode distribution.
        """
        try:
            if self.conn is None:
                return {"entries": 0, "patterns": {}}
            cutoff = time.time() - (time_window_hours * 3600)
            cond = "agent = ? AND timestamp >= ? AND confidence_score >= ?"
            params: list = [agent, cutoff, min_confidence]
            if category:
                cond += " AND category = ?"
                params.append(category)

            rows = self.conn.execute(f"""
                SELECT category, subcategory, trigger_mode, result, confidence_score
                FROM meta_insight_log WHERE {cond}
            """, params).fetchall()

            if not rows:
                return {"entries": 0, "patterns": {}}

            from collections import Counter
            cats: Counter = Counter()
            subs: Counter = Counter()
            modes: Counter = Counter()
            conf_sum = 0.0
            for r in rows:
                cats[r["category"]] += 1
                subs[r["subcategory"] or "—"] += 1
                modes[r["trigger_mode"] or "—"] += 1
                conf_sum += r["confidence_score"] or 0.0

            return {
                "entries":          len(rows),
                "avg_confidence":   round(conf_sum / len(rows), 3),
                "by_category":      dict(cats.most_common(10)),
                "by_subcategory":   dict(subs.most_common(10)),
                "by_trigger_mode":  dict(modes.most_common(5)),
                "time_window_hours": time_window_hours,
            }
        except Exception as e:
            print(f"[AgentMemoryStore]: Failed pattern_mode: {e}")
            return {"entries": 0, "error": str(e)}

    # ── Tool Phrase Store Interface ──────────────────────────────────────────
    def add_tool_phrase(self, tool_name: str, phrase: str) -> bool:
        if self.is_readonly:
            return False
        try:
            if self.conn is None:
                return False
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO tool_phrases (tool_name, phrase, created_at) VALUES (?,?,?)",
                    (tool_name.lower(), phrase.lower().strip(), time.time())
                )
            return True
        except Exception as e:
            print(f"[AgentMemoryStore]: Failed to add tool phrase: {e}")
            return False

    def remove_tool_phrase(self, tool_name: str, phrase: str) -> bool:
        if self.is_readonly:
            return False
        try:
            if self.conn is None:
                return False
            with self.conn:
                self.conn.execute(
                    "DELETE FROM tool_phrases WHERE tool_name = ? AND phrase = ?",
                    (tool_name.lower(), phrase.lower().strip())
                )
            return True
        except Exception:
            return False

    def get_tool_phrases(self, tool_name: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            if self.conn is None:
                return []
            if tool_name:
                rows = self.conn.execute(
                    "SELECT * FROM tool_phrases WHERE tool_name = ? ORDER BY hit_count DESC",
                    (tool_name.lower(),)
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM tool_phrases ORDER BY tool_name, hit_count DESC"
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def record_phrase_outcome(self, tool_name: str, phrase: str, was_hit: bool) -> bool:
        """Increment hit or miss count for a phrase — builds accuracy signal over time."""
        if self.is_readonly:
            return False
        col = "hit_count" if was_hit else "miss_count"
        try:
            if self.conn is None:
                return False
            with self.conn:
                self.conn.execute(
                    f"UPDATE tool_phrases SET {col} = {col} + 1 WHERE tool_name = ? AND phrase = ?",
                    (tool_name.lower(), phrase.lower().strip())
                )
            return True
        except Exception:
            return False

    # ── Tool Reasoning Log Interface ─────────────────────────────────────────
    def log_tool_reasoning(
        self,
        agent: str,
        session_id: str,
        turn_id: str,
        tool_name: str,
        trigger_mode: str,
        input_context: str,
        tool_args: str,
        tool_result: str,
        reasoning: str,
        chain_id: str = "",
        quality_flag: str = "unreviewed",
    ) -> Optional[int]:
        """Write one training example to the tool reasoning log."""
        if self.is_readonly:
            return None
        try:
            if self.conn is None:
                return None
            with self.conn:
                cur = self.conn.execute("""
                INSERT INTO tool_reasoning_log (
                    timestamp, agent, session_id, turn_id,
                    tool_name, trigger_mode, input_context,
                    tool_args, tool_result, reasoning,
                    chain_id, quality_flag
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    time.time(), agent, session_id, turn_id,
                    tool_name, trigger_mode, input_context[:2000],
                    tool_args[:1000], tool_result[:2000], reasoning[:1000],
                    chain_id, quality_flag,
                ))
                return cur.lastrowid
        except Exception as e:
            print(f"[AgentMemoryStore]: Failed to log tool_reasoning: {e}")
            return None

    def update_tool_reasoning(self, entry_id: int, reasoning: Optional[str] = None,
                               tool_result: Optional[str] = None, quality_flag: Optional[str] = None) -> bool:
        """Update an existing tool reasoning entry — used to append chain results or set quality."""
        if self.is_readonly:
            return False
        try:
            if self.conn is None:
                return False
            fields, params = [], []
            if reasoning is not None:
                fields.append("reasoning = ?")
                params.append(reasoning[:1000])
            if tool_result is not None:
                fields.append("tool_result = ?")
                params.append(tool_result[:2000])
            if quality_flag is not None:
                fields.append("quality_flag = ?")
                params.append(quality_flag)
            if not fields:
                return False
            params.append(entry_id)
            with self.conn:
                self.conn.execute(
                    f"UPDATE tool_reasoning_log SET {', '.join(fields)} WHERE id = ?",
                    params
                )
            return True
        except Exception:
            return False

    def query_tool_reasoning(self, agent: Optional[str] = None, tool_name: Optional[str] = None,
                              quality_flag: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Query training examples with optional filters."""
        try:
            if self.conn is None:
                return []
            conditions, params = [], []
            if agent:
                conditions.append("agent = ?")
                params.append(agent)
            if tool_name:
                conditions.append("tool_name = ?")
                params.append(tool_name)
            if quality_flag:
                conditions.append("quality_flag = ?")
                params.append(quality_flag)
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            params.append(limit)
            rows = self.conn.execute(
                f"SELECT * FROM tool_reasoning_log {where} ORDER BY timestamp DESC LIMIT ?",
                params
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            print(f"[AgentMemoryStore]: Failed to query tool_reasoning: {e}")
            return []

    def set_meta_insight_sage_access(self, entry_id: int, accessible: bool) -> bool:
        """Grant or revoke Sage's access to a specific log entry."""
        if self.is_readonly:
            return False
        try:
            if self.conn is None:
                return False
            with self.conn:
                self.conn.execute(
                    "UPDATE meta_insight_log SET sage_accessible = ? WHERE id = ?",
                    (1 if accessible else 0, entry_id)
                )
            return True
        except Exception:
            return False

    def mark_meta_insight_promoted(self, entry_id: int) -> bool:
        """Mark that this log entry was promoted to a knowledge board card."""
        if self.is_readonly:
            return False
        try:
            if self.conn is None:
                return False
            with self.conn:
                self.conn.execute(
                    "UPDATE meta_insight_log SET promoted_to_card = 1 WHERE id = ?",
                    (entry_id,)
                )
            return True
        except Exception:
            return False

    def close(self):
        if self.conn:
            self.conn.close()
