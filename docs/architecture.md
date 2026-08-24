# Architecture and protocol

The Mac sends commands through iMessage. The iPhone returns data through HTTPS.

## Components

1. The `iphone` CLI validates arguments and builds either an Apple URL or a Shortcut command.
2. The `iphone-control` skill tells Codex which CLI command to use and how to interpret its result.
3. A per-user LaunchAgent accepts commands on a Unix socket and automates Messages in the macOS GUI session.
4. A Message automation on the iPhone runs the 95-action Shortcut for messages that match `hola`.
5. The Shortcut runs an iOS action. Branches that produce data post to `/photo`, `/clipboard`, or `/get-alarm`. The legacy `/text` endpoint remains available for older shortcuts.
6. Tailscale Serve terminates HTTPS on the Mac's private `*.ts.net` origin and proxies requests to the receiver on `127.0.0.1:8787`.
7. The CLI polls the local receiver for text data or watches the private inbox for a screenshot until it gets a response or reaches the timeout.

## Command format

The sender accepts one line beginning with `hola `:

```text
hola openurl <url>
hola homescreen
hola screenshot <id>
hola screentext <id>
hola getclipboard <id>
hola copytoclipboard <text>
hola alarm get <id>
hola alarm set <HH:MM> <label>
hola alarm off <HH:MM>
hola timer start <seconds>
hola timer pause
hola timer resume
hola timer cancel
hola flashlight on|off
hola lowpower on|off
hola controlcenter open|close
hola call <phone>
```

Use the CLI rather than writing these messages yourself. The CLI checks times, phone numbers, URLs, and durations. The sender enforces the command size and line format.

## Response format

Data endpoints require the receiver token in `X-Auth`. The Shortcut puts the request ID in `X-Screenshot-Id`, a header name retained for compatibility with the original screenshot branch.

| Command | Shortcut request | CLI wait |
| --- | --- | --- |
| `hola screenshot <id>` | Image to `POST /photo` | Watch the private inbox for up to 45 seconds |
| `hola getclipboard <id>` | Text or JSON to `POST /clipboard` | Poll `GET /clipboard/<id>` for up to 30 seconds |
| `hola alarm get <id>` | Alarm data to `POST /get-alarm` | Poll `GET /get-alarm/<id>` for up to 30 seconds |

The receiver keeps up to 200 legacy screen-text, clipboard, and alarm responses in memory. It saves screenshots under `~/.local/share/codex-ios-assistant/inbox/`. `iphone screen read` runs Apple Vision OCR locally on a temporary returned screenshot and removes that image after recognition. Explicit `iphone screen capture` results remain on disk. Restarting the receiver clears the in-memory values.

## CLI status values

- `dry-run`: the CLI printed the command without sending it.
- `requested`: Messages accepted the command. The phone did not return a receipt.
- `completed`: the phone returned matching data, or a Mac-side read finished.
- `failed`: a required service is missing or a helper returned an error.

A `requested` result confirms delivery to Messages, not execution on iOS. One-way actions do not post acknowledgements.

## Messages sender

Sandboxed AppleScript can fail with `Unable to find application named 'Messages'` or an `LSCopyApplicationURLsForBundleIdentifier()` error. The sender LaunchAgent runs in the user's GUI domain, so it can resolve and automate Messages. Codex reaches it through a local socket.

The sender accepts a narrow input:

- The socket has mode `0600`.
- The sender checks the peer UID when macOS exposes it.
- Each request contains one `hola` line, with a 4 KiB limit and no newline.
- The sender calls a fixed `/usr/bin/osascript` program. The client cannot supply an executable or script.
