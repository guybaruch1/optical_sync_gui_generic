"""Owns start/stop/duration for one live sync-test run, and buffers the
rows that eventually become the CSV (domain.csv_export.export_session_csvs).

Deliberately has no idea about Qt or real hardware - engine.acquisition_loop
feeds it FramePairSample objects; engine.session_engine (the QThread
wrapper) is what actually drives that loop against real sensors.
"""

from dataclasses import dataclass, field

from engine.metrics import Metric, FramePairSample, _is_frame_drop


@dataclass
class TestSessionConfig:
    __test__ = False
    metrics: "list[Metric]" = field(default_factory=list)
    duration_s: "float | None" = None
    stream_a_fps: "float | None" = None
    stream_b_fps: "float | None" = None
    frame_drop_threshold_factor: "float | None" = None


class TestSession:
    __test__ = False

    def __init__(self, config: TestSessionConfig):
        self.config = config
        self.is_running = False
        self._rows = []
        self._prev_stream_a_ts = None
        self._prev_stream_b_ts = None

    def start(self):
        self._rows = []
        self.is_running = True

    def process_pair(self, sample: FramePairSample) -> dict:
        stream_a_drop = _is_frame_drop(
            self._prev_stream_a_ts, sample.stream_a_ts_us,
            self.config.stream_a_fps, self.config.frame_drop_threshold_factor,
        )
        stream_b_drop = _is_frame_drop(
            self._prev_stream_b_ts, sample.stream_b_ts_us,
            self.config.stream_b_fps, self.config.frame_drop_threshold_factor,
        )
        self._prev_stream_a_ts = sample.stream_a_ts_us
        self._prev_stream_b_ts = sample.stream_b_ts_us
        sample.stream_a_frame_drop = stream_a_drop
        sample.stream_b_frame_drop = stream_b_drop

        row = {
            "pair_index": sample.pair_index,
            "stream_a_ts_us": sample.stream_a_ts_us,
            "stream_b_ts_us": sample.stream_b_ts_us,
            "stream_a_frame_drop": stream_a_drop,
            "stream_b_frame_drop": stream_b_drop,
        }
        for metric in self.config.metrics:
            result = metric.update(sample)
            row[result.name] = result.value
            row[f"{result.name}_excluded"] = result.excluded
            row[f"{result.name}_exclude_reason"] = result.exclude_reason
            if result.extra:
                row.update(result.extra)
        self._rows.append(row)
        return row

    def should_auto_stop(self, elapsed_s: float) -> bool:
        if self.config.duration_s is None:
            return False
        return elapsed_s >= self.config.duration_s

    def stop(self) -> "list[dict]":
        self.is_running = False
        return self._rows
