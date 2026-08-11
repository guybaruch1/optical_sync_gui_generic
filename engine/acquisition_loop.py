"""Pure-Python frame-pair processing loop.

No Qt, no pyrealsense2 - this is the piece that used to be
pipeline_sync_test_diff.py's run_pipeline_capture, restructured so it can
be unit-tested with a fake frame_source and so the real hardware/Qt
wiring (engine.session_engine) stays a thin adapter around it.
"""

from dataclasses import dataclass

from engine.metrics import FramePairSample
from engine.test_session import TestSession


@dataclass
class AcquisitionCallbacks:
    on_frames: callable
    on_row: callable
    on_stats: callable
    on_frame_pair: "callable | None" = None


class AcquisitionLoop:
    def __init__(self, frame_source, test_session: TestSession, callbacks: AcquisitionCallbacks, display_stride: int = 10):
        self.frame_source = frame_source
        self.test_session = test_session
        self.callbacks = callbacks
        self.display_stride = display_stride

    def run_until_stopped(self, is_stop_requested, elapsed_s_fn) -> "list[dict]":
        pair_index = 0
        for stream_a_image, stream_b_image, stream_a_ts_us, stream_b_ts_us, stream_a_bright, stream_b_bright in self.frame_source:
            if is_stop_requested():
                break
            if self.test_session.should_auto_stop(elapsed_s_fn()):
                break

            sample = FramePairSample(
                pair_index=pair_index,
                stream_a_ts_us=stream_a_ts_us,
                stream_b_ts_us=stream_b_ts_us,
                stream_a_bright=stream_a_bright,
                stream_b_bright=stream_b_bright,
            )
            row = self.test_session.process_pair(sample)
            self.callbacks.on_row(row)
            if self.callbacks.on_frame_pair is not None:
                self.callbacks.on_frame_pair(stream_a_image, stream_b_image, row)

            if pair_index % self.display_stride == 0:
                self.callbacks.on_frames(stream_a_image, stream_b_image, pair_index)
                self.callbacks.on_stats(row)

            pair_index += 1

        return self.test_session.stop()
