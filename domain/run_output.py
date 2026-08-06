"""Mints a fresh, timestamped output subfolder for one run (one Calibration
page visit, one Live Session Start click), so a new run never overwrites a
previous run's CSVs/graphs/debug images the way one flat output/ directory
with fixed filenames used to."""

import datetime
import os


def create_run_dir(output_root, kind, now=None):
    """Create and return output_root/{kind}_{timestamp}. `now` is an
    injected datetime (defaults to datetime.datetime.now()) so tests can
    assert the exact folder name without monkeypatching the datetime
    module. Timestamp format is Windows-filesystem-safe (no colons). If the
    exact folder already exists (two runs within the same second), appends
    _2, _3, ... rather than silently reusing/overwriting it."""
    if now is None:
        now = datetime.datetime.now()
    base_name = "{}_{}".format(kind, now.strftime("%Y-%m-%d_%H-%M-%S"))
    run_dir = os.path.join(output_root, base_name)
    suffix = 2
    while os.path.exists(run_dir):
        run_dir = os.path.join(output_root, "{}_{}".format(base_name, suffix))
        suffix += 1
    os.makedirs(run_dir)
    return run_dir
