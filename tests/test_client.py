#!/usr/bin/env python3
"""Small end-to-end test for Persistent Root Compute MCP."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


BASE = os.environ.get("BASE", "http://127.0.0.1:8877").rstrip("/")
TOKEN = os.environ["MCP_TOKEN"]


def data_of(result):
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    raise AssertionError(f"Tool result has no structured dict data: {result!r}")


async def main() -> None:
    checks: list[str] = []

    async with httpx.AsyncClient(timeout=10) as web:
        health = await web.get(f"{BASE}/healthz")
        assert health.status_code == 200 and health.text == "ok"
        checks.append("health")

        unauthorized = await web.post(f"{BASE}/mcp", json={})
        assert unauthorized.status_code == 401
        checks.append("auth-negative")

        invalid_origin = await web.post(
            f"{BASE}/mcp",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Origin": "https://attacker.invalid",
            },
            json={},
        )
        assert invalid_origin.status_code == 403
        checks.append("origin-negative")

    transport = StreamableHttpTransport(
        f"{BASE}/mcp",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    async with Client(transport, timeout=30) as client:
        tools = await client.list_tools()
        names = [tool.name for tool in tools]
        assert names == ["terminal"], names
        checks.append("one-tool")

        root_result = data_of(
            await client.call_tool(
                "terminal",
                {
                    "action": "run",
                    "command": "printf 'uid='; id -u; printf 'cwd='; pwd; if env | grep -q '^MCP_TOKEN='; then echo token_leaked; else echo token_sanitized; fi",
                    "cwd": "/root",
                    "wait_seconds": 3,
                },
            )
        )
        assert root_result["state"] == "exited", root_result
        assert root_result["exit_code"] == 0, root_result
        assert "uid=0" in root_result["output"], root_result
        assert "cwd=/root" in root_result["output"], root_result
        assert "token_sanitized" in root_result["output"], root_result
        assert "token_leaked" not in root_result["output"], root_result
        checks.extend(["root", "cwd", "child-env-sanitized"])

        marker = f"mcp-persistence-{uuid.uuid4().hex}"
        marker_path = f"/tmp/{marker}"
        create = data_of(
            await client.call_tool(
                "terminal",
                {
                    "action": "run",
                    "command": f"printf persistent > {marker_path}",
                    "wait_seconds": 2,
                },
            )
        )
        assert create["exit_code"] == 0, create
        verify = data_of(
            await client.call_tool(
                "terminal",
                {
                    "action": "run",
                    "command": f"cat {marker_path}; rm -f {marker_path}",
                    "wait_seconds": 2,
                },
            )
        )
        assert verify["exit_code"] == 0 and "persistent" in verify["output"], verify
        checks.append("cross-call-filesystem-persistence")

        long_run = data_of(
            await client.call_tool(
                "terminal",
                {
                    "action": "run",
                    "command": "printf 'phase-one\\n'; sleep 2; printf 'phase-two\\n'",
                    "wait_seconds": 0.1,
                },
            )
        )
        assert long_run["state"] == "running", long_run
        session_id = long_run["session_id"]
        deadline = time.monotonic() + 8
        combined = long_run["output"]
        final = long_run
        while time.monotonic() < deadline and final["state"] == "running":
            final = data_of(
                await client.call_tool(
                    "terminal",
                    {
                        "action": "read",
                        "session_id": session_id,
                        "wait_seconds": 1,
                    },
                )
            )
            combined += final["output"]
        assert final["state"] == "exited" and final["exit_code"] == 0, final
        assert "phase-one" in combined and "phase-two" in combined, combined
        checks.append("async-poll")

        opened = data_of(
            await client.call_tool(
                "terminal",
                {"action": "open", "cwd": "/root", "wait_seconds": 0.2},
            )
        )
        interactive_id = opened["session_id"]
        interaction = data_of(
            await client.call_tool(
                "terminal",
                {
                    "action": "write",
                    "session_id": interactive_id,
                    "input_text": "cd /tmp && printf 'interactive-cwd=' && pwd",
                    "press_enter": True,
                    "wait_seconds": 1,
                },
            )
        )
        interaction_output = interaction["output"]
        for _ in range(8):
            if "interactive-cwd=/tmp" in interaction_output:
                break
            interaction = data_of(
                await client.call_tool(
                    "terminal",
                    {
                        "action": "read",
                        "session_id": interactive_id,
                        "wait_seconds": 1,
                    },
                )
            )
            interaction_output += interaction["output"]
        assert "interactive-cwd=/tmp" in interaction_output, interaction_output
        closed = data_of(
            await client.call_tool(
                "terminal",
                {"action": "close", "session_id": interactive_id},
            )
        )
        assert closed["state"] == "exited", closed
        checks.append("interactive-pty")

    print(f"RESULT: pass={len(checks)} fail=0")
    print("CHECKS: " + ", ".join(checks))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"RESULT: pass=0 fail=1 ({type(exc).__name__}: {exc})", file=sys.stderr)
        raise
