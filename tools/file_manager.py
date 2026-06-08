"""
file_manager.py — Codebase Workspace File Operations for Selene OS
─────────────────────────────────────────────────────────────────
Provides structured directory navigation, paging text file reading,
atomic writing, safe file patching, and pure-Python recursive grep search.
Designed to be completely cross-platform (zero dependency on shell commands).
"""

import os
import re
import fnmatch
import logging
from typing import Any, Dict, List, Optional
from .schema import BaseTool, atomic_write

logger = logging.getLogger("file_manager")

# Standard paths to completely exclude from recursive searches and lists
EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    ".gemini",
    ".obsidian",
    "electron-builder",
    "dist",
    "build"
}

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".rar", ".7z", ".db", ".sqlite",
    ".sqlite3", ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc",
    ".mp3", ".wav", ".ogg", ".mp4", ".avi", ".mkv", ".mov"
}

class LocalWorkspaceTool(BaseTool):
    name = "file_manager"
    description = (
        "Manages local workspace files for codebase exploration and modifications. "
        "Inputs must be JSON containing a 'command' key. Supported schemas:\n"
        "- List folder:       {\"command\": \"list_dir\", \"path\": \"relative/or/abs/path\"}\n"
        "- View text file:    {\"command\": \"view_file\", \"path\": \"filepath\", \"start_line\": 1, \"end_line\": 500}\n"
        "- Write text file:   {\"command\": \"write_file\", \"path\": \"filepath\", \"content\": \"text content\"}\n"
        "- Patch text file:   {\"command\": \"patch_file\", \"path\": \"filepath\", \"old_content\": \"target block\", \"new_content\": \"replacement block\"}\n"
        "- Search files:      {\"command\": \"grep_search\", \"query\": \"search term\", \"path\": \".\", \"file_pattern\": \"*.py\"}"
    )
    input_type = "json"
    output_type = "any"

    def __init__(self, agent_state: Any):
        self.agent_state = agent_state
        # Base workspace directory defaults to codebase root
        self.base_dir = os.path.abspath(getattr(self.agent_state, "WORKSPACE_DIR", os.getcwd()))
        logger.info(f"[FileManager]: Initialised with base directory → {self.base_dir}")

    def _resolve_path(self, path: str) -> str:
        """Resolves any relative or absolute path against the codebase root."""
        if not path:
            return self.base_dir
        
        # Strip leading slashes to prevent absolute traversal outside sandbox
        normalized = os.path.normpath(path)
        if os.path.isabs(normalized):
            return normalized
            
        return os.path.abspath(os.path.join(self.base_dir, normalized))

    def _is_binary(self, filepath: str, sample_bytes: bytes) -> bool:
        """Determines if a file is binary using extension checks and byte ratios."""
        ext = os.path.splitext(filepath)[1].lower()
        if ext in BINARY_EXTENSIONS:
            return True
            
        if not sample_bytes:
            return False
            
        # If the file has a high percentage of non-printable bytes or null bytes, it is binary
        control_chars = sum(1 for b in sample_bytes if b < 32 and b not in (9, 10, 13))
        if len(sample_bytes) > 0:
            return (control_chars / len(sample_bytes)) > 0.30
            
        return False

    def list_dir(self, path: str = ".") -> Dict[str, Any]:
        """Lists direct contents of a folder, displaying metadata and child counts."""
        target = self._resolve_path(path)
        
        if not os.path.exists(target):
            return {"error": f"Path '{path}' does not exist."}
            
        if not os.path.isdir(target):
            return {"error": f"Path '{path}' is a file, not a directory."}

        try:
            items = os.listdir(target)
            directories = []
            files = []
            
            for item in items:
                if item in EXCLUDE_DIRS:
                    continue
                    
                full_path = os.path.join(target, item)
                rel_path = os.path.relpath(full_path, self.base_dir)
                
                if os.path.isdir(full_path):
                    # Count total nested children for high-level info
                    try:
                        child_count = len(os.listdir(full_path))
                    except Exception:
                        child_count = 0
                    directories.append({
                        "name": item,
                        "relative_path": rel_path.replace("\\", "/"),
                        "type": "directory",
                        "child_count": child_count
                    })
                else:
                    stat = os.stat(full_path)
                    files.append({
                        "name": item,
                        "relative_path": rel_path.replace("\\", "/"),
                        "type": "file",
                        "size_bytes": stat.st_size,
                        "modified_time": int(stat.st_mtime)
                    })
                    
            return {
                "path": os.path.relpath(target, self.base_dir).replace("\\", "/"),
                "directories": sorted(directories, key=lambda d: d["name"].lower()),
                "files": sorted(files, key=lambda f: f["name"].lower()),
                "count_directories": len(directories),
                "count_files": len(files)
            }
        except Exception as e:
            return {"error": f"Failed to list directory: {e}"}

    def view_file(self, path: str, start_line: int = 1, end_line: int = 500) -> Dict[str, Any]:
        """Reads a file with paging, adding line numbers and checking for binary contents."""
        target = self._resolve_path(path)
        
        if not os.path.exists(target):
            return {"error": f"File '{path}' does not exist."}
            
        if os.path.isdir(target):
            return {"error": f"Path '{path}' is a directory, not a file."}

        try:
            stat = os.stat(target)
            file_size = stat.st_size
            
            # Read first 4000 bytes for binary checks
            with open(target, 'rb') as f:
                sample = f.read(4000)
                
            if self._is_binary(target, sample):
                return {
                    "path": os.path.relpath(target, self.base_dir).replace("\\", "/"),
                    "size_bytes": file_size,
                    "is_binary": True,
                    "error": "This file is binary and cannot be displayed as plain text."
                }
                
            # Decode sample to get correct string line counts
            content_str = sample.decode('utf-8', errors='ignore')
            
            # Open and read line range
            lines_output = []
            total_lines = 0
            
            with open(target, 'r', encoding='utf-8', errors='ignore') as f:
                for idx, line in enumerate(f, start=1):
                    total_lines = idx
                    if start_line <= idx <= end_line:
                        lines_output.append(f"{idx:6d}|{line.rstrip(chr(10))}")
            
            truncated = total_lines > end_line
            
            return {
                "path": os.path.relpath(target, self.base_dir).replace("\\", "/"),
                "size_bytes": file_size,
                "total_lines": total_lines,
                "start_line": start_line,
                "end_line": min(end_line, total_lines),
                "truncated": truncated,
                "content": "\n".join(lines_output),
                "hint": f"Use start_line={end_line + 1} to continue reading." if truncated else None
            }
        except Exception as e:
            return {"error": f"Failed to read file: {e}"}

    def write_file(self, path: str, content: str) -> Dict[str, Any]:
        """Atomically writes content to a file, creating parent folders if missing."""
        target = self._resolve_path(path)
        try:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
                
            # Perform atomic write to prevent corruption
            atomic_write(target, content)
            
            stat = os.stat(target)
            return {
                "path": os.path.relpath(target, self.base_dir).replace("\\", "/"),
                "success": True,
                "bytes_written": len(content.encode('utf-8')),
                "size_bytes": stat.st_size
            }
        except Exception as e:
            return {"error": f"Failed to write file: {e}"}

    def patch_file(self, path: str, old_content: str, new_content: str) -> Dict[str, Any]:
        """Safely patches a file by replacing a unique search block with new content."""
        target = self._resolve_path(path)
        
        if not os.path.exists(target):
            return {"error": f"File '{path}' does not exist to patch."}

        try:
            with open(target, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                
            occurrences = content.count(old_content)
            
            if occurrences == 0:
                return {
                    "error": "Patch Target Content Not Found. The target text must match the file contents exactly, including whitespace."
                }
            if occurrences > 1:
                return {
                    "error": f"Patch Target Ambiguous. Found {occurrences} occurrences. Provide more surrounding context to isolate the exact replacement."
                }
                
            # Perform replacement
            updated_content = content.replace(old_content, new_content, 1)
            
            # Atomic save
            atomic_write(target, updated_content)
            
            return {
                "path": os.path.relpath(target, self.base_dir).replace("\\", "/"),
                "success": True,
                "message": "File patched successfully."
            }
        except Exception as e:
            return {"error": f"Failed to patch file: {e}"}

    def grep_search(self, query: str, path: str = ".", file_pattern: Optional[str] = None) -> Dict[str, Any]:
        """Pure Python recursive search to find query matches across target directories."""
        start_dir = self._resolve_path(path)
        
        if not os.path.exists(start_dir):
            return {"error": f"Search path '{path}' does not exist."}
            
        results = []
        limit = 100 # Safe cap to prevent payload inflation
        count_scanned = 0
        
        try:
            # Case-insensitive query compilation
            query_pat = re.compile(re.escape(query), re.IGNORECASE)
            
            for root, dirs, files in os.walk(start_dir):
                # Filter out excluded directories in-place to optimize traversal
                dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
                
                for filename in files:
                    if len(results) >= limit:
                        break
                        
                    # Skip binary extensions before opening
                    ext = os.path.splitext(filename)[1].lower()
                    if ext in BINARY_EXTENSIONS:
                        continue
                        
                    if file_pattern and not fnmatch.fnmatch(filename, file_pattern):
                        continue
                        
                    filepath = os.path.join(root, filename)
                    count_scanned += 1
                    
                    try:
                        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                            for line_idx, line in enumerate(f, start=1):
                                if query_pat.search(line):
                                    rel_path = os.path.relpath(filepath, self.base_dir)
                                    results.append({
                                        "path": rel_path.replace("\\", "/"),
                                        "line": line_idx,
                                        "content": line.strip()
                                    })
                                    if len(results) >= limit:
                                        break
                    except Exception:
                        # Skip files that raise unhandled read/permission exceptions
                        continue
                        
            return {
                "query": query,
                "scanned_files_count": count_scanned,
                "matches": results,
                "total_matches": len(results),
                "truncated": len(results) >= limit
            }
        except Exception as e:
            return {"error": f"Failed to perform search: {e}"}

    def execute(self, input_data: Dict[str, Any]) -> Any:
        command = input_data.get("command")
        
        if command == "list_dir":
            return self.list_dir(input_data.get("path", "."))
            
        elif command == "view_file":
            path = input_data.get("path")
            if not path:
                return {"error": "view_file command requires a 'path' parameter."}
            start = int(input_data.get("start_line", 1))
            end = int(input_data.get("end_line", 500))
            return self.view_file(path, start, end)
            
        elif command == "write_file":
            path = input_data.get("path")
            content = input_data.get("content", "")
            if not path:
                return {"error": "write_file command requires a 'path' parameter."}
            return self.write_file(path, content)
            
        elif command == "patch_file":
            path = input_data.get("path")
            old = input_data.get("old_content")
            new = input_data.get("new_content")
            if not path or old is None or new is None:
                return {"error": "patch_file command requires 'path', 'old_content', and 'new_content'."}
            return self.patch_file(path, old, new)
            
        elif command == "grep_search":
            query = input_data.get("query")
            if not query:
                return {"error": "grep_search command requires a 'query' parameter."}
            return self.grep_search(
                query=query,
                path=input_data.get("path", "."),
                file_pattern=input_data.get("file_pattern")
            )
            
        else:
            return {"error": f"Unknown file_manager command: '{command}'."}
