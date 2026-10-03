"""SPEC.md section 9 test 20: nothing is written to stdout except MCP
protocol messages (logs go to stderr).

Proven end-to-end rather than by inspection: spawns the real server as a
subprocess talking real stdio, and drives it with a real mcp ClientSession.
A stray print() to stdout anywhere in the server would corrupt the
newline-delimited JSON-RPC framing that stdio_client/ClientSession rely on,
so this would fail or hang rather than silently pass if that rule were ever
violated. No upstream HTTP call is made (initialize + list_tools only), so
this needs no network access and never touches the real store.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"


async def test_server_speaks_clean_json_rpc_over_stdio_subprocess():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    # Fake, non-secret config: initialize + list_tools never call upstream,
    # so these values are never used for a real request.
    env["WC_BASE_URL"] = "https://example.test"
    env["WC_CONSUMER_KEY"] = "ck_test"
    env["WC_CONSUMER_SECRET"] = "cs_test"

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "wc_connector.server"],
        env=env,
        cwd=str(REPO_ROOT),
    )

    with anyio.fail_after(20):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_result = await session.list_tools()

    names = {tool.name for tool in tools_result.tools}
    assert names == {"get_order", "list_orders", "search_orders", "find_customer"}
