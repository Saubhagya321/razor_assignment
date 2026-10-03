"""SPEC.md section 10: the MCP tool specification deliverable.

Starts the real server in-process (no real store needed -- tools/list never
calls upstream), performs tools/list over a real MCP ClientSession, and
writes the result to docs/tool-spec.json: names, titles, descriptions,
JSON-schema inputs/outputs, and annotations for every tool.

Usage:
    python scripts/dump_tool_spec.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from wc_connector import server  # noqa: E402
from wc_connector.config import Config  # noqa: E402

OUTPUT_PATH = REPO_ROOT / "docs" / "tool-spec.json"

# A placeholder config: tools/list never makes an upstream call, so no real
# store or credentials are needed to produce this spec.
_PLACEHOLDER_CONFIG = Config(
    base_url="https://placeholder.invalid",
    consumer_key="ck_placeholder",
    consumer_secret="cs_placeholder",
    ca_bundle=None,
    page_size=20,
    page_size_max=50,
    max_retries=3,
    timeout_seconds=5,
    max_total_wait_seconds=20,
    max_concurrent_requests=2,
    min_request_interval_ms=200,
    allow_insecure_localhost=True,
)


async def main() -> None:
    server.load_config = lambda: _PLACEHOLDER_CONFIG

    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.list_tools()

    tools = [
        json.loads(tool.model_dump_json(exclude_none=True, by_alias=True))
        for tool in sorted(result.tools, key=lambda t: t.name)
    ]

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps({"tools": tools}, indent=2) + "\n")
    print(f"Wrote {len(tools)} tool(s) to {OUTPUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
