"""
knowledge_tool.py — Backend Search and Corkboard Database Manager for Selene OS
────────────────────────────────────────────────────────────────────────────────
Manages the local state of draggable idea cards, snap groupings, HTML search
scraping, arxiv paper search, and RSS feed management via blogwatcher-cli.
Outputs structured XML tags for Selene's working memory system prompt.
"""

import os
import json
import shutil
import subprocess
import urllib.request
import urllib.parse
import re
import html
import logging
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional
from .schema import BaseTool

logger = logging.getLogger("knowledge_tool")

# ── Arxiv namespace ───────────────────────────────────────────────────────────
_ARXIV_NS = {
    "atom":    "http://www.w3.org/2005/Atom",
    "arxiv":   "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}

class KnowledgeTool(BaseTool):
    name = "knowledge_manager"
    description = (
        "Manages the knowledge board: web search (DuckDuckGo with AI summaries), "
        "arxiv paper search, RSS feed tracking (blogwatcher), and visual idea cards. "
        "Commands: search, arxiv_search, rss_add, rss_list, rss_scan, "
        "add_card, delete_card, sync_board, get_state, enrich_card."
    )
    input_type = "json"
    output_type = "any"

    def __init__(self, agent_state: Any):
        self.agent_state = agent_state
        self.db_path = os.path.join(self.agent_state.MEMORY_DIR, "knowledge_board_state.json")
        self.on_state_change = None
        self._init_database()

    def _init_database(self) -> None:
        """Initialises the JSON board database if missing."""
        if not os.path.exists(self.db_path):
            self.save_state({"cards": []})

    def load_state(self) -> dict:
        """Loads knowledge board JSON state from disk."""
        try:
            if os.path.exists(self.db_path):
                with open(self.db_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"[Knowledge DB Error]: Failed to load — {e}")
        return {"cards": []}

    def save_state(self, state: dict) -> None:
        """Saves knowledge board JSON state to disk."""
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            with open(self.db_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=4)
            if self.on_state_change:
                self.on_state_change()
        except Exception as e:
            logger.error(f"[Knowledge DB Error]: Failed to save — {e}")

    def _camofox_search(self, query: str, camofox_url: str) -> Optional[List[Dict[str, str]]]:
        """Scrapes DuckDuckGo via the Camofox REST API."""
        import uuid
        import time
        
        user_id = f"selene_{uuid.uuid4().hex[:10]}"
        session_key = f"search_{uuid.uuid4().hex[:10]}"
        encoded_query = urllib.parse.quote_plus(query)
        search_url = f"https://html.duckduckgo.com/html/?q={encoded_query}"
        
        tab_id = None
        try:
            req = urllib.request.Request(
                f"{camofox_url}/tabs",
                data=json.dumps({
                    "userId": user_id,
                    "sessionKey": session_key,
                    "url": search_url
                }).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                tab_data = json.loads(resp.read().decode())
                tab_id = tab_data.get("tabId")
        except Exception as e:
            logger.warning(f"[Camofox DDG Scraper]: Failed to open tab: {e}")
            return None
            
        if not tab_id:
            return None
            
        results = []
        try:
            # Allow page rendering
            time.sleep(2.0)
            req = urllib.request.Request(
                f"{camofox_url}/tabs/{tab_id}/snapshot?userId={user_id}"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                snap_data = json.loads(resp.read().decode())
                snapshot = snap_data.get("snapshot", "")
                
                # Parse accessibility tree
                lines = snapshot.split("\n")
                current_title = None
                current_url = None
                
                for i, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped.startswith("- link ") or stripped.startswith("link "):
                        title_match = re.search(r'link\s+"([^"]*)"', stripped)
                        if title_match:
                            title = title_match.group(1).strip()
                            if i + 1 < len(lines):
                                url_match = re.search(r'/url:\s*(\S+)', lines[i + 1].strip())
                                if url_match:
                                    url = url_match.group(1).strip()
                                    if "/l/?" in url:
                                        parsed_url = urllib.parse.urlparse(url)
                                        query_params = urllib.parse.parse_qs(parsed_url.query)
                                        if "uddg" in query_params:
                                            url = query_params["uddg"][0]
                                    current_title = title
                                    current_url = url
                                    continue
                    
                    if current_title and current_url:
                        if stripped.startswith("- text ") or stripped.startswith("text "):
                            text_match = re.search(r'text\s+"([^"]*)"', stripped)
                            if text_match:
                                snippet = text_match.group(1).strip()
                                if snippet and not any(x in snippet.lower() for x in ["next page", "duckduckgo", "cookies"]):
                                    results.append({
                                        "title": current_title,
                                        "snippet": snippet,
                                        "url": current_url
                                    })
                                    current_title = None
                                    current_url = None
        except Exception as e:
            logger.warning(f"[Camofox DDG Scraper]: Failed to extract results: {e}")
            return None
        finally:
            try:
                req = urllib.request.Request(
                    f"{camofox_url}/sessions/{user_id}",
                    method="DELETE"
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    pass
            except Exception as e:
                logger.debug(f"[Camofox DDG Scraper]: Failed to close session: {e}")
                
        return results

    def scrape_duckduckgo(self, query: str) -> List[Dict[str, str]]:
        """
        Scrapes DuckDuckGo HTML results for the query.
        Tries Camofox stealth browser first if configured and running, otherwise falls back natively.
        """
        camofox_url = os.getenv("CAMOFOX_URL", "").strip() or "http://localhost:9377"
        
        # Test if Camofox is reachable
        camofox_active = False
        try:
            req = urllib.request.Request(f"{camofox_url}/health")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    camofox_active = True
        except Exception:
            pass
            
        if camofox_active:
            logger.info(f"[DDG Scraper]: Querying DuckDuckGo via Camofox browser for '{query}'...")
            results = self._camofox_search(query, camofox_url)
            if results:
                logger.info(f"[DDG Scraper]: Successfully retrieved {len(results)} results via Camofox.")
                return results
            logger.warning("[DDG Scraper]: Camofox scraping returned empty or failed. Falling back to native urllib.")

        logger.info(f"[DDG Scraper]: Querying DuckDuckGo HTML via native urllib for '{query}'...")
        encoded_query = urllib.parse.quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded_query}"
        
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=10.0) as response:
                html_content = response.read().decode("utf-8", errors="ignore")
        except Exception as e:
            logger.error(f"[DDG Scraper Error]: HTTP request failed — {e}")
            return []

        # Find web-result containers
        # DuckDuckGo HTML marks results with result__body container
        results = []
        
        # 1. Split HTML into raw result blocks to prevent regex from crossing result boundaries
        blocks = html_content.split('result__body">')
        if len(blocks) > 1:
            blocks = blocks[1:] # skip header split
        else:
            # Fallback split
            blocks = html_content.split('<div class="links_main')
            if len(blocks) > 1:
                blocks = blocks[1:]

        for block in blocks[:8]:  # Limit to top 8 search results
            # Extract URL and Title safely regardless of attribute ordering
            url_match = re.search(r'<a\s+[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
            # Extract Snippet: <a class="result__snippet" ...>(?P<snippet>.*?)</a> or <div class="result__snippet">(?P<snippet>.*?)</div>
            snippet_match = re.search(r'(?:<a\s+class="result__snippet"[^>]*>|<div\s+class="result__snippet"[^>]*>)(.*?)(?:</a>|</div>)', block, re.DOTALL)

            if url_match:
                raw_url = url_match.group(1).strip()
                # Parse out redirect parameter if DuckDuckGo uses redirect URLs
                if "/l/?" in raw_url:
                    parsed_url = urllib.parse.urlparse(raw_url)
                    query_params = urllib.parse.parse_qs(parsed_url.query)
                    if "uddg" in query_params:
                        raw_url = query_params["uddg"][0]

                raw_title = url_match.group(2)
                # Strip HTML tags inside title
                raw_title = re.sub(r"<[^>]+>", "", raw_title)
                clean_title = html.unescape(raw_title).strip()

                clean_snippet = ""
                if snippet_match:
                    raw_snippet = snippet_match.group(1)
                    raw_snippet = re.sub(r"<[^>]+>", "", raw_snippet)
                    clean_snippet = html.unescape(raw_snippet).strip()

                if clean_title and raw_url:
                    results.append({
                        "title": clean_title,
                        "snippet": clean_snippet or "(No description available)",
                        "url": raw_url
                    })

        logger.info(f"[DDG Scraper]: Successfully retrieved {len(results)} results natively.")
        return results

    def add_card(
        self,
        title: str,
        content: str,
        card_type: str = "manual_note",
        source_url: Optional[str] = None,
        is_active: bool = False,
        category: str = "",
        extended_content: Optional[str] = None,
    ) -> dict:
        """Adds a new card to the backlog side panel or tabletop."""
        state = self.load_state()
        import uuid
        card_id = f"K_{uuid.uuid4().hex[:6].upper()}"
        new_card = {
            "id":               card_id,
            "title":            title.strip(),
            "content":          content.strip(),
            "extended_content": extended_content,   # full text; None if not set
            "category":         category.strip() if category else "",
            "type":             card_type,
            "source_url":       source_url,
            "is_active":        is_active,
            "cluster_id":       None,
            "cluster_title":    "",
            "x":                100,
            "y":                100,
            "width":            200,
            "height":           150,
            "status":           "backlog", # Unified status column
            "links":            [],        # Unified relational links list
            "steps":            [],        # Multi-step task checklist array
        }
        state["cards"].append(new_card)
        self.save_state(state)
        return new_card

    def update_card(self, card_id: str, title: str, content: str, category: str = "") -> Optional[dict]:
        """Update title, content and category of an existing card."""
        state = self.load_state()
        for card in state.get("cards", []):
            if card["id"] == card_id:
                card["title"]   = title.strip()
                card["content"] = content.strip()
                if category is not None:
                    card["category"] = category.strip()
                self.save_state(state)
                return card
        return None

    def delete_card(self, card_id: str) -> bool:
        """Deletes a card from state and handles cleanups."""
        state = self.load_state()
        original_len = len(state["cards"])
        state["cards"] = [c for c in state["cards"] if c["id"] != card_id]
        
        # If any other card was snapped to this card, clean their snaps or merge them
        self.save_state(state)
        return len(state["cards"]) < original_len

    def sync_board(self, cards_list: List[dict]) -> None:
        """Overwrites card positions, snap coordinates, and active states."""
        state = self.load_state()
        # Create quick mapping dictionary of incoming values
        incoming_map = {c["id"]: c for c in cards_list}
        
        for t in state["cards"]:
            tid = t["id"]
            if tid in incoming_map:
                incoming = incoming_map[tid]
                t["is_active"]     = incoming.get("is_active", t["is_active"])
                t["cluster_id"]    = incoming.get("cluster_id", t["cluster_id"])
                t["cluster_title"] = incoming.get("cluster_title", t["cluster_title"])
                t["x"]             = incoming.get("x", t["x"])
                t["y"]             = incoming.get("y", t["y"])
                t["width"]         = incoming.get("width", t["width"])
                t["height"]        = incoming.get("height", t["height"])
                t["status"]        = incoming.get("status", t.get("status", "backlog"))
                t["links"]         = incoming.get("links", t.get("links", []))
                t["steps"]         = incoming.get("steps", t.get("steps", []))
        
        self.save_state(state)

    def compile_active_desk_xml(self) -> str:
        """Compiles only screen-active cards and snaps into structured XML context."""
        state = self.load_state()
        active_cards = [c for c in state.get("cards", []) if c.get("is_active")]
        
        if not active_cards:
            return ""

        # Separate clustered cards and individual cards
        clusters = {}
        solo_cards = []

        for card in active_cards:
            cluster_id = card.get("cluster_id")
            if cluster_id:
                if cluster_id not in clusters:
                    clusters[cluster_id] = {
                        "title": card.get("cluster_title") or f"Knowledge Cluster {cluster_id}",
                        "cards": []
                    }
                clusters[cluster_id]["cards"].append(card)
            else:
                solo_cards.append(card)

        lines = [
            "[ACTIVE TABLETOP — reference material Ghost has open. Use only if directly relevant to his message.]",
            "<active_tabletop>"
        ]

        # 1. Write active snapped clusters
        for cl_id, cl in clusters.items():
            lines.append(f"  <cluster id=\"{cl_id}\" title=\"{cl['title']}\">")
            for c in cl["cards"]:
                lines.append(f"    <card id=\"{c['id']}\" type=\"{c['type']}\">")
                lines.append(f"      <title>{c['title']}</title>")
                lines.append(f"      <content>{c['content']}</content>")
                if c.get("source_url"):
                    lines.append(f"      <source>{c['source_url']}</source>")
                lines.append(f"    </card>")
            lines.append("  </cluster>")

        # 2. Write active standalone cards
        for c in solo_cards:
            lines.append(f"  <card id=\"{c['id']}\" type=\"{c['type']}\">")
            lines.append(f"    <title>{c['title']}</title>")
            lines.append(f"    <content>{c['content']}</content>")
            if c.get("source_url"):
                lines.append(f"    <source>{c['source_url']}</source>")
            lines.append(f"  </card>")

        lines.append("</active_tabletop>")
        return "\n".join(lines)

    def extract_core_webpage_text(self, url: str) -> str:
        """Fetches the webpage URL and extracts core clean text paragraphs."""
        logger.info(f"[Webpage Scraper]: Fetching raw article body from '{url}'...")
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=8.0) as r:
                raw_html = r.read().decode("utf-8", errors="ignore")
        except Exception as e:
            logger.error(f"[Webpage Scraper Error]: Failed to fetch webpage — {e}")
            return f"Failed to retrieve detailed contents: {e}"

        # Clean HTML tags using Regex
        # 1. Remove script, style, head, nav, footer tags and their contents
        raw_html = re.sub(r"<(script|style|head|nav|footer)[^>]*>.*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
        # 2. Remove comments
        raw_html = re.sub(r"<!--.*?-->", "", raw_html, flags=re.DOTALL)
        # 3. Strip all other HTML tags
        clean_text = re.sub(r"<[^>]+>", " ", raw_html)
        # 4. Unescape HTML characters
        clean_text = html.unescape(clean_text)
        
        # 5. Extract lines and filter for high-density paragraph prose
        lines = []
        for line in clean_text.splitlines():
            line = line.strip()
            if not line:
                continue
            # Keep lines that have actual reading content (more than 45 characters, doesn't look like code or UI lists)
            if len(line) > 45 and not line.startswith(("{", "[", "function", "var ", "const ")):
                # Avoid standard cookie notice lines
                if any(x in line.lower() for x in ["cookie", "subscribe", "newsletter", "sign up", "privacy policy"]):
                    continue
                lines.append(line)

        if not lines:
            return "No readable article prose could be extracted from this webpage link."

        # Take first 6 paragraphs, joining them nicely
        summary_text = "\n\n".join(lines[:6])
        if len(summary_text) > 1000:
            summary_text = summary_text[:1000] + "..."
            
        return summary_text

    def enrich_card(self, card_id: str) -> Optional[dict]:
        """Fetches the card's source URL, extracts core body paragraphs, and enriches its content."""
        state = self.load_state()
        card_found = None
        for c in state.get("cards", []):
            if c.get("id") == card_id:
                card_found = c
                break
                
        if not card_found:
            logger.error(f"[Card Enricher Error]: Card '{card_id}' not found.")
            return None
            
        url = card_found.get("source_url")
        if not url:
            logger.error(f"[Card Enricher Error]: Card '{card_id}' does not have a source_url.")
            return None
            
        detailed_text = self.extract_core_webpage_text(url)
        card_found["content"] = detailed_text
        self.save_state(state)
        return card_found

    # ── Arxiv search ──────────────────────────────────────────────────────────

    def search_arxiv(self, query: str, max_results: int = 6) -> List[Dict[str, str]]:
        """
        Query the arxiv Atom feed API.
        Returns list of dicts: title, authors, summary, url, published.
        Requires only Python stdlib — no API key.
        """
        logger.info(f"[Arxiv]: Querying for '{query}' …")
        encoded = urllib.parse.quote_plus(query)
        url = (
            f"https://export.arxiv.org/api/query"
            f"?search_query=all:{encoded}"
            f"&start=0&max_results={max_results}"
            f"&sortBy=relevance&sortOrder=descending"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "SeleneOS/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=12.0) as resp:
                xml_bytes = resp.read()
        except Exception as e:
            logger.error(f"[Arxiv Error]: {e}")
            return []

        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            logger.error(f"[Arxiv XML Parse Error]: {e}")
            return []

        ns = _ARXIV_NS
        results = []
        for entry in root.findall("atom:entry", ns):
            title_el   = entry.find("atom:title", ns)
            summary_el = entry.find("atom:summary", ns)
            published_el = entry.find("atom:published", ns)
            authors = []
            for a in entry.findall("atom:author", ns):
                name_el = a.find("atom:name", ns)
                if name_el is not None:
                    authors.append((name_el.text or "").strip())

            # Prefer the abs link
            paper_url = ""
            for link in entry.findall("atom:link", ns):
                if link.attrib.get("rel") == "alternate":
                    paper_url = link.attrib.get("href", "")
                    break
            if not paper_url:
                id_el = entry.find("atom:id", ns)
                paper_url = (id_el.text or "").strip() if id_el is not None else ""

            results.append({
                "title":     (title_el.text or "").strip().replace("\n", " ") if title_el is not None else "",
                "authors":   ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
                "summary":   (summary_el.text or "").strip()[:500] if summary_el is not None else "",
                "url":       paper_url,
                "published": (published_el.text or "")[:10] if published_el is not None else "",
            })

        logger.info(f"[Arxiv]: Retrieved {len(results)} papers.")
        return results

    # ── Enhanced DDG search with local LLM summaries ─────────────────────────

    def _exa_search(self, query: str, num_results: int = 5) -> List[Dict[str, str]]:
        """Direct REST HTTP call to Exa's search API using httpx."""
        api_key = os.getenv("EXA_API_KEY", "").strip()
        if not api_key:
            logger.warning("[Exa Search]: EXA_API_KEY is not set.")
            return []
            
        import httpx
        url = "https://api.exa.ai/search"
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json"
        }
        payload = {
            "query": query,
            "numResults": num_results,
            "contents": {
                "highlights": True
            }
        }
        
        try:
            logger.info(f"[Exa Search]: POST to {url} with query '{query}'...")
            resp = httpx.post(url, json=payload, headers=headers, timeout=15.0)
            resp.raise_for_status()
            data = resp.json()
            
            results = []
            for item in data.get("results", []):
                title = item.get("title") or "Untitled Result"
                url_ = item.get("url") or ""
                
                # Exa highlights are a list of text highlights matching semantic bounds
                highlights = item.get("highlights", [])
                snippet = " ... ".join(highlights) if highlights else (item.get("text", "")[:300] or "(No description available)")
                
                results.append({
                    "title": title,
                    "snippet": snippet,
                    "url": url_
                })
            logger.info(f"[Exa Search]: Retrieved {len(results)} results.")
            return results
        except Exception as e:
            logger.error(f"[Exa Search Error]: REST request failed — {e}")
            return []

    def decide_search_approach(self, query: str) -> Dict[str, Any]:
        """Queries the local LLM to decide on a single query search vs. multi-query synthesis."""
        llm_caller = getattr(self.agent_state, "llm_caller", None)
        if not llm_caller:
            return {"type": "single", "reason": "No LLM caller available.", "sub_queries": []}
            
        prompt = (
            f"Analyze this search query and decide if it is a simple query (best served by a single search) "
            f"or an exploratory query (requires a deep multi-query research and synthesis of findings).\n\n"
            f"Query: {query}\n\n"
            f"Response format MUST be a valid JSON object matching this schema:\n"
            f"{{\n"
            f"  \"type\": \"single\" | \"multi\",\n"
            f"  \"reason\": \"explanation of choice\",\n"
            f"  \"sub_queries\": [\"sub-query 1\", \"sub-query 2\", \"sub-query 3\"]\n"
            f"}}\n"
            f"(Provide only the raw JSON in your output, no backticks, no preamble, and keep sub_queries empty if type is single).\n\n"
            f"JSON Response:"
        )
        
        try:
            raw_reply = llm_caller.call_llm(
                input_data=prompt,
                system_prompt="You are a precise search decision agent. Output raw, parseable JSON only.",
                temperature=0.2,
                max_tokens=256
            )
            # Parse response cleaning any markdown code blocks
            clean_reply = raw_reply.strip()
            if clean_reply.startswith("```"):
                lines = clean_reply.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                clean_reply = "\n".join(lines).strip()
                
            decision = json.loads(clean_reply)
            logger.info(f"[Search Decision]: LLM chose '{decision.get('type')}' approach — {decision.get('reason')}")
            return decision
        except Exception as e:
            logger.warning(f"[Search Decision Error]: Failed to query LLM — {e}. Defaulting to single search.")
            return {"type": "single", "reason": f"Decision error: {e}", "sub_queries": []}

    def synthesize_research_report(self, query: str, search_results: List[Dict[str, str]]) -> str:
        """Queries the local LLM to synthesize research findings into a cohesive report."""
        llm_caller = getattr(self.agent_state, "llm_caller", None)
        if not llm_caller:
            return "# Search Results\n\n" + "\n\n".join(
                f"### [{r['title']}]({r['url']})\n{r['snippet']}" for r in search_results
            )
            
        # Format the combined search results for the LLM context
        formatted_results = ""
        for i, res in enumerate(search_results, start=1):
            formatted_results += f"Source {i}:\nTitle: {res['title']}\nURL: {res['url']}\nExcerpt: {res['snippet']}\n\n"
            
        prompt = (
            f"You are a professional research analyst. Synthesize the following aggregated search results "
            f"to answer the overall topic query: '{query}'.\n\n"
            f"Aggregated Sources:\n"
            f"{formatted_results}\n"
            f"Instructions:\n"
            f"1. Synthesize these findings into a detailed, cohesive research report in markdown.\n"
            f"2. Group key insights by sub-themes or comparative viewpoints.\n"
            f"3. Explicitly cite the source URLs inline (e.g. [Source Title](URL)) when presenting facts.\n"
            f"4. Avoid fluff and keep the report structured and technical.\n\n"
            f"Research Report:"
        )
        
        try:
            logger.info(f"[Deep Research]: Synthesizing report for '{query}'...")
            report = llm_caller.call_llm(
                input_data=prompt,
                system_prompt="You are a technical research writer. Synthesize findings objectively with proper markdown.",
                temperature=0.4,
                max_tokens=1500
            )
            return report.strip()
        except Exception as e:
            logger.warning(f"[Deep Research Synthesis Error]: Failed — {e}. Falling back to default list.")
            return f"# Research Report: {query}\n\n*Error: LLM synthesis failed — {e}*\n\n" + "\n\n".join(
                f"### [{r['title']}]({r['url']})\n{r['snippet']}" for r in search_results
            )

    def unified_search(self, query: str, search_type: str = "auto", search_backend: str = "auto") -> Dict[str, Any]:
        """Consolidated search capability routing queries to Exa or DDG with single/multi-search logic."""
        # Resolve backend
        if search_backend == "auto":
            if os.getenv("EXA_API_KEY"):
                search_backend = "exa"
            else:
                search_backend = "duckduckgo"
                
        # Helper to execute search on selected backend
        def run_raw_search(q: str) -> List[Dict[str, str]]:
            if search_backend == "exa":
                return self._exa_search(q)
            else:
                return self.scrape_duckduckgo(q)

        # Resolve search type decision
        decision_type = search_type
        decision_reason = "Manual override"
        sub_queries = [query]

        if search_type == "auto":
            decision = self.decide_search_approach(query)
            decision_type = decision.get("type", "single")
            decision_reason = decision.get("reason", "Fallback")
            sub_queries = decision.get("sub_queries", [])
            if not sub_queries:
                sub_queries = [query]

        # Multi search synthesis (Deep Research)
        if decision_type == "multi":
            logger.info(f"[Deep Research]: Executing multi-search for queries: {sub_queries}...")
            all_results = []
            seen_urls = set()
            
            for q in sub_queries:
                raw_res = run_raw_search(q)
                for r in raw_res:
                    url = r.get("url")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_results.append(r)
            
            # Synthesize research report
            synthesis = self.synthesize_research_report(query, all_results)
            
            return {
                "search_type": "deep_research",
                "search_backend": search_backend,
                "query": query,
                "reasoning": decision_reason,
                "sub_queries": sub_queries,
                "synthesis": synthesis,
                "results": all_results[:8]
            }
        
        # Single query search with summaries
        else:
            logger.info(f"[Single Search]: Executing single search on '{search_backend}'...")
            raw_res = run_raw_search(query)
            
            # Enrich with summaries
            enriched = []
            llm_caller = getattr(self.agent_state, "llm_caller", None)
            
            for r in raw_res:
                summary = r.get("snippet", "")
                if llm_caller and summary and summary != "(No description available)":
                    try:
                        prompt = (
                            f"Summarise the following search result snippet in 2–3 clear sentences. "
                            f"Be concise and informative. Do not add opinions.\n\n"
                            f"Title: {r.get('title', '')}\n"
                            f"Snippet: {summary}\n\n"
                            f"Summary:"
                        )
                        ai_summary = llm_caller.call_llm(
                            input_data=prompt,
                            system_prompt="You are a concise research assistant. Reply with only the summary, no preamble.",
                            temperature=0.3,
                            max_tokens=120,
                        )
                        if ai_summary and ai_summary.strip():
                            summary = ai_summary.strip()
                    except Exception as e:
                        logger.warning(f"[Search Summary Error]: {e} — using raw snippet")
                        
                enriched.append({
                    "title":   r.get("title", ""),
                    "summary": summary,
                    "url":     r.get("url", ""),
                    "snippet": r.get("snippet", ""),
                })
                
            return {
                "search_type": "single",
                "search_backend": search_backend,
                "query": query,
                "results": enriched
            }

    # ── RSS / Blogwatcher ─────────────────────────────────────────────────────

    @staticmethod
    def _blogwatcher_available() -> bool:
        return shutil.which("blogwatcher-cli") is not None

    @staticmethod
    def _run_blogwatcher(*args: str, timeout: int = 30) -> Dict[str, Any]:
        """Run blogwatcher-cli with the given args. Returns {ok, stdout, stderr}."""
        if not KnowledgeTool._blogwatcher_available():
            return {
                "ok": False,
                "stdout": "",
                "stderr": (
                    "blogwatcher-cli not found on PATH. "
                    "Install: go install github.com/JulienTant/blogwatcher-cli/cmd/blogwatcher-cli@latest"
                ),
            }
        try:
            result = subprocess.run(
                ["blogwatcher-cli", *args],
                capture_output=True, text=True, timeout=timeout
            )
            return {
                "ok": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "stdout": "", "stderr": "blogwatcher-cli timed out."}
        except Exception as e:
            return {"ok": False, "stdout": "", "stderr": str(e)}

    def rss_add(self, name: str, url: str) -> Dict[str, Any]:
        """Add an RSS/Atom feed to blogwatcher."""
        res = self._run_blogwatcher("add", name, url, "--yes")
        return {"ok": res["ok"], "message": res["stdout"] or res["stderr"]}

    def rss_list(self) -> List[Dict[str, Any]]:
        """List all tracked RSS feeds. Returns a list of {name, url} dicts."""
        res = self._run_blogwatcher("blogs")
        feeds: List[Dict[str, Any]] = []
        output = res.get("stdout") or res.get("stderr") or ""
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            # blogwatcher outputs "name  <url>" or "name: url" or just the url
            if "  " in line:
                parts = line.split("  ", 1)
                feeds.append({"name": parts[0].strip(), "url": parts[1].strip()})
            elif ": " in line:
                parts = line.split(": ", 1)
                feeds.append({"name": parts[0].strip(), "url": parts[1].strip()})
            elif line.startswith("http"):
                feeds.append({"name": line, "url": line})
        return feeds

    def rss_scan(self, blog_name: Optional[str] = None) -> List[Dict[str, str]]:
        """
        Scan all (or one specific) feed for new articles.
        Returns new articles as card-compatible dicts.
        """
        args = ["articles", "--all"]
        if blog_name:
            args += ["--blog", blog_name]
        res = self._run_blogwatcher(*args)
        if not res["ok"] and not res["stdout"]:
            logger.error(f"[RSS Scan Error]: {res['stderr']}")
            return []

        articles = []
        # Parse blogwatcher output: lines like "[id] [new] Title\n     Blog: …\n     URL: …"
        lines = res["stdout"].splitlines()
        current: Dict[str, str] = {}
        for line in lines:
            line = line.strip()
            if not line:
                if current.get("title"):
                    articles.append(current)
                    current = {}
                continue
            if line.startswith("[") and "]" in line:
                # New article header: [id] [new] Title
                parts = re.sub(r"\[\w+\]", "", line).strip()
                current = {"title": parts, "url": "", "blog": "", "published": ""}
            elif line.startswith("Blog:"):
                current["blog"] = line[5:].strip()
            elif line.startswith("URL:"):
                current["url"] = line[4:].strip()
            elif line.startswith("Published:"):
                current["published"] = line[10:].strip()
        if current.get("title"):
            articles.append(current)

        return articles

    # ── execute() ─────────────────────────────────────────────────────────────

    def execute(self, input_data: Dict[str, Any]) -> Any:
        """Routes execution commands for knowledge_manager."""
        command = input_data.get("command")

        if command == "search":
            query = input_data.get("query", "")
            if not query:
                return "Failed to search: 'query' parameter is required."
            search_type = input_data.get("search_type", "auto")
            search_backend = input_data.get("search_backend", "auto")
            return self.unified_search(query, search_type, search_backend)

        elif command == "search_raw":
            # Raw search without synthesis — faster, for internal use
            query = input_data.get("query", "")
            if not query:
                return "Failed to search: 'query' parameter is required."
            backend = input_data.get("search_backend", "auto")
            if backend == "auto":
                backend = "exa" if os.getenv("EXA_API_KEY") else "duckduckgo"
            if backend == "exa":
                return self._exa_search(query)
            return self.scrape_duckduckgo(query)

        elif command == "arxiv_search":
            query = input_data.get("query", "")
            max_results = int(input_data.get("max_results", 6))
            if not query:
                return "Failed to search arxiv: 'query' parameter is required."
            return self.search_arxiv(query, max_results)

        elif command == "rss_add":
            name = input_data.get("name", "")
            url  = input_data.get("url", "")
            if not name or not url:
                return "rss_add requires 'name' and 'url'."
            return self.rss_add(name, url)

        elif command == "rss_list":
            return self.rss_list()

        elif command == "rss_scan":
            blog_name = input_data.get("blog_name")
            articles = self.rss_scan(blog_name)
            # Auto-add new articles as cards to the board
            added = []
            for art in articles:
                if art.get("url"):
                    card = self.add_card(
                        title=art.get("title", "RSS Article"),
                        content=f"Blog: {art.get('blog', '')}\nPublished: {art.get('published', '')}",
                        card_type="rss_article",
                        source_url=art.get("url"),
                    )
                    added.append(card)
            return {"articles_found": len(articles), "cards_added": len(added), "articles": articles}

        elif command == "view_board":
            state = self.load_state()
            active_cards = [c for c in state.get("cards", []) if c.get("is_active")]
            return {
                "active_cards": [
                    {"id": c["id"], "title": c["title"], "content": c["content"], "type": c["type"], "source_url": c.get("source_url")}
                    for c in active_cards
                ],
                "count": len(active_cards)
            }

        elif command == "clear_board":
            state = self.load_state()
            cleared_titles = []
            for c in state.get("cards", []):
                if c.get("is_active"):
                    c["is_active"] = False
                    c["cluster_id"] = None
                    c["cluster_title"] = ""
                    cleared_titles.append(c["title"])
            self.save_state(state)
            return {"cleared": True, "cleared_titles": cleared_titles, "count": len(cleared_titles)}

        elif command == "search_catalog":
            query = input_data.get("query", "").strip().lower()
            if not query:
                return {"error": "search_catalog requires a 'query' parameter."}
            state = self.load_state()
            backlog_cards = [c for c in state.get("cards", []) if not c.get("is_active")]
            matches = []
            for c in backlog_cards:
                if query in c["title"].lower() or query in c["content"].lower():
                    matches.append({
                        "id": c["id"],
                        "title": c["title"],
                        "type": c["type"],
                        "content_preview": c["content"][:80] + "..." if len(c["content"]) > 80 else c["content"]
                    })
            return {"query": query, "matches": matches, "count": len(matches)}

        elif command == "add_to_board":
            query = input_data.get("query", "").strip().lower()
            card_id = input_data.get("id", "").strip().upper()
            if not query and not card_id:
                return {"error": "add_to_board requires 'query' (matching title) or specific 'id'."}
            state = self.load_state()
            cards = state.get("cards", [])
            active_cards_count = sum(1 for c in cards if c.get("is_active"))
            
            # Find candidate card
            candidate = None
            if card_id:
                for c in cards:
                    if c["id"].upper() == card_id:
                        candidate = c
                        break
            else:
                matches = []
                for c in cards:
                    if not c.get("is_active") and query in c["title"].lower():
                        matches.append(c)
                if len(matches) > 1:
                    return {
                        "error": "multiple_matches",
                        "message": f"Multiple matching cards found for '{query}'. Please select one by specifying the exact title or card ID.",
                        "matches": [{"id": m["id"], "title": m["title"]} for m in matches]
                    }
                elif len(matches) == 1:
                    candidate = matches[0]

            if not candidate:
                return {"error": "card_not_found", "message": f"No inactive card matching '{query or card_id}' found in the catalog."}

            if candidate.get("is_active"):
                return {"message": f"Card '{candidate['title']}' is already sitting on the tabletop board."}

            # Check Active Limit Cap
            if active_cards_count >= 5:
                return {
                    "error": "limit_reached",
                    "message": "ACTIVE LIMIT REACHED: Maximum 5 cards can sit on the tabletop at once. Please ask the user to clear the board or remove an active card first."
                }

            # Add to board
            import random
            candidate["is_active"] = True
            candidate["x"] = 50 + random.random() * 150
            candidate["y"] = 60 + random.random() * 100
            self.save_state(state)
            return {"added": {"id": candidate["id"], "title": candidate["title"]}, "active_count": active_cards_count + 1}

        elif command == "create_card":
            title = input_data.get("title", "").strip()
            content = input_data.get("content", "").strip()
            card_type = input_data.get("card_type", "manual_note")
            source_url = input_data.get("source_url")
            is_active = bool(input_data.get("is_active", True)) # Pinned by default conversationally!
            
            if not title or not content:
                return {"error": "create_card requires 'title' and 'content' parameters."}
            
            state = self.load_state()
            active_cards_count = sum(1 for c in state.get("cards", []) if c.get("is_active"))
            
            if is_active and active_cards_count >= 5:
                # If active desk space is full, fall back to backlog Catalog drawer automatically
                is_active = False
                fallback_msg = " Tabletop desk space was full (max 5 cards), so this card was successfully saved to your side Catalog drawer backlog."
            else:
                fallback_msg = ""
 