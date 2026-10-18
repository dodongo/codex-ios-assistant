# Architecture and protocol

The CLI uses a private WebDriverAgent developer tunnel for navigation, gestures,
screenshots, and accessibility. An iMessage-triggered Shortcut handles native
system intents that lack an equally efficient direct WebDriverAgent API.

## Device control

Start a finite `iphone ui` session before navigation. All WebDriverAgent routes
use `iphone ui request METHOD PATH`. App-opening CLI helpers validate their URLs
and invoke that same request handler directly, without another process or
transport fallback. Starting on cellular is not supported by this setup; an
already authenticated Tailscale session can continue from Wi-Fi onto cellular.

URLs use `POST /url`; Home uses `POST /wda/homescreen`. Control Center uses
pointer gestures bound to a unique live visible window. Screenshot-only
observation saves a private PNG without a full accessibility scan. Targeted
actions preserve foreground, unlocked-device, unique-control, and finite-session
guards. Only verified state changes report completion.

## Shortcut transport

1. The CLI validates arguments and builds one native Shortcut command.
2. A per-user LaunchAgent accepts it on a mode-`0600` Unix socket, checks the
   peer UID where supported, and delivers it through Messages.
3. The iPhone Message automation runs the 70-action Shortcut for `hola` messages.
4. Clipboard and alarm reads post authenticated responses through Tailscale Serve
   to the loopback receiver on `127.0.0.1:8787`.
5. The CLI polls for the matching response until its timeout.

The sender accepts one line beginning with `hola `, with a 4 KiB limit and no
newline. It calls a fixed AppleScript; clients cannot supply an executable.
Use the public CLI instead of writing protocol messages.

```text
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
hola call <phone>
```

## Responses

Data endpoints require the receiver token in `X-Auth`. The Shortcut puts the
request ID in `X-Screenshot-Id`.

Clipboard reads post to `/clipboard` and poll `GET /clipboard/<id>`.
Alarm reads post to `/get-alarm` and poll `GET /get-alarm/<id>`.
Both default to a 30-second timeout. The receiver retains at most 200 responses
of each kind in memory; restarting clears them. It accepts no screen text or
image uploads.

## CLI status values

- `dry-run`: validated without contacting the phone.
- `requested`: a write was dispatched, not verified as effective.
- `completed`: matching data arrived or the target state was verified.
- `failed`: a required service is missing or a helper returned an error.

For Shortcut writes, `requested` means Messages accepted delivery, not that
iOS executed it. One-way Shortcut actions do not post acknowledgements.
