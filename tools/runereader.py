"""
tools/runereader.py — Conversational document parsing, chunking, and synthesis engine
─────────────────────────────────────────────────────────────────────────────
Reads PDF, DOCX, TXT, and LOG files. Groups contents into turn-by-turn chat style
or token-based chunks, builds a running comprehension summary, and synthesizes
informed reports tailored to a user prompt using Selene's local LLM pipeline.
"""

import os
import re
import json
import hashlib
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from .schema import BaseTool, atomic_write

logger = logging.getLogger("runereader_tool")


def _doc_hash(path: str) -> str:
    """Short stable hash of the file path for archiving notes."""
    return hashlib.md5(os.path.abspath(path).encode()).hexdigest()[:12]


class RuneReaderTool(BaseTool):
    name = "runereader"
    description = (
        "Adopts the Rune Reader document synthesis pipeline. Reads PDF, DOCX, "
        "TXT, and LOG files, chunks them in Chat Style or Token-Based modes, "
        "builds a running understanding forward, and presents key insights "
        "conversational-style. Commands: process."
    )
    input_type  = "json"
    output_type = "any"

    def __init__(self, agent_state: Any = None):
        self.agent_state = agent_state

    def _notes_dir(self) -> str:
        if self.agent_state and hasattr(self.agent_state, "MEMORY_DIR"):
            return os.path.join(self.agent_state.MEMORY_DIR, "runereader_notes")
        return os.path.join(os.path.dirname(__file__), "memories", "runereader_notes")

    def _report_path(self, doc_hash: str) -> str:
        return os.path.join(self._notes_dir(), f"{doc_hash}.md")

    def _get_llm(self) -> Any:
        if self.agent_state:
            return getattr(self.agent_state, "llm_caller", None)
        return None

    # ── Text Extraction ────────────────────────────────────────────────────────

    def _extract_pdf(self, path: str) -> str:
        """Extract text from PDF using pymupdf."""
        try:
            import fitz  # type: ignore
        except ImportError:
            raise ImportError("pymupdf not installed. Please check virtualenv setup.")
        
        doc = fitz.open(path)
        pages_text = []
        for page in doc:
            pages_text.append(page.get_text("text"))
        doc.close()
        return "\n\n".join(pages_text)

    def _extract_docx(self, path: str) -> str:
        """Extract text from DOCX using python-docx."""
        try:
            import docx  # type: ignore
        except ImportError:
            raise ImportError("python-docx not installed. Please check virtualenv setup.")
        
        document = docx.Document(path)
        return "\n\n".join(p.text for p in document.paragraphs if p.text.strip())

    def _extract_text(self, path: str) -> str:
        """Read plain text or log file."""
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        return content.replace("\r\n", "\n")

    def _extract(self, path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".pdf":
            return self._extract_pdf(path)
        elif ext in (".docx", ".doc"):
            return self._extract_docx(path)
        else:
            return self._extract_text(path)

    # ── Token Counting & Chunking ──────────────────────────────────────────────

    def _count_tokens(self, text: str) -> int:
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            return max(1, len(text) // 4)

    def _chunk_by_tokens(self, text: str, token_limit: int = 800) -> List[Dict[str, Any]]:
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            tokens = enc.encode(text)
            chunks = []
            i = 0
            while i < len(tokens):
                chunk_tokens = tokens[i:i + token_limit]
                chunk_text_block = enc.decode(chunk_tokens)
                chunks.append({"text": chunk_text_block, "tokens": len(chunk_tokens)})
                i += token_limit
            return chunks
        except Exception:
            # Fallback
            max_chars = token_limit * 4
            paragraphs = text.split("\n\n")
            chunks = []
            current = ""
            for p in paragraphs:
                if len(current) + len(p) > max_chars and current:
                    chunks.append({"text": current.strip(), "tokens": len(current) // 4})
                    current = p
                else:
                    current = (current + "\n\n" + p).strip()
            if current:
                chunks.append({"text": current.strip(), "tokens": len(current) // 4})
            return chunks

    def _chunk_chat_style(self, text: str, chunk_size: int = 3) -> List[Dict[str, Any]]:
        lines = text.strip().splitlines()
        messages = []
        current_role = None
        current_content = []

        for line in lines:
            line_strip = line.strip()
            line_lower = line_strip.lower()
            if line_lower in ["user"]:
                if current_role and current_content:
                    messages.append((current_role, "\n".join(current_content).strip()))
                current_role = "User"
                current_content = []
            elif line_lower in ["chatgpt", "assistant"]:
                if current_role and current_content:
                    messages.append((current_role, "\n".join(current_content).strip()))
                current_role = "ChatGPT"
                current_content = []
            else:
                if line_strip:
                    current_content.append(line_strip)

        if current_role and current_content:
            messages.append((current_role, "\n".join(current_content).strip()))

        pairs = []
        i = 0
        while i < len(messages) - 1:
            if messages[i][0] == "User" and messages[i + 1][0] == "ChatGPT":
                user_text = f"User: {messages[i][1]}"
                bot_text = f"ChatGPT: {messages[i + 1][1]}"
                pairs.append(f"{user_text}\n{bot_text}")
                i += 2
            else:
                i += 1

        chunks = []
        for i in range(0, len(pairs), chunk_size):
            chunk_text_block = "\n".join(pairs[i:i + chunk_size])
            token_count = self._count_tokens(chunk_text_block)
            chunks.append({"text": chunk_text_block, "tokens": token_count})
        
        # If no conversation pairs were matched, fall back to token-based chunking
        if not chunks:
            logger.info("[RuneReader]: Chat Style parser matched 0 turns. Falling back to Token-Based.")
            return self._chunk_by_tokens(text, token_limit=800)
            
        return chunks

    # ── Executive Processor ───────────────────────────────────────────────────

    def process_document(
        self,
        path: str,
        mode: str = "Token-Based",
        chunk_size: int = 800,
        user_prompt: str = "Summarize the key findings and key insights from this document."
    ) -> Dict[str, Any]:
        if not os.path.exists(path):
            return {"ok": False, "error": f"File not found: {path}"}

        try:
            full_text = self._extract(path)
        except Exception as e:
            return {"ok": False, "error": f"Failed to extract document contents: {e}"}

        if not full_text.strip():
            return {"ok": False, "error": "Document content appears to be empty."}

        # Select chunking strategy
        if mode == "Chat Style":
            chunks = self._chunk_chat_style(full_text, chunk_size=max(1, chunk_size // 200 or 3))
        else:
            chunks = self._chunk_by_tokens(full_text, token_limit=max(100, chunk_size))

        total_chunks = len(chunks)
        logger.info(f"[RuneReader]: Parsing '{path}' completed. Chunks: {total_chunks}")

        llm = self._get_llm()
        if not llm:
            return {"ok": False, "error": "LLM pipeline not initialized in active companion state."}

        # 🧠 Step 1: Accumulative Running Comprehension
        running_comprehension = ""
        for idx, chunk in enumerate(chunks):
            logger.info(f"[RuneReader]: Comprehending chunk {idx+1}/{total_chunks}...")
            prompt = (
                f"You are reading a document chunk-by-chunk to build a comprehensive, deep understanding.\n\n"
                f"--- CURRENT CHUNK {idx+1}/{total_chunks} ---\n"
                f"{chunk['text']}\n\n"
                f"--- RUNNING COMPREHENSION SO FAR ---\n"
                f"{running_comprehension or 'No previous context yet.'}\n\n"
                f"Analyze the current chunk. Synthesize and update the comprehensive running comprehension of the document. "
                f"Focus on extracting core concepts, key insights, structural themes, and logical flow. "
                f"Keep the comprehensive running comprehension structured, precise, and under 500 words. "
                f"Reply ONLY with the updated comprehension report."
            )
            try:
                running_comprehension = llm.call_llm(
                    input_data=prompt,
                    system_prompt="You are an expert document analyst building an accumulative, running mental model of a text. Reply only with the updated summary.",
                    temperature=0.3,
                    max_tokens=600
                ).strip()
            except Exception as e:
                logger.error(f"[RuneReader] Chunk comprehension failed: {e}")
                running_comprehension += f"\n[Error parsing chunk {idx+1}: {e}]"

        # 🧠 Step 2: Final Synthesis based on User Prompt
        filename = os.path.basename(path)
        logger.info("[RuneReader]: Compiling final synthesis...")
        final_prompt = (
            f"You have read the entire document '{filename}'. Here is your comprehensive, deep comprehension of the text:\n\n"
            f"{running_comprehension}\n\n"
            f"The user's prompt / focus area is: \"{user_prompt}\"\n\n"
            f"Using your deep comprehension, write a final conversational response to the user. "
            f"Summarize the document, answer any questions, and present your findings based on their focus area. "
            f"Ensure you address the request thoroughly but concisely, in a helpful companion tone."
        )

        try:
            final_response = llm.call_llm(
                input_data=final_prompt,
                system_prompt="You are Selene, a highly intelligent and helpful AI companion. Summarize and present findings conversational, clear, and focused on the user's query.",
                temperature=0.4,
                max_tokens=800
            ).strip()
        except Exception as e:
            final_response = f"I finished reading the document, but encountered an error generating the final response: {e}"

        # 📁 Step 3: Save Markdown notes to disk
        doc_hash = _doc_hash(path)
        os.makedirs(self._notes_dir(), exist_ok=True)
        report_content = (
            f"# RuneReader Analysis Report — {filename}\n"
            f"**Source File:** `{path}`\n"
            f"**Analyzed:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"**Focus Request:** *\"{user_prompt}\"*\n\n"
            f"---\n\n"
            f"## 🧠 Comprehensive Running Comprehension\n"
            f"{running_comprehension}\n\n"
            f"---\n\n"
            f"## 💬 Companion Findings & Presentation\n"
            f"{final_response}\n"
        )
        atomic_write(self._report_path(doc_hash), report_content)

        return {
            "ok": True,
            "doc_hash": doc_hash,
            "filename": filename,
            "path": path,
            "user_message": f"📄 **Read File:** `{filename}`\n**Focus Prompt:** *\"{user_prompt}\"*",
            "response": final_response,
            "running_comprehension": running_comprehension,
            "report_path": self._report_path(doc_hash)
        }

    # ── Execute ───────────────────────────────────────────────────────────────

    def execute(self, input_data: Dict[str, Any]) -> Any:
        command = input_data.get("command", "process")
        if command != "runereader_process":
            return {"error": f"Unknown runereader command: '{command}'"}

        path = input_data.get("path", "").strip()
        mode = input_data.get("mode", "Token-Based").strip()
        chunk_size = int(input_data.get("chunk_size", 800))
        prompt = input_data.get("prompt", "").strip()

        if not path:
            return {"ok": False, "error": "RuneReader requires 'path' parameters."}
        if not prompt:
            prompt = "Summarize the key takeaways and findings from this document."

        return self.process_document(path, mode, chunk_size, prompt)
