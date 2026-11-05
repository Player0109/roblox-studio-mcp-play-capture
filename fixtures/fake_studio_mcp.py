#!/usr/bin/env python3
"""A stand-in for Roblox's StudioMCP binary, for test_capture_proxy.py.

Speaks just enough stdio JSON-RPC for the proxy: `initialize`, `tools/list` (the real shapes of
`get_console_output` and `screen_capture`), and `tools/call` for `get_console_output`, which
answers a fixed log whose last line echoes the arguments it was called with — so a test can see
exactly what the proxy forwarded. A `studio_id` of "broken" answers a tool error, and one of
"rpc-error" a JSON-RPC error, so the pass-through of both can be checked.
"""

import json
import sys

LOG = "\n".join(
    [
        "[GAME] boot",
        "[GAME] MARK step-A",
        "Error: boom",
        "   stack line 1",
        "[GAME] MARK step-B",
        "[GAME] after B 1",
        "warn: after B 2",
    ]
)

TOOLS = [
    {
        "name": "get_console_output",
        "description": "Get the console output from the Studio output log.",
        "inputSchema": {
            "properties": {"studio_id": {"description": "Selects Roblox Studio instance", "type": "string"}},
            "required": ["studio_id"],
            "type": "object",
        },
    },
    {
        "name": "screen_capture",
        "description": "Capture current edit-time screen, return the image data.",
        "inputSchema": {
            "properties": {
                "capture_id": {"type": "string"},
                "studio_id": {"type": "string"},
            },
            "required": ["capture_id", "studio_id"],
            "type": "object",
        },
    },
    {"name": "get_studio_state", "description": "Get the state of the studio.", "inputSchema": {"type": "object"}},
]


def answer(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in iter(sys.stdin.readline, ""):
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        rid = msg.get("id")
        method = msg.get("method")
        if rid is None:
            continue  # a notification
        if method == "initialize":
            answer(rid, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                         "serverInfo": {"name": "fake-studio-mcp", "version": "0"}})
        elif method == "tools/list":
            answer(rid, {"tools": json.loads(json.dumps(TOOLS))})
        elif method == "tools/call":
            params = msg.get("params") or {}
            args = params.get("arguments") or {}
            if params.get("name") != "get_console_output":
                answer(rid, {"content": [{"type": "text", "text": "unsupported"}], "isError": True})
            elif args.get("studio_id") == "rpc-error":
                answer(rid, error={"code": -32000, "message": "studio went away"})
            elif args.get("studio_id") == "broken":
                answer(rid, {"content": [{"type": "text", "text": "no such studio"}], "isError": True})
            else:
                text = LOG + "\nargs=" + json.dumps(args, sort_keys=True)
                answer(rid, {"content": [{"type": "text", "text": text}]})
        else:
            answer(rid, {})


if __name__ == "__main__":
    main()
