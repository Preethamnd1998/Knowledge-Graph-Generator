#!/usr/bin/env python3
"""Cwd-independent entry point for the Knowledge Graph MCP server.

Run this file directly (`python mcp_server_main.py`) from *any* working
directory — unlike `python -m mcp_server.server`, which requires the
repo root to already be on sys.path (i.e. requires the caller to have
launched the process with the right cwd). Different MCP clients default
to different cwds when spawning a stdio server (Claude Code happened to
use the repo root; VS Code's MCP client uses the open workspace root
one level up, which broke `-m mcp_server.server` with
"ModuleNotFoundError: No module named 'mcp_server'") — this file removes
that dependency entirely by adding its own directory to sys.path before
importing anything from the package.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_server.server import main

if __name__ == "__main__":
    main()
