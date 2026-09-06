# Output axis metadata

`keymasq.common.output_axes.OutputAxis` describes one advertised output axis:
its evdev name/code, label, minimum, maximum, and neutral value. Ranges must
increase and contain the neutral value.

`STANDARD_OUTPUT_AXES` describes the built-in virtual gamepad, including its
two Hat 0 axes. `learned_output_axes()` flattens hardware controls into axes,
including each component of a stick, using learned ranges and rest/center
values. Numeric-only identities use the canonical ABS name for `evdev_code`;
unknown codes and explicitly invalid names are omitted.

The hardware router fills missing metadata from the destination's cached
grab-time calibration and axis ranges before constructing output capabilities.
Saved calibration takes precedence. It supplies the resolved metadata to legacy
learned-control routing too, without modifying the saved hardware description or
querying the physical device during output resolution. Axes with neither saved nor
runtime-resolved valid ranges are omitted.

Output providers populate `GamepadOutputTarget.output_axes`. An empty tuple
means no supported axes. Virtual-device templates supply their advertised axes
through `template_output_axes()`. The editor and runtime use the same labels,
ranges, and neutral values, including individual stick components and custom
axes. A flight stick therefore offers twist and throttle without advertising
unsupported standard-gamepad axes.

Axis selection and range conversion must use the destination's metadata.
An evdev code being valid does not mean every output supports it.
