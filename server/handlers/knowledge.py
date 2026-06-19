"""
server/handlers/knowledge.py — Knowledge board and research tools
"""

from server.state import clients
import server.state as _st


async def handle(websocket, data: dict, loop) -> bool:
    msg_type = data.get("type")
    selene   = _st.selene_ref

    if not msg_type.startswith("knowledge_"):
        return False

    if not selene:
        await websocket.send_json({"type": "error", "message": "Selene not initialised."})
        return True

    k_tool = selene.tool_router.tools.get("knowledge_manager")

    if msg_type == "knowledge_get_state":
        if k_tool:
            await websocket.send_json({"type": "knowledge_state", "data": k_tool.load_state()})
        return True

    elif msg_type == "knowledge_save_card":
        if k_tool:
            card_type = data.get("card_type") or data.get("type") or "manual_note"
            if card_type == "knowledge_save_card":
                card_type = "manual_note"
            k_tool.add_card(
                data.get("title", "Untitled Card"),
                data.get("content", ""),
                card_type,
                data.get("source_url"),
            )
            state = k_tool.load_state()
            for client in clients:
                await client.send_json({"type": "knowledge_state", "data": state})
        return True

    elif msg_type == "knowledge_delete_card":
        if k_tool:
            k_tool.delete_card(data.get("id", ""))
            state = k_tool.load_state()
            for client in clients:
                await client.send_json({"type": "knowledge_state", "data": state})
        return True

    elif msg_type == "knowledge_update_card":
        if k_tool:
            card_id  = data.get("id", "")
            title    = data.get("title", "")
            content  = data.get("content", "")
            category = data.get("category", "")
            if card_id and title:
                k_tool.update_card(card_id, title, content, category)
                state = k_tool.load_state()
                for client in clients:
                    await client.send_json({"type": "knowledge_state", "data": state})
        return True

    elif msg_type == "knowledge_sync_board":
        if k_tool:
            k_tool.sync_board(data.get("cards", []))
            state = k_tool.load_state()
            for client in clients:
                await client.send_json({"type": "knowledge_state", "data": state})
        return True

    elif msg_type == "knowledge_search_web":
        if k_tool:
            query = data.get("query", "")
            await websocket.send_json({"type": "knowledge_searching", "query": query})
            res_dict = await loop.run_in_executor(None, k_tool.unified_search, query)
            results  = res_dict.get("results", []) if isinstance(res_dict, dict) else []
            await websocket.send_json({"type": "knowledge_search_results", "data": results})
        return True

    elif msg_type == "knowledge_enrich_card":
        if k_tool:
            await loop.run_in_executor(None, k_tool.enrich_card, data.get("id", ""))
            state = k_tool.load_state()
            for client in clients:
                await client.send_json({"type": "knowledge_state", "data": state})
        return True

    elif msg_type == "knowledge_summarize_and_save":
        if k_tool:
            raw_text = data.get("content", data.get("text", ""))
            if not raw_text.strip():
                await websocket.send_json({"type": "knowledge_save_error", "error": "No text provided."})
            else:
                await websocket.send_json({"type": "knowledge_summarizing"})
                word_count = len(raw_text.split())
                if word_count <= 80:
                    summary = raw_text.strip()
                else:
                    def _summarize():
                        prompt = (
                            f"Summarize the following into 2--4 concise sentences "
                            f"suitable as a knowledge card. Capture the core idea.\n\n"
                            f"{raw_text[:4000]}"
                        )
                        si = _st.selene_ref
                        if si:
                            return si.llm_caller.call_llm(
                                input_data=prompt,
                                system_prompt="Output only the summary. No preamble.",
                                history=[], temperature=0.3, max_tokens=200,
                            )
                        return raw_text[:400]
                    summary = await loop.run_in_executor(None, _summarize)
                    summary = (summary or "").strip() or raw_text[:400]

                new_card = k_tool.add_card(
                    title=data.get("title", "Untitled Card"),
                    content=summary,
                    card_type=data.get("card_type", "manual_note"),
                    source_url=data.get("source_url"),
                    category=data.get("category", ""),
                    extended_content=raw_text if word_count > 80 else None,
                )
                state = k_tool.load_state()
                for client in clients:
                    await client.send_json({"type": "knowledge_state", "data": state})
                await websocket.send_json({"type": "knowledge_summarized", "card": new_card})
        return True

    elif msg_type == "knowledge_arxiv_search":
        if k_tool:
            query       = data.get("query", "")
            max_results = int(data.get("max_results", 6))
            await websocket.send_json({"type": "knowledge_searching", "query": query, "source": "arxiv"})
            results = await loop.run_in_executor(None, k_tool.search_arxiv, query, max_results)
            await websocket.send_json({"type": "knowledge_arxiv_results", "data": results, "query": query})
        return True

    elif msg_type == "knowledge_rss_add":
        if k_tool:
            res = await loop.run_in_executor(None, k_tool.rss_add, data.get("name", ""), data.get("url", ""))
            await websocket.send_json({"type": "knowledge_rss_result", "data": res})
        return True

    elif msg_type == "knowledge_rss_list":
        if k_tool:
            res = await loop.run_in_executor(None, k_tool.rss_list)
            await websocket.send_json({"type": "knowledge_rss_list", "data": res})
        return True

    elif msg_type == "knowledge_rss_scan":
        if k_tool:
            blog_name = data.get("blog_name")
            await websocket.send_json({"type": "knowledge_searching", "query": "RSS feeds", "source": "rss"})
            res   = await loop.run_in_executor(None, k_tool.rss_scan, blog_name)
            added = []
            for art in res:
                if art.get("url"):
                    card = k_tool.add_card(
                        title=art.get("title", "RSS Article"),
                        content=f"Blog: {art.get('blog','')}\nPublished: {art.get('published','')}",
                        card_type="rss_article",
                        source_url=art.get("url"),
                    )
                    added.append(card)
            state = k_tool.load_state()
            for client in clients:
                await client.send_json({"type": "knowledge_state", "data": state})
            await websocket.send_json({
                "type": "knowledge_rss_scan_result",
                "articles_found": len(res), "cards_added": len(added),
            })
        return True

    return False
