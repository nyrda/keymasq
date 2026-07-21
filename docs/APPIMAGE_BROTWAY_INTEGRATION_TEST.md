# AppImage Brotway Integration Test

This test is the end-to-end acceptance gate for the Brotway runtime bundled in
the Keymasq AppImage. It consumes an exact local AppImage artifact; it does not
build or install the Nix Keymasq package and does not import the Keymasq NixOS
module.

## Running It

Build the AppImage, then pass that exact artifact to the runner:

```bash
scripts/test-appimage-brotway dist/appimage/Keymasq-0.19.0-x86_64.AppImage
```

The runner checks the input and passes its absolute local path to the impure
flake evaluation. `builtins.path` copies it into the Nix store once and makes
that immutable store path a derivation input. Changing even one byte in the
AppImage creates a different test input and prevents an older successful VM
result from standing in for the new artifact.

The check is only added to the flake when
`KEYMASQ_APPIMAGE_TEST_ARTIFACT` is set during impure evaluation. Consequently,
plain `nix flake check` skips it. The runner sets the variable and passes
`--impure`; use the runner rather than invoking the check through a plain flake
check.

The test is x86_64-linux only and is VM-heavy. A host with KVM acceleration is
strongly recommended. The first run also builds or downloads NixOS VM,
Chromium, ChromeDriver, Selenium, and Pillow dependencies.

## Topology

The NixOS test starts two isolated nodes:

- `deck` is a small, systemd-based Steam Deck analogue. It contains the host
  tools expected by the distribution-neutral installer, but no Nix-built copy
  of Keymasq. It has FUSE and uinput devices available.
- `browser` contains a version-matched Chromium and ChromeDriver. Selenium
  connects from this node to Brotway's TCP listener on `deck`, so a local curl
  response cannot masquerade as browser rendering.

## Assertions

The test executes the actual Type-2 AppImage with `--install --user deck`.
Neither `APPIMAGE_EXTRACT_AND_RUN` nor the installer's extracted-source test
override is set. It then requires all of the following:

1. `/opt/keymasq/Keymasq.AppImage` has the same SHA-256 as the input artifact,
   and `/opt/keymasq/runtime/current` points to that hash.
2. The installed Brotway launcher, daemon, debug-menu helper, and private
   `libgtk-4.so.1` are present in the extracted runtime. The root and user
   launch wrappers are also present.
3. The AppImage-installed `keymasqd` and `keymasq-session` services are active,
   the session socket exists, and the installed CLI completes a real JSON
   status request.
4. The installed `gtk4-brotway-run` starts the installed Keymasq GUI on port
   18101. The cross-VM test explicitly opts into `0.0.0.0`; normal AppImage
   launches default to the loopback-only `127.0.0.1` listener. `/proc/PID/maps`
   must prove that the Keymasq GUI process—not merely `gtk4-broadwayd`—loaded
   `lib/gtk4-brotway/libgtk-4.so`.
5. Real headless Chromium observes a Brotway WebSocket and incoming frames,
   finds a non-zero canvas, and captures a non-blank rendered screenshot with
   no severe browser-console errors.
6. Selenium presses and releases Shift three times in that canvas. The rendered
   page must change, and the Deck VM must observe the resulting
   `gtk4-brotway-debugmenu` process with the private GTK library in its maps.
7. A standalone gallery launched through the installed `gtk4-brotway-run`
   resolves every icon in `packaging/appimage/assets/gui-icon-names.txt` to the
   AppImage's private theme, decodes every resolved PNG, and decodes the bundled
   gamepad image asset. Missing names and loader errors fail the test.
8. Chromium captures the gallery's nominal 1500x760 GTK window over a second
   Brotway WebSocket, requiring a rendered surface of at least 1400x700. The
   test then restarts the gallery in a fresh process and requires the same
   complete runtime audit, with no image-loader failures in either gallery
   journal.

Assertion 6 is the input round trip:

```text
Chromium key events -> Brotway WebSocket -> gtk4-broadwayd
  -> native debug-menu GTK window -> Brotway render -> Chromium screenshot
```

An import-only check, a direct `gtk4-broadwayd` HTTP probe, or an extracted
AppDir launch is intentionally not accepted as a replacement for this test.

The upstream Brotway launcher sets `GDK_BACKEND=broadway` only for its direct
GUI child. Keymasq uses that backend to select `Gio.ApplicationFlags.NON_UNIQUE`
so the browser-facing GUI does not activate or replace an existing desktop
Keymasq instance. The AppImage wrapper also forces `DISPLAY` to the resolved
Brotway display instead of inheriting a host display.

## Failure Diagnostics

On failure, the Nix log includes:

- the source, staged, and installed AppImage hashes;
- the installed runtime tree and active runtime target;
- processes, listening sockets, and private-GTK process maps;
- system and user service status and journals;
- Chromium console and WebSocket event summaries;
- screenshot dimensions, hashes, pixel extrema/variance, and before/after
  changed-pixel fraction;
- the icon-gallery inventory result and rendered gallery screenshot;
- the paths, sizes, and hashes of the before/after PNGs inside the browser VM.

The ordinary browser probe writes `brotway-before.png`,
`brotway-after-triple-shift.png`, and `brotway-browser-result.json` below
`/tmp/keymasq-brotway` in the browser VM. The gallery probe additionally writes
`icon-gallery.png` and `icon-gallery-browser-result.json` below
`/tmp/keymasq-icon-gallery`. Both JSON results and the gallery's complete icon
audit are printed into the normal test log so they survive a failed derivation.

Run this gate after any change to the AppImage's Brotway artifact, dependency
collection, launcher, installer/runtime layout, GTK/libadwaita bundle, or GUI
startup behavior, and against every AppImage release candidate.
