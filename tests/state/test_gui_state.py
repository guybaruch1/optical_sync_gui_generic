from state.gui_state import GuiState, load_gui_state, save_gui_state


def test_load_gui_state_missing_file_returns_defaults(tmp_path):
    path = tmp_path / "gui_state.json"
    state = load_gui_state(str(path))
    assert state == GuiState()


def test_load_gui_state_ignores_corrupt_file(tmp_path):
    path = tmp_path / "gui_state.json"
    path.write_text("{not valid json")
    state = load_gui_state(str(path))
    assert state == GuiState()


def test_save_then_load_round_trips_stream_picks_and_camera_controls(tmp_path):
    path = tmp_path / "gui_state.json"
    original = GuiState(
        device_serial="123456",
        stream_a_type="infrared", stream_a_index=1, stream_a_width=1280, stream_a_height=720,
        stream_a_fps=30, stream_a_roi=[10, 20, 100, 100],
        stream_a_emitter_enabled=False, stream_a_auto_exposure=True, stream_a_exposure=None, stream_a_gain=None,
        stream_b_type="color", stream_b_index=0, stream_b_width=1280, stream_b_height=720,
        stream_b_fps=30, stream_b_roi=[5, 15, 90, 90],
        stream_b_emitter_enabled=None, stream_b_auto_exposure=False, stream_b_exposure=150, stream_b_gain=16,
    )
    save_gui_state(original, str(path))
    loaded = load_gui_state(str(path))
    assert loaded == original
