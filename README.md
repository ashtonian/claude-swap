# claude-swap

Multi-account switcher for Claude Code. Easily switch between multiple Claude accounts without logging out, or let it switch for you before you hit a rate limit. Track usage for every account in a live dashboard, and run accounts in parallel. Works with both the Claude Code CLI and the VS Code extension.

## Installation

### Using uv (recommended)

```bash
uv tool install claude-swap
```

### Using pipx

```bash
pipx install claude-swap
```

### From source

```bash
git clone https://github.com/realiti4/claude-swap.git
cd claude-swap
uv sync
uv run cswap help
```

### This fork

Session restarts on switch, off-by-default auto-switching and switch triggers (below) live in this fork:

```bash
uv tool install git+https://github.com/ashtonian/claude-swap
```

### Updating

```bash
cswap upgrade          # uv/pipx installs on macOS/Linux: auto-detects and upgrades
# or run your installer directly:
uv tool upgrade claude-swap
pipx upgrade claude-swap
```

## Usage

### Add your first account

Log into Claude Code with your first account, then:

```bash
cswap add
```

### Add more accounts

Log in with another account, then:

```bash
cswap add
```

Do not run `/logout` first: current Claude Code may revoke the refresh token stored for the account you are leaving.

### Switch accounts

Rotate to the next account:

```bash
cswap switch
```

Or switch to a specific account:

```bash
cswap switch 2
cswap switch user@example.com
cswap switch dev                # or by alias, once set with `cswap alias 2 dev`
```

Not sure which one? `cswap list` is the dashboard — every account's 5-hour and 7-day usage and reset times at a glance:

```bash
cswap list
```

Or let claude-swap auto-pick by remaining quota — `cswap switch --strategy best` (most quota left) or `--strategy next-available` (skip rate-limited accounts).

A switch closes your running Claude Code sessions, swaps the account, and reopens each one where it was, resumed — see [Restart sessions on switch](#restart-sessions-on-switch). `cswap switch --no-restart` (or `cswap config set restart.onSwitch false`) leaves them running instead: on Linux/Windows they pick the new account up on their next message, on macOS once the Keychain cache expires (about 30 seconds). See [Tips](#tips) for the per-platform details.

### Restart sessions on switch

Every switch — `cswap switch`, [`cswap auto`](#automatic-switching), the [menu bar](#menu-bar-macos) and the [TUI](#interactive-dashboard-tui) — restarts the Claude Code sessions running on this machine around it:

1. **Close.** Each session closes as soon as it is idle. One that is working (in a turn, running a background shell, or waiting on a question) gets `restart.busyTimeoutSeconds` (default 120) to finish; then it is closed mid-turn, and on resume Claude Code re-runs the turn it was cut off in. `--close-busy` skips the wait; `restart.onBusyTimeout abort` calls the switch off instead (the sessions already closed reopen on the old account).
2. **Switch.**
3. **Reopen.** Each session is reopened — in its own terminal where it can be, otherwise in a new window — with `claude --resume <id>`, the options it was started with (`--model`, `--name`, `--remote-control`, ...), the Claude Code settings in its environment (`ANTHROPIC_*`, `CLAUDE_*`, `ENABLE_*`/`DISABLE_*`, proxies), and in its directory. The full conversation comes back: Claude Code's offer to resume a long, old conversation from a summary is skipped (`restart.resume ask` keeps it). A turn the close — or a rate limit — cut off is re-run (`restart.continueInterrupted`). Sessions reopen even when the switch fails. Background agents (`claude --bg`) are not closed first: once the switch lands, Claude Code's own `claude respawn` restarts them.

```bash
cswap sessions                  # what a switch would close and reopen, and what it leaves running (and why)
cswap switch 2                  # close, switch, reopen
cswap switch 2 --close-busy     # don't wait for working sessions
cswap switch 2 --no-restart     # leave the sessions alone this time
cswap reopen                    # reopen sessions a restart closed and never saw come back
```

<details>
<summary>Which sessions, and how</summary>

- **Every interactive CLI session, in any terminal**, comes back, by the first of these that works (`cswap sessions` shows which, per session):
  1. **In place**, in a **tmux** pane, a **Muxy** terminal, an **iTerm2** session or a **Terminal.app** tab: the resume command is typed into the shell the session was started from, once that shell has the terminal back, with a leading space so shells that ignore space-led lines keep it out of history. It runs a one-shot launcher script that deletes itself. A session that was its tmux pane's own process (`tmux new-window claude`) gets the pane respawned.
  2. **A new window in the same app**: the same tmux session, a new Muxy terminal in the same project (listed under *Existing Terminals*), a new iTerm2 or Terminal.app window — when the tab closed with the session, or the shell did not take the terminal back.
  3. **A new window in `restart.fallback`** (default: Terminal.app on macOS, a tmux session named `cswap` elsewhere): sessions in a terminal cswap cannot type into — VS Code's or Cursor's integrated terminal, Ghostty, Warp, ... — and anything steps 1 and 2 could not take. `restart.fallback none` leaves those sessions running instead.
- **Background agents** (`claude --bg`) restart through `claude respawn <job id>` after the switch.
- **Left running, with the reason in `cswap sessions`:** sessions inside an app — the VS Code/Cursor extension panel, Claude Desktop, SDK scripts — which only that app can restart; sessions signed in with their own credentials (`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, Bedrock/Vertex/Foundry — a switch does not reach them). They pick the new account up from the credential store as before. `cswap run` session-mode sessions run on their own account, so a switch neither touches nor lists them.
- **A first prompt is not sent again.** `claude "fix the build"` reopens as `claude --resume <id>`: positional arguments are dropped, as are options that pick or start a conversation (`--continue`, `--resume`, `--session-id`, `--fork-session`, `--worktree`, ...). The option grammar comes from that `claude`'s own `--help`.
- **Run from inside a session** (Claude Code's Bash tool, or `!cswap switch`), the switch would close the session running it. It hands over to a detached worker instead and returns at once; the session finishes its turn, then is closed and reopened with the rest. The worker logs to `relaunch/worker.log` in the backup directory and posts a macOS notification when done.
- **macOS permissions:** typing into iTerm2 and Terminal.app uses Apple Events, so the first restart asks (System Settings → Privacy & Security → Automation) to let the app cswap runs from — your terminal, or the menu bar's Python — control iTerm2 or Terminal. Until that is allowed, those sessions reopen in a new window instead (new windows need no permission for Terminal.app).

</details>

<details>
<summary>Troubleshooting</summary>

- **"N session(s) still working ... the switch was called off"** — `restart.onBusyTimeout` is `abort`. Wait for the turns to end, re-run with `--close-busy`, or set it back to `close`.
- **A session did not come back** — the switch reports it (`reopened 23 of 24`). Its terminal shows the typed ` sh .../relaunch/<id>.command` line and whatever went wrong there. Run `cswap reopen`, or `claude --resume <id>` in that terminal. `relaunch/plan.json` in the backup directory lists every session of the last restart with its state (`closed`, `reopened`, `failed`).
- **"did not return to its shell"** — after the session exited, something else took the terminal (a wrapper that relaunches claude in a loop): cswap does not type into it. Start the session from an interactive shell.
- **A session was left running** — `cswap sessions` says why.
- **Sessions in iTerm2 or Terminal.app came back in new windows** — macOS has not allowed Automation of that app yet (see above), or the planning call to it timed out; the switch log says which.
- **A session came back in a `cswap` tmux session** — that is the `restart.fallback` on Linux: `tmux attach -t cswap`.
- **"Another switch is restarting sessions"** — one restart at a time; the other one is still waiting on working sessions.

</details>

### Automatic switching

Let claude-swap watch your usage and switch for you. When the active account's 5-hour or 7-day window reaches the threshold (default 90%), it switches to the account with the most quota left — before you hit the limit, and safe to run while Claude Code is working. Each switch [restarts the sessions](#restart-sessions-on-switch) like a manual one.

Automatic switching is **off until you turn it on** — `cswap auto`, the menu bar's and the TUI's auto-switch refuse to switch while it is off; `--dry-run` always works:

```bash
cswap config set autoswitch.enabled true
cswap config set autoswitch.triggers weekly   # optional: switch only on the weekly limits
```

`autoswitch.triggers` (or `cswap auto --triggers`) names what may set off a switch: `5h`, `7d`, `model` (the per-model weekly windows named by `autoswitch.model`), `weekly` (= `7d,model`), `failover` (the active account's usage stays unreadable), `consume-first` (that strategy's below-the-line move), or `all` (the default). A window outside it may sit over its line — even exhausted — and the engine waits for its reset, logging `no switch: trigger-off (5h 95% >= 90%; autoswitch.triggers is weekly)`.

```bash
cswap auto                     # foreground loop, polls every 60s
cswap auto --triggers weekly   # switch on the 7-day and per-model limits only
cswap auto --threshold 80      # switch earlier
cswap auto --weekly-threshold 97   # let the weekly window run higher before switching
cswap auto --model Fable       # also switch when the Fable weekly limit is hit
cswap auto --once              # single check-and-switch, for cron/scripts
cswap auto --dry-run           # log what it would do, never switch
cswap auto --strategy consume-first   # burn the soonest-resetting account first
```

<details>
<summary>How it behaves & advanced usage</summary>

- Runs safely alongside Claude Code: switches take the same credential locks Claude Code uses, so a swap never collides with a token refresh.
- A cooldown (default 5 min) and a hysteresis margin stop it flip-flopping near the threshold: a proactive switch only lands on an account that's below the threshold *and* better than the current one by the margin — a candidate that clears the margin is always taken, but two accounts hovering at the line never ping-pong. When every account is exhausted it keeps checking on a bounded slow cadence, waking sooner for an imminent reset.
- **Strategies** (`--strategy`, or `cswap config set autoswitch.strategy`): `best` (default) stays put until the active account nears its limit, then moves to the account with the most quota left. `consume-first` proactively keeps you on the account whose **weekly window resets soonest** — use-it-or-lose-it — switching to a sooner-resetting account (with room to spare) even below the threshold, so perishable weekly quota isn't wasted.
- **A separate line for the weekly windows** (`--weekly-threshold 97`, or `cswap config set autoswitch.weeklyThreshold 97`): one threshold compared against the busiest window forces the same number on windows that want different ones — the 5-hour window recycles in hours (an overshoot costs one interrupted turn), while the weekly window is the budget (quota unspent before its reset is gone). With a weekly line, the 7-day window and any per-model window (`--model`) are judged against it and the threshold keeps governing the 5-hour window; every window is compared against its own line, so 7d 93% under a 97 line holds while 5h 95% under an 85 line switches, and the log names the window it measured (`7d 93% < 97%`). Points on different windows are not the same amount of work — the lines are comparable, the clocks are not.
- Usage polling is adaptive — a couple of accounts per check, busy alternates watched more closely, and exhausted ones checked about every ten minutes (or slower after 429s) — so API traffic stays flat no matter how many accounts you manage.
- It fails safe: if a usage check errors it keeps trusting the last-known numbers while retries back off, and an expired token on an idle machine makes it hold rather than fail over (Claude Code refreshes the token on your next message).
- An account whose refresh token has died is quarantined and reported until you either log in with it and re-run `cswap add --slot N`, or replace its stored credentials from a known-good export — a plain `cswap import backup.cswap` replaces dead-token slots on its own (`--force` is still required to replace other existing accounts; note a stale export can carry an already-superseded token). API-key accounts are never rotated onto unless you pass `--include-api-key-accounts`.
- To hold an account out of rotation yourself — a work account you don't want touched, one you're resting — run `cswap disable <num|email>`; `cswap enable <num|email>` puts it back. Disabled accounts are skipped by auto-switch, bare `cswap switch`, and the `best` / `next-available` strategies, but stay fully managed and remain a valid explicit `cswap switch <num|email>` target. They show a `(disabled)` marker in `cswap list`, in the [TUI](#interactive-dashboard-tui), and in the [menu bar](#menu-bar-macos) — both of which also let you toggle the state in place (TUI: menu → *Disable / enable account…*; menu bar: *Disable / enable account*).
- **Per-account threshold.** `cswap threshold <num|email> <pct>` gives one account its own switch-away line (50–99.9) in place of the global one: auto-switch leaves that account when *its* line is reached and prefers candidates still under *theirs*, with ranking and the hysteresis margin counting room under each account's own line rather than raw headroom. Like the global threshold it is a switch-away line, not a hard cap — with nothing under its line, or the active account at its limit, it still lands somewhere rather than stall. With a weekly line set, the override applies to the 5-hour window only. Accounts without one keep the global threshold. `cswap threshold` alone lists the default and every override, `--unset` clears one, and the [TUI](#interactive-dashboard-tui) shows a `th 85%` badge.
- By default only the account-wide 5h/7d windows drive switching. If you work on one model and hit its **weekly per-model limit** first (e.g. Fable), add `--model Fable` (or `cswap config set autoswitch.model Fable`) to fold that model's window into the decision, so it switches off an account whose model quota is spent even while its 5h/7d windows still have room.
  - **Model names** are Anthropic's own per-model `display_name`s, matched case-insensitively. The exact strings for your accounts are the per-model rows in `cswap list` (e.g. a line reading `Fable: 100%`).

For cron/systemd timers, `--once` reports the outcome in its exit code (`0` switched, `1` error, `2` nothing to do, `3` blocked — no viable target), and `--json` emits one JSON event per line:

```bash
*/5 * * * * cswap auto --once --json >> ~/.cswap-auto.log 2>&1
```

Defaults like the threshold and cooldown are configurable with `cswap config set autoswitch.threshold 80` — flags override them (see [Configuration](#configuration)).

</details>

### Run multiple accounts at the same time (session mode)

Launch Claude Code as a specific account in the current terminal only — every other terminal and the VS Code extension stay on your default account, so two accounts can work in parallel.

```bash
cswap run 2                     # launch Claude Code as account 2, here only
cswap run user@example.com      # by email
cswap run 2 -- --resume         # everything after '--' is forwarded to claude
cswap run 2 --share-history     # share your chat history with this account too
cswap run 2 --require-session   # refuse rather than run plain claude if 2 is the default login
```

Sessions use your normal `~/.claude` setup (settings, CLAUDE.md, skills, MCP servers, etc.), but each account keeps its own chat history — pass `--share-history` if you want your accounts to continue the same conversations.

Running the account that is already your default login launches plain `claude` on that login instead of a session (a second copy of the active credential would go stale). Scripts that need the isolation guaranteed can pass `--require-session`, which refuses in that case instead.
  
A session refreshes its own copy of the account's token, so once it exits, the credential it rotated is captured back into the account's stored backup before a switch or usage check uses that backup. While a session is still running, `cswap switch` refuses to move the default login onto its account if the stored backup has already fallen behind (activating it could only fail); exit the session first, or pick another account. While a session runs, its account's usage is read with the session's own credential and never refreshed by cswap; a read the server refuses shows as token expired, and is not requested again, until the session renews the credential on its next call.

<details>
<summary>Sharing details — MCP servers & chat history</summary>

- With `--share-history`, a session started under one account shows up in `--resume` under the others, and nothing already saved is lost.
- User-scope MCP servers (`claude mcp add -s user`) are mirrored from your default profile on every launch — manage them there; changes made inside a session don't persist. Definitions are copied as-is (including inline `env`/`headers` values), but MCP OAuth logins are not — HTTP servers may ask you to authenticate once per profile via `/mcp`.
- `--no-share` turns sharing off and removes the mirrored MCP config (profiles that never mirrored are left alone).

</details>

<details>
<summary>Map accounts to directories — auto-pick per repo</summary>

Bind a directory to an account, and a bare `cswap run` there launches that account in session mode — e.g. work account in work repos, personal elsewhere:

```bash
cswap map 2 ~/work/client-app   # map a directory to account 2
cswap map user@example.com      # map the current directory
cswap map                       # list mappings
cswap unmap ~/work/client-app   # remove one (defaults to current directory)

cd ~/work/client-app/src
cswap run                       # → account 2, session mode
```

Subfolders inherit the nearest mapped ancestor. In an unmapped directory, `cswap run` just launches plain `claude` with your default login. Mappings are per-machine (not part of `cswap export`) and are cleaned up when their account is removed.

</details>

### Interactive dashboard (TUI)

Run `cswap` on its own (or `cswap tui`) for the full-screen dashboard: live usage for every account, switching, and the auto-switcher, all keyboard-driven. `cswap watch` opens it straight to the live monitor. Works on macOS, Linux, and Windows.

<img src="assets/tui-watch.png" width="760" alt="cswap watch — live 5h/7d usage bars for every account, with reset times and the active account marked">

### Refresh expired tokens

If an account's token expires, log back into Claude Code with that account and re-run:

```bash
cswap add
```

This will update the stored credentials without creating a duplicate.

### Other commands

```bash
cswap run 2                     # Run an account in this terminal only (session mode)
cswap auto                      # Auto-switch when nearing rate limits (see above)
cswap sessions                  # Sessions a switch restarts, and those it leaves running
cswap reopen                    # Reopen sessions a restarting switch left closed
cswap config                    # Show or edit settings (see Configuration below)
cswap list                      # Show all accounts with 5h/7d usage and reset times
cswap list --token-status       # Add source-labelled OAuth token diagnostics
cswap status                    # Show current account
cswap add --slot 3              # Add account to a specific slot (prompts before overwrite)
cswap add --alias dev           # Add account and give it a short alias
cswap remove 2                  # Remove an account
cswap disable 2                 # Hold an account out of auto-rotation (keeps its login)
cswap enable 2                  # Return a disabled account to rotation
cswap threshold 2 85            # Switch away from account 2 at 85%, not the global default
cswap threshold 2 --unset       # Return account 2 to the global default
cswap threshold                 # Show the global default and every per-account override
cswap alias 2 dev               # Give an account a short alias (usable anywhere NUM|EMAIL is)
cswap alias 2 --unset           # Remove an account's alias
cswap alias                     # List all aliases
cswap move 2 1                  # Assign an account to a slot (relocates to an empty slot, swaps if taken)
cswap unclaimed                 # List stashed credential entries (slot + why they were stashed)
cswap unclaimed --purge ID      # Drop one (deletes its bytes; recover with /login + `cswap add`)
cswap tui                       # Interactive dashboard (also: bare `cswap`)
cswap watch                     # Dashboard, opened on the live watch page
cswap upgrade                   # Upgrade claude-swap to the latest version
cswap purge                     # Remove all claude-swap data
```

The original flag spellings (`cswap --switch`, `cswap --list`, ...) keep working.

## Tips

- **Do you need to restart after switching?** cswap does it for the sessions it can reopen ([Restart sessions on switch](#restart-sessions-on-switch)). For the rest, usually not: on **Linux and Windows**, credentials are stored in a file and Claude Code re-reads them whenever that file changes, so the new account takes effect on your next message. On **macOS**, credentials live in the Keychain, which Claude Code caches for about 30 seconds; a running session picks up the switch once that cache expires. Restart Claude Code (or close and reopen the VS Code extension tab) only if you want the change to apply instantly.
- **Continuing sessions after switching:** a restarted session is the same conversation, resumed. Either way, the first message on the new account may use extra usage as its conversation cache rebuilds.

## How it works

- Backs up OAuth tokens and config when you add an account
- Swaps only the account-specific Claude login when you switch accounts;
  live account-independent OAuth state (such as MCP server logins) is
  preserved instead of being overwritten by a slot's older snapshot
- Account credentials stored securely using platform-appropriate methods
- Switches (manual and automatic) hold Claude Code's own credential locks while writing, so a swap never interleaves with a token refresh
- Auto-switch freshens a target's token before activating it, and quarantines accounts whose refresh token has died (recover by re-adding it with `cswap add --slot N`, or by replacing its stored credentials from a known-good export — a plain `cswap import backup.cswap` replaces dead-token slots automatically)
- Usage numbers refresh every few minutes — faster for an account being used or close to switching, slower for idle ones — keeping cswap comfortably inside Anthropic's rate limits however many dashboards you keep open on a machine. An age note like `· 6m ago` just means the next scheduled check hasn't come yet, not that something is stuck.

## Data locations

| Platform | Credentials | Config backups |
|----------|-------------|----------------|
| Windows | File-based (inside the backup directory, under `credentials/`) | `~/.claude-swap-backup/` |
| macOS | macOS Keychain | `~/.claude-swap-backup/` |
| Linux / WSL | File-based (inside the backup directory, under `credentials/`) | `${XDG_DATA_HOME:-~/.local/share}/claude-swap/` |

Session-mode profiles (`cswap run`) live under the backup directory in `sessions/`. Tool preferences (`settings.json`) and auto-switch state (`autoswitch_state.json` — cooldown and quarantined accounts; delete it to reset) live in the backup directory root. Session restarts keep their last plan (`plan.json` — each session's command line, carried settings and state; mode 0600), the detached worker's log (`worker.log`) and, briefly, one-shot launcher scripts (`<session id>.command`, mode 0700) in `relaunch/`.

On Linux/WSL, set `XDG_DATA_HOME` to override the default location.

## Menu bar (macOS)

<details>
<summary>Optional macOS menu bar app — usage at a glance, click to switch</summary>

Needs the `menubar` extra (macOS only):

```bash
uv tool install 'claude-swap[menubar]'   # or: pipx install 'claude-swap[menubar]'
cswap menubar
```

Shows every account's 5h / 7d / spend usage and switches with a click (specific / rotate / best / next-available), plus the TUI's add / disable-enable / remove / refresh actions. Enable *Settings → Auto-switch accounts* to run the same engine as [`cswap auto`](#automatic-switching) in the background; it shares the `autoswitch.*` settings, so the menu bar and CLI stay in sync. Off until you turn it on — and it needs `autoswitch.enabled` on too. Switches from the menu run in the background and [restart the sessions](#restart-sessions-on-switch); the notification says how many came back.

**Keep it running without a terminal.** `cswap menubar` runs in the foreground, so the status item dies with the terminal that started it and does not come back after a reboot. `--install-service` hands it to launchd instead — starts at login, restarts on crash, no `.app` bundle:

```bash
cswap menubar --install-service     # start now, and at every login
cswap menubar --service-status      # installed? loaded? pid?
cswap menubar --uninstall-service   # stop it and remove the plist
```

The agent lives at `~/Library/LaunchAgents/com.cswap.menubar.plist` and logs to `~/Library/Logs/com.cswap.menubar.{log,err}`. It pins the `cswap` console script, whose path survives an upgrade — but the running process keeps the old build until it restarts, so after `cswap upgrade` either re-run `--install-service` or `launchctl kickstart -k gui/$(id -u)/com.cswap.menubar`.

</details>

## Advanced

### Configuration

Tool preferences live in `settings.json` in the backup root; `cswap config` reads and edits it with validation, so you never have to find the file or guess valid ranges.

<details>
<summary>Commands & usage</summary>

```bash
cswap config                              # list effective settings ("(default)" = not set)
cswap config get autoswitch.threshold
cswap config set autoswitch.threshold 80  # validated: rejects out-of-range values loudly
cswap config set autoswitch.weeklyThreshold 97  # own switch line for the 7-day/per-model windows
cswap config set autoswitch.model Fable   # per-model switching (see "auto"); Fable,Opus for several
cswap config unset autoswitch.threshold   # back to the default
cswap config path                         # where settings.json lives
```

`cswap config --help` lists every key with its valid range and default. Hand-editing the file still works — `cswap config` is just a safer front door. `list` and `get` take `--json` for scripting.

</details>

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `autoswitch.enabled` | bool | `false` | Allow automatic switching (`cswap auto`, menu bar, TUI). Off: a live engine refuses to start; `--dry-run` always works. |
| `autoswitch.triggers` | list | all | What may set off an automatic switch: `5h`, `7d`, `model`, `weekly` (`7d,model`), `failover`, `consume-first`, `all`. |
| `autoswitch.threshold` | float 50–99.9 | `90` | Switch line for the 5h window, and for every window while no weekly line is set. |
| `autoswitch.weeklyThreshold` | float 50–99.9 | unset | Switch line for the 7d and per-model windows. |
| `autoswitch.intervalSeconds` | float 15–3600 | `60` | Poll interval of the `cswap auto` loop. |
| `autoswitch.cooldownSeconds` | float 0–86400 | `300` | Minimum time between proactive switches. |
| `autoswitch.hysteresisPct` | float 0–50 | `10` | How much more headroom a target needs than the active account. |
| `autoswitch.strategy` | `best` \| `consume-first` | `best` | How the target is picked. |
| `autoswitch.includeApiKeyAccounts` | bool | `false` | Allow rotating onto API-key accounts (billed per token). |
| `autoswitch.unhealthyTicks` | int 1–100 | `3` | Failed polls of the active account before it fails over. |
| `autoswitch.model` | string | unset | Per-model weekly windows that also count (`Fable`, `Fable,Opus`, `all`). |
| `restart.onSwitch` | bool | `true` | Close the running sessions around every switch and reopen them resumed. |
| `restart.resume` | `full` \| `ask` | `full` | `full` resumes the whole conversation; `ask` keeps Claude Code's offer to resume from a summary. |
| `restart.busyTimeoutSeconds` | float 0–3600 | `120` | How long working sessions get to finish their turn. |
| `restart.onBusyTimeout` | `close` \| `abort` | `close` | Then close the ones still working, or call the switch off. |
| `restart.continueInterrupted` | bool | `true` | Reopened sessions re-run a turn the close or a rate limit cut off. |
| `restart.closeTimeoutSeconds` | float 1–300 | `15` | How long a session gets to exit after SIGTERM before SIGKILL. |
| `restart.reopenTimeoutSeconds` | float 5–600 | `60` | How long a reopened session gets to come back before it is reported. |
| `restart.fallback` | `auto` \| `terminal` \| `iterm` \| `tmux` \| `none` | `auto` | Where a session reopens when its own terminal cannot take it back: a new Terminal.app or iTerm2 window, a tmux session named `cswap`; `auto` = Terminal.app on macOS, tmux elsewhere; `none` leaves such sessions running. |
| `ui.theme` | `dark` \| `light` \| `auto` | `auto` | Color theme; `auto` follows the terminal background. |

### Backup and migration

Move account data between machines or back it up:

```bash
cswap export backup.cswap                    # All accounts to a file
cswap export backup.cswap --account 2        # One account
cswap export backup.cswap --full             # Include full ~/.claude.json and credential object (same-PC backup)
cswap import backup.cswap                    # Skips accounts that already exist
cswap import backup.cswap --force            # Overwrite existing
```

The export file is plaintext JSON and, by default, carries only each account's own login — machine-shared MCP/plugin OAuth tokens and the device token stay on the source machine (`--full` keeps everything, for same-PC backups). If you need encryption, pipe through your tool of choice (e.g. `cswap export - | gpg -c > backup.gpg`).

If an imported account is the one you're currently logged in as, activate the imported credentials with `cswap switch N --force` (a plain `switch` to the current account is a safe no-op and won't touch the import).

### Share usage readings between machines

Machines that hold the same accounts can end up spending one usage-endpoint budget: when they share a login (moved between them with `export`/`import`, so the same token is live on each), or when the account's usage requests are limited per account rather than per token. Their polling then adds up. `import-usage` lets one machine poll and hand its readings to the others:

```bash
cswap list --json | ssh laptop cswap import-usage - --hold 600
```

<details>
<summary>How it works — matching, holds & when a hold ends</summary>

The input is `cswap list --json` output. Each row with `usageStatus: "ok"` is matched to a local account by email and organization, and adopted when it is newer than the reading already stored. Its age comes from `usageAgeSeconds`, so the two machines' clocks never have to agree; a script that delays the hand-over should add the delay to that field. `--hold SECONDS` keeps every collector on the receiving machine (`list`, `status`, `auto`, the dashboard, the menu bar) from fetching those accounts for that long, and the held reading stays trusted for switch decisions meanwhile. A hold never runs past the reading's earliest window reset (per-model windows included), nor past an hour after the reading was taken. Renew it with each hand-over, or lift it early with `--hold 0`; when it lapses, the machine goes back to fetching for itself.

</details>

### JSON output for scripting

Add `--json` to `list`, `status`, or `switch` to emit a single machine-readable JSON object on stdout (human-readable notices go to stderr). Useful for scripting auto-swap and quota tracking.

```bash
cswap list --json                   # all accounts with usage/quota
cswap status --json                 # current active account
cswap switch --strategy best --json # switch, then report the result
cswap switch 2 --json
```

<details>
<summary>Example output & schema notes</summary>

```json
{
  "schemaVersion": 1,
  "activeAccountNumber": 2,
  "accounts": [
    { "number": 2, "email": "you@example.com", "active": true, "usageStatus": "ok",
      "usage": { "fiveHour": { "pct": 25.0, "resetsAt": "2026-06-22T23:29:59Z" },
                 "sevenDay": { "pct": 16.0, "resetsAt": "2026-06-26T17:59:59Z" } } }
  ]
}
```

Every payload carries a `schemaVersion` (currently `1`); on a handled error stdout is `{"schemaVersion":1,"error":{...}}` with a non-zero exit code. `--switch`/`--switch-to` report `{"switched": true|false, "from": …, "to": …, "reason": …}`.

Usage is served from a per-account cache: when the usage API is briefly unreachable, the last-known numbers are shown instead of nothing (the human view marks them with their age, e.g. `· 2m ago`). Rows with decision-trusted usage carry additive `usageFetchedAt`/`usageAgeSeconds` fields telling you how old the measurement is. Whenever `usage` is null but a last-known measurement exists — data too old to drive a decision (`usageStatus` stays `unavailable`), or a row in a non-`ok` state such as `token_expired` — additive `lastGoodUsage`/`lastGoodFetchedAt`/`lastGoodAgeSeconds` fields preserve the human display without making the account actionable. When `usage` is null and nothing else explains it (`usageStatus` is `unavailable`), an additive `usageError` names the last fetch failure by kind (e.g. `http-429`, `timeout`) and, while the cache is backing off from it, `usageRetryAt` gives the time of the next attempt. These fields apply to list rows and the managed active row from `status --json`. An account held out of rotation with `cswap disable` carries an additive `"disabled": true` on its row (absent otherwise). Per-account policy follows the same convention: a row carries `"threshold": 85.0` once one is set with `cswap threshold` — absent otherwise, so a fleet that never sets a policy emits rows identical to before.

A row carries an additive `loginExpiresAt` (ISO-8601 UTC) when the stored login records when its refresh token expires, which is the moment the slot will need a fresh `/login` and `cswap add --slot N`; a script can warn a few days ahead instead of discovering `relogin_required`. Absent when Claude Code recorded no such date for that login.

An account row also carries an additive `alias` field once one is set with `cswap alias` (e.g. `"alias": "dev"`); accounts without one simply omit the key.

Weekly windows (`sevenDay` and per-model `scoped` entries — never `fiveHour`) additively carry pace fields once the week is ~a day old: `expectedPct` (where usage would sit if spread evenly across the week) and `aheadOfPace` (`true` when meaningfully above that — the same signal the human views show as an `(ahead)`/`(ahead of pace)` marker). `projectedExhaustionAt`/`willLastToReset` extrapolate the current rate into an ETA to 100% and a yes/no "will it last to the reset"; they stay `--json`-only since a linear projection is too rough to present as fact in the UI.

</details>

`cswap auto --json` emits an event *stream* instead — one JSON object per line (`{"schemaVersion":1,"event":"switch","ts":…, …}` with kinds like `poll`, `switch`, `no-switch`, `account-quarantined`, `all-exhausted`, `error`). The contract is additive: new kinds and fields may appear, so scripts should ignore unknown ones.

### Add an account from a raw token or API key

If you only have a long-lived setup-token (e.g., produced by `claude setup-token`)
or a Console API key (`sk-ant-api03-...` workspace key, `sk-ant-usr-...` personal
key, or a service-account key) and you don't want to log in via the browser
flow first — useful on headless servers or when receiving a token from another
machine — register it directly. The token type is auto-detected:

```bash
cswap add-token sk-ant-oat01-...             # OAuth setup-token
cswap add-token sk-ant-api03-...             # API key (any Console key family)
cswap add-token sk-ant-oat01-... --slot 3
cswap add-token - --slot 3                   # read token from stdin
cswap add-token --email user@example.com     # optional label override
```

`--email` is optional; omitted values use `setup-token-{slot}@token.local`
(or `api-key-{slot}@token.local` for API keys). No Anthropic API calls are made.

**API-key accounts.** Any bare `sk-ant-...` value that is not an OAuth token
(`sk-ant-oat...`) registers a managed API-key account (the kind Claude Code uses
after `/login` with a key) rather than an OAuth setup-token. It switches like any
other account; since API keys have no subscription quota, they show no usage and the
usage-aware `switch` strategies never skip them as rate-limited. Create personal and
service-account keys scoped to a single workspace: a multi-workspace key needs an
`anthropic-workspace-id` header on every request, which Claude Code does not add for
a stored key.

## Uninstall

Remove all data:

```bash
cswap purge
```

Then uninstall the tool:

```bash
uv tool uninstall claude-swap
# or
pipx uninstall claude-swap
```

## Requirements

- Python 3.12+
- Claude Code installed and logged in

## License

MIT
