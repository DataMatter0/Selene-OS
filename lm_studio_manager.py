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

    def list_models(self) -> List[Dict[str, Any]]:
        """
        Lists all models available to LM Studio.
        """
        try:
            response = httpx.get(f"{self.api_base_url}/models", timeout=10)
            response.raise_for_status()
            data = response.json()

            model_list = []
            # The structure of this response can vary between LM Studio versions.
            # This handles `{"data": [...]}` (newer), `{"models": [...]}` (older), and a raw `[...]`.
            if isinstance(data, dict):
                if "data" in data:
                    model_list = data["data"]
                elif "models" in data:
                    model_list = data["models"]
            elif isinstance(data, list):
                model_list = data

            if not model_list and data:
                # Log unexpected structures for easier debugging in the future.
                print(f"\n[LM Studio Manager Warning] Unexpected response structure from list_models: {data}")
                return []

            # Normalize the model identifier to a 'path' key for consistency.
            # The management API uses 'key', while the OpenAI API uses 'id'.
            for model in model_list:
                if 'key' in model and 'path' not in model:
                    model['path'] = model['key']
            
            return model_list
        except (httpx.RequestError, json.JSONDecodeError) as e:
            # Provide more specific feedback on connection failure.
            print(f"\n[LM Studio Manager Error] Could not list models. Is the server running at {self.api_base_url}? Details: {e}")
            return []

    def get_loaded_model_info(self) -> Optional[Dict[str, Any]]:
        """
        Retrieves information about the currently loaded model by querying the
        OpenAI-compatible endpoint, which returns only the active model.
        """
        try:
            # The /v1/models endpoint returns a list with a single entry for the loaded model.
            response = httpx.get(f"{self.openai_base_url}/models", timeout=5)
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
        """
        try:
            payload = {"path": model_path}
            if config:
                payload.update(config)
            
            # httpx handles JSON serialization automatically with the `json` parameter.
            response = httpx.post(
                f"{self.api_base_url}/models/load",
                json=payload,
                timeout=60
            )
            response.raise_for_status()
            return True
        except (httpx.RequestError, json.JSONDecodeError):
            return False

    def unload_model(self, instance_id: str) -> bool:
        """Sends a request to unload a specific model instance."""
        try:
            # This endpoint requires the specific ID of the model instance to unload.
            payload = {"instance_id": instance_id}
            response = httpx.post(
                f"{self.api_base_url}/models/unload",
                json=payload,
                timeout=10
            )
            response.raise_for_status()
            return True
        except (httpx.RequestError, json.JSONDecodeError):
            return False