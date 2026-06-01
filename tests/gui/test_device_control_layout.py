from keymasq.gui.widgets.device_control_layout import label_sort_key


def test_label_sort_key_orders_trailing_numbers_numerically() -> None:
    labels = ["Extra Button 1", "Extra Button 10", "Extra Button 2", "Back"]

    assert sorted(labels, key=label_sort_key) == [
        "Back",
        "Extra Button 1",
        "Extra Button 2",
        "Extra Button 10",
    ]
