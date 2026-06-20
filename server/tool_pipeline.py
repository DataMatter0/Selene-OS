"""
server/tool_pipeline.py — Tool execution and message routing
──────────────────────────────────────────────────────────────
Owns:
  _generate_tool_reasoning_background()  — post-hoc reasoning logger (background thread)
  _execute_tool_and_respond()            — shared tool execution path
  process_message()                      — routes user input: slash → suggestion → chat
  set_last_message_status()              — stamps read/observed/ignored on last user turn
  update_memory_and_energy()             — commits a turn to working_memory
"""

import re
import threading
import time
import uuid
from typing import Any, Optional

from .state import get_state, broadcast
from .utils import _format_tool_data, clean_xml_tags


# ── Lazy selene accessor ──────────────────────────────────────────────────────
# tool_pipeline is imported at module level by handlers, so it cannot import
# state.selene_ref at import time (circular). Use the accessor instead.

def _selene():
    from . import state as _s
    return _s.selene_ref


# ── Background reasoning logger ───────────────────────────────────────────────

def _generate_tool_reasoning_background(
    selene_ref, tool_name: str, trigger_mode: str,
    user_input: str, tool_args: str, tool_result: str,
    session_id: str, turn_id: str, chain_id: str = ""
):
    """
    Background thread: generates post-hoc validation reasoning for a tool call
    and writes it to tool_reasoning_log.
    """
    try:
        agent = getattr(selene_ref, "active_agent_name", "selene").lower()

        if trigger_mode == "direct":
            reasoning = (
                f"Direct user request. Ghost's message contained a keyword that maps to "
                f"{tool_name}. Result confirmed the call was appropriate."
            )
        else:
            reasoning_prompt = (
                f"You called the '{tool_name}' tool during this conversation turn.\n\n"
                f"Ghost said: {user_input[:300]}\n"
                f"Tool args: {tool_args[:200]}\n"
                f"Tool result: {tool_result[:300]}\n\n"
                f"In one sentence, explain why calling this tool was or was not necessary "
                f"given what Ghost said. Be honest — if the call was unnecessary, say so."
            )
            reasoning = selene_ref.llm_caller.call_llm(
                input_data="/no_think\n" + reasoning_prompt,
                system_prompt="Reply with exactly one sentence of honest post-hoc reasoning. No preamble.",
                history=[],
                temperature=0.3,
                max_tokens=80,
            )
            reasoning = re.sub(r'<think>[\s\S]*?</think>', '', reasoning, flags=re.IGNORECASE).strip()

        selene_ref.db.log_tool_reasoning(
            agent=agent,
            session_id=session_id,
            turn_id=turn_id,
            tool_name=tool_name,
            trigger_mode=trigger_mode,
            input_context=user_input,
            tool_args=tool_args,
            tool_result=tool_result,
            reasoning=reasoning,
            chain_id=chain_id,
        )
        print(f"[Tool Reasoning]: Logged for {tool_name} ({trigger_mode})")
    except Exception as e:
        print(f"[Tool Reasoning]: Failed — {e}")


# ── Shared tool execution path ────────────────────────────────────────────────

def _execute_tool_and_respond(triggered_name: str, triggered_args: Any,
                               user_input: str, trigger_mode: str = "direct") -> str:
    """Shared tool execution path used by both keyword and suggestion routes."""
    selene = _selene()
    if hasattr(selene, "thought_callback") and selene.thought_callback:
        selene.thought_callback("tool_call", f"Tool: {triggered_name}", f"Args: {triggered_args}")

    _emotion_before = {"energy": selene.creative_energy, "status": "idle"}
    result          = selene.tool_router.route_and_execute(triggered_name, triggered_args)
    result_data     = _format_tool_data(result.get("data", ""))
    _emotion_after  = {"energy": selene.creative_energy, "status": "idle"}

    session_id = selene.active_conversation_id or "default"
    turn_id    = str(uuid.uuid4())

    try:
        selene.db.log_meta_insight(
            agent=getattr(selene, "active_agent_name", "selene").lower(),
            category="tool_use", subcategory=triggered_name,
            input_context=user_input[:500],
            reasoning=f"Trigger: {trigger_mode}. Args: {str(triggered_args)[:400]}",
            result=result_data[:1000],
            emotional_state_before=_emotion_before,
            emotional_state_after=_emotion_after,
            confidence_score=1.0 if trigger_mode in ("direct", "slash") else 0.85,
            trigger_mode=trigger_mode, session_id=session_id,
        )
    except Exception:
        pass

    threading.Thread(
        target=_generate_tool_reasoning_background,
        args=(selene, triggered_name, trigger_mode, user_input,
              str(triggered_args), result_data, session_id, turn_id),
        daemon=True
    ).start()

    if hasattr(selene, "thought_callback") and selene.thought_callback:
        selene.thought_callback("tool_response", f"Tool Completed: {triggered_name}",
                                f"Status: {result.get('status')}\nResult: {result_data[:300]}")

    thought_log = (f"[Tool: {triggered_name} | trigger: {trigger_mode}]\n"
                   f"Args: {triggered_args}\nResult: {result_data}")
    selene.db.log_dialog(session_id, "assistant", result_data, thought_log, "read")

    final_reply = _format_tool_data(result.get("data")) if result.get("status") == "success" \
                  else f"I tried to use {triggered_name} but something went wrong: {result.get('message')}"

    return f"<tool_reasoning turn_id=\"{turn_id}\">\n{thought_log}\n</tool_reasoning>\n{final_reply}"


# ── Message router ────────────────────────────────────────────────────────────

def process_message(user_input: str, disable_tools: bool = False,
                    suggestion_warning: str = "",
                    response_mode: str = "CONVERSATIONAL") -> str:
    """
    Routes a user message: slash command → phrase suggestion gate → normal chat.
    suggestion_warning is injected when the suggestion layer flagged low confidence.
    """
    selene = _selene()
    if selene is None:
        return "[System Error]: Selene is not initialised."

    if not disable_tools and hasattr(selene, "tool_suggestion") and selene.tool_suggestion:
        decision = selene.tool_suggestion.process(user_input)

        if decision["decision"] == "execute":
            return _execute_tool_and_respond(
                decision["tool_name"], decision["args"],
                user_input, decision.get("trigger", "direct")
            )

        elif decision["decision"] == "suggest":
            return selene.chat(user_input, disable_tools=True,
                               _suggestion_warning=decision.get("warning", ""),
                               _response_mode=response_mode)

        # decision == "pass" — fall through to normal chat

    # Legacy keyword trigger fallback
    triggered_args = None
    triggered_name = None
    if not disable_tools:
        allowed = getattr(selene, "allowed_tools", None)
        for tool in selene.tool_router.tools.values():
            if allowed is not None and tool.name not in allowed:
                continue
            if hasattr(tool, "check_and_trigger"):
                triggered_args = tool.check_and_trigger(user_input)
                if triggered_args:
                    triggered_name = tool.name
                    break

    if triggered_name and triggered_args is not None:
        print(f"[Selene Server]: Tool triggered -- {triggered_name}")
        if hasattr(selene, "thought_callback") and selene.thought_callback:
            selene.thought_callback(
                "tool_call", f"Keyword Triggered Tool: {triggered_name}",
                f"Trigger args: {triggered_args}"
            )

        _emotion_before = {"energy": selene.creative_energy, "status": "idle"}
        result      = selene.tool_router.route_and_execute(triggered_name, triggered_args)
        result_data = _format_tool_data(result.get("data", ""))
        _emotion_after = {"energy": selene.creative_energy, "status": "idle"}

        session_id = selene.active_conversation_id or "default"
        turn_id    = str(uuid.uuid4())

        try:
            selene.db.log_meta_insight(
                agent=getattr(selene, "active_agent_name", "selene").lower(),
                category="tool_use", subcategory=triggered_name,
                input_context=user_input[:500],
                reasoning=f"Keyword triggered. Args: {str(triggered_args)[:400]}",
                result=result_data[:1000],
                emotional_state_before=_emotion_before,
                emotional_state_after=_emotion_after,
                confidence_score=1.0,
                trigger_mode="keyword", session_id=session_id,
            )
        except Exception:
            pass

        threading.Thread(
            target=_generate_tool_reasoning_background,
            args=(selene, triggered_name, "direct", user_input,
                  str(triggered_args), result_data, session_id, turn_id),
            daemon=True
        ).start()

        if hasattr(selene, "thought_callback") and selene.thought_callback:
            selene.thought_callback(
                "tool_response", f"Tool Completed: {triggered_name}",
                f"Status: {result.get('status')}\nResult: {result_data[:300]}"
                + ("..." if len(result_data) > 300 else "")
            )

        thought_log = (
            f"[Keyword Triggered Tool: {triggered_name}] Trigger args: {triggered_args}\n"
            f"[Tool Completed: {triggered_name}] Status: {result.get('status')}\nResult: {result_data}"
        )
        selene.db.log_dialog(session_id, "assistant", result_data, thought_log, "read")

        final_reply = _format_tool_data(result.get("data")) if result.get("status") == "success" \
                      else f"I tried to use the {triggered_name} tool, but something went wrong: {result.get('message')}"
        return f"<tool_reasoning turn_id=\"{turn_id}\">\n{thought_log}\n</tool_reasoning>\n{final_reply}"

    return selene.chat(user_input, disable_tools=disable_tools, _response_mode=response_mode)


# ── Memory helpers ────────────────────────────────────────────────────────────


# ── Multi-step todo execution loop ────────────────────────────────────────────

def run_todo_loop(user_input: str, websocket, loop_ref, response_mode: str = "CONVERSATIONAL"):
    """
    Drives an active todo plan to completion, executing each tool step
    automatically and pausing for LLM responses on null-tool steps.

    Called from chat.py after process_message() when a todo plan was just
    created. Runs synchronously in an executor thread; sends WS messages via
    asyncio.run_coroutine_threadsafe.

    Returns list of (description, result) tuples for all steps executed.
    """
    import asyncio as _asyncio
    import json as _json

    selene = _selene()
    if selene is None:
        return []

    todo = selene.tool_router.tools.get("todo")
    if todo is None:
        return []

    def _ws_send(payload: dict):
        _asyncio.run_coroutine_threadsafe(
            websocket.send_json(payload), loop_ref
        )

    results = []
    MAX_STEPS = 20   # hard cap — prevents runaway loops
    step_count = 0

    while step_count < MAX_STEPS:
        plan    = todo.get_plan()
        steps   = plan.get("steps", [])
        current = next((s for s in steps if s.get("status") == "in_progress"), None)

        if current is None:
            break

        step_count += 1
        desc      = current.get("description", f"Step {step_count}")
        tool_name = current.get("tool")
        tool_args = current.get("args") or {}

        _ws_send({"type": "thought", "step": "todo_step",
                  "title": f"Step {step_count}: {desc}",
                  "content": f"Tool: {tool_name or 'LLM'}"})

        if tool_name:
            # ── Tool step — execute directly ───────────────────────────────
            accumulated = todo.get_accumulated_results()
            if accumulated and isinstance(tool_args, dict):
                args_str = _json.dumps(tool_args)
                if "<will be filled" in args_str or "<prior_result>" in args_str:
                    args_str = args_str.replace(
                        "<will be filled from prior step>", accumulated[:800]
                    ).replace("<prior_result>", accumulated[:800])
                    try:
                        tool_args = _json.loads(args_str)
                    except Exception:
                        pass

            allowed = getattr(selene, "allowed_tools", None)
            if allowed is not None and tool_name not in allowed:
                step_result = f"[Blocked] Tool '{tool_name}' is not in this agent's allowed tools."
            else:
                result = selene.tool_router.route_and_execute(tool_name, tool_args)
                from .utils import _format_tool_data
                step_result = _format_tool_data(result.get("data", "")) if result.get("status") == "success" \
                              else f"[Tool error] {result.get('message', 'unknown error')}"

            todo.store_step_result(current["id"], step_result)
            results.append((desc, step_result))

            _ws_send({"type": "thought", "step": "todo_result",
                      "title": f"Done: {desc}", "content": step_result[:400]})

            advance = todo.execute({"command": "advance"})
            if advance.get("all_done"):
                _ws_send({"type": "thought", "step": "todo_complete",
                          "title": "Plan complete",
                          "content": f"All steps finished for: {plan.get('task', '')}"})
                break

        else:
            # ── LLM step — generate a response with prior context ──────────
            accumulated = todo.get_accumulated_results()
            llm_prompt = (
                f"Multi-step task in progress: {plan.get('task', '')}\n\n"
                + (f"Prior steps completed:\n{accumulated}\n\n" if accumulated else "")
                + f"Current step: {desc}\n\n"
                + f"Ghost's original request: {user_input}"
            )

            try:
                llm_result = selene.chat(
                    llm_prompt,
                    disable_tools=True,
                    _response_mode=response_mode,
                )
                from .utils import clean_xml_tags
                llm_clean = clean_xml_tags(llm_result)
            except Exception as e:
                llm_clean = f"[LLM step error: {e}]"

            todo.store_step_result(current["id"], llm_clean)
            results.append((desc, llm_clean))

            active_agent_name = getattr(selene, "active_agent_name", "selene").lower()
            _ws_send({"type": "response", "content": llm_clean, "agent": active_agent_name})

            advance = todo.execute({"command": "advance"})
            if advance.get("all_done"):
                break

    try:
        _ws_send({"type": "todo_state", "data": todo.get_plan()})
    except Exception:
        pass

    return results


def set_last_message_status(session_id: str, status: str) -> None:
    """
    Update the status of the last user message in both SQLite and working_memory.
    Ensures read/observed/ignored markers survive conversation reloads.
    """
    selene = _selene()
    if selene is None:
        return
    try:
        selene.db.update_last_message_status(session_id, status)
    except Exception:
        pass
    with selene.lock:
        for i in range(len(selene.working_memory) - 1, -1, -1):
            if selene.working_memory[i].get("role") == "user":
                selene.working_memory[i]["status"] = status
                break


def update_memory_and_energy(user_input: str, response: str, chunks: list | None = None, response_mode: str = "CONVERSATIONAL") -> None:
    """
    Commit a turn to working_memory.

    chunks — if provided (Selene's split delivery), each chunk is stored as a
    separate assistant entry sharing a chunk_group ID. On conversation reload
    they render as individual bubbles exactly as originally sent.
    """
    selene = _selene()
    if selene is None:
        return
    selene.creative_energy       = min(100, selene.creative_energy + 10)
    selene.last_interaction_time = time.time()

    clean = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL | re.IGNORECASE)
    clean = clean_xml_tags(clean).strip()
    if not clean:
        clean = response.strip()

    _agent = getattr(selene, "active_agent_name", "selene").lower()

    with selene.lock:
        _ts = time.time()
        selene.working_memory.append({"role": "user", "content": user_input, "ts": _ts})

        if chunks and len(chunks) > 1:
            _group_id = str(uuid.uuid4())[:8]
            for chunk in chunks:
                selene.working_memory.append({
                    "role":        "assistant",
                    "content":     chunk,
                    "ts":          _ts,
                    "chunk_group": _group_id,
                    "agent":       _agent,
                })
        else:
            selene.working_memory.append({
                "role":    "assistant",
                "content": clean,
                "ts":      _ts,
                "agent":   _agent,
            })

        window = selene.memory_window * 2
        if len(selene.working_memory) > window:
            selene.working_memory = selene.working_memory[-window:]

    selene.maybe_extract_memory(user_input, response, reflective_turn=(response_mode.upper() == "REFLECT"))
