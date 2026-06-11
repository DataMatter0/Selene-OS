"""
server/handlers/youtube.py — YouTube co-watching and search
"""

import asyncio
import json
import re
import time

from server.tool_pipeline import update_memory_and_energy
from server.utils         import _format_tool_data
import server.state       as _st


async def handle(websocket, data: dict, loop, yt_state: dict) -> bool:
    """
    yt_state is a per-session dict owned by the WS endpoint:
      awaiting_ghost_reply, absence_prompted, dormant
    """
    msg_type = data.get("type")
    selene   = _st.selene_ref

    if msg_type == "youtube_query":
        if selene:
            yt = selene.tool_router.tools.get("youtube")
            if yt:
                await websocket.send_json({"type": "youtube_thinking"})
                tool_input = {k: v for k, v in data.items() if k != "type"}
                res = await loop.run_in_executor(None, yt.execute, tool_input)
                await websocket.send_json({"type": "youtube_result", "data": res})
            else:
                await websocket.send_json({"type": "youtube_result", "data": {"error": "YouTube tool not loaded."}})
        return True

    elif msg_type == "youtube_search":
        query     = data.get("query", "").strip()
        limit     = int(data.get("limit", 8))
        sort_type = data.get("sort", "relevance")
        if not query:
            await websocket.send_json({"type": "youtube_search_results", "results": []})
        elif selene:
            yt = selene.tool_router.tools.get("youtube")
            if yt:
                await websocket.send_json({"type": "youtube_searching"})
                try:
                    results = await loop.run_in_executor(None, yt.search_youtube, query, limit, sort_type)
                except Exception as e:
                    print(f"[YouTube Search Error]: {e}")
                    results = []
                await websocket.send_json({"type": "youtube_search_results", "results": results})
            else:
                await websocket.send_json({"type": "youtube_search_results", "results": [], "error": "YouTube tool not loaded."})
        return True

    elif msg_type == "youtube_watch_start":
        video_id  = data.get("video_id", "")
        seg_dur_s = int(data.get("segment_duration_s", 60))
        if not video_id:
            await websocket.send_json({"type": "youtube_segments", "error": "No video_id provided."})
        elif selene:
            yt = selene.tool_router.tools.get("youtube")
            if yt:
                await websocket.send_json({"type": "youtube_segments_loading"})
                try:
                    result = await loop.run_in_executor(None, yt.get_segments, video_id, seg_dur_s)
                except Exception as e:
                    result = {"ok": False, "error": str(e), "segments": []}
                await websocket.send_json({"type": "youtube_segments", "data": result})
            else:
                await websocket.send_json({"type": "youtube_segments", "error": "YouTube tool not loaded."})
        return True

    elif msg_type == "youtube_segment_push":
        selene_instance = selene
        if selene_instance is None:
            return True

        video_title = data.get("video_title", "this video")
        seg_idx     = data.get("segment_idx", 0)
        timestamp   = data.get("timestamp_label", "??:??")
        seg_text    = data.get("segment_text", "").strip()
        video_id    = data.get("video_id", "")
        watch_mode  = data.get("watch_mode", "normal").lower()

        if watch_mode == "ignore" or not seg_text:
            return True

        if yt_state["awaiting_ghost_reply"] and not yt_state["dormant"]:
            if not yt_state["absence_prompted"]:
                await websocket.send_json({
                    "type": "youtube_reaction", "video_id": video_id,
                    "segment_idx": seg_idx, "timestamp_label": timestamp,
                    "reaction": "Still watching? 👀",
                })
                yt_state["absence_prompted"] = True
            else:
                yt_state["dormant"] = True
            return True

        if yt_state["dormant"]:
            return True

        seg_prompt = f"[Co-watching: {video_title} @ {timestamp}] Transcript: {seg_text}"
        _sp = seg_prompt
        try:
            system_prompt = selene_instance.system_prompt
            if watch_mode == "observe":
                system_prompt += "\nRecord your observations in a <think>...</think> block. Stay silent and do not speak or use tools."

            raw_out = await loop.run_in_executor(
                None,
                lambda: selene_instance.llm_caller.call_llm(
                    input_data=_sp, system_prompt=system_prompt,
                    history=[], temperature=0.7, max_tokens=256,
                )
            )

            think_match   = re.search(r'<think>([\s\S]*?)</think>', raw_out or "", re.DOTALL | re.IGNORECASE)
            thoughts_text = think_match.group(1).strip() if think_match else ""

            reaction = ""
            if watch_mode != "observe":
                tc_match = re.search(
                    r'<tool_call\s+name=["\']?([^"\' \t>]+)["\']?\s*>(.*?)</tool_call>',
                    raw_out or "", re.DOTALL | re.IGNORECASE
                )
                if tc_match:
                    tc_name     = tc_match.group(1).lower()
                    tc_args_raw = tc_match.group(2).strip()
                    if tc_name == "chat":
                        try:
                            args     = json.loads(tc_args_raw) if tc_args_raw else {}
                            reaction = args.get("message", tc_args_raw).strip()
                        except Exception:
                            reaction = tc_args_raw
                    elif tc_name in ("observe", "ignore"):
                        reaction = ""
                    else:
                        _tn = tc_name; _ta = tc_args_raw
                        result   = await loop.run_in_executor(
                            None, lambda: selene_instance.tool_router.route_and_execute(_tn, _ta)
                        )
                        reaction = _format_tool_data(result.get("data", "")).strip()
                else:
                    cleaned  = re.sub(r'<think>.*?</think>', '', raw_out or '', flags=re.DOTALL | re.IGNORECASE)
                    reaction = cleaned.strip()

            if reaction or thoughts_text:
                if reaction:
                    update_memory_and_energy(_sp, reaction)
                    selene_instance.maybe_extract_memory(_sp, reaction)
                await websocket.send_json({
                    "type": "youtube_reaction", "video_id": video_id,
                    "segment_idx": seg_idx, "timestamp_label": timestamp,
                    "reaction": reaction,
                    "thoughts": thoughts_text if thoughts_text else None,
                })
                if reaction:
                    yt_state["awaiting_ghost_reply"] = True
                    yt_state["absence_prompted"]     = False
        except Exception as e:
            print(f"[YouTube Reaction Error]: {e}")
        return True

    elif msg_type == "youtube_chat":
        selene_instance = selene
        if selene_instance is None:
            return True

        video_title  = data.get("video_title", "")
        user_message = data.get("message", "").strip()
        context_segs = data.get("context_segments", [])
        no_video     = data.get("no_video", False)

        if not user_message:
            return True

        yt_state["awaiting_ghost_reply"] = False
        yt_state["absence_prompted"]     = False
        yt_state["dormant"]              = False

        if selene_instance.active_conversation_id is None:
            await loop.run_in_executor(None, selene_instance.new_conversation)
            await websocket.send_json({
                "type": "conversation_loaded",
                "id":   selene_instance.active_conversation_id,
                "name": selene_instance.active_conversation_name,
            })

        if no_video or not video_title:
            video_context = (
                "\n\n-- CURRENT CONTEXT --\n"
                "Ghost has the YouTube co-watching panel open but has NOT selected any video yet. "
                "There is no active video playing. Do not assume what video is playing or reference "
                "any specific content. If he is asking about a video, let him know he needs to "
                "search for and select one first."
                "\n\nThis is casual chat. Reply in plain conversational text only."
            )
        else:
            context_text = ""
            if context_segs:
                seg_lines    = [f"[{s.get('start_label', s.get('timestamp_label','?'))}] {s.get('text','')}" for s in context_segs[-5:]]
                context_text = "\n".join(seg_lines)
            video_context = (
                f"\n\n-- CURRENT CONTEXT --\n"
                f"You and Ghost are co-watching \"{video_title}\" right now."
                + (f"\n\nRecent transcript:\n{context_text}" if context_text else "")
                + "\n\nThis is casual co-watching chat. Reply in plain conversational text only."
            )
        chat_system = selene_instance.system_prompt + video_context

        try:
            _um = user_message; _cs = chat_system
            raw_out = await loop.run_in_executor(
                None,
                lambda: selene_instance.llm_caller.call_llm(
                    input_data=_um, system_prompt=_cs,
                    history=selene_instance.working_memory,
                    temperature=0.6, max_tokens=4096,
                )
            )
            clean_response = ""
            tc_match = re.search(
                r'<tool_call\s+name=["\']?([^"\' \t>]+)["\']?\s*>(.*?)</tool_call>',
                raw_out or "", re.DOTALL | re.IGNORECASE
            )
            if tc_match:
                tc_name     = tc_match.group(1).lower()
                tc_args_raw = tc_match.group(2).strip()
                if tc_name == "chat":
                    try:
                        args           = json.loads(tc_args_raw) if tc_args_raw else {}
                        clean_response = args.get("message", tc_args_raw).strip()
                    except Exception:
                        clean_response = tc_args_raw
                elif tc_name in ("observe", "ignore"):
                    clean_response = ""
                else:
                    _tn = tc_name; _ta = tc_args_raw
                    result         = await loop.run_in_executor(
                        None, lambda: selene_instance.tool_router.route_and_execute(_tn, _ta)
                    )
                    clean_response = _format_tool_data(result.get("data", "")).strip()
            else:
                raw = re.sub(r'<tool_call[^>]*>.*?</tool_call>', '', raw_out or '', flags=re.DOTALL | re.IGNORECASE)
                raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL | re.IGNORECASE)
                clean_response = raw.strip()

            if clean_response:
                with selene_instance.lock:
                    _ts = time.time()
                    selene_instance.working_memory.append({"role": "user",      "content": user_message,   "ts": _ts})
                    selene_instance.working_memory.append({"role": "assistant", "content": clean_response, "ts": _ts})
                await loop.run_in_executor(None, selene_instance.save_current_conversation)
            await websocket.send_json({"type": "youtube_chat_response", "message": clean_response})
        except Exception as e:
            print(f"[YouTube Chat Error]: {e}")
            await websocket.send_json({"type": "youtube_chat_response", "message": f"(Error: {e})"})
        return True

    return False
