from gui.widgets.stats_panel import StatsPanel


def test_add_field_and_set_value_updates_label_text(qapp):
    panel = StatsPanel()
    panel.add_field("frame_index", "Frame Index")
    panel.set_value("frame_index", 42)
    assert "42" in panel._value_labels["frame_index"].text()


def test_set_value_on_unregistered_key_is_ignored(qapp):
    panel = StatsPanel()
    panel.set_value("nonexistent", 123)  # must not raise


def test_multiple_fields_are_independent(qapp):
    panel = StatsPanel()
    panel.add_field("pairing_gap_us", "Pairing Gap (us)")
    panel.add_field("switch_time_ms", "Switch Time (ms)")
    panel.set_value("pairing_gap_us", -12.5)
    panel.set_value("switch_time_ms", 1.0)
    assert "-12.5" in panel._value_labels["pairing_gap_us"].text()
    assert "1.0" in panel._value_labels["switch_time_ms"].text()


def test_add_section_header_does_not_raise_and_is_a_separate_widget(qapp):
    panel = StatsPanel()
    panel.add_section_header("Live Data")
    panel.add_field("frame_index", "Frame Index")
    panel.add_section_header("Stats")
    panel.add_stats_table([("hw_ts_latency", "HW TS Latency")])
    # 2 headers + 1 field tile + 1 table widget = 4 top-level items in the layout
    assert panel._layout.count() == 4


def test_add_stats_table_registers_min_avg_std_max_per_row(qapp):
    panel = StatsPanel()
    panel.add_stats_table([
        ("hw_ts_latency", "HW TS Latency"),
        ("optical_sync", "Optical Sync"),
    ])
    for key in ("hw_ts_latency", "optical_sync"):
        for column in ("min", "avg", "std", "max"):
            full_key = "{}_{}".format(key, column)
            assert panel._value_labels[full_key].text() == "-"

    panel.set_value("hw_ts_latency_min", -40.0)
    panel.set_value("optical_sync_max", 2.0)
    assert panel._value_labels["hw_ts_latency_min"].text() == "-40.0"
    assert panel._value_labels["optical_sync_max"].text() == "2.0"
    # Rows are independent - setting one row's cell must not affect another's.
    assert panel._value_labels["hw_ts_latency_max"].text() == "-"
