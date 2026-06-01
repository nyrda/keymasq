from keymasq.gui.widgets.docs_links import actions_docs_url, docs_page_url, docs_version


def test_docs_version_normalizes_empty_dev_and_release_versions() -> None:
    assert docs_version("") == "master"
    assert docs_version("1.2.3.dev1") == "master"
    assert docs_version("1.2.3") == "v1.2.3"
    assert docs_version("v1.2.3") == "v1.2.3"


def test_docs_page_urls_use_shared_versioned_base() -> None:
    assert docs_page_url("PERFORMANCE", anchor="diagnostics-labels", version="1.2.3") == (
        "https://keymasq.tools/docs/v1.2.3/PERFORMANCE/#diagnostics-labels"
    )
    assert docs_page_url("MACROS", anchor="live-recording", version="") == (
        "https://keymasq.tools/docs/master/MACROS/#live-recording"
    )
    assert actions_docs_url("media", version="1.2.3") == (
        "https://keymasq.tools/docs/v1.2.3/ACTIONS/#media"
    )
    assert actions_docs_url("analog-controls", version="1.2.3") == (
        "https://keymasq.tools/docs/v1.2.3/ANALOG_CONTROLS/"
    )
