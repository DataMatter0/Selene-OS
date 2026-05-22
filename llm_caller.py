import openai
from typing import Sequence, Any, Optional

class LLMCaller:
    def __init__(self, base_url: str, model_name: str, system_prompt: Optional[str] = None):
        # Point the OpenAI client to the local LM Studio server.
        # The api_key is not needed for local servers but the library expects a value.
        self.client = openai.OpenAI(base_url=base_url, api_key="not-needed")
        self.system_prompt = system_prompt or "You are a helpful assistant."
        self.model_name = model_name
        print(f"LLMCaller connected to LM Studio API, configured for model: '{self.model_name}'")

    def call_llm(self, input_data: str, system_prompt: Optional[str] = None, history: Optional[Sequence[Any]] = None, temperature: float = 0.7, max_tokens: int = 2048) -> str:
        if input_data is None:
            raise ValueError("Input data cannot be None")
        if not isinstance(input_data, str):
            raise TypeError("Input data must be a string")

        # Use the provided system_prompt, or fall back to the instance's default.
        final_system_prompt = system_prompt or self.system_prompt

        messages: Sequence[Any] = [{"role": "system", "content": final_system_prompt}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": input_data})

        # The call is now a network request to the local server.
        # The 'model' parameter MUST match the identifier of the loaded model.
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            # Add stop tokens to prevent the model from generating past its response
            stop=["\nYou:", "###", "<|im_end|>"],
        )

        content = response.choices[0].message.content
        return content or ""
