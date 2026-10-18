# Shortcut maintenance

`shortcut/actions.template.plist` contains 95 native Shortcuts actions. The committed template replaces private values with:

```text
__IOS_ASSISTANT_PUBLIC_URL__
__IOS_ASSISTANT_RECEIVER_TOKEN__
```

`scripts/render-shortcut.py` reads the private config, substitutes both values, and writes `build/ios-assistant-actions.plist` with mode `0600`.

## Pasteboard installation

Shortcuts on macOS copies actions under the pasteboard type `com.apple.shortcuts.action`. Each action is a separate pasteboard item containing a binary plist.

`scripts/copy-shortcut-actions.swift` recreates those items from the action array. The install flow is:

1. `scripts/copy-shortcut` renders the private action list.
2. The Swift helper writes one pasteboard item per action and checks the item count.
3. You paste once into a blank Shortcut.
4. Shortcuts rebuilds the UUID references and grouped control flow.

This relies on an undocumented Shortcuts pasteboard format. Apple may change it in a future macOS release.

## Branches

The Shortcut handles:

- timer start, pause, resume, and cancel;
- flashlight, Low Power Mode, and Control Center;
- calls, clipboard reads and writes, and URL opening;
- screen text, screenshots, and Home Screen;
- enabled-alarm listing, alarm creation, and time-based alarm disabling.

`iphone screen capture` sends `hola screenshot <id>`, which posts an image to `/photo`, and returns the saved image path. Open that image directly to inspect the phone screen.

The alarm list branch filters for enabled alarms before it loops over results. The off branch filters enabled alarms by hour and minute, then disables all matches. Labels are not part of the comparison.

## Inspect a native action

`scripts/inspect-shortcuts-clipboard.swift` prints the plist for copied Shortcuts actions.

To study an action:

1. Build a small scratch Shortcut.
2. Select the action or connected block and press Command-C.
3. Run `swift scripts/inspect-shortcuts-clipboard.swift`.
4. Record the action identifier, parameters, output UUIDs, and grouping identifiers.
5. Apply the same structure to a working copy of the template.
6. Run `make test`.
7. Paste the full action list into a new Shortcut and test the changed branch on an iPhone.

An `ActionOutput` attachment refers to an earlier action through `OutputUUID`. Conditional and repeat blocks pair start and end actions with one `GroupingIdentifier`. Alarm predicates contain archived native objects; reproduce them in Shortcuts instead of guessing their plist format.

## Commit policy

Commit the sanitized template. Do not commit rendered plists, exported Shortcuts, screenshots, tokens, tailnet details, or personal phone data.

`scripts/validate-shortcut.py` checks the action count, placeholder count, unsupported legacy actions, output references, and control-flow groups. Add a validation rule when a Shortcut change introduces another structural assumption.
