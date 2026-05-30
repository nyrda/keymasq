import re
from importlib import resources
from xml.etree import ElementTree

import pytest

_STYLE_RULE_PATTERN = re.compile(r"([^{}]+)\{([^{}]*)\}")
_SIMPLE_SELECTOR_PATTERN = re.compile(
    r"^(?P<tag>[a-zA-Z][\w-]*)?(?P<id>#[\w-]+)?(?P<classes>(?:\.[\w-]+)*)$"
)


def _style_declarations(style: str) -> dict[str, str]:
    declarations = {}
    for declaration in style.split(";"):
        name, separator, value = declaration.partition(":")
        if separator:
            declarations[name.strip().lower()] = value.strip().lower()
    return declarations


def _local_name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _stylesheet_rules(
    root: ElementTree.Element,
) -> list[tuple[str, dict[str, str]]]:
    rules = []
    for element in root.iter():
        if _local_name(element) != "style" or not element.text:
            continue
        for selectors, declarations in _STYLE_RULE_PATTERN.findall(element.text):
            style = _style_declarations(declarations)
            for selector in selectors.split(","):
                selector = selector.strip()
                if selector:
                    rules.append((selector, style))
    return rules


def _simple_selector_matches(element: ElementTree.Element, selector: str) -> bool:
    match = _SIMPLE_SELECTOR_PATTERN.match(selector)
    assert match is not None, f"Unsupported selector: {selector}"

    tag = match.group("tag")
    if tag and tag.lower() != _local_name(element).lower():
        return False

    selector_id = match.group("id")
    if selector_id and element.get("id") != selector_id[1:]:
        return False

    classes = {name for name in element.get("class", "").split() if name}
    required_classes = {
        class_name for class_name in match.group("classes").split(".") if class_name
    }
    return required_classes <= classes


def _selector_matches(
    element: ElementTree.Element,
    selector: str,
    ancestors: tuple[ElementTree.Element, ...] = (),
) -> bool:
    selector_parts = selector.split()
    assert selector_parts, f"Unsupported selector: {selector}"
    if not _simple_selector_matches(element, selector_parts[-1]):
        return False

    remaining_ancestors = list(ancestors)
    for ancestor_selector in reversed(selector_parts[:-1]):
        while remaining_ancestors:
            ancestor = remaining_ancestors.pop(0)
            if _simple_selector_matches(ancestor, ancestor_selector):
                break
        else:
            return False
    return True


def _assert_symbolic_icon_uses_filled_paths(
    root: ElementTree.Element,
    icon_name: str,
) -> None:
    rules = _stylesheet_rules(root)
    parent_map = {child: parent for parent in root.iter() for child in parent}
    for element in root.iter():
        ancestors = []
        parent = parent_map.get(element)
        while parent is not None:
            ancestors.append(parent)
            parent = parent_map.get(parent)
        style = {}
        for selector, declarations in rules:
            if _selector_matches(element, selector, tuple(ancestors)):
                style.update(declarations)
        style.update(_style_declarations(element.get("style", "")))
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
        '<svg><style>.outline path { fill: none; stroke: #000; }</style>'
        '<g class="outline"><path d="M0 0h1v1z" /></g></svg>',
    ),
)
def test_symbolic_icon_styles_reject_stroked_empty_paths(svg: str) -> None:
    root = ElementTree.fromstring(svg)

    with pytest.raises(AssertionError) as exc_info:
        _assert_symbolic_icon_uses_filled_paths(root, "inline-style.svg")
    assert "inline-style.svg" in str(exc_info.value)
    assert "Unsupported selector" not in str(exc_info.value)


def test_symbolic_icon_styles_ignore_unused_rules() -> None:
    root = ElementTree.fromstring(
        '<svg><style>.outline { fill: none; stroke: #000; }</style>'
        '<path d="M0 0h1v1z" /></svg>'
    )

    _assert_symbolic_icon_uses_filled_paths(root, "unused-rule.svg")
