from state.gui_state import GuiState, load_gui_state, save_gui_state


def test_load_gui_state_missing_file_returns_defaults(tmp_path):
    path = tmp_path / "gui_state.json"
    state = load_gui_state(str(path))
    assert state == GuiState()


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "gui_state.json"
    original = GuiState(
        device_serial="123456",
        ir_fps=30, ir_width=1280, ir_height=720,
        rgb_fps=30, rgb_width=1280, rgb_height=720,
        ir_roi=[10, 20, 100, 100], rgb_roi=[5, 15, 90, 90],
    )
    save_gui_state(original, str(path))
    loaded = load_gui_state(str(path))
    assert loaded == original


def test_load_gui_state_ignores_corrupt_file(tmp_path):
    path = tmp_path / "gui_state.json"
    path.write_text("{not valid json")
    state = load_gui_state(str(path))
    assert state == GuiState()
