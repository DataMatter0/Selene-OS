"""
server/ — Selene OS server package
────────────────────────────────────
Extracted from selene_server.py. selene_server.py is now a thin entry point.

Modules:
  config          — env vars, constants, _normalize()
  utils           — clean_xml_tags, split_response_chunks, _format_tool_data,
                    extract_presence_decision
  state           — get_state(), broadcast(), _cached_emotion, clients set
  tool_pipeline   — process_message(), _execute_tool_and_respond(),
                    _generate_tool_reasoning_background(),
                    set_last_message_status(), update_memory_and_energy()
  startup         — _init_selene(), lifespan(), background tasks
  handlers/       — WS message router, one file per domain
"""
