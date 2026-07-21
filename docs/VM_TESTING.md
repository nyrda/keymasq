# Manual VM Test Gates

Keymasq's NixOS VM integration suites are deliberate manual gates. They are too
resource-heavy for the regular CI flow, so CI does not run them and there is no
plan to add them there. Instead, they are required manual steps at three points:

- before opening a pull request (contributor)
- before merging a pull request (maintainer)
- before tagging a release (maintainer)

The person running the gate picks the suites from the matrix below based on
what the change touches. "Not required" never means "not allowed": run more
when in doubt.

## The suites

| Suite | Command | Documented in |
| ----- | ------- | ------------- |
| Daemon/session runtime | `./scripts/integration.sh daemon-session` | [DAEMON_SESSION_INTEGRATION_TEST.md](DAEMON_SESSION_INTEGRATION_TEST.md) |
| AppImage/Brotway artifact | `scripts/test-appimage-brotway <Keymasq.AppImage>` | [APPIMAGE_BROTWAY_INTEGRATION_TEST.md](APPIMAGE_BROTWAY_INTEGRATION_TEST.md) |
| Listener VM matrix (all compositors) | `./scripts/integration.sh listeners` | [LISTENER_VM_TESTS.md](LISTENER_VM_TESTS.md) |
| Single listener VM | `./scripts/integration.sh <gnome\|kde\|hyprland\|niri\|xfce\|cosmic\|sway\|gnome-bridge>` | [LISTENER_VM_TESTS.md](LISTENER_VM_TESTS.md) |
| Documentation screenshots | `scripts/check-doc-screenshots` | [SCREENSHOTS.md](SCREENSHOTS.md) |

List every integration shortcut with `./scripts/integration.sh --help`.
`./scripts/integration.sh all` runs the daemon/session suite plus the full
listener matrix. All VM suites want a Linux host with KVM acceleration.

## Which suites apply to which change

| Change category | Typical paths | Required manual gate |
| --------------- | ------------- | -------------------- |
| Daemon / remap runtime | `keymasq/keymasqd/**` | `daemon-session` |
| Session broker, profiles, recording | `keymasq/session/manager/**`, `keymasq/session/*.py` | `daemon-session` |
| Compositor listeners | `keymasq/session/listeners/**`, `keymasq/session/wayland_protocols/**` | Listener VM test(s) for the affected compositor(s); the full `listeners` matrix for shared listener-path or Wayland-protocol changes |
| Shared code, IPC, models | `keymasq/common/**` | `daemon-session` and the full `listeners` matrix |
| GUI and assets | `keymasq/gui/**`, `assets/**` | `scripts/check-doc-screenshots`, or include the regenerated screenshots in the PR |
| GNOME Shell extension | `gnome-extension/**` | `gnome-bridge` and `gnome` |
| Nix/VM infrastructure | `flake.nix`, `flake.lock`, `nix/**` | `daemon-session` and the full `listeners` matrix |
| Services, udev, packaging payload | `systemd/**`, `udev/**`, `sysusers.d/**`, `tmpfiles.d/**`, `polkit/**`, and packaged copies of these payloads (for example `packaging/appimage/assets/**`) | `daemon-session` |
| AppImage Brotway payload, runtime, or test harness | `packaging/appimage/**` Brotway artifact, dependency collection, launcher, installer/runtime layout, or GUI startup integration; `nix/appimage-brotway-integration-test.nix`; `nix/appimage-brotway-integration-test/**`; `scripts/test-appimage-brotway` | Build the candidate AppImage and run `scripts/test-appimage-brotway <artifact>` |
| Gate harness scripts | `scripts/integration.sh`, `scripts/check-doc-screenshots`, `scripts/update-doc-screenshots` | Run the changed harness itself: `daemon-session` plus at least one listener suite for `integration.sh`; `scripts/check-doc-screenshots` for the screenshot scripts |
| Docs, packaging metadata, unrelated tooling only | `docs/**`, `.github/**`, `scripts/**` not listed above, and `packaging/**` metadata that does not ship service/udev payloads | None |

Multi-category changes take the union of the rows they touch. If a change does
not fit a row cleanly, treat it as shared code.

GUI changes only need a VM runtime suite when they also change session or
daemon behavior; the screenshot check is their dedicated gate because the
screenshot VM boots the real daemon and session stack.

## Pull request gate

Before opening a PR, run the suites the matrix requires and record them in the
PR template's Testing section. Suites that were skipped must be listed with a
reason (for example "docs only").

The merging maintainer is the second gate: verify that the recorded suites
match the matrix for the final diff, and rerun (or run additional) suites when
the diff grew beyond the original scope.

## Release gate

Before tagging a stable `v*` release, run the full set regardless of what
changed since the last tag:

```bash
./scripts/integration.sh daemon-session
./scripts/integration.sh listeners
scripts/check-doc-screenshots
```

Prereleases built through the `Package` workflow's manual dispatch should meet
the same bar unless the prerelease exists specifically to test packaging
changes.

Every AppImage release candidate must additionally pass its artifact-specific
gate after it has been built:

```bash
scripts/test-appimage-brotway \
  "dist/appimage/Keymasq-0.19.0-x86_64.AppImage"
```

## Optional: running the gates on GitHub

The manually dispatched `VM Integration` workflow
(`.github/workflows/vm-integration.yml`) runs the same
`./scripts/integration.sh` suites on a GitHub-hosted runner with KVM. It only
runs via `workflow_dispatch` and is never part of push or PR CI. Use it when a
local KVM host is unavailable; local runs remain the primary path.
