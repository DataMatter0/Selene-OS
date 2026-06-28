"""
server/handlers/chat.py — Chat, force_generate, rollback, clear_memory
"""

import asyncio
import re
import time

from server.state        import get_state, broadcast, clients, _cached_emotion
from server.utils        import clean_xml_tags, split_response_chunks, extract_presence_decision
from server.tool_pipeline import (
    process_message, set_last_message_status, update_memory_and_energy,
    _execute_tool_and_respond, run_todo_loop,
)
import server.state as _st
from server.roster import build_ping_map, agents_with_cap, default_agent_slug, get_roster


async def handle(websocket, data: dict, loop) -> bool:
    msg_type = data.get("type")
    selene   = _st.selene_ref

    # ── chat ──────────────────────────────────────────────────────────────────
    if msg_type == "chat":
        t_start    = time.perf_counter()
        user_input = data.get("content", "").strip()
        print(f"[Chat]: received msg, selene={'SET' if selene else 'NONE'}, input={user_input[:40]!r}")
        if not user_input:
            return True

        # Pending idea routing intercept (Sage) — check before tool confirmation
        if selene and getattr(selene, "pending_idea_routing", None):
            _pending_route = selene.pending_idea_routing
            _lower = user_input.strip().lower()
            _KEEP  = {"keep", "keep it", "keep here", "save here", "sage", "me", "mine", "here"}
            _YES   = {"yes", "yeah", "yep", "sure", "ok", "okay", "go ahead", "do it", "y",
                      "route it", "send it", "that works", "sounds good"}
            # Check if user named an agent slug directly — built from roster
            _AGENT_SLUGS = {a["display_name"] for a in get_roster()}
            _named_agent = next((s for s in _AGENT_SLUGS if s in _lower), None)

            is_keep = _lower in _KEEP or any(_lower.startswith(k) for k in _KEEP)
            is_yes  = _lower in _YES or any(_lower.startswith(y) for y in _YES)

            if is_keep or is_yes or _named_agent:
                selene.pending_idea_routing = None
                distilled     = _pending_route["distilled"]
                original_text = _pending_route["original_text"]
                suggestion    = _pending_route["suggestion"]

                if is_keep or (is_yes is False and not _named_agent):
                    _target = "sage"
                    _command = "save_idea_to_agent"
                elif _named_agent and _named_agent not in ("Selene/Sage", "me"):
                    _target  = _named_agent
                    _command = "save_idea_to_agent"
                else:
                    _target  = suggestion["agent"] if is_yes else "Selene/Sage"
                    _command = "save_idea_to_agent"

                _route_res = await loop.run_in_executor(
                    None, selene.tool_router.route_and_execute, "manifest_manager",
                    {"command": _command, "agent": _target,
                     "distilled": distilled, "original_text": original_text}
                )
                _inner = _route_res.get("data", "")
                if isinstance(_inner, str):
                    _reply = _inner
                elif _target == "Selene/Sage":
                    _reply = f"Saved to my own sketchpad — {distilled.get('title', 'idea')} is staying here."
                else:
                    _reply = f"Routed **{distilled.get('title', 'idea')}** to {_target.capitalize()}'s sketchpad."

                update_memory_and_energy(user_input, _reply)
                _agent_name = getattr(selene, "active_agent_name", "Selene").lower()
                await websocket.send_json({"type": "response", "content": _reply, "agent": _agent_name})
                await websocket.send_json({"type": "state", "data": get_state()})
                return True
            # Ambiguous reply — fall through to normal chat, keep pending

        # Pending tool confirmation intercept
        if selene and hasattr(selene, "tool_suggestion") and selene.tool_suggestion:
            conf_result = selene.tool_suggestion.check_pending_confirmation(user_input)
            if conf_result is not None:
                if conf_result["action"] == "execute":
                    _tool_resp = await loop.run_in_executor(
                        None,
                        lambda: _execute_tool_and_respond(
                            conf_result["tool_name"], conf_result["args"],
                            conf_result["context"], "confirmed"
                        )
                    )
                    update_memory_and_energy(user_input, _tool_resp)
                    cleaned           = clean_xml_tags(_tool_resp)
                    active_agent_name = getattr(selene, "active_agent_name", "Selene").lower()
                    await websocket.send_json({"type": "response", "content": cleaned, "agent": active_agent_name})
                    await websocket.send_json({"type": "state", "data": get_state()})
                    return True
                # cancel or ambiguous — fall through to normal chat

        # /invite @agent — adds agent as participant, grants history access
        _INVITE_MATCH = re.match(r'^/invite\s+@(\w+)', user_input.strip(), re.IGNORECASE)
        if _INVITE_MATCH and selene:
            _inv_slug = _INVITE_MATCH.group(1).lower()
            _conv_id  = selene.active_conversation_id
            if _conv_id:
                _inv_ok = selene.add_participant(_conv_id, _inv_slug)
                # Surface current history so invited agent has full context
                _hist_snapshot = list(selene.working_memory)
                _inv_msg = (
                    f"@{_inv_slug.capitalize()} has been invited to this conversation."
                    if _inv_ok else
                    f"Could not invite @{_inv_slug} — are they a valid agent?"
                )
                await websocket.send_json({
                    "type": "participant_added", "conv_id": _conv_id,
                    "agent": _inv_slug, "ok": _inv_ok,
                    "participants": selene.get_participants(_conv_id),
                })
                await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})
                await websocket.send_json({"type": "response", "content": _inv_msg,
                                           "agent": getattr(selene, "active_agent_name", "selene").lower()})
                await websocket.send_json({"type": "state", "data": get_state()})
            return True

        # ── Agent ping routing ────────────────────────────────────────────────
        # Collect ALL @mentions in order — multi-ping supported (@Sage @Akari etc.)
        _PING_MAP  = build_ping_map()   # {slug: display_name}
        _slug_pat  = "|".join(re.escape(s) for s in _PING_MAP.keys())
        _mentions  = re.findall(rf'@({_slug_pat})\b', user_input, flags=re.IGNORECASE) if _slug_pat else []
        _pinged_slugs = list(dict.fromkeys(m.lower() for m in _mentions))  # dedup, preserve order

        # Check if this is a group chat (>1 participant) with no explicit pings
        _conv_participants: list = []
        if selene and selene.active_conversation_id:
            _conv_participants = selene.get_participants(selene.active_conversation_id) or []
        _is_group = len(_conv_participants) > 1
        _origin_slug = getattr(selene, "active_agent_slug", "selene") if selene else "selene"

        # Build the list of agents that will respond this turn:
        #   - Explicit pings → respond in mention order
        #   - Group chat, no pings → all participants respond in succession
        #   - Single agent / no pings → normal path (empty list, falls through below)
        _respond_slugs: list[str] = []
        if _pinged_slugs:
            _respond_slugs = _pinged_slugs
        elif _is_group:
            _respond_slugs = list(_conv_participants)

        # Strip @mentions from the message the agents will see
        if _slug_pat:
            user_input = re.sub(rf'@(?:{_slug_pat})\b', '', user_input, flags=re.IGNORECASE).strip()

        # ── Multi-agent response loop ──────────────────────────────────────────
        if _respond_slugs and selene:
            # Common pre-work: create conv, log user turn once
            if selene.active_conversation_id is None:
                await loop.run_in_executor(None, selene.new_conversation)
            _ms_session = selene.active_conversation_id or "default"
            await loop.run_in_executor(
                None, selene.db.log_dialog, _ms_session, "user", user_input, "", "sent"
            )
            set_last_message_status(_ms_session, "read")
            await websocket.send_json({"type": "read_receipt", "status": "read"})

            _is_first_msg = (len(selene.working_memory) == 0 and selene.active_conversation_name == "New Conversation")

            for _resp_slug in _respond_slugs:
                # Only swap if not already on this agent
                _current_slug = getattr(selene, "active_agent_slug", None)
                if _current_slug != _resp_slug:
                    try:
                        await loop.run_in_executor(None, selene.swap_agent, _resp_slug)
                    except Exception as _sw_err:
                        print(f"[Ping]: swap to {_resp_slug} failed — {_sw_err}")
                        continue

                _agent_label = getattr(selene, "active_agent_name", _resp_slug).lower()
                await websocket.send_json({"type": "thinking", "agent": _agent_label})

                def _make_thought_cb(_lbl=_agent_label):
                    def _cb(step, title, content):
                        asyncio.run_coroutine_threadsafe(
                            websocket.send_json({"type": "thought", "step": step, "title": title,
                                                 "content": content, "agent": _lbl}),
                            loop
                        )
                    return _cb
                selene.thought_callback = _make_thought_cb()

                # Presence layer
                try:
                    _gate_res = await loop.run_in_executor(None, selene.run_choice_layer, user_input)
                    _gate = _gate_res.get("gating", "RESPOND")
                    _rmode = _gate_res.get("response_mode", "CONVERSATIONAL")
                except Exception:
                    _gate, _rmode = "RESPOND", "CONVERSATIONAL"

                if _gate == "IGNORE":
                    await websocket.send_json({"type": "read_receipt", "status": "ignored", "agent": _agent_label})
                    continue

                # Generate response
                try:
                    _resp = await loop.run_in_executor(
                        None, lambda: process_message(user_input, response_mode=_rmode)
                    )
                except Exception as _exc:
                    await websocket.send_json({"type": "response",
                                               "content": f"[{_agent_label} error]: {_exc}",
                                               "agent": _agent_label})
                    continue
                finally:
                    selene.thought_callback = None

                _cleaned = clean_xml_tags(_resp)
                await websocket.send_json({"type": "response", "content": _cleaned, "agent": _agent_label})
                update_memory_and_energy(user_input, _resp, response_mode=_rmode)

            # Swap back to origin agent
            if selene.active_agent_slug != _origin_slug:
                try:
                    await loop.run_in_executor(None, selene.swap_agent, _origin_slug)
                except Exception:
                    pass

            # Auto-name conversation
            if _is_first_msg and selene.active_conversation_id:
                _cid = selene.active_conversation_id
                _auto = selene.auto_name_from_message(user_input)
                selene.rename_conversation(_cid, _auto)
                await websocket.send_json({"type": "conversation_renamed", "id": _cid, "name": _auto})

            await loop.run_in_executor(None, selene.save_current_conversation)
            await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})
            await broadcast({"type": "state", "data": get_state()})
            return True

        # Auto-create conversation on clean boot
        if selene and selene.active_conversation_id is None:
            await loop.run_in_executor(None, selene.new_conversation)
        is_first_message = (
            selene is not None
            and len(selene.working_memory) == 0
            and selene.active_conversation_name == "New Conversation"
        )
        session_id = selene.active_conversation_id or "default" if selene else "default"
        if selene:
            await loop.run_in_executor(
                None, selene.db.log_dialog, session_id, "user", user_input, "", "sent"
            )

        # Presence Layer
        gating = "RESPOND"
        choice: dict = {}
        choice_latency = 0.0
        if selene:
            t_choice_start = time.perf_counter()
            choice         = await loop.run_in_executor(None, selene.run_choice_layer, user_input)
            choice_latency = (time.perf_counter() - t_choice_start) * 1000.0
            gating         = choice.get("gating", "RESPOND")
            print(f"[Selene Server]: Presence Layer → {gating} ({choice_latency:.0f}ms)")

        if gating == "IGNORE":
            if selene:
                set_last_message_status(session_id, "ignored")
            await websocket.send_json({"type": "read_receipt", "status": "ignored"})
            await websocket.send_json({"type": "state", "data": get_state()})
            return True

        elif gating == "OBSERVE":
            if selene:
                set_last_message_status(session_id, "observed")
            await websocket.send_json({"type": "read_receipt", "status": "observed"})

            _obs_thoughts = ""
            if selene:
                _observe_system = (
                    f"{selene.system_prompt}\n\n"
                    "[OBSERVE MODE] You are silently witnessing this moment. "
                    "Think about what you're observing — your reactions, feelings, "
                    "and internal impressions. Do NOT speak or reply aloud."
                )
                try:
                    _obs_resp = await loop.run_in_executor(
                        None,
                        lambda: selene.llm_caller.call_llm(
                            input_data=user_input,
                            system_prompt=_observe_system,
                            history=[{k: v for k, v in m.items() if k != "ts"} for m in selene.working_memory[-6:]],
                            temperature=0.8,
                            max_tokens=512,
                        )
                    )
                    _tm = re.search(r'<think>([\s\S]*?)</think>', _obs_resp or "", re.DOTALL | re.IGNORECASE)
                    if _tm and _tm.group(1).strip():
                        _obs_thoughts = _tm.group(1).strip()
                    elif _obs_resp and not _tm:
                        _stripped = re.sub(r'\n{2,}.*', '', _obs_resp, flags=re.DOTALL).strip()
                        _obs_thoughts = _stripped or _obs_resp.strip()

                    if _obs_thoughts:
                        await websocket.send_json({
                            "type": "thought", "step": "reasoning",
                            "title": "Silent Observation", "content": _obs_thoughts
                        })
                except Exception as _obs_err:
                    print(f"[Selene Server]: Observe think pass failed — {_obs_err}")

            if selene:
                _obs_ts = time.time()
                _agent  = getattr(selene, "active_agent_name", "selene").lower()
                # Store both sides so working_memory stays alternating user/assistant.
                # The assistant entry uses the observation thoughts if available,
                # otherwise a placeholder so the model knows it silently observed.
                _obs_memory = (
                    f"[OBSERVED SILENTLY] {_obs_thoughts}"
                    if _obs_thoughts
                    else "[OBSERVED SILENTLY — no internal thoughts recorded]"
                )
                with selene.lock:
                    selene.working_memory.append({"role": "user",      "content": user_input,   "ts": _obs_ts})
                    selene.working_memory.append({"role": "assistant",  "content": _obs_memory,  "ts": _obs_ts, "agent": _agent})
                    window = selene.memory_window * 2
                    if len(selene.working_memory) > window:
                        selene.working_memory = selene.working_memory[-window:]
                selene.db.log_dialog(session_id, "assistant", "[OBSERVED — no spoken reply]", _obs_thoughts, "observed")
                selene.maybe_extract_memory(user_input, _obs_thoughts or "[observed]")
                if _obs_thoughts:
                    try:
                        selene.db.log_meta_insight(
                            agent=getattr(selene, "active_agent_name", "selene").lower(),
                            category="observation", subcategory="silent_observe",
                            input_context=user_input[:500], reasoning=_obs_thoughts[:3000],
                            result="[no spoken reply — observe mode]",
                            emotional_state_before={"energy": selene.creative_energy, "status": "idle"},
                            emotional_state_after={"energy": selene.creative_energy,  "status": "idle"},
                            confidence_score=0.9, trigger_mode="observe", session_id=session_id,
                        )
                    except Exception:
                        pass

            await websocket.send_json({"type": "state", "data": get_state()})
            return True

        # RESPOND path
        if selene:
            set_last_message_status(session_id, "read")
        await websocket.send_json({"type": "read_receipt", "status": "read"})
        await websocket.send_json({"type": "thinking"})

        def handle_thought(step, title, content):
            asyncio.run_coroutine_threadsafe(
                websocket.send_json({"type": "thought", "step": step, "title": title, "content": content}),
                loop
            )
        if selene:
            selene.thought_callback = handle_thought

        _response_mode = choice.get("response_mode", "CONVERSATIONAL") if selene else "CONVERSATIONAL"
        t_llm_start = time.perf_counter()
        try:
            response = await loop.run_in_executor(
                None, lambda: process_message(user_input, response_mode=_response_mode)
            )
        except Exception as exc:
            err_msg = f"[Selene Error]: LM Studio call failed -- {type(exc).__name__}: {exc}"
            print(err_msg)
            if selene:
                selene.thought_callback = None
                # Save failed turn to working memory so it stays in context
                import time as _t
                _ts = _t.time()
                with selene.lock:
                    selene.working_memory.append({"role": "user", "content": user_input, "ts": _ts})
                    selene.working_memory.append({"role": "assistant", "content": f"[ERROR] {type(exc).__name__}: {exc}", "ts": _ts})
            await websocket.send_json({"type": "response", "content": err_msg})
            await websocket.send_json({"type": "state",    "data": get_state()})
            return True
        llm_latency = (time.perf_counter() - t_llm_start) * 1000.0

        if selene:
            selene.thought_callback = None

        # ── Multi-step todo loop ───────────────────────────────────────────
        # If process_message caused a todo plan to be created (the model called
        # the todo tool with plan command), drive the remaining steps now.
        if selene:
            _todo = selene.tool_router.tools.get("todo")
            if _todo:
                _plan = _todo.get_plan()
                _steps = _plan.get("steps", [])
                # A plan is "just started" if it has steps and the first one is
                # still in_progress (the model planned but hasn't executed yet)
                # AND at least one step has a tool defined (otherwise it's a
                # pure LLM plan the model will handle via its response)
                _has_tool_steps = any(s.get("tool") for s in _steps)
                _current = next((s for s in _steps if s.get("status") == "in_progress"), None)
                if _current and _has_tool_steps:
                    _todo_results = await loop.run_in_executor(
                        None,
                        lambda: run_todo_loop(user_input, websocket, loop, _response_mode)
                    )
                    # If the loop produced results, update memory with a summary
                    if _todo_results:
                        _summary = "\n".join(
                            f"• {desc}: {res[:200]}" for desc, res in _todo_results
                        )
                        update_memory_and_energy(
                            user_input,
                            f"[Multi-step plan complete]\n{_summary}",
                            response_mode=_response_mode,
                        )
                        await websocket.send_json({"type": "state", "data": get_state()})
                        return True

        presence_mode = extract_presence_decision(response)
        if presence_mode in ("observe", "ignore"):
            receipt_status = "observed" if presence_mode == "observe" else "ignored"
            if selene:
                set_last_message_status(session_id, receipt_status)
            total_latency = (time.perf_counter() - t_start) * 1000.0
            await websocket.send_json({"type": "read_receipt", "status": receipt_status})
            await websocket.send_json({"type": "latency_metrics",
                                       "choice_latency_ms": round(choice_latency, 2),
                                       "llm_latency_ms": round(llm_latency, 2),
                                       "total_latency_ms": round(total_latency, 2)})
            await websocket.send_json({"type": "state", "data": get_state()})
            return True

        total_latency     = (time.perf_counter() - t_start) * 1000.0
        cleaned_response  = clean_xml_tags(response)
        active_agent_name = getattr(selene, "active_agent_name", "Selene").lower() if selene else "selene"

        if active_agent_name == "selene":
            import random as _rand
            chunks = split_response_chunks(cleaned_response)
            for i, chunk in enumerate(chunks):
                if i > 0:
                    await websocket.send_json({"type": "thinking", "inter_chunk": True})
                    base_delay = _rand.uniform(1.2, 2.8)
                    char_delay = len(chunk) * 0.008
                    delay      = min(4.5, base_delay + char_delay)
                    await asyncio.sleep(delay)
                await websocket.send_json({"type": "response", "content": chunk, "agent": active_agent_name})
            update_memory_and_energy(user_input, response, chunks=chunks, response_mode=_response_mode)
        else:
            await websocket.send_json({"type": "response", "content": cleaned_response, "agent": active_agent_name})
            update_memory_and_energy(user_input, response, response_mode=_response_mode)

        # Refresh emotion cache after turn
        if selene:
            try:
                _mo = selene.emotion_classifier.mood_observer
                _dom, _int = _mo.get_dominant_mood()
                _st._cached_emotion["mood_index"] = int(_int * 100)
                _st._cached_emotion["emotion"]    = _dom if _dom != "neutral" else ""
            except Exception:
                pass

        # Auto-name on first message
        if is_first_message and selene and selene.active_conversation_id:
            conv_id   = selene.active_conversation_id
            auto_name = selene.auto_name_from_message(user_input)
            selene.rename_conversation(conv_id, auto_name)
            await websocket.send_json({"type": "conversation_renamed", "id": conv_id, "name": auto_name})

        if selene:
            await loop.run_in_executor(None, selene.save_current_conversation)
            await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})

        await websocket.send_json({"type": "latency_metrics",
                                   "choice_latency_ms": round(choice_latency, 2),
                                   "llm_latency_ms": round(llm_latency, 2),
                                   "total_latency_ms": round(total_latency, 2)})
        await websocket.send_json({"type": "state", "data": get_state()})
        return True

    # ── force_generate ────────────────────────────────────────────────────────
    elif msg_type == "force_generate":
        if selene:
            _continue_prompt  = data.get("prompt", "").strip() or "Continue."
            active_agent_name = getattr(selene, "active_agent_name", "Selene").lower()

            await websocket.send_json({"type": "thinking"})

            def handle_continue_thought(step, title, content):
                asyncio.run_coroutine_threadsafe(
                    websocket.send_json({"type": "thought", "step": step, "title": title, "content": content}),
                    loop
                )
            selene.thought_callback = handle_continue_thought

            try:
                _resp = await loop.run_in_executor(
                    None, lambda: selene.chat(_continue_prompt, disable_tools=False)
                )
            except Exception as _exc:
                _resp = f"[Generation error: {_exc}]"
            selene.thought_callback = None

            _cleaned = clean_xml_tags(_resp)
            if active_agent_name == "selene":
                import random as _r
                _chunks = split_response_chunks(_cleaned)
                for i, chunk in enumerate(_chunks):
                    if i > 0:
                        await websocket.send_json({"type": "thinking", "inter_chunk": True})
                        await asyncio.sleep(_r.uniform(1.2, 2.5))
                    await websocket.send_json({"type": "response", "content": chunk, "agent": active_agent_name})
                update_memory_and_energy(_continue_prompt, _resp, chunks=_chunks)
            else:
                await websocket.send_json({"type": "response", "content": _cleaned, "agent": active_agent_name})
                update_memory_and_energy(_continue_prompt, _resp)

            await websocket.send_json({"type": "state", "data": get_state()})
        return True

    # ── rollback_last_turn ────────────────────────────────────────────────────
    elif msg_type == "rollback_last_turn":
        if selene:
            selene.rollback_last_turn()
            await websocket.send_json({"type": "rollback_ack"})
        return True

    # ── clear_memory (legacy) ─────────────────────────────────────────────────
    elif msg_type == "clear_memory":
        if selene:
            conv_info = await loop.run_in_executor(None, selene.new_conversation)
            await websocket.send_json({
                "type": "conversation_loaded",
                "id": conv_info["id"], "name": conv_info["name"], "messages": [],
                "participants": conv_info.get("participants", []),
            })
            print("[Selene Server]: Memory cleared by UI (legacy).")
        await websocket.send_json({"type": "state", "data": get_state()})
        return True
    return False
