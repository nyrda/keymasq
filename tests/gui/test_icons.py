from pathlib import Path
from types import SimpleNamespace

from keymasq.gui import icons


class _Settings:
    def __init__(self) -> None:
        self.properties: dict[str, str] = {}

    def set_property(self, name: str, value: str) -> None:
        self.properties[name] = value


class _IconTheme:
    def __init__(self) -> None:
        self.search_paths: list[str] = []

    def add_search_path(self, path: str) -> None:
        self.search_paths.append(path)


def _mock_gtk(monkeypatch) -> tuple[_Settings, _IconTheme]:
    display = object()
    settings = _Settings()
    theme = _IconTheme()
    monkeypatch.setattr(
        icons,
        "Gdk",
        SimpleNamespace(Display=SimpleNamespace(get_default=lambda: display)),
    )
    monkeypatch.setattr(
        icons,
        "Gtk",
        SimpleNamespace(
            Settings=SimpleNamespace(get_for_display=lambda candidate: settings),
            IconTheme=SimpleNamespace(get_for_display=lambda candidate: theme),
        ),
    )
    return settings, theme


def _write_appimage_icon_theme(appdir: Path, names: tuple[str, ...]) -> None:
    theme_root = appdir / "share/icons/Keymasq"
    icon_dir = theme_root / "48x48/apps"
    icon_dir.mkdir(parents=True)
    (theme_root / "index.theme").write_text("[Icon Theme]\n", encoding="utf-8")
    manifest = appdir / "share/keymasq/appimage/gui-icon-names.txt"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("\n".join(names) + "\n", encoding="utf-8")
    for name in names:
        filename = f"{name}.png" if name == "tools.keymasq.keymasq" else f"{name}.symbolic.png"
        (icon_dir / filename).touch()


def test_register_icon_search_path_selects_private_appimage_theme(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings, theme = _mock_gtk(monkeypatch)
    _write_appimage_icon_theme(
        tmp_path,
        ("list-add-symbolic", "tools.keymasq.keymasq"),
    )
    monkeypatch.setenv("APPDIR", str(tmp_path))

    icons.register_icon_search_path()

    assert settings.properties == {"gtk-icon-theme-name": "Keymasq"}
    assert theme.search_paths == [str(Path(icons.__file__).parent / "assets")]


def test_register_icon_search_path_rejects_partial_appimage_theme(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings, theme = _mock_gtk(monkeypatch)
    _write_appimage_icon_theme(tmp_path, ("list-add-symbolic", "edit-delete-symbolic"))
    (tmp_path / "share/icons/Keymasq/48x48/apps/edit-delete-symbolic.symbolic.png").unlink()
    monkeypatch.setenv("APPDIR", str(tmp_path))

    icons.register_icon_search_path()

    assert settings.properties == {}
    assert theme.search_paths == [str(Path(icons.__file__).parent / "assets")]


def test_register_icon_search_path_does_not_force_theme_outside_appimage(
    monkeypatch,
) -> None:
    settings, theme = _mock_gtk(monkeypatch)
    monkeypatch.delenv("APPDIR", raising=False)

    icons.register_icon_search_path()

    assert settings.properties == {}
    assert theme.search_paths == [str(Path(icons.__file__).parent / "assets")]
