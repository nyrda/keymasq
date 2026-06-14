# Mouse UI Automation

Use a macro to click fixed positions in an application that does not have good
keyboard shortcuts.

Examples:

- click through a repeated settings dialog
- open a menu, wait, then click an item
- fill a legacy app workflow that only exposes mouse controls
- replay a fixed tool sequence in a graphics, CAD, or admin application

For reliable UI automation, build the click sequence manually with natural mouse
move events when realtime cursor feedback is available. Use a high natural move
speed for near-instant positioning. Avoid replaying recorded mouse movement for
this kind of workflow; pointer paths are fragile, while fixed positions and
explicit waits are easier to inspect and adjust on the timeline.

## Bind A Cancel Key First

Before testing mouse automation, bind **Cancel Macro Playback** to a key or combo.
Use it as an emergency stop if anything goes wrong.

## Create An Empty Macro

1. Click **Open Macros**.
2. Click **Empty**.
3. Give the macro a clear name, for example:

```text
open_export_dialog
```

## Add A Fixed Click

In the macro editor, the middle lane is the mouse movement lane.

1. Right-click the middle lane at the time where the pointer should move.
2. Select **Add Mouse Move**.
3. Select the new move event and leave **Natural** selected.
4. Use **Capture** to capture the screen position you want to click.
5. Right-click the mouse-click lane shortly after the move event.
6. Add the mouse button press and release for the click.

Use a small delay between the natural move and the click, for example:

| Event | Time |
|---|---|
| Move Natural | `0 ms` |
| Mouse button press | `50 ms` |
| Mouse button release | `80 ms` |

Repeat the same pattern for each UI target.

## Position Events On The Timeline

Place later events far enough apart for the target app to react:

- waiting for a menu to open
- waiting for a dialog to appear
- waiting for a web UI to update

For example, if a menu takes time to open, place the next natural move and click
later in the timeline. Start with conservative spacing, then reduce it
after testing if the app responds reliably.

## Bind The Macro

Bind the macro to a key, mouse button, combo, or superkey slot.

When choosing the macro action:

| Option | Recommended value |
|---|---|
| Replay mouse movement | Off. This disables recorded pointer paths; manually added natural or absolute move events still run. |
| Replay mouse clicks | On |
| Speed | Start at `1.0` |

Keep macro playback **Speed** at `1.0` until the macro works reliably. Tune the
natural move event's own `kpx/s` speed for cursor positioning. If the app misses
clicks or opens the wrong menu, move the fragile step later in the timeline.

## Reliability Tips

- Keep the target window in the same position and size.
- Use a conditional profile so the macro only runs for the intended app.
- Use your compositor's window rules to keep that app at a stable position and
  size.
- Use natural move events for fixed UI targets when realtime cursor feedback is
  available.
- Use absolute move events only as a fallback, preferably on unscaled
  single-monitor setups.
- Place events at deliberate times instead of relying on recorded pauses.
- Prefer keyboard shortcuts or compositor actions when the app exposes them.
- Test in a harmless state before using the macro on real data.
