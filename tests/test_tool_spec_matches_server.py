"""Drift guard for docs/tool-spec.json (SPEC.md section 10): the checked-in
file must match what scripts/dump_tool_spec.py would regenerate from the
live server right now, so the submission's "MCP tool specification" never
goes stale relative to the actual tools.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session

from wc_connector import server

from conftest import make_config

TOOL_SPEC_PATH = Path(__file__).resolve().parent.parent / "docs" / "tool-spec.json"


async def test_tool_spec_json_matches_live_server(monkeypatch):
    monkeypatch.setattr(server, "load_config", lambda: make_config())

    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.list_tools()

    live_tools = [
        json.loads(tool.model_dump_json(exclude_none=True, by_alias=True))
        for tool in sorted(result.tools, key=lambda t: t.name)
    ]

    on_disk = json.loads(TOOL_SPEC_PATH.read_text())

    assert on_disk == {"tools": live_tools}, (
        "docs/tool-spec.json is stale; regenerate it with "
        "`python scripts/dump_tool_spec.py`."
    )
