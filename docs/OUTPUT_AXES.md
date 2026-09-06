# Output axis metadata

`keymasq.common.output_axes.OutputAxis` describes one advertised output axis:
its evdev name/code, label, minimum, maximum, and neutral value. Ranges must
increase and contain the neutral value.

`STANDARD_OUTPUT_AXES` describes the built-in virtual gamepad, including its
two Hat 0 axes. `learned_output_axes()` flattens hardware controls into axes,
including each component of a stick, using learned ranges and rest/center
values. Axes without valid ranges are omitted.

Output providers populate `GamepadOutputTarget.output_axes`. An empty tuple
means no supported axes. A future virtual-device template provider can supply
its own tuple of `OutputAxis` objects using the capabilities advertised when
creating its uinput device. The editor should use the same descriptions.

Axis selection and range conversion must use the destination's metadata.
An evdev code being valid does not mean every output supports it.
