# Device Inspector

The Device Inspector is a floating read-only window for checking one configured
device at runtime.

Open it from a device tab with the inspect button. Keymasq uses the capture
unlock flow before the window can start, because the inspector observes
original hardware events.

## What It Shows

- the final resolved mapping for the selected device
- the profiles that produced those final mappings
- raw events from the inspected device, in an evtest-style stream
- configured analog inputs, such as sticks and triggers, with live values

The mapping view is read-only. To change a mapping, close the inspector, edit
the device tab, then open the inspector again.

The analog viewer only shows inputs already configured in the hardware setup.
Unknown raw axis events still appear in the raw event stream so you can identify
which event names and codes to add to the device setup.

The raw event stream shows buttons and keys by default. Axes, mouse movement,
and `EV_SYN` reports can be enabled from the filter buttons in the inspector.
The window keeps the most recent 100 events per filter category and displays
the most recent 100 events that match the active filters.

## Suppression Mode

The inspector has a suppression switch for testing mappings safely. When
suppression is on:

- events from the inspected hardware ID are still shown in the inspector
- normal remap output from that hardware ID is blocked
- macros and combos are not part of the inspector flow
- any raw `KEY_ESC` press seen by `keymasqd` turns active inspector suppression
  off, even when the suppressed device is a mouse

The Escape press is consumed by the inspector escape path and is not emitted as
normal output. Escape release events and non-Escape events do not disable
suppression.

Suppression is scoped to the inspected hardware ID. Closing the inspector stops
inspection, clears suppression for that hardware ID, and lets the session apply
the normal profile grab state again.

## Security

Starting the inspector and enabling suppression are sensitive session commands
when `[recording_guard].unlock_required = true`. They require the active GUI
that owns the capture unlock flow, just like macro recording and live input
capture.

The inspector force-grabs configured interfaces for the selected device while it
is open so raw events can be observed even if the current profile has no mapping
on a particular interface. It does not guess or create new controls from unknown
events.
