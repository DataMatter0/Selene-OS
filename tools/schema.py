import json
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# -- Atomic file write --------------------------------------------------------
# Write to a temp file in the same directory, then rename into place.
# Rename is atomic on all major OSes -- prevents partial writes corrupting
# files if the server crashes mid-save.

def atomic_write(path: str, content: str, encoding: str = "utf-8") -> None:
    """Write *content* to *path* atomically via a temp-file rename."""
    dir_ = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# -- BaseTool -----------------------------------------------------------------

class BaseTool:
    """Base class for all tools to ensure a consistent I/O schema."""
    name: str        = "base_tool"
    description: str = "A basic tool."
    input_type: str  = "text"   # 'text' | 'json'
    output_type: str = "text"   # 'text' | 'json' | 'any'
    # Set dormant=True when a credential/env var is missing.
    # The ToolRouter still registers the tool so the UI can show a badge.
    dormant: bool = False
    on_state_change: Any = None

    def execute(self, input_data: Any) -> Any:
        # Subclasses must override this.  Default returns a clear error string
        # so the router never blows up on an un-implemented tool.
        return f"[{self.name}] Error: execute() is not implemented for this tool."

    def check_and_trigger(self, user_input: str) -> Optional[Dict[str, Any]]:
        """Optional: Override to allow keyword-based triggers."""
        return None

    def load_state(self) -> Any:
        """Optional: Override for tools that maintain persistent state."""
        return None

    def save_state(self, state: Any) -> None:
        """Optional: Override for tools that maintain persistent state."""
        pass

    def compile_active_desk_xml(self) -> str:
        """Optional: Override to return XML-formatted context for the system prompt."""
        return ""


# -- ToolGroup ----------------------------------------------------------------

class ToolGroup:
    """
    A named collection of related tools presented to Selene as a single group.

    When the system prompt is built, groups are rendered like:

        [Research Tools]  arxiv_search . web_search
          Search academic papers and the web.
    """

    def __init__(self, name: str, description: str, tools: Optional[List[BaseTool]] = None):
        self.name        = name
        self.description = description
        self.tools: List[BaseTool] = tools or []

    def add(self, tool: BaseTool) -> "ToolGroup":
        self.tools.append(tool)
        return self

    def tool_names(self) -> List[str]:
        return [t.name for t in self.tools]

    def to_prompt_block(self) -> str:
        active  = [t for t in self.tools if not t.dormant]
        dormant = [t for t in self.tools if t.dormant]
        names_str   = " . ".join(t.name for t in active)
        dormant_str = f"  (dormant: {', '.join(t.name for t in dormant)})" if dormant else ""
        lines = [f"[{self.name}]  {names_str}{dormant_str}", f"  {self.description}"]
        for tool in active:
            lines.append(f"  - {tool.name}: {tool.description}")
        return "\n".join(lines)


# -- ToolRouter ---------------------------------------------------------------

class ToolRouter:
    """
    Routes tool calls to the correct BaseTool, handling JSON parsing and errors.
    Also tracks ToolGroups for system-prompt generation.
    """

    def __init__(self, llm_caller=None):
        self.llm_caller = llm_caller
        self.tools: Dict[str, BaseTool]  = {}
        self.groups: List[ToolGroup]     = []

    def register_tool(self, tool: BaseTool) -> None:
        self.tools[tool.name] = tool
        logging.info("Registered tool: %s%s", tool.name, " [dormant]" if tool.dormant else "")

    def register_group(self, group: ToolGroup) -> None:
        """Register a ToolGroup and auto-register all its member tools."""
        self.groups.append(group)
        for tool in group.tools:
            self.register_tool(tool)

    def build_tool_context(self) -> str:
        """
        Returns a compact string describing all tool groups for injection into
        Selene's system prompt.  Ungrouped tools appear at the end.
        """
        lines = ["## Available Tools\n"]
        grouped_names: set = set()

        for group in self.groups:
            lines.append(group.to_prompt_block())
            lines.append("")
            grouped_names.update(group.tool_names())

        ungrouped = [t for name, t in self.tools.items()
                     if name not in grouped_names and not t.dormant]
        if ungrouped:
            lines.append("[Utility Tools]  " + " . ".join(t.name for t in ungrouped))
            for t in ungrouped:
                lines.append(f"  - {t.name}: {t.description}")

        lines.append(
            "\n## Tool Usage Notes\n"
            "- When responding via Discord or any non-UI channel: always summarise tool output "
            "as clear, structured plain text in your reply -- the user cannot see the visual board.\n"
            "- When responding via the UI: tool results are pushed to the visual panel automatically; "
            "give a brief conversational acknowledgement in chat.\n"
            "- Dormant tools (missing credentials) should be mentioned to Ghost if he asks for them, "
            "noting they need configuration in .env."
        )
        return "\n".join(lines)

    def route_and_execute(self, tool_name: str, input_data: Any) -> Dict[str, Any]:
        """
        Route a tool call and execute it safely.

        Always returns a dict -- never raises:
          {"status": "success", "data": <result>, "type": <output_type>}
          {"status": "error",   "message": <human-readable string>}

        Every failure path returns an error dict so Selene can relay
        the problem in chat without the server crashing.
        """
        logging.info("Routing to: %s", tool_name)

        if tool_name not in self.tools:
            msg = (
                f"Tool '{tool_name}' is not available. "
                "It may have failed to load at startup -- check the server log for details."
            )
            logging.error(msg)
            return {"status": "error", "message": msg}

        tool = self.tools[tool_name]

        if tool.dormant:
            msg = (
                f"Tool '{tool_name}' is not configured yet. "
                "Check .env for the required credentials and restart the server."
            )
            return {"status": "error", "message": msg}

        # -- Parse JSON input if the tool expects it --------------------------
        if tool.input_type == "json" and isinstance(input_data, str):
            clean = input_data.strip()
            if clean.startswith("```"):
                lines = clean.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                clean = "\n".join(lines).strip()
            try:
                input_data = json.loads(clean)
            except json.JSONDecodeError as exc:
                msg = f"[{tool_name}] Bad input -- expected JSON: {exc}"
                logging.error(msg)
                return {"status": "error", "message": msg}

        # -- Execute with full exception guard --------------------------------
        try:
            result = tool.execute(input_data)
            logging.info("Tool '%s' executed successfully.", tool_name)
            return {"status": "success", "data": result, "type": tool.output_type}
        except NotImplementedError:
            msg = f"[{tool_name}] Error: execute() is not implemented for this tool."
            logging.error(msg)
            return {"status": "error", "message": msg}
        except Exception as exc:
            msg = f"[{tool_name}] Error: {type(exc).__name__}: {exc}"
            logging.exception("Unhandled exception in tool '%s'", tool_name)
            return {"status": "error", "message": msg}
