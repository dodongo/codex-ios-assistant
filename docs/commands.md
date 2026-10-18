# Commands

Run `iphone --help` or `iphone <resource> --help` for full syntax. Global flags work before or after a leaf command.

## Output and diagnostics

```bash
iphone doctor
iphone --version
iphone --dry-run timer start 10m
iphone --json --dry-run weather open --location Chicago --lat 41.8781 --lng -87.6298
```

`--dry-run` prints the request without contacting the phone. `--json` returns structured output. `--verbose` prints status and URL details on stderr.

## Screen, clipboard, and alarms

```bash
iphone screen capture --timeout 45
iphone screen capture --output ~/Desktop/iphone.png
iphone clipboard get
iphone clipboard copy 'Text copied by Codex'
iphone alarm list
iphone alarm set '7:30 AM' --label 'Wake up'
iphone alarm off '7:30 AM'
```

`alarm list` returns enabled alarms. `alarm off` disables every enabled alarm at the specified hour and minute, including unlabeled alarms. It does not delete them.

## Device controls

```bash
iphone home
iphone flashlight on
iphone flashlight off
iphone timer start 10m
iphone timer pause
iphone timer resume
iphone timer cancel
iphone low-power on
iphone control-center open
iphone call 'Jane Appleseed'
```

A call starts at once. Confirm the recipient unless the user's current request names the person or number and asks you to call.

## Apps and links

```bash
iphone url open https://example.com
iphone camera open --mode video --facing front
iphone weather open
iphone weather open --location Chicago --lat 41.8781 --lng -87.6298
iphone calendar open --date 2027-01-01
iphone calculator evaluate '40+2'
iphone messages open --thread 'Jane Appleseed'
iphone messages compose --to 'Jane Appleseed' --body 'On my way'
iphone find-my open --tab people
iphone find-my open --person 'Jane Appleseed'
iphone uber open --destination 'Ferry Building' --lat 37.7955 --lng -122.3937
iphone doordash open --store-url 'https://www.doordash.com/store/...'
iphone spotify open 'https://open.spotify.com/track/...'
iphone photos open
iphone wallet open
iphone notes open
```

`messages compose` opens an unsent draft. Store, rideshare, media, and App Store commands open the requested page without completing a purchase, order, ride, playback action, or installation.

## Mac data

```bash
iphone contacts search 'Jane'
iphone messages chats --limit 20
iphone messages history --chat-id 42 --limit 20
```

Contacts uses the bundled Swift helper. Message history uses `imsg` and the Mac's Messages database. These read-only commands do not run the iPhone Shortcut.
