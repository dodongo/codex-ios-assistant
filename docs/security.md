# Security

This project connects Codex, Messages, an iPhone Shortcut, and a private Tailscale HTTPS origin. Screen text, screenshots, clipboard contents, alarms, Contacts, and Messages history may contain private data.

## Files that must stay private

- `~/.config/codex-ios-assistant/config.env` contains the iMessage target and receiver token.
- `build/ios-assistant-actions.plist` contains the receiver hostname and token.
- `~/.local/share/codex-ios-assistant/inbox/` contains screenshots.

The installer keeps these files outside Git or under ignored paths. Config files and screenshots use mode `0600`; their parent directories use `0700`.

## Receiver

The receiver binds to `127.0.0.1`. Tailscale Serve is its private tailnet route, and tailnet access controls apply before traffic reaches the receiver. Phone-data endpoints also require an exact `X-Auth` token with at least 32 characters. `/` and `/health` expose a fixed status string to devices that can reach the Serve origin.

Receiver logs include byte counts, alarm counts, and request IDs. They do not include screen text, clipboard values, or alarm details. Text responses live in memory. Screenshots remain on disk until you remove them.

The token is a bearer secret stored in the Shortcut. A tailnet member who obtains the token and hostname could submit false responses or read a response after guessing its request ID. The current protocol has no request signature, expiration, or replay check.

## Messages sender

The sender listens on a mode-`0600` Unix socket. It checks the peer UID when macOS provides one, rejects newlines and requests over 4 KiB, and accepts commands beginning with `hola `. It runs fixed AppleScript through `/usr/bin/osascript`; clients cannot choose the program or script.

Restrict the iPhone Message automation to the expected sender and messages containing `hola`. Do not add a Shortcut branch that turns message text into arbitrary commands or runs an arbitrary Shortcut.

The CLI opens message drafts for review. It does not send them. Commerce and rideshare links open a page without placing an order or requesting a ride.

## Check a commit

Run these commands before pushing:

```bash
make test
git status --short
git diff --cached
```

Inspect the staged diff for personal tailnet names, email addresses, phone numbers, `/Users/<name>` paths, tokens, and private keys. `.gitignore` does not protect a file after Git has staged it.

If a token reaches a commit, rotate it and remove it from Git history before publishing the repository. Deleting it in a later commit leaves the original value in history.
