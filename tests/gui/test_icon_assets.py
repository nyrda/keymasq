from importlib import resources
from xml.etree import ElementTree

_SYMBOLIC_ICON_NAMES = (
    "keymasq-combos-symbolic.svg",
    "keymasq-keyboard-symbolic.svg",
    "keymasq-mouse-symbolic.svg",
)


def _local_name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def test_custom_symbolic_icons_use_direct_filled_paths() -> None:
    assets = resources.files("keymasq").joinpath("gui/assets")

    for icon_name in _SYMBOLIC_ICON_NAMES:
        root = ElementTree.parse(assets.joinpath(icon_name)).getroot()
        paths = [element for element in root.iter() if _local_name(element) == "path"]

        assert paths, icon_name
        for path in paths:
            assert path.get("style") is None, icon_name
            assert path.get("stroke") is None, icon_name
            assert path.get("fill") != "none", icon_name
