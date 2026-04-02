from keyforge.session.compositor import detect_compositor_sync, is_compositor_supported_sync


def _probe_result(value: bool):
    async def _probe(_cls, _dbus=None) -> bool:
        return value

    return classmethod(_probe)


def _set_probes(
    monkeypatch,
    *,
    hyprland: bool,
    kde: bool,
    gnome: bool,
    gnome_supported: bool | None = None,
    cosmic: bool,
    wayland: bool,
    x11: bool,
) -> None:
    monkeypatch.setattr(
        "keyforge.session.compositor.HyprlandListener.probe_available",
        _probe_result(hyprland),
    )
    monkeypatch.setattr(
        "keyforge.session.compositor.KDEListener.probe_available",
        _probe_result(kde),
    )
    monkeypatch.setattr(
        "keyforge.session.compositor.GnomeListener.probe_session",
        _probe_result(gnome),
    )
    monkeypatch.setattr(
        "keyforge.session.compositor.GnomeListener.probe_available",
        _probe_result(gnome if gnome_supported is None else gnome_supported),
    )
    monkeypatch.setattr(
        "keyforge.session.compositor.CosmicListener.probe_available",
        _probe_result(cosmic),
    )
    monkeypatch.setattr(
        "keyforge.session.compositor.WlrootsWaylandListener.probe_available",
        _probe_result(wayland),
    )
    monkeypatch.setattr(
        "keyforge.session.compositor.X11Listener.probe_available",
        _probe_result(x11),
    )


def test_detect_priority_hyprland(monkeypatch) -> None:
    _set_probes(
        monkeypatch, hyprland=True, kde=True, gnome=True, cosmic=True, wayland=True, x11=True
    )
    assert detect_compositor_sync() == "hyprland"


def test_detect_priority_kde(monkeypatch) -> None:
    _set_probes(
        monkeypatch, hyprland=False, kde=True, gnome=True, cosmic=True, wayland=True, x11=True
    )
    assert detect_compositor_sync() == "kde"


def test_detect_priority_gnome_over_cosmic(monkeypatch) -> None:
    _set_probes(
        monkeypatch, hyprland=False, kde=False, gnome=True, cosmic=True, wayland=True, x11=True
    )
    assert detect_compositor_sync() == "gnome"


def test_detect_priority_cosmic_over_wayland_and_x11(monkeypatch) -> None:
    _set_probes(
        monkeypatch, hyprland=False, kde=False, gnome=False, cosmic=True, wayland=True, x11=True
    )
    assert detect_compositor_sync() == "cosmic"


def test_detect_priority_gnome_over_wayland_and_x11(monkeypatch) -> None:
    _set_probes(
        monkeypatch, hyprland=False, kde=False, gnome=True, cosmic=False, wayland=True, x11=True
    )
    assert detect_compositor_sync() == "gnome"


def test_detect_gnome_even_when_bridge_support_is_unavailable(monkeypatch) -> None:
    _set_probes(
        monkeypatch,
        hyprland=False,
        kde=False,
        gnome=True,
        gnome_supported=False,
        cosmic=False,
        wayland=True,
        x11=True,
    )
    assert detect_compositor_sync() == "gnome"
    assert is_compositor_supported_sync("gnome") is False


def test_detect_priority_wayland_over_x11(monkeypatch) -> None:
    _set_probes(
        monkeypatch, hyprland=False, kde=False, gnome=False, cosmic=False, wayland=True, x11=True
    )
    assert detect_compositor_sync() == "wayland"


def test_detect_x11(monkeypatch) -> None:
    _set_probes(
        monkeypatch, hyprland=False, kde=False, gnome=False, cosmic=False, wayland=False, x11=True
    )
    assert detect_compositor_sync() == "x11"


def test_detect_none(monkeypatch) -> None:
    _set_probes(
        monkeypatch, hyprland=False, kde=False, gnome=False, cosmic=False, wayland=False, x11=False
    )
    assert detect_compositor_sync() is None


def test_support_gates(monkeypatch) -> None:
    _set_probes(
        monkeypatch, hyprland=False, kde=False, gnome=True, cosmic=True, wayland=True, x11=True
    )
    assert is_compositor_supported_sync("x11") is True
    assert is_compositor_supported_sync("wayland") is True
    assert is_compositor_supported_sync("kde") is False
    assert is_compositor_supported_sync("cosmic") is True
    assert is_compositor_supported_sync("gnome") is True
    assert is_compositor_supported_sync("hyprland") is False
