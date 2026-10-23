# iterm-xshell

Turns iTerm2 on macOS into an **Xshell + Xftp**-like experience:

- **SSH channels** (Session Manager): saved channels grouped into folders, automatically imported from `~/.ssh/config`. One click opens SSH in a new tab.
- **Xftp**: browse files on the server the current tab is ssh'd into. Drag-and-drop upload, background downloads with progress, create folders, rename, delete, quick view, and `cd` the terminal to the folder you are looking at.

A single Python daemon (iTerm2 Python API, AutoLaunch) runs an aiohttp web server on `127.0.0.1` that serves two plain HTML/JS panels:

| Panel | Where it lives |
|---|---|
| SSH channels | **A separate window docked to the left edge** of the active iTerm2 window, because the Toolbelt cannot be placed on the left (see *Verified assumptions*). Optionally also added to the Toolbelt. |
| Xftp | The `Xftp` tool in the Toolbelt (right side). |

## Install

Download the `.dmg` (or `.tar.gz`) from the [Releases](https://github.com/duongtx90/iterm-xshell/releases) page and double-click `Cài đặt.command` (the installer; the name is Vietnamese for "Install"), or from a checkout:

```bash
./install.sh            # install/update and start right away, no iTerm2 restart needed
./install.sh --help     # --python PATH, --no-start, --dev, --uninstall, --purge
```

The DMG is not signed or notarized; on first launch you may need to right-click > Open.

Requirements:
- macOS and iTerm2 3.5 or later (tested on 3.7.3), with **Settings > General > Magic > Enable Python API** turned on.
- Python 3.9 or later. install.sh looks for one in this order: Homebrew, python.org, iTerm2's Python runtime, then the Command Line Tools `/usr/bin/python3`.

install.sh does the following:
1. Creates a venv at `~/Library/Application Support/iterm-xshell/venv` and installs `iterm2 aiohttp asyncssh pyobjc-framework-WebKit pyobjc-framework-Quartz`. On Apple Silicon it always runs under `arch -arm64`.
2. Copies the source to `~/Library/Application Support/iterm-xshell/app` and creates `run.sh`. `run.sh` replaces any running instance.
3. Compiles `~/Library/Application Support/iTerm2/Scripts/AutoLaunch/XshellPanels.scpt`. Every time iTerm2 starts, this file requests a Python API cookie with `request cookie and key` and then calls `run.sh`.

To restart the daemon, choose **Scripts > AutoLaunch > XshellPanels** from the menu, or re-run `./install.sh`.
Logs are in `~/Library/Logs/iterm-xshell.log` (app) and `~/Library/Logs/iterm-xshell.out` (stdout/stderr).

Uninstall: `./install.sh --uninstall` keeps your channel list, `--purge` deletes it too.

### Server tab (hardware, firewall, access history)

A third tab, **Server**, in the same panel (⌘3). Nothing is polled until you open it.

- **Resources**: CPU, RAM and every real disk with used/total, and load average. Refreshed every 15 s while the tab is open; a banner warns when CPU/RAM/disk reaches 90% or load per core reaches 1.5.
- **Heavy services**: processes grouped by name, sorted by CPU or RAM.
- **Firewall & open ports**: `ufw` / `firewalld` / `nftables` / `iptables` rules and listening ports (public vs local-only).
- **Access history**: current logins, `last`, failed logins and recent sshd accepted/failed events.
- **Actions**: recent `sudo` commands from the journal or `auth.log`.

Everything is read-only and uses a fixed whitelist of commands over the existing SSH connection (no arbitrary command execution). Sections that need root use `sudo -n` when it works without a password; otherwise the tab says some data may be missing. Non-Linux servers only show disks and processes.

## Usage

### SSH channels (left window)

- The window is only visible while iTerm2 is frontmost, and always hugs the left edge of the active iTerm2 window. Drag its right edge to resize (stored in `~/.config/iterm-xshell/window.json`). If there is not enough room on the left (the window is at the screen edge), the iTerm2 window is shifted right once to make space.
- Clicking the panel does **not** steal focus from iTerm2 (non-activating panel), yet you can still type in the search box and forms.
- **Click** or **Enter** opens the channel in a **new tab**. **Shift+Enter** opens it in a vertical split.
- **Right-click** (or the Menu key) opens a menu: new tab, vertical split, horizontal split, current tab, switch to the open tab, edit, duplicate, copy ssh command, delete.
- A green `●` / `● 2` badge shows that the channel is open in 1 or 2 tabs.
- Channels imported from `~/.ssh/config` have a ⚙ icon and live in the `~/.ssh/config` group (you can change the group). If the alias disappears from the config, the channel is shown struck through and cannot be opened, but it is not deleted automatically.
- The ⟳ button syncs immediately. The daemon also syncs on startup and whenever `~/.ssh/config` or one of its `Include` files changes (checked every 2 seconds by mtime/size).
- Click × on the title bar to hide the window. Bring it back with a shortcut (see below).

### Xftp (Toolbelt)

Enable it with **View > Toolbelt > Show Toolbelt**, then tick **Xftp** in **View > Toolbelt**.

- The panel detects the host of the current tab. If the tab was opened from the SSH channels panel (it has the variable `user.xshellSessionId`), the panel uses that channel's exact settings, including `identity_file` and any `-o` options in the extra arguments. Otherwise it parses the tab's `ssh …` command and resolves it with `ssh -G`.
- **⇵ follow terminal**: when the server has [iTerm2 Shell Integration](https://iterm2.com/documentation-shell-integration.html), `cd` in the terminal makes the panel follow. The **cd ↵** button does the reverse: it `cd`s the terminal to the folder the panel is showing.
- Upload: drag files **or whole folders** into the list, or click ⤒. There is a progress bar, a **✕ Cancel** button (a partial file is removed from the server) and a confirmation before overwriting.
- **Download** saves to `~/Downloads`; folders work too. Name clashes become `name (1)`, and Finder opens at the downloaded file when it finishes. Downloads **run in the background inside the daemon**: a "Downloads" strip under the file list shows percentage, size, speed, ETA and a Cancel button for each job. It survives switching chat sessions or reloading the panel, and warns when no data has arrived for 15 seconds. A cancelled or failed download deletes its partial file. The SFTP read window is enlarged (`block_size=-1`, 256 in-flight requests) to cope with high-latency links.
- **View** (or Space) quick-views the first 256 KB of a text file. Binary files are rejected.
- ⇄ connects manually to any alias or `user@host:port`. Leave it empty to go back to following the terminal.
- If the server only allows password login, the panel asks for the password. Passwords are kept in RAM only, unless the channel has Keychain storage enabled.

### Shortcuts

| Where | Key | Action |
|---|---|---|
| SSH channels | ⌘F | Focus the search box (accent-insensitive: `kho van` matches "Kho vận") |
| SSH channels | ↑ ↓, Enter, Shift+Enter | Select a channel, open in new tab, open in vertical split |
| SSH channels | ⌘N | Add a channel |
| SSH channels | ⌫ | Delete a channel (with confirmation) |
| SSH channels | Esc | Clear search, close menu/form |
| Xftp | ⌘↑ / Enter | Go to parent folder / open folder |
| Xftp | ⌘A, ⌘-click, Shift-click | Multi-select |
| Xftp | Space | Quick view |
| Xftp | ⌫ | Delete (with confirmation) |
| Xftp | ⌘L | Type a path |

Global shortcut to show/hide the SSH channels window: go to **Settings > Keys > Key Bindings > +**, choose the action **Invoke Script Function**, and enter `xshell_toggle_sessions()` to toggle, or `xshell_focus_sessions()` to show it and focus the search box.

## Configuration

Stored in `~/.config/iterm-xshell/sessions.json` (directory mode `700`, file mode `600`, written atomically via a temp file + `rename`). The `"settings"` section:

| Key | Default | Meaning |
|---|---|---|
| `download_dir` | `~/Downloads` | Download folder |
| `strict_host_key_checking` | `true` | Verify host keys against the `UserKnownHostsFile` reported by `ssh -G`. `StrictHostKeyChecking no` in your config is honored |
| `ssh_config` | `~/.ssh/config` | Config file used for import and resolution (`ssh -F`) |
| `sessions_window` | `true` | Enable the left SSH channels window |
| `sessions_window_make_room` | `true` | Shift the iTerm2 window right when there is not enough room on the left |
| `sessions_in_toolbelt` | `false` | Also add the SSH channels panel to the Toolbelt |
| `keychain_enabled` | `true` | Allow storing channel passwords in the macOS Keychain |

Restart the daemon after editing.

A channel consists of the fields `id, name, group, host, port, user, identity_file, proxy_jump, extra_args, remote_dir, color, tags, source (manual|ssh_config), missing, keychain, autotype_password`. An imported channel stores only the alias in `host`; HostName/Port/User are read by `ssh` from your config at connect time.

**Passwords**: by default everything relies on SSH keys/agent. If "Save password in Keychain" is enabled for a channel, the password is stored with `security` (service `iterm-xshell`, account = the channel id) through `security -i` reading from stdin, so it never appears in argv. The Xftp panel uses this password when the server asks for one. The "Auto-type password" option types it into the terminal when a `password:` prompt shows up within the first 30 seconds.

**Open-channel command**: built with `shlex.join`, e.g. `ssh -p 2222 -i key -J bastion user@host`. If `remote_dir` is set, it adds `-t … 'cd DIR && exec $SHELL -l'`. New tabs use a *custom command* profile `/bin/sh -c 'ssh …; …'`. That way the tab closes normally when ssh exits; only when ssh fails (exit code 255) is the tab kept so you can read the error. Each tab also lives at least 5 seconds to avoid an iTerm2 modal warning (see below). The "current tab" mode sends the ssh command into the already-open shell.

## Security

- The server binds to `127.0.0.1` only, on a random port, with a random token generated on every run, and every `/api/*` call requires the `X-Token` header. The URL containing the token is passed to the SSH channels window through an environment variable, not argv (so other users' `ps` cannot see it).
- File names from the client always go through `basename`. Sub-folder paths in folder uploads reject `..`, empty parts and `\0`.
- Passwords and file contents are never logged.
- Delete and overwrite always ask for confirmation through a custom modal, because `confirm()`/`prompt()` do not work in the webview.

## Verified assumptions (SPEC section 6, iTerm2 3.7.3, macOS 26.5, Apple Silicon)

| # | Assumption | Result |
|---|---|---|
| 1 | `iterm2.tool.async_register_web_view_tool(connection, name, id, reveal, url)` | **Signature is correct**, returns `None`. The webview loads `http://127.0.0.1:<port>`, and `fetch` with custom headers works. Each iTerm2 window has its own webview, loaded even while the Toolbelt is hidden. When the daemon restarts and re-registers the same `id` with a new URL, open webviews switch to the new URL automatically. |
| 2 | Variables `commandLine`, `jobPid`, `jobName`, `hostname`, `path` | **Available.** While ssh'ing: `commandLine='ssh xe-pay-243'`, `jobName='ssh'`, `jobPid` is the ssh pid. Without Shell Integration on the server, `hostname`/`path` stay local. With Shell Integration (tested with a container), `hostname` is the server name and `path` the remote folder, updating immediately on `cd`. Unset `user.*` variables return `None`. |
| 3 | Dragging files from Finder into the Toolbelt webview | **Not verified on a real GUI**, because the screen was locked for the whole session. The JS side was verified with `DragEvent`/`DataTransfer` in a WKWebView (e2e/ui_snapshots.py). A real Finder drag is on the remaining checklist. |
| 4 | `confirm()`/`prompt()` | **Unusable**: `confirm()` returns `false`, `prompt()` returns `null`, `alert()` does nothing, no dialog appears. Both panels use a custom modal. |
| 5 | `SSH_AUTH_SOCK` in the script process | **Present**: iTerm2 inherits launchd's `SSH_AUTH_SOCK`, identical to `launchctl getenv SSH_AUTH_SOCK`. `run.sh` still falls back to `launchctl getenv`. Note that iTerm2's `PATH` is only `/usr/bin:/bin:/usr/sbin:/sbin`, so the code calls `/usr/bin/ssh` and `/usr/bin/security` by absolute path. |
| 6 | `asyncssh.connect` parameters (2.24) | `known_hosts=None` **disables** host key checking, while the default reads `~/.ssh/known_hosts`. `client_keys` is used after the agent's keys. `tunnel` accepts a `[user@]host[:port]` string or a comma-separated string. `agent_path` defaults to `SSH_AUTH_SOCK`; `None` disables the agent. **Also discovered**: asyncssh reads `~/.ssh/config` itself and matches by HostName, not by alias, so the code passes `config=[]` after resolving with `ssh -G`. For ProxyJump, the code does not use the `tunnel` string; it resolves each hop with `ssh -G` (so the jump host's key/known_hosts behave exactly like ssh) and chains them with `tunnel=<conn>`. Integration tests include a password-only server, a real ProxyJump and an unknown host key. |
| 7 | Toolbelt on the left | **Not possible.** There is no setting, defaults key or API. The help string in the binary says: *"The toolbelt appears on the right side of your terminal window."* Per the user's decision, the SSH channels panel is a separate window docked on the left. |

Additional findings from real runs:
- **iTerm2's "full environment" scripts cannot use a self-built venv.** iTerm2 3.7 requires `iterm2env-metadata.json` and a runtime it downloads itself. Without them it shows an "Upgrade Python Runtime?" dialog and does not run the script. Therefore install.sh uses an AutoLaunch `.scpt` plus its own venv, and this flow has been verified: API cookie, `SSH_AUTH_SOCK`, tool registration.
- **Modal warning "A session ended very soon after starting"**: when a session using a custom command ends within a few seconds, iTerm2 shows this warning (advanced setting `shortLivedSessionDuration`). While it is open, the **entire Python API hangs**, including new connections. Mitigation: the wrapper keeps the tab alive for at least 5 seconds, every RPC to iTerm2 has a 4-second timeout, and the panel reports "iTerm2 is not responding (is a dialog open?)" instead of hanging.
- On Apple Silicon, a universal2 Python launched from an x86 process (e.g. an old Intel Homebrew `timeout`) runs under Rosetta and cannot load arm64 wheels. `install.sh`/`run.sh` therefore always run through `arch -arm64`.
- File names in multipart `filename=` are escaped inconsistently: WebKit only turns `"` into `%22`, while the aiohttp client percent-encodes everything. The UI also sends a `name` field with the original UTF-8 name.
- `sftp.get(..., recurse=True)` copies symlinks as symlinks by default, so `follow_symlinks=True` is used to download the folder a symlink points to.

## Tests

```bash
./install.sh --dev --no-start          # or create a venv yourself from requirements-dev.txt
~/Library/Application\ Support/iterm-xshell/venv/bin/python -m pytest -q
```

- **Unit** (`tests/test_*.py`): ssh command parsing (`-p`, `-p2222`, `-l`, `-o Port=`, `-o User=`, `-J`, `ssh://`, options with arguments, remote commands, non-ssh commands); `~/.ssh/config` import (several hosts per line, wildcards, nested/glob/relative/circular `Include`, comments, `=`, mixed case); the store (CRUD, atomic writes, file permissions, sync without losing `manual` channels); ssh command building (escaping, `remote_dir`); the tab wrapper; dock geometry.
- **Integration** (`tests/integration/`): a real asyncssh SFTP server running inside the tests, with 3 servers (key, password-only, jump), called through the aiohttp test client. Cases: list/upload/download/mkdir/rename/delete, a 300 MB file (sha256), names with spaces + Vietnamese characters + quotes, symlinks to folders and broken symlinks, path traversal, folder upload, cancelling an upload midway, automatic reconnect after a lost connection, password-only servers returning `code: "auth"`, unknown host keys returning `code: "hostkey"`, ProxyJump, the SSH channels API, mtime-based config sync, background download jobs.
- **UI** (`e2e/ui_snapshots.py`): loads both panels into an offscreen WKWebView (light/dark, 280px wide) against a real backend, simulates click/right-click/forms/drag-and-drop, and saves screenshots to `e2e/screenshots/`.
- **E2E on iTerm2** (`e2e/run_e2e.py`): drives a real iTerm2 through the Python API plus the installed daemon's HTTP API, using a `linuxserver/openssh-server` container (one container has Shell Integration).

## Known limitations

- The Toolbelt is always on the right. The SSH channels panel is a separate window docked on the left, so:
  - with a native full-screen iTerm2 window, the panel overlaps the left edge of the terminal content (Spaces cannot be split);
  - the panel follows by polling the position every 150 ms, so it lags a beat when you drag the iTerm2 window;
  - iTerm2's hotkey (floating) window is not docked.
- A passphrase-protected key that is not loaded into ssh-agent (`ssh-add --apple-use-keychain`) cannot be used by the Xftp panel (asyncssh skips encrypted keys). The terminal still ssh's normally.
- `ProxyCommand` is passed to asyncssh (with `%h %p %r %n` substituted). `Match exec` and other OpenSSH-only features are still resolved by `ssh -G`, but asyncssh may not support every rare algorithm/cipher.
- The Xftp panel follows the terminal's folder only when the server has Shell Integration.
- "Auto-type password" only recognizes `password:`-style prompts (not OTP/2FA).
- On every iTerm2 start, the AutoLaunch script requests the cookie through AppleScript. The first time, macOS may ask for Automation permission.

## License

MIT © 2026 DuongTx — see [LICENSE](LICENSE).
