#!/usr/bin/env python3
"""Show every icon used by Keymasq and report unavailable entries.

This is intentionally a standalone GTK application so the exact same file can be
copied to a target machine and launched through ``gtk4-brotway-run``.  The gallery
checks icon-theme discovery before creating each image.  Missing entries therefore
remain conspicuous instead of turning into GTK's easy-to-miss generic placeholder.
"""

from __future__ import annotations

import argparse
import json
from importlib import resources
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]  # noqa: E402
    Gdk,  # pyright: ignore[reportAttributeAccessIssue]
    GLib,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,  # pyright: ignore[reportAttributeAccessIssue]
    Pango,  # pyright: ignore[reportAttributeAccessIssue]
)

from keymasq.gui.icons import register_icon_search_path  # noqa: E402

# Keep every fallback name as well as the preferred names: an AppImage should be
# able to render the exact name selected on any supported host.
ICON_NAMES = (
    "applications-games",
    "applications-games-symbolic",
    "audio-volume-high-symbolic",
    "audio-volume-low-symbolic",
    "audio-volume-muted-symbolic",
    "channel-insecure-symbolic",
    "channel-secure-symbolic",
    "dialog-information-symbolic",
    "display-brightness-symbolic",
    "document-edit-symbolic",
    "document-new-symbolic",
    "document-save-symbolic",
    "edit-clear-symbolic",
    "edit-copy-symbolic",
    "edit-delete",
    "edit-delete-symbolic",
    "edit-find-symbolic",
    "edit-paste-symbolic",
    "emblem-system-symbolic",
    "go-down-symbolic",
    "go-next-symbolic",
    "go-up-symbolic",
    "help-about-symbolic",
    "input-gaming",
    "input-gaming-symbolic",
    "input-keyboard",
    "input-keyboard-symbolic",
    "input-mouse",
    "input-mouse-symbolic",
    "input-tablet",
    "keymasq-combos-symbolic",
    "keymasq-keyboard-symbolic",
    "keymasq-mouse-symbolic",
    "list-add",
    "list-add-symbolic",
    "list-remove-symbolic",
    "media-playback-pause-symbolic",
    "media-playback-start-symbolic",
    "media-playback-stop-symbolic",
    "media-record-symbolic",
    "media-skip-backward-symbolic",
    "media-skip-forward-symbolic",
    "microphone-sensitivity-muted-symbolic",
    "object-select-symbolic",
    "open-menu-symbolic",
    "preferences-desktop-keyboard-shortcuts",
    "preferences-desktop-keyboard-shortcuts-symbolic",
    "preferences-desktop-keyboard-symbolic",
    "system-search-symbolic",
    "tools.keymasq.keymasq",
    "user-trash-symbolic",
    "view-refresh-symbolic",
    "view-restore-symbolic",
    "view-reveal-symbolic",
    "window-close-symbolic",
    "zoom-in-symbolic",
)

IMAGE_ASSETS = ("gamepad",)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--result-json",
        type=Path,
        help="also write the availability report to this path",
    )
    parser.add_argument(
        "--quit-after",
        type=float,
        metavar="SECONDS",
        help="quit automatically after the gallery has been shown",
    )
    return parser.parse_args()


def _add_styles() -> None:
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(
        """
        .icon-card {
          border: 1px solid alpha(currentColor, 0.20);
          border-radius: 8px;
          padding: 5px;
        }
        .icon-card-missing {
          border: 2px solid #e01b24;
          background: alpha(#e01b24, 0.12);
        }
        .icon-name { font-size: 9px; }
        .icon-ok { color: #2ec27e; font-weight: bold; }
        .icon-missing { color: #f66151; font-weight: bold; }
        .missing-mark { color: #f66151; font-size: 32px; font-weight: bold; }
        """
    )
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


def _status_label(text: str, css_class: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.add_css_class(css_class)
    return label


def _card(title: str, content: Gtk.Widget, available: bool) -> Gtk.Box:
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    card.set_size_request(110, 88)
    card.set_valign(Gtk.Align.START)
    card.add_css_class("icon-card")
    if not available:
        card.add_css_class("icon-card-missing")

    content.set_halign(Gtk.Align.CENTER)
    content.set_valign(Gtk.Align.CENTER)
    card.append(content)

    name = Gtk.Label(label=title)
    name.set_wrap(True)
    name.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    name.set_justify(Gtk.Justification.CENTER)
    name.add_css_class("icon-name")
    card.append(name)
    card.append(
        _status_label(
            "AVAILABLE" if available else "MISSING",
            "icon-ok" if available else "icon-missing",
        )
    )
    return card


def _missing_mark() -> Gtk.Label:
    mark = Gtk.Label(label="×")
    mark.add_css_class("missing-mark")
    return mark


def _theme_icon_card(
    theme: Gtk.IconTheme, icon_name: str
) -> tuple[Gtk.Box, bool, str | None, str | None]:
    error: str | None = None
    resolved_path: str | None = None
    try:
        if not theme.has_icon(icon_name):
            raise RuntimeError("icon theme does not contain this name")
        paintable = theme.lookup_icon(
            icon_name,
            None,
            42,
            1,
            Gtk.TextDirection.NONE,
            Gtk.IconLookupFlags.PRELOAD,
        )
        icon_file = paintable.get_file()
        if icon_file is None or (resolved_path := icon_file.get_path()) is None:
            raise RuntimeError("icon did not resolve to a local file")
        texture = Gdk.Texture.new_from_filename(resolved_path)
        if texture.get_width() <= 0 or texture.get_height() <= 0:
            raise RuntimeError("decoded icon has no visible dimensions")
        available = True
    except (GLib.Error, OSError, RuntimeError) as exc:
        available = False
        error = str(exc)

    if available:
        content = Gtk.Image.new_from_icon_name(icon_name)
        content.set_pixel_size(30)
    else:
        content = _missing_mark()
    return _card(icon_name, content, available), available, error, resolved_path


def _asset_card(asset_name: str) -> tuple[Gtk.Box, bool, str | None]:
    error: str | None = None
    try:
        asset_dir = resources.files("keymasq").joinpath("gui/assets")
        png_asset = asset_dir.joinpath(f"{asset_name}.png")
        asset = png_asset if png_asset.is_file() else asset_dir.joinpath(f"{asset_name}.svg")
        with resources.as_file(asset) as asset_path:
            texture = Gdk.Texture.new_from_filename(str(asset_path))
        content: Gtk.Widget = Gtk.Picture.new_for_paintable(texture)
        content.set_size_request(50, 32)
        content.set_can_shrink(True)
        available = True
    except (GLib.Error, OSError) as exc:
        content = _missing_mark()
        available = False
        error = str(exc)
    return _card(f"asset:{asset_name}", content, available), available, error


def _write_report(report: dict[str, object], result_path: Path | None) -> None:
    encoded = json.dumps(report, sort_keys=True)
    print(f"KEYMASQ_ICON_GALLERY_RESULT={encoded}", flush=True)
    if result_path is not None:
        result_path.write_text(f"{encoded}\n", encoding="utf-8")


def _activate(app: Gtk.Application, args: argparse.Namespace) -> None:
    register_icon_search_path()
    _add_styles()

    display = Gdk.Display.get_default()
    if display is None:
        raise RuntimeError("no GTK display")
    theme = Gtk.IconTheme.get_for_display(display)

    flow = Gtk.FlowBox()
    flow.set_valign(Gtk.Align.START)
    flow.set_selection_mode(Gtk.SelectionMode.NONE)
    flow.set_homogeneous(True)
    flow.set_row_spacing(4)
    flow.set_column_spacing(4)
    flow.set_min_children_per_line(12)
    flow.set_max_children_per_line(12)

    missing_icons: list[str] = []
    icon_errors: dict[str, str] = {}
    resolved_icon_files: dict[str, str] = {}
    for icon_name in ICON_NAMES:
        card, available, error, resolved_path = _theme_icon_card(theme, icon_name)
        flow.append(card)
        if not available:
            missing_icons.append(icon_name)
            if error is not None:
                icon_errors[icon_name] = error
        if resolved_path is not None:
            resolved_icon_files[icon_name] = resolved_path

    missing_assets: list[str] = []
    asset_errors: dict[str, str] = {}
    for asset_name in IMAGE_ASSETS:
        card, available, error = _asset_card(asset_name)
        flow.append(card)
        if not available:
            missing_assets.append(asset_name)
            if error is not None:
                asset_errors[asset_name] = error

    title = Gtk.Label()
    title.set_markup("<span size='x-large' weight='bold'>Keymasq AppImage icon gallery</span>")
    title.set_halign(Gtk.Align.START)
    summary = Gtk.Label(
        label=(
            f"{len(ICON_NAMES) - len(missing_icons)}/{len(ICON_NAMES)} theme icons and "
            f"{len(IMAGE_ASSETS) - len(missing_assets)}/{len(IMAGE_ASSETS)} image assets available"
        )
    )
    summary.set_halign(Gtk.Align.START)
    if missing_icons or missing_assets:
        summary.add_css_class("icon-missing")
    else:
        summary.add_css_class("icon-ok")

    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    body.set_margin_top(8)
    body.set_margin_bottom(8)
    body.set_margin_start(8)
    body.set_margin_end(8)
    body.append(title)
    body.append(summary)
    body.append(flow)

    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.set_child(body)

    window = Gtk.ApplicationWindow(application=app, title="Keymasq icon gallery")
    window.set_default_size(1500, 760)
    window.set_child(scroller)
    window.present()

    report: dict[str, object] = {
        "asset_errors": asset_errors,
        "available_asset_count": len(IMAGE_ASSETS) - len(missing_assets),
        "available_icon_count": len(ICON_NAMES) - len(missing_icons),
        "icon_errors": icon_errors,
        "missing_assets": missing_assets,
        "missing_icons": missing_icons,
        "resolved_icon_files": resolved_icon_files,
        "tested_assets": list(IMAGE_ASSETS),
        "tested_icons": list(ICON_NAMES),
        "total_asset_count": len(IMAGE_ASSETS),
        "total_icon_count": len(ICON_NAMES),
    }
    _write_report(report, args.result_json)

    if args.quit_after is not None:
        GLib.timeout_add(max(1, round(args.quit_after * 1000)), app.quit)


def main() -> int:
    args = _parse_args()
    app = Gtk.Application(application_id="io.github.nyrda.Keymasq.IconGallery")
    app.connect("activate", _activate, args)
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
