# Connecting this connector to Claude Desktop

This documents two `mcpServers` entries: one against the real local WooCommerce
store (`woo-real`), one against the mock server (`woo-mock`). Values below are
placeholders — copy the JSON, then substitute your own Read key/secret.

## Find the config file Claude Desktop actually reads

Claude Desktop ships in more than one packaging on Windows, and they read
different files. Check **Settings -> Developer** inside the app for the exact
path it is using before you edit anything — don't assume the path below.

- Classic installs: `%APPDATA%\Claude\claude_desktop_config.json`
- MSIX/Windows Store packaged installs: a per-package virtualized path such as
  `%LOCALAPPDATA%\Packages\<PackageId>\LocalCache\Roaming\Claude\claude_desktop_config.json`

Editing the wrong file looks like nothing happening at all after a restart —
not even previously-working entries reload — because the app never reads that
file. If a quit-and-reopen doesn't pick up your change, re-check the
Developer tab for the real path.

## Config entries

Merge these into the existing `mcpServers` object — don't replace the whole
file, and don't remove entries that are already there.

```json
{
  "mcpServers": {
    "woo-real": {
      "command": "/path/to/python",
      "args": ["-m", "wc_connector.server"],
      "cwd": "/path/to/this/repo",
      "env": {
        "PYTHONPATH": "/path/to/this/repo/src",
        "WC_BASE_URL": "https://localhost:8443",
        "WC_CONSUMER_KEY": "<YOUR_READ_KEY>",
        "WC_CONSUMER_SECRET": "<YOUR_READ_SECRET>",
        "WC_CA_BUNDLE": "/path/to/this/repo/certs/caddy-root.crt",
        "WC_ALLOW_INSECURE_LOCALHOST": "false"
      }
    },
    "woo-mock": {
      "command": "/path/to/python",
      "args": ["-m", "wc_connector.server"],
      "cwd": "/path/to/this/repo",
      "env": {
        "PYTHONPATH": "/path/to/this/repo/src",
        "WC_BASE_URL": "http://localhost:8080",
        "WC_CONSUMER_KEY": "ck_mock_readonly",
        "WC_CONSUMER_SECRET": "cs_mock_readonly",
        "WC_CA_BUNDLE": "",
        "WC_ALLOW_INSECURE_LOCALHOST": "true"
      }
    }
  }
}
```

Notes:

- `WC_CA_BUNDLE` must be an **absolute** path, not relative — `cwd` isn't
  guaranteed to be what you expect once the host spawns the process.
- Set every `WC_*` key explicitly in both entries, even ones that would
  otherwise just take their default (e.g. `WC_CA_BUNDLE: ""` for `woo-mock`).
  Some hosts reuse a cached base environment across the Python processes they
  spawn for different MCP servers; a key left unset in one entry can silently
  pick up a stale value left behind by a different entry's last-launched
  process instead of getting a clean default. Setting every key explicitly
  closes that gap regardless of the host's spawn/env-caching behavior.
- `ck_mock_readonly` / `cs_mock_readonly` are the mock server's hardcoded
  public dev defaults (see `mock/app.py`) — not secrets, safe to commit.
- Both entries expose the same tool names. Only enable one of `woo-real` /
  `woo-mock` at a time to avoid ambiguity about which store a tool call hits.
- Fully quit the host from the system tray (not just closing the window) and
  reopen it after editing its config — most hosts cache MCP connections on
  startup.

See `scripts/write_claude_desktop_config.py` for a helper that writes these
entries for you by reading your real `.env` file, without ever printing the
secret values.
