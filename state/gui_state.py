"""The GUI's own persisted state - last device/stream/ROI choices.

Deliberately separate from settings.yaml (optical_sync_poc_'s hand-edited,
comment-preserving reference file, which the GUI must never overwrite -
see the design doc's "Settings persistence" decision). This file is
plain, disposable, machine-written JSON.
"""

import json
import dataclasses
from dataclasses import dataclass


@dataclass
class GuiState:
    device_serial: "str | None" = None
    stream_a_type: "str | None" = None
    stream_a_index: "int | None" = None
    stream_a_width: "int | None" = None
    stream_a_height: "int | None" = None
    stream_a_fps: "int | None" = None
    stream_a_roi: "list[int] | None" = None
    stream_a_emitter_enabled: "bool | None" = None
    stream_a_auto_exposure: "bool | None" = None
    stream_a_exposure: "int | None" = None
    stream_a_gain: "int | None" = None
    stream_b_type: "str | None" = None
    stream_b_index: "int | None" = None
    stream_b_width: "int | None" = None
    stream_b_height: "int | None" = None
    stream_b_fps: "int | None" = None
    stream_b_roi: "list[int] | None" = None
    stream_b_emitter_enabled: "bool | None" = None
    stream_b_auto_exposure: "bool | None" = None
    stream_b_exposure: "int | None" = None
    stream_b_gain: "int | None" = None


def load_gui_state(path="gui_state.json"):
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return GuiState()

    known_fields = {f.name for f in dataclasses.fields(GuiState)}
    filtered = {k: v for k, v in data.items() if k in known_fields}
    return GuiState(**filtered)


def save_gui_state(state, path="gui_state.json"):
    with open(path, "w") as f:
        json.dump(dataclasses.asdict(state), f, indent=2)
