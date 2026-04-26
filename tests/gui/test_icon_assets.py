from importlib import resources
from xml.etree import ElementTree


def test_custom_symbolic_icons_use_filled_paths() -> None:
    assets = resources.files("keymasq").joinpath("gui/assets")

    for icon_name in (
        "keymasq-combos-symbolic.svg",
        "keymasq-keyboard-symbolic.svg",
        "keymasq-mouse-symbolic.svg",
    ):
        root = ElementTree.parse(assets.joinpath(icon_name)).getroot()
        for element in root.iter():
            assert element.get("stroke") is None, icon_name
            assert element.get("fill") != "none", icon_name
