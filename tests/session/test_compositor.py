import asyncio

import pytest

from keymasq.session import compositor as compositor_module
from keymasq.session.compositor import (
    detect_compositor,
    get_compositor_support_details,
    get_listener_class,
    is_compositor_supported,
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


@pytest.mark.parametrize(
    ("available", "expected"),
    [
        (list(compositor_module.SUPPORTED_COMPOSITORS), "hyprland"),
        (["niri", "kde", "gnome", "cosmic", "wayland", "x11"], "niri"),
        (["kde", "gnome", "cosmic", "wayland", "x11"], "kde"),
        (["gnome", "cosmic", "wayland", "x11"], "gnome"),
        (["cosmic", "wayland", "x11"], "cosmic"),
        (["gnome", "wayland", "x11"], "gnome"),
        (["wayland", "wayland-layer-shell", "x11"], "wayland"),
        (["wayland-layer-shell", "x11"], "wayland-layer-shell"),
        (["x11"], "x11"),
        ([], None),
    ],
)
def test_detect_compositor_picks_highest_priority_available_session(
    monkeypatch,
    available: list[str],
    expected: str | None,
) -> None:
    _set_probes(monkeypatch, **dict.fromkeys(available, True))

    assert asyncio.run(detect_compositor()) == expected


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


def test_detect_gnome_even_when_bridge_support_is_unavailable(monkeypatch) -> None:
    _set_probes(
        monkeypatch,
        gnome=True,
        gnome_supported=False,
        wayland=True,
        x11=True,
    )
    assert asyncio.run(detect_compositor()) == "gnome"
    assert asyncio.run(is_compositor_supported("gnome")) is False
