# Niri First-Class Support Checklist

Status: in progress

## Tracking

- [x] Add first-class `NiriListener`
- [x] Register `niri` in compositor detection and listener exports
- [x] Implement Niri event socket handling and focused-window tracking
- [x] Implement Niri command socket handling
- [x] Implement fixed Niri dispatcher allowlist and validators
- [x] Add Niri GUI compositor action page and presets
- [x] Add unit tests for compositor detection, listener behavior, GUI, and profile round-trip
- [x] Add Niri VM integration test to the listener matrix
- [x] Update docs for direct Niri support and security model
- [ ] Run `./scripts/check.sh full`
- [ ] Run and pass the Niri VM test

## Notes

- Keep this file updated as phases complete so the implementation state stays visible.
- Niri support should follow the KDE/GNOME constrained-dispatch model, not raw passthrough IPC.
