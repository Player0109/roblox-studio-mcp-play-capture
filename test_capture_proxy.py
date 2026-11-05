#!/usr/bin/env python3
"""`mcp_capture_proxy.py`: the `get_console_output` filters.

`python3 -m unittest discover -p 'test_*.py'` from the repo root.

StudioMCP returns the whole Output log on every call and nothing else lets a caller narrow it, so
the proxy adds `since`, `pattern` and `tail`. Three things are pinned here:
  * `filter_console` applies since -> pattern -> tail in that order, drops the marker line, uses
    the marker's LAST occurrence, refuses a marker that never appears (an error, never an empty or
    a full log), refuses a bad regex or a bad tail, and ends with a line that says how many of the
    original lines survived;
  * `tools/list` gains the three optional properties on `get_console_output` and nothing else;
  * through the real proxy process (with `fixtures/fake_studio_mcp.py` standing in for StudioMCP),
    a call without filters is forwarded byte-for-byte, a call with filters is forwarded without
    the filter keys, and a tool error or a JSON-RPC error from StudioMCP comes back unfiltered.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mcp_capture_proxy as proxy  # noqa: E402

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


def body(text):
    """The filtered lines without the trailer."""
    lines = text.split("\n")
    assert lines[-1].startswith("[mcp_capture_proxy] "), lines[-1]
    return lines[:-1]


class FilterConsoleTests(unittest.TestCase):
    def test_since_keeps_only_the_lines_after_the_last_marker_line(self):
        out = proxy.filter_console(LOG, since="[GAME] MARK")
        self.assertEqual(body(out), ["[GAME] after B 1", "warn: after B 2"])
        self.assertTrue(out.endswith("2 of 7 console lines shown (since='[GAME] MARK')"), out)

    def test_since_is_a_literal_not_a_regex(self):
        out = proxy.filter_console(LOG, since="[GAME] MARK step-A")
        self.assertEqual(body(out)[0], "Error: boom")

    def test_missing_marker_is_an_error_not_an_empty_or_a_full_log(self):
        with self.assertRaises(ValueError) as cm:
            proxy.filter_console(LOG, since="never printed")
        self.assertIn("'never printed' not found in the 7 console lines", str(cm.exception))

    def test_missing_marker_names_studio_truncation_when_the_log_starts_with_it(self):
        with self.assertRaises(ValueError) as cm:
            proxy.filter_console("[TRUNCATED DUE TO LENGTH LIMIT]\n" + LOG, since="never printed")
        self.assertIn("not found in the 8 console lines (StudioMCP kept only", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            proxy.filter_console(LOG, since="never printed")
        self.assertNotIn("StudioMCP kept", str(cm.exception))

    def test_pattern_matches_each_line_on_its_own(self):
        out = proxy.filter_console(LOG, pattern=r"^Error:")
        self.assertEqual(body(out), ["Error: boom"])  # the stack line is not carried along

    def test_invalid_pattern_is_an_error(self):
        with self.assertRaises(ValueError) as cm:
            proxy.filter_console(LOG, pattern="[")
        self.assertIn("invalid pattern", str(cm.exception))

    def test_tail_keeps_the_last_lines_and_accepts_an_integral_float(self):
        self.assertEqual(body(proxy.filter_console(LOG, tail=2)), ["[GAME] after B 1", "warn: after B 2"])
        self.assertEqual(body(proxy.filter_console(LOG, tail=2.0)), ["[GAME] after B 1", "warn: after B 2"])
        self.assertEqual(len(body(proxy.filter_console(LOG, tail=100))), 7)

    def test_bad_tail_is_an_error(self):
        for bad in (0, -1, 1.5, "3", True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                proxy.filter_console(LOG, tail=bad)

    def test_order_is_since_then_pattern_then_tail(self):
        out = proxy.filter_console(LOG, since="step-A", pattern=r"\[GAME\]", tail=1)
        # since -> [Error, stack, MARK step-B, after B 1, after B 2]; pattern -> [MARK step-B, after B 1];
        # tail -> [after B 1]
        self.assertEqual(body(out), ["[GAME] after B 1"])
        self.assertTrue(out.endswith("(since='step-A', pattern='\\\\[GAME\\\\]', tail=1)"), out)

    def test_empty_selection_still_reports_the_count(self):
        out = proxy.filter_console(LOG, pattern="no such line")
        self.assertEqual(out, "[mcp_capture_proxy] 0 of 7 console lines shown (pattern='no such line')")
        self.assertEqual(proxy.filter_console("", tail=3),
                         "[mcp_capture_proxy] 0 of 0 console lines shown (tail=3)")


class PatchToolsListTests(unittest.TestCase):
    def test_console_tool_gains_the_three_filters_and_nothing_else_changes(self):
        msg = {"result": {"tools": [
            {"name": "get_console_output", "description": "Get the console output.",
             "inputSchema": {"properties": {"studio_id": {"type": "string"}},
                             "required": ["studio_id"], "type": "object"}},
            {"name": "screen_capture", "description": "edit-time"},
            {"name": "execute_luau", "description": "run", "inputSchema": {"properties": {}}},
        ]}}
        proxy.Proxy.patch_tools_list(msg)
        console, capture, luau = msg["result"]["tools"]
        self.assertEqual(sorted(console["inputSchema"]["properties"]),
                         ["pattern", "since", "studio_id", "tail"])
        self.assertEqual(console["inputSchema"]["required"], ["studio_id"])
        self.assertTrue(console["description"].startswith("Get the console output."))
        self.assertIn("since, pattern, tail", console["description"])
        self.assertEqual(capture["description"], proxy.PATCHED_DESCRIPTION)
        self.assertEqual(luau, {"name": "execute_luau", "description": "run", "inputSchema": {"properties": {}}})


class ProxyProcessTests(unittest.TestCase):
    """The real proxy process in front of `fixtures/fake_studio_mcp.py`."""

    FAKE = os.path.join(HERE, "fixtures", "fake_studio_mcp.py")

    def setUp(self):
        os.chmod(self.FAKE, 0o755)
        self.stderr = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "mcp_capture_proxy.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            env={**os.environ, "STUDIO_MCP_BIN": self.FAKE}, bufsize=0)
        self.watchdog = threading.Timer(30, self.proc.kill)
        self.watchdog.start()
        self.seq = 0
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "0"}})

    def tearDown(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.watchdog.cancel()
        self.stderr.close()

    def request(self, method, params):
        self.seq += 1
        rid = self.seq
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                           "params": params}) + "\n").encode())
        for line in iter(self.proc.stdout.readline, b""):
            msg = json.loads(line)
            if msg.get("id") == rid:
                return msg
        self.fail(f"proxy closed stdout before answering {method} #{rid}")

    def call(self, arguments):
        return self.request("tools/call", {"name": "get_console_output", "arguments": arguments})

    def text_of(self, msg):
        return "\n".join(c["text"] for c in msg["result"]["content"] if c["type"] == "text")

    def test_tools_list_is_patched(self):
        tools = {t["name"]: t for t in self.request("tools/list", {})["result"]["tools"]}
        self.assertEqual(sorted(tools["get_console_output"]["inputSchema"]["properties"]),
                         ["pattern", "since", "studio_id", "tail"])
        self.assertEqual(tools["screen_capture"]["description"], proxy.PATCHED_DESCRIPTION)

    def test_a_call_without_filters_is_forwarded_unchanged(self):
        msg = self.call({"studio_id": "s1"})
        self.assertNotIn("isError", msg["result"])
        self.assertEqual(self.text_of(msg), LOG + '\nargs={"studio_id": "s1"}')

    def test_a_call_with_filters_is_forwarded_without_them_and_answered_filtered(self):
        msg = self.call({"studio_id": "s1", "since": "step-B", "pattern": r"^args=|after B"})
        self.assertNotIn("isError", msg["result"])
        self.assertEqual(body(self.text_of(msg)),
                         ["[GAME] after B 1", "warn: after B 2", 'args={"studio_id": "s1"}'])

    def test_a_missing_marker_is_a_tool_error(self):
        msg = self.call({"studio_id": "s1", "since": "never printed"})
        self.assertTrue(msg["result"]["isError"])
        self.assertIn("'never printed' not found in the 8 console lines", self.text_of(msg))

    def test_a_tool_error_from_studio_passes_through_unfiltered(self):
        msg = self.call({"studio_id": "broken", "tail": 1})
        self.assertTrue(msg["result"]["isError"])
        self.assertEqual(self.text_of(msg), "no such studio")

    def test_a_json_rpc_error_from_studio_passes_through_unfiltered(self):
        msg = self.call({"studio_id": "rpc-error", "tail": 1})
        self.assertEqual(msg["error"], {"code": -32000, "message": "studio went away"})
        self.assertNotIn("result", msg)


if __name__ == "__main__":
    unittest.main()
