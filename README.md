# ctfdash

`ctfdash` is a reusable terminal dashboard for CTFd contests and parallel solver runs. It synchronizes the challenge list and the first three competitor solves from CTFd. External runners report task state and structured logs through a small CLI event protocol.

## Install from Git

```bash
git clone <private-or-local-repository-url> ctfdash
cd ctfdash
./install.sh
```

The installer creates a repository-local virtual environment and links `ctfdash` into `~/.local/bin`. Update with `git pull`; rerun the installer only after moving the repository or changing package metadata.

## Create a contest project

Run the interactive wizard. Contest settings are deliberately asked as questions rather than command-line flags.

```bash
mkdir whitehat-2026
cd whitehat-2026
ctfdash init
```

The wizard writes `ctfdash.toml` and creates `.ctfdash/state.sqlite3`. API token values are never written to configuration. Export the environment variable selected by the wizard, then synchronize:

```bash
export CTFD_TOKEN='...'
ctfdash sync
ctfdash dashboard
```

For a Cloudflare browser session, create the configured session file with mode `0600`:

```json
{
  "user_agent": "Mozilla/5.0 ...",
  "cookies": [
    {"name": "session", "value": "...", "domain": "play.example.com", "path": "/"},
    {"name": "cf_clearance", "value": "...", "domain": ".example.com", "path": "/"}
  ]
}
```

## Runner events

Challenges must be synchronized before events are accepted. Columns are defined by the setup wizard.

```bash
ctfdash emit task.started --challenge 25 --column D2
ctfdash emit task.session --challenge 25 --column D2 --index 2
ctfdash emit task.succeeded --challenge 25 --column D2 --candidate 'flag{example}'
ctfdash emit task.failed --challenge 26 --column D2 --reason timeout_30m

printf '%s\n' 'solver explanation' | \
  ctfdash emit log.codex --challenge 25 --column D2 --stdin

printf '%s\n' 'python solve.py' | \
  ctfdash emit log.command --challenge 25 --column D2 --stdin
```

Each event accepts `--event-id` for idempotent retry and `--at` with an offset-aware ISO 8601 timestamp. `ctfdash ingest --stdin` accepts the same version-1 events as NDJSON.

The dashboard reads SQLite in WAL mode directly, so the dashboard and runners do not need a shared daemon. Click a condition cell to open that task's structured transcript. Use `1` for focus mode, `2` for Codex-only mode, `3` for raw JSONL, and `q` or `Ctrl+C` to return.

