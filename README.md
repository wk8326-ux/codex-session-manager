# Local Project Console

Local Project Console is a lightweight Windows dashboard for starting local projects, opening remote tools, and monitoring selected local Codex sessions.

Run `start-console.bat` or `python app.py`, then open `http://127.0.0.1:8765`.

Register each project with its working directory, start command, optional stop command, port, and local URL. The dashboard checks whether the configured port is listening and whether a process started by the dashboard is still alive.

Projects that already run on a server can use the `External website` mode. They
need only an HTTP or HTTPS URL and appear as an `Open` action. The console checks
the URL on a short interval and reports whether the remote website is online; it
does not pretend to start or stop the remote service.

The project definitions are saved in `projects.json` after the first launch. Command output is appended to `logs/<project-id>.log`.

## Session watchdog

Open `http://127.0.0.1:8765/watchdog`, or use the Session watchdog link in the sidebar. This workspace is isolated from the project launcher and has three views:

- Monitored sessions: explicitly selected local Codex sessions, their state, interval, channel, and manual check actions.
- Run history: the reason each check stayed silent, resumed, failed, or needs attention.
- Monitoring channels: OpenAI-compatible API channels used for availability probes.

The scheduler starts and stops with the console process. It monitors only sessions the user adds and enables; opening the local-session picker does not automatically add every Codex session.

### Prerequisites and compatibility

The watchdog currently requires Windows because API keys are protected with Windows DPAPI in the current-user scope. It also requires a Codex CLI version that supports the App Server stdio protocol used by `thread/list`, `thread/read`, `thread/resume`, and `turn/start`.

Check the installed CLI and the read-only protocol path before enabling resume actions:

```powershell
codex --version
python scripts/probe_codex_app_server.py --list --limit 5
```

There is no hard-coded Codex CLI version. If the read-only probe fails after an upgrade, leave resume actions disabled until protocol compatibility is restored.

### Safe defaults

- Monitoring is enabled, but `resumeActionsEnabled` is `false` by default.
- The global check interval is 15 minutes; a session can override it, with a five-minute minimum.
- The built-in prompt means "continue the current development task" and can be customized per session.
- Running, completed, waiting-for-user, unknown, unmatched, and channel-unavailable states stay silent.
- A recoverable incident is deduplicated. Definite send failures stop after three total attempts; uncertain outcomes require manual confirmation and are not blindly retried.
- Execution records default to 90 days and 10,000 rows, with daily cleanup while the scheduler runs.

Before turning on automatic resume, use a disposable Codex session titled exactly `watchdog-integration-test` and follow the gated send procedure in the operator guide. Do not use an active development session for the first send test.

### Configuration and diagnostics

The complete device-independent operator guide is in [docs/watchdog-configuration.md](docs/watchdog-configuration.md). It covers:

- channel and session fields;
- finding a local thread ID;
- check intervals and per-session prompts;
- strict recovery and retry rules;
- DPAPI backup implications;
- API examples, status categories, record retention, and troubleshooting.

Useful read-only checks:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/watchdog/status
Invoke-RestMethod http://127.0.0.1:8765/api/watchdog/settings
Invoke-RestMethod 'http://127.0.0.1:8765/api/watchdog/runs?limit=20'
```

All watchdog endpoints live under `/api/watchdog/`; the watchdog UI never uses `/api/projects`.

## Local data and security

Watchdog configuration and audit records are stored in `watchdog.db`. API keys are DPAPI-encrypted and are never returned by the API or written to logs in plaintext. A database copied to another device or Windows user normally cannot decrypt the saved keys; re-enter channel keys after migration.
The repository ignores `.superpowers/`, `watchdog.db`, `watchdog.db-shm`, and `watchdog.db-wal`. Never commit real API keys, local database files, personal paths, or production thread IDs.
