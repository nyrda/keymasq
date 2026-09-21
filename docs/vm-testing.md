# Manual VM test gates

Keymasq's NixOS VM integration suites are manual gates by design. They are too
resource-heavy for the regular CI flow, so CI does not run them and there is no
plan to add them there. Instead, someone must run them by hand at three points:

- before opening a pull request (contributor)
- before merging a pull request (maintainer)
- before tagging a release (maintainer)

The person running the gate picks the suites from the matrix below based on
what the change touches. "Not required" never means "not allowed", so run more
when in doubt.

## The suites

| Suite | Command | Documented in |
| ----- | ------- | ------------- |
| Daemon/session runtime | `./scripts/integration.sh daemon-session` | [daemon-session-integration-test.md](daemon-session-integration-test.md) |
| Masking behavior | `./scripts/integration.sh masking-behavior` | [masking-vm-tests.md](masking-vm-tests.md) |
| Masking recovery | `./scripts/integration.sh masking-recovery` | [masking-vm-tests.md](masking-vm-tests.md) |
| AppImage/Brotway artifact | `scripts/test-appimage-brotway <Keymasq.AppImage>` | [AppImage/Brotway artifact gate](#appimagebrotway-artifact-gate) |
| Listener VM matrix (all compositors) | `./scripts/integration.sh listeners` | [listener-vm-tests.md](listener-vm-tests.md) |
| Single listener VM | `./scripts/integration.sh <gnome\|kde\|hyprland\|niri\|xfce\|cosmic\|sway\|gnome-bridge>` | [listener-vm-tests.md](listener-vm-tests.md) |
| Documentation screenshots | `scripts/check-doc-screenshots` | [screenshots.md](screenshots.md) |

List every integration shortcut with `./scripts/integration.sh --help`.
`./scripts/integration.sh all` runs the daemon/session suite, both masking suites,
and the full listener matrix. Run all VM suites on a Linux host with KVM acceleration.

## Which suites apply to which change

| Change category | Typical paths | Required manual gate |
| --------------- | ------------- | -------------------- |
| Daemon / remap runtime | `keymasq/keymasqd/**` | `daemon-session` |
| Session broker, profiles, recording | `keymasq/session/manager/**`, `keymasq/session/*.py` | `daemon-session` |
| Compositor listeners | `keymasq/session/listeners/**`, `keymasq/session/wayland_protocols/**` | Listener VM test(s) for the affected compositor(s). Shared listener-path or Wayland-protocol changes need the full `listeners` matrix |
| Shared code, IPC, models | `keymasq/common/**` | `daemon-session` and the full `listeners` matrix |
| GUI and assets | `keymasq/gui/**`, `assets/**` | `scripts/check-doc-screenshots`, or include the regenerated screenshots in the PR |
| GNOME Shell extension | `gnome-extension/**` | `gnome-bridge` and `gnome` |
| Nix/VM infrastructure | `flake.nix`, `flake.lock`, `nix/**` | `daemon-session` and the full `listeners` matrix |
| Services, udev, packaging payload | `systemd/**`, `udev/**`, `sysusers.d/**`, `tmpfiles.d/**`, `polkit/**`, and packaged copies of these payloads (for example `packaging/appimage/assets/**`) | `daemon-session` |
| AppImage Brotway payload, runtime, or test harness | `packaging/appimage/**` Brotway artifact, dependency collection, launcher, installer/runtime layout, or GUI startup integration. Also `nix/appimage-brotway-integration-test.nix`, `nix/appimage-brotway-integration-test/**`, and `scripts/test-appimage-brotway` | Build the candidate AppImage and run `scripts/test-appimage-brotway <artifact>` |
| Gate harness scripts | `scripts/integration.sh`, `scripts/check-doc-screenshots`, `scripts/update-doc-screenshots` | Run the changed harness itself. For `integration.sh` that means `daemon-session` plus at least one listener suite. For the screenshot scripts it means `scripts/check-doc-screenshots` |
| Docs, packaging metadata, unrelated tooling only | `docs/**`, `.github/**`, `scripts/**` not listed above, and `packaging/**` metadata that does not ship service/udev payloads | None |

Multi-category changes take the union of the rows they touch. If a change does
not fit a row cleanly, treat it as shared code.

GUI changes only need a VM runtime suite when they also change session or
daemon behavior. The screenshot check is their dedicated gate because the
screenshot VM boots the real daemon and session stack.

## Pull request gate

Before opening a PR, run the suites the matrix requires and record them in the
PR template's Testing section. List any skipped suite with a reason (for
example "docs only").

The merging maintainer is the second gate. Verify that the recorded suites
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

Prereleases built through the `Package` workflow's manual dispatch should pass
the same gates unless the prerelease exists specifically to test packaging
changes.

Every AppImage release candidate must also pass its artifact-specific gate
after the build:

```bash
scripts/test-appimage-brotway \
  "dist/appimage/Keymasq-0.19.0-x86_64.AppImage"
```

## AppImage/Brotway artifact gate

This gate tests the Brotway runtime bundled in the Keymasq AppImage. It runs
one exact local AppImage file. It does not build or install the Nix Keymasq
package and does not import the Keymasq NixOS module. The assertions live in
`nix/appimage-brotway-integration-test.nix` and the probes under
`nix/appimage-brotway-integration-test/`.

Always run it through the runner:

```bash
scripts/test-appimage-brotway dist/appimage/Keymasq-0.19.0-x86_64.AppImage
```

The flake only adds the check when `KEYMASQ_APPIMAGE_TEST_ARTIFACT` is set
during impure evaluation, so plain `nix flake check` skips it. The runner sets
the variable and passes `--impure`. `builtins.path` copies the AppImage into
the Nix store and makes that store path a derivation input. A one-byte change
to the AppImage is therefore a new test input, and Nix cannot reuse an older
passing result.

The test is x86_64-linux only. The first run also builds or downloads the
NixOS VMs, Chromium, ChromeDriver, Selenium, and Pillow.

The design choices below are deliberate. Keep them when you change the test:

- The test uses two VMs. `deck` is a small systemd-based Steam Deck analogue
  with FUSE and uinput but no Nix-built Keymasq. `browser` runs Chromium and
  Selenium against Brotway's TCP listener on `deck`, so a local curl response
  cannot pass for browser rendering.
- The test installs the real Type-2 AppImage with `--install --user deck`. It
  sets neither `APPIMAGE_EXTRACT_AND_RUN` nor the installer's extracted-source
  test override. An import-only check, a direct `gtk4-broadwayd` HTTP probe, or
  an extracted AppDir launch does not replace it.
- `/proc/PID/maps` of the Keymasq GUI process must show the private
  `lib/gtk4-brotway/libgtk-4.so`. Seeing the library only in `gtk4-broadwayd`
  is not enough.
- The test binds Brotway to `0.0.0.0` so the second VM can reach it. Normal
  AppImage launches default to the loopback-only `127.0.0.1` listener.
- Input is tested as a round trip:

  ```text
  Chromium key events → Brotway WebSocket → gtk4-broadwayd
    → native debug-menu GTK window → Brotway render → Chromium screenshot
  ```

- A separate icon gallery checks that every name in
  `packaging/appimage/assets/gui-icon-names.txt` resolves to the AppImage's
  private theme and decodes.

The upstream Brotway launcher sets `GDK_BACKEND=broadway` only for its direct
GUI child. Keymasq uses that backend to select `Gio.ApplicationFlags.NON_UNIQUE`
so the browser-facing GUI does not activate or replace an existing desktop
Keymasq instance. The AppImage wrapper also forces `DISPLAY` to the resolved
Brotway display instead of inheriting a host display.

On failure, the test prints hashes, the runtime tree, process maps, service
journals, and both browser JSON results into the Nix log, so they survive a
failed derivation. Inside the browser VM, the screenshots and JSON results are
under `/tmp/keymasq-brotway` and `/tmp/keymasq-icon-gallery`.

## Optional: running the gates on GitHub

The manually dispatched `VM Integration` workflow
(`.github/workflows/vm-integration.yml`) runs the same
`./scripts/integration.sh` suites on a GitHub-hosted runner with KVM. It only
runs via `workflow_dispatch` and is never part of push or PR CI. Use it when a
local KVM host is unavailable. Local runs remain the primary path.
