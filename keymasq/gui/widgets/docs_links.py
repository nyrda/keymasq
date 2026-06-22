from keymasq import __version__

_DOCS_BASE_URL = "https://keymasq.tools/docs"


def docs_version(version: str | None = None) -> str:
    normalized = (__version__ if version is None else version).strip()
    if not normalized or "dev" in normalized:
        return "master"
    return f"v{normalized.removeprefix('v')}"


def docs_page_url(
    page: str = "",
    *,
    anchor: str | None = None,
    version: str | None = None,
) -> str:
    page_path = page.strip("/")
    url = f"{_DOCS_BASE_URL}/{docs_version(version)}/"
    if page_path:
        url = f"{url}{page_path}/"
    if anchor:
        url = f"{url}#{anchor}"
    return url


def actions_docs_url(anchor: str, *, version: str | None = None) -> str:
    if anchor == "analog-controls":
        return docs_page_url("ANALOG_CONTROLS", version=version)
    if anchor == "type-macro-inline-controls":
        return docs_page_url("MACROS", anchor=anchor, version=version)
    return docs_page_url("ACTIONS", anchor=anchor, version=version)
