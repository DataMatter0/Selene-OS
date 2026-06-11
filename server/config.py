"""
server/config.py — Environment config and shared constants
───────────────────────────────────────────────────────────
Owns:
  - LM Studio connection settings
  - Server host/port
  - _normalize() — model name comparison helper
"""

import os


BASE_URL      = os.environ.get("LM_STUDIO_URL") or os.environ.get("LM_BASE_URL") or "http://10.0.0.35:1234"
DESIRED_MODEL = os.environ.get("LM_STUDIO_MODEL") or os.environ.get("LM_MODEL") or "nvidia/nemotron-3-nano-4b"
SERVER_HOST   = "127.0.0.1"   # explicit IPv4 — avoids localhost→::1 ambiguity on Windows
SERVER_PORT   = 8766


def _normalize(name: str) -> str:
    """Case/separator-insensitive model name comparison."""
    return name.lower().replace(" ", "").replace("-", "").replace("_", "").replace(".", "").replace("/", "")
