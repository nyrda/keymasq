import re
from importlib import resources
from xml.etree import ElementTree

import pytest

_STYLE_RULE_PATTERN = re.compile(r"\{([^{}]*)\}")


def _style_declarations(style: str) -> dict[str, str]:
    declarations = {}
    for rule in _STYLE_RULE_PATTERN.findall(style) or [style]:
        for declaration in rule.split(";"):
            name, separator, value = declaration.partition(":")
            if separator:
                declarations[name.strip().lower()] = value.strip().lower()
    return declarations


def _assert_symbolic_icon_uses_filled_paths(
    root: ElementTree.Element,
    icon_name: str,
) -> None:
    for element in root.iter():
        style = _style_declarations(element.get("style", ""))
        if element.tag.rsplit("}", 1)[-1] == "style" and element.text:
            style.update(_style_declarations(element.text))
        assert element.get("stroke") is None, icon_name
        assert "stroke" not in style, icon_name
        assert element.get("fill") != "none", icon_name
        assert style.get("fill") != "none", icon_name


def test_custom_symbolic_icons_use_filled_paths() -> None:
    assets = resources.files("keymasq").joinpath("gui/assets")

    for icon_name in (
        "keymasq-combos-symbolic.svg",
        "keymasq-keyboard-symbolic.svg",
        "keymasq-mouse-symbolic.svg",
    ):
        root = ElementTree.parse(assets.joinpath(icon_name)).getroot()
        _assert_symbolic_icon_uses_filled_paths(root, icon_name)


@pytest.mark.parametrize(
    "svg",
    (
        '<svg><path d="M0 0h1v1z" style="fill:none; stroke: #000" /></svg>',
        '<svg><style>.outline { fill: none; stroke: #000; }</style>'
        '<path class="outline" d="M0 0h1v1z" /></svg>',
    ),
)
def test_symbolic_icon_styles_reject_stroked_empty_paths(svg: str) -> None:
    root = ElementTree.fromstring(svg)

    with pytest.raises(AssertionError):
        _assert_symbolic_icon_uses_filled_paths(root, "inline-style.svg")
