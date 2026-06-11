"""
server/handlers/ — WebSocket message handlers, one file per domain
────────────────────────────────────────────────────────────────────
Each handler module exposes a coroutine:
    async def handle(websocket, data, loop) -> bool

Returns True if it handled the message, False to pass to the next handler.
The dispatcher in selene_server.py calls them in sequence.

Domains:
  chat          — chat, force_generate, rollback_last_turn, clear_memory
  conversations — new/load/rename/list/delete conversation
  memory        — get_memory, save_memory, force_memory_extract,
                  get/add/remove tool_phrases
  manifest      — add/update/toggle/delete/reorder tasks, guidelines,
                  reorganize, compile_and_push, get_manifest, todo_get/clear
  knowledge     — knowledge_get_state, save/delete/update/sync/search/enrich/
                  summarize/arxiv/rss cards
  system        — get_state, get_models, set_model, toggle_agent,
                  save_dashboard_layout, run_latency_test, update_gamepad_config,
                  get_discord_status, check_discord_connectivity,
                  get_integrations_status
  steam         — get_steam_games, launch_steam_game
  youtube       — youtube_query, youtube_search, youtube_watch_start,
                  youtube_segment_push, youtube_chat
  story         — Infinite Story Engine (story_*)
  misc          — maps_query, polymarket_query, document_query, notion_query,
                  meta_insight_query, meta_insight_promote_card
"""
