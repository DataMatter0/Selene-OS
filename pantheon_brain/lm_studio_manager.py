import os
import httpx
import json
from typing import Optional, List, Dict, Any

class LMStudioManager:
    """
    A client for the LM Studio local server management API.
    This allows for programmatic control over the server, such as loading,
    unloading, and listing models.
    This implementation uses 'httpx' for robust and consistent networking.
    """
    def __init__(self, base_url: str = "http://10.0.0.35:1234"):
        # The management API is at /api/v1, the OpenAI-compatible one is at /v1
        self.api_base_url = f"{base_url}/api/v1"
        self.openai_base_url = f"{base_url}/v1"
        self.headers = {}
        api_key = os.environ.get("LM_API_KEY")
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    def list_models(self) -> List[Dict[str, Any]]:
        """
        Lists loaded models via the OpenAI-compat /v1/models endpoint.
        /api/v1/models is not a documented LM Studio endpoint.
        """
        try:
            response = httpx.get(f"{self.openai_base_url}/models", headers=self.headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            model_list = data.get("data", []) if isinstance(data, dict) else data
            for m in model_list:
                if 'path' not in m:
                    m['path'] = m.get('id', '')
            return model_list
        except (httpx.RequestError, json.JSONDecodeError) as e:
            print(f"\n[LM Studio Manager Error] Could not list models: {e}")
            return []

    def get_loaded_model_info(self) -> Optional[Dict[str, Any]]:
        """
        Retrieves information about the currently loaded model by querying the
        OpenAI-compatible endpoint, which returns only the active model.
        """
        try:
            # The /v1/models endpoint returns a list with a single entry for the loaded model.
            response = httpx.get(f"{self.openai_base_url}/models", headers=self.headers, timeout=5)
            response.raise_for_status()
            data = response.json()
            models = data.get("data", [])
            if models:
                # The 'id' field contains the model identifier. We add a 'path' key
                # for compatibility with the existing logic in llm_chat.py.
                loaded_model = models[0]
                loaded_model['path'] = loaded_model.get('id', '')
                return loaded_model
            return None
        except (httpx.RequestError, json.JSONDecodeError):
            # This can fail if no model is loaded or the server is down, which is an expected state.
            return None

    def load_model(self, model_path: str, config: Optional[Dict[str, Any]] = None) -> bool:
        """
        Sends a request to load a model by its path identifier.

        LM Studio has used two different field names across versions:
          - Older builds:  {"path": "..."}
          - Newer builds:  {"model": "..."}
        We send both so either version accepts the request.
        """
        # Try payloads in order of newest → oldest LM Studio API shape.
        # Newer builds only accept "model" and reject unknown keys.
        # Older builds use "path". We try each separately so an unrecognized-key
        # error on one shape doesn't block us from trying the next.
        _payloads = [
            {"model": model_path},   # newer LM Studio
            {"path":  model_path},   # older LM Studio
        ]
        if config:
            _payloads = [{**p, **config} for p in _payloads]

        last_exc = None
        for payload in _payloads:
            try:
                response = httpx.post(
                    f"{self.api_base_url}/models/load",
                    json=payload,
                    headers=self.headers,
                    timeout=60
                )
                response.raise_for_status()
                print(f"[LM Studio Manager]: load_model succeeded with payload keys {list(payload.keys())}")
                return True
            except httpx.HTTPStatusError as e:
                print(f"[LM Studio Manager]: load_model attempt {list(payload.keys())} -> "
                      f"HTTP {e.response.status_code}: {e.response.text}")
                last_exc = e
                continue
            except (httpx.RequestError, json.JSONDecodeError) as e:
                print(f"[LM Studio Manager]: load_model request failed: {e}")
                return False

        # All payload shapes failed — re-raise the last HTTP error so callers
        # can surface the actual LM Studio error message to the UI.
        if last_exc:
            raise last_exc
        return False

    def unload_model(self, identifier: str) -> bool:
        """
        Sends a request to unload a model.

        LM Studio has used different field names across versions:
          - {"instance_id": "..."}   (older management API)
          - {"identifier": "..."}    (some builds)
          - {"model": "..."}         (newer builds)
        We attempt all three payload shapes via separate requests so the right
        one lands regardless of the LM Studio version installed.
        """
        payloads = [
            {"instance_id": identifier},  # per docs
            {"model":       identifier},  # fallback
        ]
        for payload in payloads:
            try:
                response = httpx.post(
                    f"{self.api_base_url}/models/unload",
                    json=payload,
                    headers=self.headers,
                    timeout=10
                )
                response.raise_for_status()
                print(f"[LM Studio Manager]: unload succeeded with payload {list(payload.keys())}")
                return True
            except httpx.HTTPStatusError as e:
                print(f"[LM Studio Manager]: unload attempt {list(payload.keys())} -> "
                      f"HTTP {e.response.status_code}: {e.response.text}")
                continue   # try next payload shape
            except (httpx.RequestError, json.JSONDecodeError) as e:
                print(f"[LM Studio Manager]: unload attempt {list(payload.keys())} -> request error: {e}")
                break      # network failure — no point retrying
        return False

    def get_loaded_instance_id(self) -> Optional[str]:
        """Returns the id of the currently loaded model via /v1/models."""
        loaded = self.get_loaded_model_info()
        if loaded:
            return loaded.get('id') or loaded.get('path') or None
        return None