import asyncio
import gc
import warnings

import pytest

from keymasq.session import compositor as compositor_module
from keymasq.session.compositor import (
    detect_compositor_sync,
    get_compositor_support_details,
    get_listener_class,
    is_compositor_supported,
    is_compositor_supported_sync,
)

EXPECTED_PROBE_ORDER = [
    "hyprland",
    "niri",
    "kde",
    "gnome",
    "cosmic",
    "wayland",
    "wayland-layer-shell",
    "x11",
]


def _probe_result(value: bool):
    async def _probe(_cls, _dbus=None) -> bool:
        return value

    return classmethod(_probe)


def _set_probes(
    monkeypatch,
    *,
    gnome_supported: bool | None = None,
    **probes: bool,
) -> None:
    for compositor_id, metadata in compositor_module.SUPPORTED_COMPOSITORS.items():
        listener_class = metadata["listener"]
        probe_value = probes.get(compositor_id, False)
        if compositor_id == "gnome":
            monkeypatch.setattr(listener_class, "probe_session", _probe_result(probe_value))
            probe_value = probe_value if gnome_supported is None else gnome_supported
        monkeypatch.setattr(listener_class, "probe_available", _probe_result(probe_value))


def test_probe_order_is_explicit_priority_order() -> None:
    assert [compositor_id for compositor_id, _listener in compositor_module.PROBE_ORDER] == (
        EXPECTED_PROBE_ORDER
    )


@pytest.mark.parametrize("compositor_id", list(compositor_module.SUPPORTED_COMPOSITORS))
def test_registry_dispatches_listener_lookup_and_support_probe(
    monkeypatch,
    compositor_id: str,
) -> None:
    listener_class = compositor_module.SUPPORTED_COMPOSITORS[compositor_id]["listener"]
    calls: list[str] = []

    async def probe_available(_cls, _dbus=None) -> bool:
        calls.append(compositor_id)
        return True

    monkeypatch.setattr(listener_class, "probe_available", classmethod(probe_available))

    assert get_listener_class(compositor_id) is listener_class
    assert asyncio.run(is_compositor_supported(compositor_id)) is True
    assert calls == [compositor_id]


def test_detect_priority_hyprland(monkeypatch) -> None:
    _set_probes(
        monkeypatch,
        **dict.fromkeys(compositor_module.SUPPORTED_COMPOSITORS, True),
    )
    assert detect_compositor_sync() == "hyprland"


def test_gnome_support_details_uses_single_detailed_probe(monkeypatch) -> None:
    calls: list[str] = []

    async def get_support_details(_cls, _dbus=None) -> dict[str, bool | str]:
        calls.append("details")
        return {"supported": False, "warning": "bridge disabled"}

    async def probe_available(_cls, _dbus=None) -> bool:
        raise AssertionError("GNOME support details should not call probe_available")

    monkeypatch.setattr(
        "keymasq.session.compositor.GnomeListener.get_support_details",
        classmethod(get_support_details),
    )
    monkeypatch.setattr(
        "keymasq.session.compositor.GnomeListener.probe_available",
        classmethod(probe_available),
    )

    result = asyncio.run(get_compositor_support_details("gnome"))

    assert result == {"supported": False, "warning": "bridge disabled"}
    assert calls == ["details"]


@pytest.mark.asyncio
async def test_sync_probe_called_in_running_loop_closes_coroutine() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)

        assert detect_compositor_sync() is None
        gc.collect()

    assert not [
        warning
        for warning in caught
        if "coroutine" in str(warning.message) and "was never awaited" in str(warning.message)
    ]


def test_detect_priority_niri_over_kde_and_wayland(monkeypatch) -> None:
    _set_probes(
        monkeypatch,
        niri=True,
        kde=True,
        gnome=True,
        cosmic=True,
        wayland=True,
        x11=True,
    )
    assert detect_compositor_sync() == "niri"


def test_detect_priority_kde(monkeypatch) -> None:
    _set_probes(
        monkeypatch,
        kde=True,
        gnome=True,
        cosmic=True,
        wayland=True,
        x11=True,
    )
    assert detect_compositor_sync() == "kde"


def test_detect_priority_gnome_over_cosmic(monkeypatch) -> None:
    _set_probes(
        monkeypatch,
        gnome=True,
        cosmic=True,
        wayland=True,
        x11=True,
    )
    assert detect_compositor_sync() == "gnome"


def test_detect_priority_cosmic_over_wayland_and_x11(monkeypatch) -> None:
    _set_probes(
        monkeypatch,
        cosmic=True,
        wayland=True,
        x11=True,
    )
    assert detect_compositor_sync() == "cosmic"


def test_detect_priority_gnome_over_wayland_and_x11(monkeypatch) -> None:
    _set_probes(
        monkeypatch,
        gnome=True,
        wayland=True,
        x11=True,
    )
    assert detect_compositor_sync() == "gnome"


def test_detect_gnome_even_when_bridge_support_is_unavailable(monkeypatch) -> None:
    _set_probes(
        monkeypatch,
        gnome=True,
        gnome_supported=False,
        wayland=True,
        x11=True,
    )
    assert detect_compositor_sync() == "gnome"
    assert is_compositor_supported_sync("gnome") is False


def test_detect_priority_wayland_over_x11(monkeypatch) -> None:
    _set_probes(
        monkeypatch,
        **{"wayland": True, "wayland-layer-shell": True, "x11": True},
    )
    assert detect_compositor_sync() == "wayland"


def test_detect_priority_layer_shell_wayland_over_x11(monkeypatch) -> None:
    _set_probes(
        monkeypatch,
        **{"wayland-layer-shell": True, "x11": True},
    )
    assert detect_compositor_sync() == "wayland-layer-shell"


def test_detect_x11(monkeypatch) -> None:
    _set_probes(
        monkeypatch,
        x11=True,
    )
    assert detect_compositor_sync() == "x11"


def test_detect_none(monkeypatch) -> None:
    _set_probes(monkeypatch)
    assert detect_compositor_sync() is None


def test_support_gates(monkeypatch) -> None:
    _set_probes(
        monkeypatch,
        **{
            "gnome": True,
            "cosmic": True,
            "wayland": True,
            "wayland-layer-shell": True,
            "x11": True,
        },
    )
    assert is_compositor_supported_sync("x11") is True
    assert is_compositor_supported_sync("wayland") is True
    assert is_compositor_supported_sync("wayland-layer-shell") is True
    assert is_compositor_supported_sync("kde") is False
    assert is_compositor_supported_sync("cosmic") is True
    assert is_compositor_supported_sync("gnome") is True
    assert is_compositor_supported_sync("hyprland") is False
    assert is_compositor_supported_sync("niri") is False
