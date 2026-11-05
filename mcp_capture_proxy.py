#!/usr/bin/env python3
"""Stdio MCP proxy in front of Roblox's StudioMCP that makes `screen_capture` work in Play mode.

StudioMCP's `screen_capture` reads the edit-time viewport. While a playtest is running that
surface is never drawn, so the tool returns a solid magenta JPEG. This proxy forwards every
JSON-RPC message to the real StudioMCP unchanged, except `screen_capture` while Studio is in
Play mode. Then it:

  1. draws a 1px marker border around the client viewport (a ScreenGui, via `execute_luau`),
  2. screenshots the Studio window with macOS `screencapture`,
  3. removes the marker, crops the screenshot to the inside of the border,
  4. returns that JPEG in the same shape StudioMCP would.

If `camera_position` / `look_at_position` are given in Play mode, the client camera is pinned
to that view for the shot (re-asserted synchronously on every write, so the game's own
per-frame camera code cannot override it) and restored afterwards.

It also adds three optional arguments to `get_console_output` — `since`, `pattern`, `tail`,
applied in that order by `filter_console` below — because StudioMCP always returns the whole
Output log and a play session makes that log long. A call without any of them is forwarded
byte-for-byte; a call with one is forwarded without the filter keys, and the text StudioMCP
returns is filtered here. Errors from StudioMCP are passed through unfiltered.

Register it in place of StudioMCP (see README.md), then restart Claude Code:

  claude mcp remove Roblox_Studio -s user
  claude mcp add -s user Roblox_Studio -- <clone>/.venv/bin/python3 <clone>/mcp_capture_proxy.py

Requires: Pillow, pyobjc-framework-Quartz (falls back to AppleScript + region capture without
it), and Screen Recording permission for the terminal that launches Claude Code. The Studio
window must be on screen (not minimised, not on another Space).

Set STUDIO_MCP_BIN to override the StudioMCP binary path.
"""

import base64
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

STUDIO_MCP_BIN = os.environ.get(
    "STUDIO_MCP_BIN", "/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP"
)
MAX_WIDTH = 1920  # StudioMCP returns 1920-wide JPEGs; match that so payload sizes stay similar
JPEG_QUALITY = 85
MARKER_GUI = "__McpCaptureMarker"
CAMERA_BIND = "__McpCaptureCamera"
CONSOLE_TOOL = "get_console_output"
CONSOLE_FILTER_KEYS = ("since", "pattern", "tail")
CONSOLE_DESCRIPTION = (
    " Optional filters, applied in the order since, pattern, tail; the last line of a filtered "
    "result says how many lines survived. Without them the full log is returned unchanged."
)
CONSOLE_FILTER_SCHEMA = {
    "since": {
        "type": "string",
        "description": (
            "Keep only the lines after the last line containing this literal marker (print a "
            "unique marker from Luau first). A marker that never appears is an error, not an "
            "empty log."
        ),
    },
    "pattern": {
        "type": "string",
        "description": (
            "Regular expression; keep only the lines that match it. Lines are matched one at a "
            "time, so a multi-line message is kept only where its own lines match."
        ),
    },
    "tail": {
        "type": "integer",
        "minimum": 1,
        "description": "Keep only the last N lines (applied after since and pattern).",
    },
}

PATCHED_DESCRIPTION = (
    "Capture the current Studio viewport and return the image. In Edit mode this is StudioMCP's "
    "native capture. In Play mode the client viewport is captured from the Studio window on "
    "screen (Studio must not be minimised), so it works during playtests. If camera_position and "
    "look_at_position are provided, the camera is temporarily moved to that view for the shot."
)

LUAU_SETUP = """
local pg = game:GetService("Players").LocalPlayer:WaitForChild("PlayerGui", 5)
local old = pg:FindFirstChild("%(marker)s")
if old then old:Destroy() end
local sg = Instance.new("ScreenGui")
sg.Name = "%(marker)s"
sg.IgnoreGuiInset = true
sg.ScreenInsets = Enum.ScreenInsets.None
sg.DisplayOrder = 1000000
sg.ResetOnSpawn = false
local function edge(size, pos)
    local f = Instance.new("Frame")
    f.BackgroundColor3 = Color3.fromRGB(255, 0, 255)
    f.BorderSizePixel = 0
    f.Size = size
    f.Position = pos
    f.Parent = sg
end
edge(UDim2.new(1, 0, 0, 1), UDim2.new(0, 0, 0, 0))
edge(UDim2.new(1, 0, 0, 1), UDim2.new(0, 0, 1, -1))
edge(UDim2.new(0, 1, 1, 0), UDim2.new(0, 0, 0, 0))
edge(UDim2.new(0, 1, 1, 0), UDim2.new(1, -1, 0, 0))
sg.Parent = pg
%(camera)s
local RunService = game:GetService("RunService")
RunService.RenderStepped:Wait()
RunService.RenderStepped:Wait()
local v = workspace.CurrentCamera.ViewportSize
return string.format("%%d,%%d", v.X, v.Y)
"""

LUAU_CAMERA = """
local cam = workspace.CurrentCamera
local target = CFrame.lookAt(Vector3.new(%(cx)s, %(cy)s, %(cz)s), Vector3.new(%(lx)s, %(ly)s, %(lz)s))
local state = { Type = cam.CameraType, CFrame = cam.CFrame, guard = false }
-- The game may rewrite the camera every frame (e.g. from RenderStepped), after any render-step
-- priority we could bind at. Re-assert the target synchronously on every write instead, so the
-- game's pose never reaches the renderer.
state.conn = cam:GetPropertyChangedSignal("CFrame"):Connect(function()
    if state.guard or cam.CFrame == target then return end
    state.guard = true
    cam.CFrame = target
    state.guard = false
end)
_G.%(bind)s = state
cam.CameraType = Enum.CameraType.Scriptable
cam.CFrame = target
"""

LUAU_CLEANUP = """
local pg = game:GetService("Players").LocalPlayer:FindFirstChildOfClass("PlayerGui")
local m = pg and pg:FindFirstChild("%(marker)s")
if m then m:Destroy() end
local saved = _G.%(bind)s
if saved then
    if saved.conn then saved.conn:Disconnect() end
    local cam = workspace.CurrentCamera
    cam.CameraType = saved.Type
    cam.CFrame = saved.CFrame
    _G.%(bind)s = nil
end
return "ok"
"""


def filter_console(text, since=None, pattern=None, tail=None):
    """Apply since -> pattern -> tail to the console text, in that order.

    `since` keeps the lines after the LAST line containing that literal marker (the marker line
    itself is dropped); a marker that never appears raises ValueError rather than answering with
    an empty or a full log. `pattern` is a Python regex tested against each line on its own, so a
    multi-line message such as a stack trace is kept only where its individual lines match.
    `tail` keeps the last N lines of what is left. The final line of the result says how many of
    the original lines survived, so an empty selection cannot be mistaken for an empty log.
    """
    lines = text.splitlines()
    total = len(lines)
    applied = []
    if since is not None:
        marker = str(since)
        hits = [i for i, line in enumerate(lines) if marker in line]
        if not hits:
            hint = ""
            if lines and lines[0].startswith("[TRUNCATED"):
                hint = " (StudioMCP kept only the most recent part of the log; the marker may be older than that)"
            raise ValueError(f"since marker {marker!r} not found in the {total} console lines{hint}")
        lines = lines[hits[-1] + 1 :]
        applied.append(f"since={marker!r}")
    if pattern is not None:
        try:
            rx = re.compile(str(pattern))
        except re.error as e:
            raise ValueError(f"invalid pattern {pattern!r}: {e}")
        lines = [line for line in lines if rx.search(line)]
        applied.append(f"pattern={str(pattern)!r}")
    if tail is not None:
        if isinstance(tail, float) and tail.is_integer():
            tail = int(tail)
        if isinstance(tail, bool) or not isinstance(tail, int) or tail < 1:
            raise ValueError(f"tail must be a positive integer, got {tail!r}")
        lines = lines[-tail:]
        applied.append(f"tail={tail}")
    trailer = f"[mcp_capture_proxy] {len(lines)} of {total} console lines shown ({', '.join(applied)})"
    return "\n".join(lines + [trailer])


def log(msg):
    sys.stderr.write(f"[mcp_capture_proxy] {msg}\n")
    sys.stderr.flush()


def request_meta(msg):
    """The client request's `_meta`, for the requests the proxy makes on that request's behalf.

    StudioMCP (rmcp, protocol 2026-07-28) rejects a request whose `_meta` lacks the client's
    io.modelcontextprotocol/protocolVersion, clientInfo and clientCapabilities ("request _meta is
    missing or has malformed required fields"). The progress token names the client's own request.
    """
    meta = dict((msg.get("params") or {}).get("_meta") or {})
    meta.pop("progressToken", None)
    return meta


class Proxy:
    def __init__(self):
        self.child = subprocess.Popen(
            [STUDIO_MCP_BIN],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            bufsize=0,
        )
        self.child_lock = threading.Lock()
        self.out_lock = threading.Lock()
        self.pending = {}  # internal request id -> (Event, [response])
        self.internal_seq = 0
        self.tools_list_ids = set()
        threading.Thread(target=self.pump_child, daemon=True).start()

    # --- transport -------------------------------------------------------------------

    def send_child(self, msg):
        data = (json.dumps(msg) + "\n").encode()
        with self.child_lock:
            self.child.stdin.write(data)
            self.child.stdin.flush()

    def send_client(self, msg):
        data = (json.dumps(msg) + "\n").encode()
        with self.out_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def pump_child(self):
        for line in iter(self.child.stdout.readline, b""):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log(f"dropping non-JSON line from StudioMCP: {line[:120]!r}")
                continue
            rid = msg.get("id")
            if isinstance(rid, str) and rid in self.pending:
                ev, holder = self.pending.pop(rid)
                holder.append(msg)
                ev.set()
                continue
            if rid in self.tools_list_ids:
                self.tools_list_ids.discard(rid)
                self.patch_tools_list(msg)
            self.send_client(msg)
        log("StudioMCP exited; shutting down")
        os._exit(self.child.returncode or 0)

    def call_child(self, method, params, timeout=120):
        self.internal_seq += 1
        rid = f"__proxy:{self.internal_seq}"
        ev, holder = threading.Event(), []
        self.pending[rid] = (ev, holder)
        self.send_child({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not ev.wait(timeout):
            self.pending.pop(rid, None)
            raise TimeoutError(f"StudioMCP did not answer {method} within {timeout}s")
        return holder[0]

    def tool(self, name, arguments, meta):
        resp = self.call_child("tools/call", {"name": name, "arguments": arguments, "_meta": meta})
        if "error" in resp:
            raise RuntimeError(f"{name}: {resp['error']}")
        result = resp.get("result", {})
        text = "\n".join(
            c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"
        )
        if result.get("isError"):
            raise RuntimeError(f"{name}: {text}")
        return text

    @staticmethod
    def patch_tools_list(msg):
        for tool in msg.get("result", {}).get("tools", []) or []:
            if tool.get("name") == "screen_capture":
                tool["description"] = PATCHED_DESCRIPTION
            elif tool.get("name") == CONSOLE_TOOL:
                tool["description"] = tool.get("description", "") + CONSOLE_DESCRIPTION
                schema = tool.setdefault("inputSchema", {"type": "object"})
                schema.setdefault("properties", {}).update(CONSOLE_FILTER_SCHEMA)

    # --- main loop -------------------------------------------------------------------

    def run(self):
        for line in iter(sys.stdin.buffer.readline, b""):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log(f"dropping non-JSON line from client: {line[:120]!r}")
                continue
            method = msg.get("method")
            params = msg.get("params") or {}
            if method == "tools/call" and params.get("name") == "screen_capture" and "id" in msg:
                threading.Thread(target=self.handle_capture, args=(msg,), daemon=True).start()
                continue
            if (
                method == "tools/call"
                and params.get("name") == CONSOLE_TOOL
                and "id" in msg
                and any(k in (params.get("arguments") or {}) for k in CONSOLE_FILTER_KEYS)
            ):
                threading.Thread(target=self.handle_console, args=(msg,), daemon=True).start()
                continue
            if method == "tools/list" and "id" in msg:
                self.tools_list_ids.add(msg["id"])
            self.send_child(msg)
        log("client closed stdin; stopping StudioMCP")
        self.child.terminate()

    # --- get_console_output ----------------------------------------------------------

    def handle_console(self, msg):
        """Forward the call without its filter keys, then filter the text StudioMCP returns."""
        params = dict(msg.get("params") or {})
        args = dict(params.get("arguments") or {})
        filters = {k: args.pop(k) for k in CONSOLE_FILTER_KEYS if k in args}
        params["arguments"] = args
        reply = {"jsonrpc": "2.0", "id": msg["id"]}
        try:
            resp = self.call_child("tools/call", params)
        except Exception as e:
            log(f"{CONSOLE_TOOL} failed: {e}")
            reply["result"] = {
                "content": [{"type": "text", "text": f"{CONSOLE_TOOL} failed: {e}"}],
                "isError": True,
            }
            self.send_client(reply)
            return
        if "error" in resp:
            reply["error"] = resp["error"]  # never filtered
            self.send_client(reply)
            return
        result = resp.get("result") or {}
        content = result.get("content") or []
        if result.get("isError"):
            reply["result"] = result  # never filtered
            self.send_client(reply)
            return
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        others = [c for c in content if c.get("type") != "text"]
        try:
            filtered = filter_console(text, **filters)
            reply["result"] = {"content": [{"type": "text", "text": filtered}] + others}
        except ValueError as e:
            reply["result"] = {
                "content": [{"type": "text", "text": f"{CONSOLE_TOOL}: {e}"}],
                "isError": True,
            }
        self.send_client(reply)

    # --- screen_capture --------------------------------------------------------------

    def handle_capture(self, msg):
        args = (msg.get("params") or {}).get("arguments") or {}
        studio_id = args.get("studio_id")
        meta = request_meta(msg)
        try:
            state = self.tool("get_studio_state", {"studio_id": studio_id}, meta)
        except Exception as e:
            log(f"get_studio_state failed ({e}); forwarding screen_capture unchanged")
            self.send_child(msg)
            return
        mode = re.search(r"Current Studio Mode:\s*(\w+)", state)
        focused = re.search(r"Focused DataModel in the viewport:\s*(\w+)", state)
        if not mode or mode.group(1) != "Play":
            self.send_child(msg)  # Edit mode: native capture works, let StudioMCP answer
            return
        try:
            result = self.capture_play(studio_id, focused.group(1) if focused else "", args, meta)
        except Exception as e:
            log(f"play-mode capture failed: {e}")
            result = {
                "content": [{"type": "text", "text": f"Play-mode screen capture failed: {e}"}],
                "isError": True,
            }
        self.send_client({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    def capture_play(self, studio_id, focused, args, meta):
        from PIL import Image

        use_marker = focused == "Client"
        viewport = None
        if use_marker:
            camera = ""
            cam_pos, look_at = args.get("camera_position"), args.get("look_at_position")
            if cam_pos and look_at and len(cam_pos) == 3 and len(look_at) == 3:
                camera = LUAU_CAMERA % dict(
                    bind=CAMERA_BIND,
                    cx=float(cam_pos[0]), cy=float(cam_pos[1]), cz=float(cam_pos[2]),
                    lx=float(look_at[0]), ly=float(look_at[1]), lz=float(look_at[2]),
                )
            code = LUAU_SETUP % dict(marker=MARKER_GUI, camera=camera)
            text = self.tool(
                "execute_luau",
                {"studio_id": studio_id, "datamodel_type": "Client", "code": code},
                meta,
            )
            m = re.search(r"(\d+),(\d+)", text)
            if m:
                viewport = (int(m.group(1)), int(m.group(2)))
        try:
            img, bounds = screenshot_studio_window(self.window_title_hint(studio_id, meta))
        finally:
            if use_marker:
                try:
                    code = LUAU_CLEANUP % dict(marker=MARKER_GUI, bind=CAMERA_BIND)
                    self.tool(
                        "execute_luau",
                        {"studio_id": studio_id, "datamodel_type": "Client", "code": code},
                        meta,
                    )
                except Exception as e:
                    log(f"marker cleanup failed: {e}")

        note = f"Play mode ({focused or 'unknown'} view): whole Studio window captured."
        if use_marker and viewport:
            scale = img.width / bounds["Width"] if bounds.get("Width") else 1.0
            box = find_marker_box(img, viewport, scale)
            if box:
                img = img.crop(box)
                note = (
                    f"Play mode: client viewport {viewport[0]}x{viewport[1]} captured from the "
                    "Studio window."
                )
            else:
                note = "Play mode: viewport marker not found; whole Studio window captured."
        if img.width > MAX_WIDTH:
            img = img.resize(
                (MAX_WIDTH, round(img.height * MAX_WIDTH / img.width)), Image.Resampling.LANCZOS
            )
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        data = base64.b64encode(buf.getvalue()).decode()
        return {
            "content": [
                {"type": "image", "data": data, "mimeType": "image/jpeg"},
                {"type": "text", "text": note},
            ],
            "isError": False,
        }

    def window_title_hint(self, studio_id, meta):
        try:
            text = self.tool("list_roblox_studios", {}, meta)
            for s in json.loads(text).get("studios", []):
                if s.get("id") == studio_id:
                    return s.get("name", "").split(" (placeId")[0].strip()
        except Exception as e:
            log(f"list_roblox_studios failed ({e}); picking the largest Studio window")
        return ""


# --- macOS window capture -----------------------------------------------------------------


def find_studio_window(title_hint):
    """Return (window_number or None, bounds dict with X/Y/Width/Height in points)."""
    try:
        import Quartz
    except ImportError:
        Quartz = None
    if Quartz is not None:
        infos = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        candidates = []
        for w in infos:
            owner = (w.get("kCGWindowOwnerName") or "").lower()
            if "roblox studio" not in owner or w.get("kCGWindowLayer", 0) != 0:
                continue
            b = dict(w.get("kCGWindowBounds") or {})
            if b.get("Width", 0) < 400 or b.get("Height", 0) < 300:
                continue
            candidates.append((int(w["kCGWindowNumber"]), w.get("kCGWindowName") or "", b))
        if not candidates:
            raise RuntimeError(
                "no Roblox Studio window is on screen (minimised, hidden, or on another Space?)"
            )
        if title_hint:
            for num, name, b in candidates:
                if name.startswith(title_hint):
                    return num, b
        num, _, b = max(candidates, key=lambda c: c[2]["Width"] * c[2]["Height"])
        return num, b
    # Fallback without pyobjc: AppleScript bounds of the front Studio window.
    script = (
        'tell application "System Events" to tell process "RobloxStudio" '
        "to get {position, size} of window 1"
    )
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", out.stdout)]
    if out.returncode != 0 or len(nums) < 4:
        raise RuntimeError(f"could not locate the Studio window via AppleScript: {out.stderr.strip()}")
    return None, {"X": nums[0], "Y": nums[1], "Width": nums[2], "Height": nums[3]}


def screenshot_studio_window(title_hint):
    from PIL import Image

    num, bounds = find_studio_window(title_hint)
    fd, path = tempfile.mkstemp(prefix="studio_capture_", suffix=".png")
    os.close(fd)
    try:
        if num is not None:
            cmd = ["screencapture", "-x", "-o", "-l", str(num), "-t", "png", path]
        else:
            region = "{X},{Y},{Width},{Height}".format(**bounds)
            cmd = ["screencapture", "-x", "-R", region, "-t", "png", path]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not os.path.getsize(path):
            raise RuntimeError(
                "screencapture failed (is Screen Recording permission granted for the terminal "
                f"running Claude Code?): {proc.stderr.strip()}"
            )
        img = Image.open(path)
        img.load()
        img = img.convert("RGB")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if all(band.getextrema()[0] == band.getextrema()[1] for band in img.split()):
        raise RuntimeError("window capture came back blank")
    return img, bounds


def find_marker_box(img, viewport, scale):
    """Locate the 1pt magenta border drawn around the viewport; return the inner crop box."""
    from PIL import Image, ImageChops

    r, g, b = img.split()
    mask = ImageChops.multiply(
        ImageChops.multiply(
            r.point(lambda v: 255 if v > 200 else 0), g.point(lambda v: 255 if v < 90 else 0)
        ),
        b.point(lambda v: 255 if v > 200 else 0),
    )
    # Rows/columns that are mostly marker: average the mask down to one column/row. The viewport
    # renders scaled to fit the Studio window, so the border spans a large fraction of the capture
    # but its size is not known in advance; half the row is a safe floor (UI magenta is far smaller).
    row_avg = mask.resize((1, img.height), Image.Resampling.BOX).tobytes()
    col_avg = mask.resize((img.width, 1), Image.Resampling.BOX).tobytes()
    rows = [y for y, v in enumerate(row_avg) if v >= 0.5 * 255]
    cols = [x for x, v in enumerate(col_avg) if v >= 0.5 * 255]
    if not rows or not cols:
        return None
    top, bottom, left, right = rows[0], rows[-1], cols[0], cols[-1]
    width, height = right - left + 1, bottom - top + 1
    # The client viewport renders scaled to fit the Studio window ("Fit to Window"), so the border
    # is smaller than `viewport * scale`; only its aspect ratio has to match the viewport's.
    if viewport[1] > 0 and viewport[0] > 0:
        expected = viewport[0] / viewport[1]
        if abs(width / height - expected) > 0.02:
            log(
                f"marker box {width}x{height} (aspect {width / height:.3f}) does not match viewport "
                f"{viewport[0]}x{viewport[1]} (aspect {expected:.3f}); using the whole window"
            )
            return None
    thick = max(1, round(scale))
    return (left + thick, top + thick, right + 1 - thick, bottom + 1 - thick)


if __name__ == "__main__":
    Proxy().run()
