from __future__ import annotations

import ast
import subprocess
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parents[1]
_GALLERY = _PROJECT_ROOT / "nix/appimage-brotway-integration-test/icon_gallery.py"
_RUNNER = _PROJECT_ROOT / "nix/appimage-brotway-integration-test/run_icon_gallery.sh"
_APPIMAGE_ICON_MANIFEST = _PROJECT_ROOT / "packaging/appimage/assets/gui-icon-names.txt"
_ICON_ARGUMENT_CALLS = frozenset(
    {
        "_make_button_content",
        "_make_icon_label_box",
        "new_from_icon_name",
        "set_icon_name",
    }
)
_ICON_VARIADIC_CALLS = frozenset({"image_from_icon_names", "resolve_icon_name"})
_ICON_DATA_CONSTANTS = frozenset({"MEDIA_KEY_GROUPS", "MPRIS_MEDIA_GROUPS", "SYSTEM_KEY_GROUPS"})


def _gallery_constant(name: str) -> tuple[str, ...]:
    module = ast.parse(_GALLERY.read_text(encoding="utf-8"))
    for statement in module.body:
        if isinstance(statement, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == name for target in statement.targets
            ):
                value = ast.literal_eval(statement.value)
                assert isinstance(value, tuple)
                return value
    raise AssertionError(f"{name} is not defined in {_GALLERY}")


def _symbolic_literals(node: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and child.value.endswith("-symbolic")
    }


def _icon_usage_literals(module: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Call):
            call_name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            for keyword in node.keywords:
                if keyword.arg == "icon_name":
                    names.update(_symbolic_literals(keyword.value))
            if call_name in _ICON_ARGUMENT_CALLS and node.args:
                names.update(_symbolic_literals(node.args[0]))
            elif call_name in _ICON_VARIADIC_CALLS:
                for argument in node.args:
                    names.update(_symbolic_literals(argument))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            if any(
                isinstance(target, ast.Name) and target.id in _ICON_DATA_CONSTANTS
                for target in targets
            ):
                names.update(_symbolic_literals(node.value))
    return names


def test_icon_gallery_includes_every_symbolic_icon_usage() -> None:
    gallery_names = set(_gallery_constant("ICON_NAMES"))
    used_names: set[str] = set()
    for source in (_PROJECT_ROOT / "keymasq").rglob("*.py"):
        module = ast.parse(source.read_text(encoding="utf-8"))
        used_names.update(_icon_usage_literals(module))

    assert used_names <= gallery_names


def test_icon_gallery_inventory_ignores_unrelated_symbolic_strings() -> None:
    module = ast.parse('ERROR_CODE = "not-an-icon-symbolic"')

    assert _icon_usage_literals(module) == set()


def test_icon_gallery_inventory_is_unique_and_sorted() -> None:
    names = _gallery_constant("ICON_NAMES")
    assert names == tuple(sorted(set(names)))


def test_icon_gallery_matches_appimage_icon_manifest() -> None:
    manifest_names = tuple(_APPIMAGE_ICON_MANIFEST.read_text(encoding="utf-8").splitlines())

    assert manifest_names == _gallery_constant("ICON_NAMES")


def test_icon_gallery_includes_every_device_icon_fallback() -> None:
    gallery_names = set(_gallery_constant("ICON_NAMES"))
    icons_module = ast.parse((_PROJECT_ROOT / "keymasq/gui/icons.py").read_text(encoding="utf-8"))
    fallback_names: set[str] = set()
    for statement in icons_module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id.endswith("_ICON_NAMES")
            for target in statement.targets
        ):
            continue
        values = ast.literal_eval(statement.value)
        fallback_names.update(values)

    assert fallback_names <= gallery_names


def test_icon_gallery_checks_every_non_theme_image_asset() -> None:
    gallery_assets = set(_gallery_constant("IMAGE_ASSETS"))
    asset_dir = _PROJECT_ROOT / "keymasq/gui/assets"
    non_theme_assets = {
        path.stem
        for path in asset_dir.iterdir()
        if path.suffix == ".svg" and not path.name.endswith("-symbolic.svg")
    }

    assert gallery_assets == non_theme_assets


def test_icon_gallery_includes_application_icon() -> None:
    assert "tools.keymasq.keymasq" in _gallery_constant("ICON_NAMES")


def test_icon_gallery_runner_is_valid_posix_shell() -> None:
    subprocess.run(["sh", "-n", _RUNNER], check=True)
