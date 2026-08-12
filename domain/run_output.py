"""Mints a fresh, timestamped output subfolder for one run (one Calibration
page visit, one Live Session Start click), so a new run never overwrites a
previous run's CSVs/graphs/debug images the way one flat output/ directory
with fixed filenames used to."""

import datetime
import os


def create_run_dir(output_root, kind, now=None, suffix=None):
    """Create and return output_root/{kind}_{timestamp}[_{suffix}]. `now` is
    an injected datetime (defaults to datetime.datetime.now()) so tests can
    assert the exact folder name without monkeypatching the datetime
    module. Timestamp format is Windows-filesystem-safe (no colons). If the
    exact folder already exists (two runs within the same second), appends
    _2, _3, ... rather than silently reusing/overwriting it - numbering
    always goes after the whole name, suffix included.

    suffix defaults to None (no change to the filename shape) so
    gui/pages/calibration_page.py's own create_run_dir(output_root,
    "calibration") call is unaffected - only
    gui/pages/live_session_page.py's start_session() passes one, built by
    build_live_session_config_suffix below."""
    if now is None:
        now = datetime.datetime.now()
    base_name = "{}_{}".format(kind, now.strftime("%Y-%m-%d_%H-%M-%S"))
    if suffix:
        base_name = "{}_{}".format(base_name, suffix)
    run_dir = os.path.join(output_root, base_name)
    collision_suffix = 2
    while os.path.exists(run_dir):
        run_dir = os.path.join(output_root, "{}_{}".format(base_name, collision_suffix))
        collision_suffix += 1
    os.makedirs(run_dir)
    return run_dir


def _format_number(value):
    # Whole-valued floats (e.g. switch_time_ms=1.0) print as "1", not "1.0" -
    # but a real fraction (0.5) still prints in full. int/None pass through
    # str() unchanged.
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def build_live_session_config_suffix(width, height, fps, duration_s, auto_exposure, exposure_a, exposure_b,
                                      display_stride, switch_time_ms):
    """Builds the descriptive suffix create_run_dir appends after the
    timestamp for a Live Session run - lets the operator identify a run's
    camera/test configuration from its output folder name alone, without
    opening its CSV. See gui/pages/live_session_page.py's start_session(),
    the only caller.

    Resolution/fps are the caller's stream_a pick's own width/height/fps -
    every settings.yaml camera.stream_options sensor_options entry pairs
    stream_a/stream_b with matching width/height/fps, so stream_a's values
    represent the run. duration_s=None (the toolbar's "0 = unlimited"
    convention) renders as "unlimited". Manual exposure includes both
    per-stream values (engine.streams.exposure_for_group - different
    sensors can genuinely need different exposure now), not gain, by
    explicit choice - keeps the name shorter."""
    resolution = "{}x{}".format(width, height)
    fps_part = "{}fps".format(_format_number(fps))
    duration_part = "unlimited" if duration_s is None else "{}s".format(_format_number(duration_s))
    exposure_part = (
        "auto" if auto_exposure
        else "manualA{}B{}".format(_format_number(exposure_a), _format_number(exposure_b))
    )
    interval_part = "interval{}".format(display_stride)
    switch_part = "switch{}ms".format(_format_number(switch_time_ms))
    return "_".join([resolution, fps_part, duration_part, exposure_part, interval_part, switch_part])
