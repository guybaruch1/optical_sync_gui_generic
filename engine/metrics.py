"""Live, per-frame-pair sync metrics.

Ported from optical_sync_poc_/pipeline_sync_test_diff.py, restructured
from "run once over the fully recorded arrays after capture finishes"
into incremental versions callable one frame-pair at a time, so the GUI
can plot them live instead of only after a run ends. find_last_on_led and
compute_position_gap already operated per-pair in the original script and
are ported unchanged; the frame-drop check is the one piece rewritten
from a batch np.diff over the whole array into a rolling
previous-timestamp comparison.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np


@dataclass
class FramePairSample:
    pair_index: int
    stream_a_ts_us: float
    stream_b_ts_us: float
    stream_a_bright: "np.ndarray | None" = None
    stream_b_bright: "np.ndarray | None" = None


@dataclass
class MetricResult:
    name: str
    value: "float | None"
    excluded: bool
    exclude_reason: "str | None" = None
    extra: "dict | None" = None


class Metric(ABC):
    name: str

    @abstractmethod
    def update(self, sample: FramePairSample) -> MetricResult:
        raise NotImplementedError


def find_last_on_led(on):
    n = len(on)
    idx = np.where(on)[0]
    if len(idx) == 0:
        return None, 0

    runs = []
    start = idx[0]
    prev = idx[0]
    for i in idx[1:]:
        if i == prev + 1:
            prev = i
        else:
            runs.append((start, prev))
            start = i
            prev = i
    runs.append((start, prev))

    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][1] == n - 1:
        first = runs[0]
        last = runs[-1]
        middle = runs[1:-1]
        wrap_len = (first[1] - first[0] + 1) + (last[1] - last[0] + 1)
        candidates = middle + [("wrap", last[0], first[1], wrap_len)]
    else:
        candidates = runs

    best = None
    best_len = -1
    for r in candidates:
        if r[0] == "wrap":
            _, last_start, first_end, length = r
            if length > best_len:
                best_len = length
                best = ("wrap", last_start, first_end)
        else:
            s, e = r
            length = e - s + 1
            if length > best_len:
                best_len = length
                best = ("plain", s, e)

    if best[0] == "wrap":
        _, _, first_end = best
        return int(first_end), best_len
    else:
        _, s, e = best
        return int(e), best_len


def compute_position_gap(stream_a_last, stream_b_last, n):
    diff = stream_a_last - stream_b_last
    half = n / 2.0
    if diff > half:
        diff -= n
    elif diff <= -half:
        diff += n
    return diff


class PairingGapMetric(Metric):
    name = "pairing_gap_us"

    def __init__(self, outlier_threshold_us):
        self.outlier_threshold_us = outlier_threshold_us

    def update(self, sample: FramePairSample) -> MetricResult:
        gap = sample.stream_a_ts_us - sample.stream_b_ts_us
        excluded = abs(gap) > self.outlier_threshold_us
        return MetricResult(
            name=self.name,
            value=gap,
            excluded=excluded,
            exclude_reason="syncer_outlier" if excluded else None,
        )


def _is_frame_drop(prev_ts, curr_ts, fps, threshold_factor):
    if prev_ts is None:
        return False
    if fps <= 0:
        # Shouldn't happen with real hardware-reported fps, but a stray 0/negative
        # value would otherwise divide-by-zero here instead of just reporting
        # "can't tell" for this pair.
        return False
    delta = curr_ts - prev_ts
    expected_delta = 1_000_000.0 / fps
    return delta < 0 or delta > expected_delta * threshold_factor


class PositionGapMetric(Metric):
    name = "position_gap_ms"

    def __init__(self, stream_a_threshold, stream_b_threshold, num_leds, switch_time_ms,
                 stream_a_fps, stream_b_fps, frame_drop_threshold_factor, warmup_pairs_to_skip):
        self.stream_a_threshold = stream_a_threshold
        self.stream_b_threshold = stream_b_threshold
        self.num_leds = num_leds
        self.switch_time_ms = switch_time_ms
        self.stream_a_fps = stream_a_fps
        self.stream_b_fps = stream_b_fps
        self.frame_drop_threshold_factor = frame_drop_threshold_factor
        self.warmup_pairs_to_skip = warmup_pairs_to_skip
        self._prev_stream_a_ts = None
        self._prev_stream_b_ts = None
        self._pair_count = 0
        # Per-LED on/off classification from the most recent update() call that
        # actually had brightness data - a side channel read directly by
        # gui/pages/live_session_page.py to build LED on/off debug snapshots.
        # Deliberately NOT part of MetricResult.extra: that dict gets folded
        # into the CSV row, and these are full per-LED boolean arrays, not
        # CSV-sized scalars.
        self.last_stream_a_on_mask = None
        self.last_stream_b_on_mask = None

    def update(self, sample: FramePairSample) -> MetricResult:
        stream_a_drop = _is_frame_drop(self._prev_stream_a_ts, sample.stream_a_ts_us, self.stream_a_fps, self.frame_drop_threshold_factor)
        stream_b_drop = _is_frame_drop(self._prev_stream_b_ts, sample.stream_b_ts_us, self.stream_b_fps, self.frame_drop_threshold_factor)
        self._prev_stream_a_ts = sample.stream_a_ts_us
        self._prev_stream_b_ts = sample.stream_b_ts_us
        self._pair_count += 1
        is_warmup = self._pair_count <= self.warmup_pairs_to_skip
        drop_extra = {"stream_a_frame_drop": stream_a_drop, "stream_b_frame_drop": stream_b_drop}

        if sample.stream_a_bright is None or sample.stream_b_bright is None:
            return MetricResult(name=self.name, value=None, excluded=True, exclude_reason="no_led_data", extra=drop_extra)

        stream_a_on = sample.stream_a_bright > self.stream_a_threshold
        stream_b_on = sample.stream_b_bright > self.stream_b_threshold
        self.last_stream_a_on_mask = stream_a_on
        self.last_stream_b_on_mask = stream_b_on
        stream_a_last, _ = find_last_on_led(stream_a_on)
        stream_b_last, _ = find_last_on_led(stream_b_on)

        if stream_a_last is None or stream_b_last is None:
            return MetricResult(name=self.name, value=None, excluded=True, exclude_reason="miss", extra=drop_extra)

        diff = compute_position_gap(stream_a_last, stream_b_last, self.num_leds)
        gap_ms = diff * self.switch_time_ms

        if stream_a_drop or stream_b_drop:
            return MetricResult(name=self.name, value=gap_ms, excluded=True, exclude_reason="frame_drop", extra=drop_extra)
        if is_warmup:
            return MetricResult(name=self.name, value=gap_ms, excluded=True, exclude_reason="warmup", extra=drop_extra)
        return MetricResult(name=self.name, value=gap_ms, excluded=False, exclude_reason=None, extra=drop_extra)
