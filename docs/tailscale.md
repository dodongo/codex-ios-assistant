# Tailscale Serve

The iPhone needs an HTTPS address for the Mac receiver. Tailscale Serve gives the Shortcut a private `*.ts.net` origin that is reachable only from the same tailnet.

## Setup

Install Tailscale on the Mac and iPhone, sign both devices into the same tailnet, and connect them. Then configure the project and enable Serve:

```bash
./scripts/configure
./scripts/setup-tailscale
./scripts/install-services
```

`scripts/configure` derives the Mac's HTTPS origin from `tailscale status` when Tailscale is connected. `scripts/setup-tailscale` verifies that origin and runs the equivalent of:

```bash
tailscale serve --bg http://127.0.0.1:8787
```

The first run may open a browser to enable HTTPS certificates for the tailnet. Tailscale stores the Serve configuration in its daemon and resumes it after restarts. Check it with:

```bash
tailscale serve status
```

## Private network access

The receiver listens only on `127.0.0.1`, so other machines cannot contact the backend directly. Tailscale terminates HTTPS and proxies requests from permitted tailnet devices to that loopback address. Tailnet access controls apply in addition to the receiver's `X-Auth` token.

Keep Tailscale connected on the iPhone so the Message automation can post Shortcut responses to the Mac. This fork intentionally uses Serve rather than Funnel; the receiver is not exposed to the public internet.

## Change the Mac or hostname

1. Connect Tailscale on the new Mac.
2. Run `scripts/configure --url https://new-mac.tailnet-name.ts.net`.
3. Run `scripts/setup-tailscale` and `scripts/install-services`.
4. Run `scripts/copy-shortcut`, then paste the new actions into a blank Shortcut.
5. Point the iPhone Message automation at the new Shortcut.

Keep the old Shortcut until `/health` and one response command work through the new origin.

## Remove Serve

`scripts/uninstall-services` removes the sender and receiver LaunchAgents but leaves Tailscale Serve unchanged. Inspect `tailscale serve status` before changing it. If this project is the only service hosted by Tailscale Serve on the Mac, remove the configuration with:

```bash
tailscale serve reset
```
