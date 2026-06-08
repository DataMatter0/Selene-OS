import os
import httpx
import re
from typing import Any, Dict, List, Optional, Sequence


_TOOL_CALL_RE = re.compile(
    r"<tool_call>.*?</tool_call>|"
    r"<function=\w+>.*?</function>|"
    r"\[TOOL_CALL\].*?\[/TOOL_CALL\]",
    re.DOTALL | re.IGNORECASE,
)


class LLMCaller:
    """
    Calls LM Studio's OpenAI-compatible endpoint at /v1/chat/completions
    using httpx directly — no openai library dependency.

    Conversation history is passed explicitly each turn via the `history`
    parameter (managed by LLMChat.working_memory), so no server-side state
    or response_id chaining is needed.

    Tool calling is supported via the `tools` parameter.  When the model
    decides to call a tool, call_llm() returns the raw message dict so the
    caller can inspect `tool_calls` and route accordingly.  When no tools are
    provided the return value is still a plain string for backward compatibility.
    """

    def __init__(self, base_url: str, model_name: str, system_prompt: Optional[str] = None):
        # base_url = "http://10.0.0.35:1234"  (no suffix)
        self.chat_url      = f"{base_url}/v1/chat/completions"
        self.system_prompt = system_prompt or "You are a helpful assistant."
        self.model_name    = model_name
        self.headers       = {"Content-Type": "application/json"}
        self.last_entropy  = None
        api_key = os.environ.get("LM_API_KEY")
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        print(f"[LLMCaller]: endpoint -> {self.chat_url}")
        print(f"[LLMCaller]: model    -> '{self.model_name}'")

    # ── Public API ─────────────────────────────────────────────────────────────

    def call_llm(
        self,
        input_data: str,
        system_prompt: Optional[str] = None,
        history: Optional[Sequence[Any]] = None,
        temperature: float = 0.8,
        max_tokens: int = 4096,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = "auto",
    ) -> Any:
        """
        Send the current message plus full conversation history to LM Studio.

        Returns:
          - str  when no tools are provided (backward-compatible).
          - dict (the raw assistant message) when tools are provided, so callers
            can inspect `message.get("tool_calls")` and route accordingly.
        """
        if input_data is None:
            raise ValueError("Input data cannot be None")
        if not isinstance(input_data, str):
            raise TypeError("Input data must be a string")

        final_system_prompt = system_prompt or self.system_prompt

        messages: list = [{"role": "system", "content": final_system_prompt}]
        if history:
            messages.extend(list(history))
        messages.append({"role": "user", "content": input_data})

        self.last_entropy = None
        payload: Dict[str, Any] = {
            "model":       self.model_name,
            "messages":    messages,
            "temperature": temperature,
            "max_tokens":  max_tokens,
            "stop":        ["\nYou:", "###", "<|im_end|>"],
            "logprobs":     True,
            "top_logprobs": 1
        }

        if tools:
            payload["tools"]       = tools
            payload["tool_choice"] = tool_choice

        try:
            response = httpx.post(
                self.chat_url,
                json=payload,
                headers=self.headers,
                timeout=120.0,
            )
            response.raise_for_status()
            data    = response.json()
            
            # Extract logprobs and calculate Raw Entropy
            try:
                logprobs_obj = data["choices"][0].get("logprobs")
                if logprobs_obj and "content" in logprobs_obj:
                    content_logprobs = logprobs_obj["content"]
                    logprobs_list = [t.get("logprob", 0.0) for t in content_logprobs if t.get("logprob") is not None]
                    if logprobs_list:
                        mean_logprob = sum(logprobs_list) / len(logprobs_list)
                        self.last_entropy = -mean_logprob
            except Exception as e:
                print(f"[LLMCaller]: Failed to parse logprobs: {e}")
                
            message = data["choices"][0]["message"]

            # If the model wants to call a tool, return the full message dict
            # so the caller (selene_server or tool_router) can handle it.
            if message.get("tool_calls"):
                return message

            # Standard text response — return plain string (backward-compatible).
            content          = (message.get("content") or "").strip()
            reasoning        = (message.get("reasoning_content") or "").strip()

            # Nemotron-3, Qwen3, DeepSeek-R1 and similar reasoning models return
            # chain-of-thought in a separate `reasoning_content` field alongside
            # the final reply in `content`.  We normalise both cases here:
            #
            #   Case A: content present, reasoning_content present
            #     → prepend reasoning as a <think> block so the pipeline sees it
            #
            #   Case B: content empty, reasoning_content present (think-only models)
            #     → use reasoning_content as the full response
            #
            #   Case C: normal model — just content, no reasoning_content
            #     → return content as-is
            if content and reasoning:
                # Case A — wrap reasoning so chat()'s think_match regex captures it
                return f"<think>\n{reasoning}\n</think>\n{content}"
            elif not content and reasoning:
                # Case B — reply is inside reasoning_content
                return reasoning
            else:
                # Case C — plain response
                return content

        except httpx.HTTPStatusError as e:
            print(f"[LLMCaller Error]: HTTP {e.response.status_code} — {e.response.text}")
            raise
        except httpx.RequestError as e:
            print(f"[LLMCaller Error]: Connection failed — {type(e).__name__}: {e}")
            raise
        except (KeyError, IndexError) as e:
            print(f"[LLMCaller Error]: Unexpected response shape — {e}")
            raise
        except Exception as e:
            print(f"[LLMCaller Error]: {type(e).__name__}: {e}")
            raise

    def call_with_messages(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.8,
        max_tokens: int = 4096,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = "auto",
    ) -> Dict[str, Any]:
        """
        Lower-level call that accepts a pre-built messages array directly.
        Used by the OpenAI-compatible REST endpoint so the full conversation
        context from Hermes (including tool results) passes through unchanged,
        with Selene's system prompt prepended.

        Always returns the raw assistant message dict.
        """
        # Always prepend Selene's system prompt (soul.md) so her personality and
        # instructions take precedence over any system prompt Hermes injects.
        # If Hermes sends its own system message, we keep it too but Selene's
        # comes first so the model's primary persona stays intact.
        full_messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt}
        ]
        # Skip any system message Hermes placed at position 0 to avoid
        # overwriting Selene's soul with Hermes's tool-use instructions.
        start = 1 if messages and messages[0].get("role") == "system" else 0
        full_messages.extend(messages[start:])

        self.last_entropy = None
        payload: Dict[str, Any] = {
            "model":       self.model_name,
            "messages":    full_messages,
            "temperature": temperature,
            "max_tokens":  max_tokens,
            "logprobs":     True,
            "top_logprobs": 1
        }

        if tools:
            payload["tools"]       = tools
            payload["tool_choice"] = tool_choice

        try:
            response = httpx.post(self.chat_url, json=payload, headers=self.headers, timeout=120.0)
            response.raise_for_status()
            data = response.json()
            
            # Extract logprobs and calculate Raw Entropy
            try:
                logprobs_obj = data["choices"][0].get("logprobs")
                if logprobs_obj and "content" in logprobs_obj:
                    content_logprobs = logprobs_obj["content"]
                    logprobs_list = [t.get("logprob", 0.0) for t in content_logprobs if t.get("logprob") is not None]
                    if logprobs_list:
                        mean_logprob = sum(logprobs_list) / len(logprobs_list)
                        self.last_entropy = -mean_logprob
            except Exception as e:
                print(f"[LLMCaller]: Failed to parse logprobs: {e}")
                
            message = data["choices"][0]["message"]

            # Reasoning models (Qwen3, DeepSeek-R1) may leave content empty and
            # put the final answer in reasoning_content.  Normalise so callers
            # always find the reply in message["content"].
            content = (message.get("content") or "").strip()
            if not content:
                content = (message.get("reasoning_content") or "").strip()

            # Strip any <tool_call> XML the model generated in plain text.
            # Qwen3.5 is trained on Hermes tool-call format and will embed
            # <tool_call>...</tool_call> blocks in content even when no tools
            # are provided.  Hermes parses these out and tries to execute them,
            # creating an infinite loop.  We scrub them here so Hermes only
            # ever receives clean prose.
            if content:
                content = _TOOL_CALL_RE.sub("", content).strip()

            message = dict(message)
            message["content"] = content
            # Only zero out tool_calls if the model didn't return real ones.
            # DeepHermes returns proper tool_calls objects — we pass them through
            # intact so Hermes can route tool execution correctly.
            if not message.get("tool_calls"):
                message["tool_calls"] = []

            return message

        except httpx.HTTPStatusError as e:
            print(f"[LLMCaller Error]: HTTP {e.response.status_code} — {e.response.text}")
            raise
        except httpx.RequestError as e:
            print(f"[LLMCaller Error]: Connection failed — {type(e).__name__}: {e}")
            raise
        except (KeyError, IndexError) as e:
            print(f"[LLMCaller Error]: Unexpected response shape — {e}")
            raise
        except Exception as e:
            print(f"[LLMCaller Error]: {type(e).__name__}: {e}")
            raise

    def reset_conversation(self):
        """No-op — history is managed client-side via working_memory."""
        pass
