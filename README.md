# Roblox Studio MCP: Play-mode screenshots

Fixes the **solid magenta image** that Roblox Studio MCP's `screen_capture` returns during a
playtest, so Claude Code (or any MCP client) can see your game while it runs.

Roblox's `StudioMCP` captures the Edit viewport. Once you press Play, Studio stops drawing that
viewport, so every screenshot comes back plain magenta. This small proxy sits between Claude Code
and `StudioMCP`. It passes every tool call through unchanged, and in Play mode it takes the
screenshot itself, from the Studio window on your screen.

**macOS only.** It uses macOS `screencapture` and the Quartz window list.

## Install with Claude Code

Copy this whole block into Claude Code and send it:

```text
Install the Play-mode screenshot fix for Roblox Studio MCP from
https://github.com/Player0109/roblox-studio-mcp-play-capture and connect it to Claude Code:

1. Check that this Mac has /Applications/RobloxStudio.app/Contents/MacOS/StudioMCP. If it does
   not, or this is not macOS, stop and tell me.
2. Clone the repo to ~/roblox-studio-mcp-play-capture. If that folder already exists, run
   git pull in it instead.
3. In that folder, create a virtual environment with `python3 -m venv .venv`, upgrade its pip
   with `.venv/bin/python3 -m pip install --upgrade pip` (the old pip that comes with macOS
   Python 3.9 picks a broken PyObjC), then install requirements.txt with .venv/bin/pip.
4. Run the tests there: `.venv/bin/python3 -m unittest discover -p 'test_*.py'`. Stop and show
   me the output if any fail.
5. Find every Claude Code MCP server that runs StudioMCP: look in ~/.claude.json (the top-level
   "mcpServers" for user scope, and "projects" -> <folder> -> "mcpServers" for local scope) and in
   any .mcp.json in the current folder. List them for me with their name, scope and folder.
6. Point each one at the proxy, keeping its name and scope. Use the claude mcp commands, not
   hand edits, and for a local-scope server run them from that project's folder:
     claude mcp remove <name> -s <scope>
     claude mcp add -s <scope> <name> -- <HOME>/roblox-studio-mcp-play-capture/.venv/bin/python3 <HOME>/roblox-studio-mcp-play-capture/mcp_capture_proxy.py
   Replace <HOME> with my real home folder path; do not leave a ~ in the command. If no server
   runs StudioMCP yet, add one named Roblox_Studio in user scope.
7. Run `claude mcp get <name>` for each and show me that it now runs mcp_capture_proxy.py.
8. Tell me the two things I must do myself: give my terminal app (or the Claude app) Screen
   Recording permission in System Settings -> Privacy & Security -> Screen & System Audio
   Recording, and then quit and restart Claude Code.
```

After the restart, start a playtest in Studio and ask Claude Code to take a screenshot.

## Install by hand

```sh
git clone https://github.com/Player0109/roblox-studio-mcp-play-capture.git ~/roblox-studio-mcp-play-capture
cd ~/roblox-studio-mcp-play-capture
python3 -m venv .venv
.venv/bin/python3 -m pip install --upgrade pip   # macOS Python 3.9's old pip picks a broken PyObjC
.venv/bin/pip install -r requirements.txt

# Replace your current Roblox Studio server (use -s local instead if you added it per project)
claude mcp remove Roblox_Studio -s user
claude mcp add -s user Roblox_Studio -- "$HOME/roblox-studio-mcp-play-capture/.venv/bin/python3" "$HOME/roblox-studio-mcp-play-capture/mcp_capture_proxy.py"
```

Then:

1. Open **System Settings → Privacy & Security → Screen & System Audio Recording** and turn it on
   for the app that runs Claude Code: Terminal, iTerm, VS Code, or the Claude app.
2. Quit and restart Claude Code.

Run `claude mcp list` to see where your server is registered. A local-scope entry in a project
folder overrides the user-scope one there, so update that entry as well.

## What you get

| Tool | In Edit mode | In Play mode |
|---|---|---|
| `screen_capture` | Roblox's own capture, unchanged | The game view, cropped from the Studio window |
| `get_console_output` | Gains optional `since`, `pattern`, `tail` filters | Same |
| Every other tool | Unchanged | Unchanged |

**`screen_capture` in Play mode.** `camera_position` and `look_at_position` (each `[x, y, z]`)
still work. The proxy holds the camera on that view for the shot, even if your game moves the
camera every frame, and puts it back afterwards.

**`get_console_output` filters.** Roblox returns the whole Output log every time, and a playtest
makes it long. The filters apply in this order:

| Argument | Keeps |
|---|---|
| `since` | the lines after the **last** line that contains this text. A marker that never appears is an error |
| `pattern` | the lines that match this regular expression, tested one line at a time |
| `tail` | the last N lines of what is left |

A filtered result ends with a line saying how many lines survived, so an empty result cannot be
mistaken for an empty log. A useful habit: `print("MARK-42")` from Luau before an action, then ask
for the console `since: "MARK-42"`.

## Requirements and limits

- macOS with Roblox Studio installed in `/Applications`. Set `STUDIO_MCP_BIN` to use another path.
- Python 3.9 or later, with `Pillow` and `pyobjc-framework-Quartz` (from `requirements.txt`).
- Screen Recording permission for the app that runs Claude Code.
- The Studio window must be **on screen**: not minimised and not on another desktop (Space).
- If Studio is showing the **Server** view in Play mode, you get the whole Studio window
  instead of a cropped game view. Switch to the Client view for the cropped shot.

## How it works

When `screen_capture` is called, the proxy asks Studio which mode it is in. In Edit mode it
forwards the call unchanged. In Play mode it:

1. draws a 1-pixel magenta border around the client viewport (a temporary ScreenGui, added with
   `execute_luau`);
2. screenshots the Studio window with `screencapture`;
3. removes the border, finds it in the screenshot and crops to just inside it;
4. returns the JPEG in the same format `StudioMCP` uses.

If it cannot find the border, it returns the whole Studio window and says so in the result text.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Still magenta | Claude Code is still running `StudioMCP` directly. Check `claude mcp get Roblox_Studio`, then restart Claude Code |
| `screencapture failed` | Grant Screen Recording permission to the app that runs Claude Code, then restart it |
| `no Roblox Studio window is on screen` | Un-minimise Studio and bring it onto the current desktop |
| Whole window instead of the game view | Switch Studio to the Client view; the result text says why the crop was skipped |

## Uninstall

```sh
claude mcp remove Roblox_Studio -s user
claude mcp add -s user Roblox_Studio -- /Applications/RobloxStudio.app/Contents/MacOS/StudioMCP
rm -rf ~/roblox-studio-mcp-play-capture
```

## Tests

```sh
.venv/bin/python3 -m unittest discover -p 'test_*.py'
```

The tests run the real proxy in front of a fake `StudioMCP` (`fixtures/fake_studio_mcp.py`), so
they do not need Studio open.

## License

MIT. See [LICENSE](LICENSE).
