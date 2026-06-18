# pyright: reportUnusedFunction=false

from __future__ import annotations

from keymasq.gui.icons import device_icon_names, image_from_icon_names
from keymasq.gui.preferences import AppearanceMode, load_appearance_mode

from . import _runtime


def _icon_from_name(window, icon_name: str) -> _runtime.Gio.Icon:
    return _runtime.Gio.ThemedIcon.new(icon_name)


def _setup_content(window) -> None:
    window.main_box = _runtime.Gtk.Box(orientation=_runtime.Gtk.Orientation.VERTICAL)

    window.tab_view = _runtime.Adw.TabView()
    window.tab_view.set_vexpand(True)
    window.tab_view.set_shortcuts(
        _runtime.Adw.TabViewShortcuts.CONTROL_TAB
        | _runtime.Adw.TabViewShortcuts.CONTROL_SHIFT_TAB
        | _runtime.Adw.TabViewShortcuts.CONTROL_PAGE_UP
        | _runtime.Adw.TabViewShortcuts.CONTROL_PAGE_DOWN
    )
    window.tab_view.connect("notify::selected-page", window._on_selected_tab_changed)
    window.tab_view.connect("close-page", window._on_tab_close_page)
    window.tab_view.connect("page-reordered", window._on_tab_page_reordered)

    window.tab_bar = _runtime.Adw.TabBar()
    window.tab_bar.set_view(window.tab_view)
    window.tab_bar.set_autohide(False)
    window.tab_bar.set_expand_tabs(False)
    window.tab_bar.set_hexpand(True)
    window.tab_bar.set_halign(_runtime.Gtk.Align.FILL)

    add_device_button = _runtime.Gtk.Button(icon_name="list-add-symbolic")
    add_device_button.set_tooltip_text("Add device")
    add_device_button.connect("clicked", window._on_add_device_clicked)
    window.tab_bar.set_start_action_widget(add_device_button)

    header = _runtime.Gtk.WindowHandle()
    header_box = _runtime.Gtk.Box(orientation=_runtime.Gtk.Orientation.HORIZONTAL, spacing=0)
    header_box.add_css_class("titlebar")
    header_box.add_css_class("keymasq-main-header")
    header_box.set_hexpand(True)

    menu_button = _runtime.Gtk.MenuButton()
    menu_button.set_icon_name("open-menu-symbolic")
    menu_button.add_css_class("flat")

    menu_popover = _runtime.Gtk.Popover()
    menu_box = _runtime.Gtk.Box(orientation=_runtime.Gtk.Orientation.VERTICAL, spacing=0)
    menu_box.set_margin_top(6)
    menu_box.set_margin_bottom(6)
    menu_box.set_margin_start(6)
    menu_box.set_margin_end(6)

    appearance_box = _runtime.Gtk.Box(orientation=_runtime.Gtk.Orientation.HORIZONTAL, spacing=0)
    appearance_box.add_css_class("linked")
    appearance_box.set_margin_bottom(6)
    appearance_group: _runtime.Gtk.ToggleButton | None = None
    appearance_options: tuple[tuple[AppearanceMode, str], ...] = (
        ("system", "System"),
        ("light", "Light"),
        ("dark", "Dark"),
    )
    for mode, label in appearance_options:
        button = _runtime.Gtk.ToggleButton(label=label)
        button.set_hexpand(True)
        if appearance_group is not None:
            button.set_group(appearance_group)
        else:
            appearance_group = button
        button.connect("toggled", window._on_appearance_mode_toggled, mode)
        window._appearance_buttons[mode] = button
        appearance_box.append(button)

    current_appearance = load_appearance_mode()
    window._syncing_appearance = True
    window._appearance_buttons[current_appearance].set_active(True)
    window._syncing_appearance = False
    menu_box.append(appearance_box)
    menu_box.append(window._create_menu_separator())

    combos_btn = _runtime.Gtk.Button(label="Combos")
    window._configure_menu_button(combos_btn)
    combos_btn.connect("clicked", window._on_combos_menu_clicked, menu_popover)
    menu_box.append(combos_btn)

    superkeys_btn = _runtime.Gtk.Button(label="Super Keys")
    window._configure_menu_button(superkeys_btn)
    superkeys_btn.connect("clicked", window._on_menu_action_clicked, "superkeys", menu_popover)
    menu_box.append(superkeys_btn)

    macros_btn = _runtime.Gtk.Button(label="Macros")
    window._configure_menu_button(macros_btn)
    macros_btn.connect("clicked", window._on_menu_action_clicked, "macros", menu_popover)
    menu_box.append(macros_btn)

    analog_controls_btn = _runtime.Gtk.Button(label="Analog Controls")
    window._configure_menu_button(analog_controls_btn)
    analog_controls_btn.connect(
        "clicked",
        window._on_menu_action_clicked,
        "analog-controls",
        menu_popover,
    )
    menu_box.append(analog_controls_btn)

    diagnostics_btn = _runtime.Gtk.Button(label="Diagnostics")
    window._configure_menu_button(diagnostics_btn)
    diagnostics_btn.connect(
        "clicked",
        window._on_menu_action_clicked,
        "diagnostics",
        menu_popover,
    )
    menu_box.append(diagnostics_btn)

    settings_btn = _runtime.Gtk.Button(label="Settings")
    window._configure_menu_button(settings_btn)
    settings_btn.connect(
        "clicked",
        window._on_menu_action_clicked,
        "settings",
        menu_popover,
    )
    menu_box.append(settings_btn)

    menu_box.append(window._create_menu_separator())

    menu_unlock_btn = _runtime.Gtk.Button(label="Unlock Capture")
    window._configure_menu_button(menu_unlock_btn)
    menu_unlock_btn.set_tooltip_text(
        "Authorize raw original-input capture for adding inputs, combo capture, "
        "and live macro recording. Uses Polkit and stays tied to this GUI session."
    )
    menu_unlock_btn.connect("clicked", window._on_menu_unlock_clicked, menu_popover)
    menu_box.append(menu_unlock_btn)
    window._menu_unlock_btn = menu_unlock_btn

    menu_unlock_separator = window._create_menu_separator()
    menu_box.append(menu_unlock_separator)
    window._menu_unlock_separator = menu_unlock_separator

    feedback_btn = _runtime.Gtk.Button(label="Feedback")
    window._configure_menu_button(feedback_btn)
    feedback_btn.connect("clicked", window._on_menu_action_clicked, "feedback", menu_popover)
    menu_box.append(feedback_btn)

    about_btn = _runtime.Gtk.Button(label="About")
    window._configure_menu_button(about_btn)
    about_btn.connect("clicked", window._on_menu_action_clicked, "about", menu_popover)
    menu_box.append(about_btn)

    quit_btn = _runtime.Gtk.Button(label="Quit")
    window._configure_menu_button(quit_btn)
    quit_btn.connect("clicked", window._on_menu_action_clicked, "quit", menu_popover)
    menu_box.append(quit_btn)

    menu_popover.set_child(menu_box)
    menu_button.set_popover(menu_popover)

    if window.demo_mode:
        demo_label = _runtime.Gtk.Label(label="DEMO MODE")
        demo_label.add_css_class("error")
        header_box.append(demo_label)

    header_box.append(window.tab_bar)
    header_box.append(menu_button)

    window_controls = _runtime.Gtk.WindowControls(side=_runtime.Gtk.PackType.END)
    header_box.append(window_controls)
    header.set_child(header_box)

    toolbar = _runtime.Adw.ToolbarView()
    toolbar.add_top_bar(header)

    content_box = _runtime.Gtk.Box(orientation=_runtime.Gtk.Orientation.VERTICAL)

    window.warning_banner = _runtime.Adw.Banner()
    window.warning_banner.set_visible(False)
    window.warning_banner.set_revealed(False)
    content_box.append(window.warning_banner)

    content_box.append(window.tab_view)

    from keymasq.gui.widgets.recording_overlay import RecordingOverlay

    window._recording_overlay = RecordingOverlay(window)
    window._recording_overlay.set_halign(_runtime.Gtk.Align.FILL)
    window._recording_overlay.set_valign(_runtime.Gtk.Align.FILL)
    window._recording_overlay.set_visible(False)

    content_overlay = _runtime.Gtk.Overlay()
    content_overlay.set_child(content_box)
    content_overlay.add_overlay(window._recording_overlay)

    toolbar.set_content(content_overlay)

    window.status_bar = _runtime.Gtk.Box(
        orientation=_runtime.Gtk.Orientation.HORIZONTAL, spacing=12
    )
    window.status_bar.set_margin_top(6)
    window.status_bar.set_margin_bottom(6)
    window.status_bar.set_margin_start(12)
    window.status_bar.set_margin_end(12)

    window.keymasqd_status = _runtime.Gtk.Label(label="keymasqd: ⚪")
    window.keymasqd_status.add_css_class("caption")
    window.keymasqd_status.set_tooltip_text(
        "keymasqd status (via session):\n"
        "🟢 Running\n"
        "🔴 Not running\n"
        "⚪ Unknown (session not connected)"
    )
    window.status_bar.append(window.keymasqd_status)

    window.session_status = _runtime.Gtk.Label(label="session: ⚪")
    window.session_status.add_css_class("caption")
    window.session_status.set_tooltip_text(
        "keymasq-session status:\n"
        "🟢 Running and connected to keymasqd\n"
        "🟡 Running but NOT connected to keymasqd\n"
        "🔴 Not running"
    )
    window.status_bar.append(window.session_status)

    window.compositor_status = _runtime.Gtk.Label()
    window.compositor_status.add_css_class("caption")
    compositor_click = _runtime.Gtk.GestureClick()
    compositor_click.connect("released", window._on_compositor_status_released)
    window.compositor_status.add_controller(compositor_click)
    window._update_compositor_status()
    window.status_bar.append(window.compositor_status)

    status_spacer = _runtime.Gtk.Box()
    status_spacer.set_hexpand(True)
    window.status_bar.append(status_spacer)

    unlock_status_label = _runtime.Gtk.Label(label="")
    unlock_status_label.add_css_class("caption")
    unlock_status_label.set_halign(_runtime.Gtk.Align.END)
    unlock_status_label.set_visible(False)
    window.status_bar.append(unlock_status_label)
    window._unlock_status_label = unlock_status_label

    toolbar.add_bottom_bar(window.status_bar)

    window.set_content(toolbar)

    window._setup_placeholder()
    window._setup_combo_tab()
    window._update_unlock_state(None)


def _create_menu_separator(window) -> _runtime.Gtk.Widget:
    separator = _runtime.Gtk.Separator(orientation=_runtime.Gtk.Orientation.HORIZONTAL)
    separator.set_margin_top(9)
    separator.set_margin_bottom(9)
    separator.set_margin_start(4)
    separator.set_margin_end(4)
    separator.set_size_request(-1, 2)
    return separator


def _configure_menu_button(window, button: _runtime.Gtk.Button) -> None:
    button.set_halign(_runtime.Gtk.Align.FILL)
    button.set_margin_top(2)
    button.set_margin_bottom(2)


def _on_appearance_mode_toggled(
    window,
    button: _runtime.Gtk.ToggleButton,
    mode: AppearanceMode,
) -> None:
    if window._syncing_appearance or not button.get_active():
        return

    app = window.get_application()
    apply_appearance_mode = getattr(app, "apply_appearance_mode", None)
    if callable(apply_appearance_mode):
        apply_appearance_mode(mode)


def _on_menu_action_clicked(
    window,
    _button: _runtime.Gtk.Button,
    action_name: str,
    popover: _runtime.Gtk.Popover,
) -> None:
    popover.popdown()
    app = window.get_application()
    if app is None:
        return
    app.activate_action(action_name, None)


def _on_combos_menu_clicked(
    window, _button: _runtime.Gtk.Button, popover: _runtime.Gtk.Popover
) -> None:
    popover.popdown()
    window.show_combo_tab()


def _on_menu_unlock_clicked(
    window, _button: _runtime.Gtk.Button, popover: _runtime.Gtk.Popover
) -> None:
    popover.popdown()
    window.present_unlock_dialog()


def _setup_placeholder(window) -> None:
    window._create_placeholder_widget(
        title_text="Loading devices...",
        subtitle_text="Checking compositor support and loading saved hardware",
    )
    window._ensure_placeholder_page()


def _create_placeholder_widget(window, *, title_text: str, subtitle_text: str) -> None:
    window.placeholder = _runtime.Gtk.Box(
        orientation=_runtime.Gtk.Orientation.VERTICAL,
        valign=_runtime.Gtk.Align.CENTER,
        halign=_runtime.Gtk.Align.CENTER,
        spacing=12,
    )

    icon = image_from_icon_names(*device_icon_names(False), pixel_size=96)
    icon.add_css_class("dim-label")
    window.placeholder.append(icon)

    title = _runtime.Gtk.Label(label=title_text)
    title.add_css_class("title-1")
    window.placeholder.append(title)
    window._placeholder_title = title

    subtitle = _runtime.Gtk.Label(label=subtitle_text)
    subtitle.add_css_class("dim-label")
    window.placeholder.append(subtitle)
    window._placeholder_subtitle = subtitle
