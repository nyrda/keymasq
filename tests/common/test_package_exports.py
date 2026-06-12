import pytest


def test_common_package_lazy_loads_explicit_submodule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keymasq.common as common

    monkeypatch.delattr(common, "paths", raising=False)
    imports: list[str] = []
    real_import_module = common.import_module

    def import_module(module_name: str):
        imports.append(module_name)
        return real_import_module(module_name)

    monkeypatch.setattr(common, "import_module", import_module)

    assert common.paths.__name__ == "keymasq.common.paths"
    assert imports == ["keymasq.common.paths"]


def test_common_package_missing_attribute_does_not_scan_submodules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keymasq.common as common

    monkeypatch.delattr(common, "Command", raising=False)
    imports: list[str] = []
    monkeypatch.setattr(common, "import_module", lambda name: imports.append(name))

    with pytest.raises(AttributeError):
        common.__getattr__("Command")

    assert imports == []
