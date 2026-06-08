"""
tools/registry.py - Central tool registry for Selene OS
---------------------------------------------------------
Imports every tool class and wires them all into the ToolRouter.

Each registration is individually fault-tolerant: if a tool fails to
initialise (missing library, bad env var, etc.) it is skipped with a
warning and Selene keeps running normally.

To add a new tool:
  1. Create tools/<name>.py with a class that inherits BaseTool
  2. Import it here
  3. Add a _safe_register() call in register_all_tools()
"""

import logging

from .builtin      import ChronicleTool, MemoryTool, StatusTool, ManifestTool, TodoTool
from .meta_insight import MetaInsightTool
from .knowledge import KnowledgeTool
from .file_manager import LocalWorkspaceTool
from .document  import DocumentTool
from .runereader import RuneReaderTool
from .workspace import GoogleWorkspaceTool
from .hass      import HomeAssistantTool
from .maps      import MapsTool
from .notion    import NotionTool
from .spotify   import SpotifyTool
from .youtube   import YouTubeTool
from .schedule  import ScheduleTool
from .story_engine import (
    StoryAddLocationTool,
    StoryAddNPCTool,
    StoryTriggerEventTool,
    StoryToggleCardTool,
    StoryOpenMerchantTool
)

logger = logging.getLogger(__name__)


def _safe_register(tool_router, tool_cls, /, **kwargs):
    """
    Instantiate tool_cls(**kwargs) and register it.
    If anything raises, log a warning and continue -- never crash the server.
    """
    try:
        tool = tool_cls(**kwargs)
        tool_router.register_tool(tool)
    except Exception as exc:
        logger.warning(
            "[Registry]: Could not load tool '%s' -- %s: %s  (skipping)",
            getattr(tool_cls, "name", tool_cls.__name__),
            type(exc).__name__,
            exc,
        )


def register_all_tools(agent_state, tool_router):
    """
    Instantiate and register every tool with the ToolRouter.
    Called once from LLMChat.__init__ during startup.
    Broken tools are skipped cleanly -- the server always starts.
    """
    # Core / built-in tools (all take agent_state)
    _safe_register(tool_router, ChronicleTool, agent_state=agent_state)
    _safe_register(tool_router, MemoryTool,    agent_state=agent_state)
    _safe_register(tool_router, StatusTool,    agent_state=agent_state)
    _safe_register(tool_router, ManifestTool,  agent_state=agent_state)
    _safe_register(tool_router, TodoTool,        agent_state=agent_state)
    _safe_register(tool_router, ScheduleTool,    agent_state=agent_state)
    _safe_register(tool_router, MetaInsightTool, agent_state=agent_state)

    # Knowledge & research (takes agent_state)
    _safe_register(tool_router, KnowledgeTool, agent_state=agent_state)
    _safe_register(tool_router, LocalWorkspaceTool, agent_state=agent_state)

    # Documents (agent_state optional)
    _safe_register(tool_router, DocumentTool,  agent_state=agent_state)
    _safe_register(tool_router, RuneReaderTool, agent_state=agent_state)

    # External integrations (no agent_state -- self-configure from .env)
    _safe_register(tool_router, GoogleWorkspaceTool)
    _safe_register(tool_router, HomeAssistantTool)
    _safe_register(tool_router, MapsTool)
    _safe_register(tool_router, NotionTool)
    _safe_register(tool_router, SpotifyTool)

    # Media (agent_state optional)
    _safe_register(tool_router, YouTubeTool,   agent_state=agent_state)

    # Infinite Story Engine Tabletop Tools (all take agent_state)
    _safe_register(tool_router, StoryAddLocationTool, agent_state=agent_state)
    _safe_register(tool_router, StoryAddNPCTool,      agent_state=agent_state)
    _safe_register(tool_router, StoryTriggerEventTool, agent_state=agent_state)
    _safe_register(tool_router, StoryToggleCardTool,  agent_state=agent_state)
    _safe_register(tool_router, StoryOpenMerchantTool, agent_state=agent_state)
