# Documentation screenshots

A NixOS VM fixture generates the Keymasq documentation screenshots. The VM
creates a controlled Keymasq installation with real `keymasqd`,
`keymasq-session`, virtual evdev devices, and a curated config that mirrors the
maintainer hardware names used in the docs.

The manifest is `docs/screenshots.toml`. Each `[[shot]]` describes the GUI state
to show: main tabs, mapping dialogs, macro dialogs, combo dialogs, GNOME setup
dialogs, or example screenshots.

Per-shot crop adjustments also belong in `docs/screenshots.toml`. The fixture
renders most shots in process from GTK render nodes, so dialog and widget crops
use exact GTK coordinates. Use `crop_padding`, `crop_padding_top`,
`crop_padding_end`, `crop_padding_bottom`, and `crop_padding_start` to add or
remove whitespace around the computed crop. Use `crop_widget` to crop a named
widget exposed by the prepared dialog, and `scroll_crop_widget = true` when the
fixture should scroll that widget before capture.

The popover shots keep a legacy X11 root-window capture path because GTK
popovers and dropdown lists are separate native surfaces. For those shots only,
`crop = [x, y, width, height]` crops the captured main-window image.

## Generate

```sh
scripts/update-doc-screenshots
```

This builds `path:.#docshots`, then copies dark screenshots to
`docs/assets/screenshots/`.

Generate light screenshots on demand:

```sh
scripts/update-doc-screenshots --all
```

That builds `path:.#docshots-all` and also copies light screenshots to
`docs/assets/screenshots/light/`. Use `--modes light` to update only the light
set.

The current Markdown docs reference the dark screenshots, which are also the
default for GitHub. Light screenshots are an opt-in pass so normal closeout
checks do not spend time rendering unused assets.

## Check

```sh
scripts/check-doc-screenshots
```

This rebuilds the dark VM screenshots and diffs the result against the
checked-in assets. Use it as a PR closeout check when visible GUI changes should
update the documentation screenshots. Use `scripts/check-doc-screenshots --all`
to include light screenshots.

## Fixture scope

The fixture uses real-looking config on purpose:

- `DYGMA RAISE2 Keyboard`
- `Razer Naga V2 HyperSpeed`
- `Xbox 360 1`

Profiles, combos, superkeys, macros, and analog controls are clean
documentation examples instead of private local config.

The screenshot harness lives under `nix/docshots/` and imports production GUI
modules. Screenshot orchestration should stay there, and GUI behavior needed by
users should stay in `keymasq/gui/`.
