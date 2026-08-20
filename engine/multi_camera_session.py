"""Orchestrates up to 3 cameras' worth of Live Session runs together for the
multi-camera sync test - master/slave genlock role assignment, one
completely unmodified engine.session_engine.SessionEngineThread per camera
(same constructor call LiveSessionPage.start_session() makes today for a
single camera), and a shared engine.cross_camera_reconciler.
CrossCameraReconciler feeding the new cross-camera HW TS Latency metric.

Deliberately does NOT reshape SessionEngineThread/ContinuousCapture/
TestSession/AcquisitionLoop/dual_panel_control - each camera's pipeline
stays a byte-for-byte-unchanged, independently-instantiated copy of today's
proven single-camera code; this module only owns starting/stopping N of
them together and relaying their signals, tagged by camera_id, plus feeding
row_ready into the reconciler. See docs/superpowers's multi-camera design
doc's "Design detail" section 2.

Lives on the GUI thread as a plain QObject, not a QThread itself - it never
touches hardware directly except the two injectable collaborators
(device_lookup, sync_setter), both mockable for testing. Actual concurrent
multi-thread hardware behavior is untested by design, same convention as
engine/session_engine.py itself - this module's own tests
(tests/engine/test_multi_camera_session.py) only prove the sequencing/
relaying logic against a fake thread_factory.
"""

import time
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal

from engine.cross_camera_reconciler import CrossCameraReconciler, build_cross_camera_pair_specs
from engine.session_engine import SessionEngineThread
from engine.streams import find_device_by_serial, set_inter_cam_sync_mode, INTER_CAM_SYNC_DEFAULT


@dataclass
class CameraSessionSpec:
    """Everything MultiCameraSessionController needs for one configured
    camera. `thread_kwargs` is passed straight through to the thread
    factory (normally SessionEngineThread's constructor) - ctx and
    device_serial are supplied by the controller itself, and
    hardware_reset_before_start is always forced False there (see
    start_all's docstring for why), so thread_kwargs should carry
    everything else SessionEngineThread's real constructor takes
    (pick_a, pick_b, camera_controls, test_session, stream_a_xy, ...)."""
    camera_id: str
    is_master: bool
    # Raw rs.option.inter_cam_sync_mode value for THIS camera's generation
    # (D400 vs D500-series use different value schemes - see
    # engine.streams.set_inter_cam_sync_mode's own docstring). None skips
    # genlock entirely for this camera (e.g. a lone camera with nothing to
    # sync against).
    inter_cam_sync_value: "int | None"
    # {"stream_a": "infrared1", "stream_b": "color"} - this camera's own
    # engine.streams.stream_slug mapping, for cross-camera identity matching.
    stream_identities: dict
    device_serial: str
    # This camera's own num_leds/switch_time_ms - read by
    # engine.cross_camera_reconciler.build_cross_camera_pair_specs off
    # whichever spec is the designated master, for the cross-camera Optical
    # Sync computation (CrossCameraPairSpec's own docstring: master's config
    # wins, the slave's own values are never read).
    num_leds: int
    switch_time_ms: float
    hardware_reset_before_start: bool = False
    hardware_reset_settle_s: float = 8.0
    thread_kwargs: dict = field(default_factory=dict)


class MultiCameraSessionController(QObject):
    # Every per-camera signal is tagged with camera_id as its first
    # argument, mirroring the per-camera row_ready/stats_ready/etc. shape
    # unchanged from SessionEngineThread - only the camera_id tag is new.
    camera_frame_ready = Signal(str, str, object, int, object)
    camera_row_ready = Signal(str, dict)
    camera_stats_ready = Signal(str, dict)
    camera_session_finished = Signal(str, list)
    camera_error = Signal(str, str)
    # Unthrottled, mirrors row_ready's own cadence - one emission per
    # cross-camera match the reconciler produces.
    cross_pair_ready = Signal(dict)
    # Throttled to piggyback on whichever camera's own stats_ready fires
    # next (mirrors that per-camera cadence rather than a second,
    # independent timer) - carries a snapshot dict of every pair's most
    # recent cross-row, keyed by (slave_camera_id, stream_identity). Signal
    # type is `object`, not `dict` - PySide6's Signal(dict) only round-trips
    # through Qt's C++ QVariantMap for str-keyed dicts; this dict's keys are
    # tuples, so Signal(dict) raises a Shiboken conversion error at emit
    # time. `object` passes the Python dict through untouched, same
    # convention as frame_ready's own `object` fields elsewhere.
    cross_stats_ready = Signal(object)
    # camera_id -> that camera's own buffered rows, fired once every
    # started camera's thread has fully finished (QThread's own `finished`,
    # not session_finished/error - same "wait for the thread's own hardware
    # cleanup to actually complete" reasoning LiveSessionPage already uses
    # for a single camera, generalized to N).
    all_sessions_finished = Signal(dict)

    def __init__(self, camera_specs, pairing_gap_outlier_threshold_us=100_000,
                 thread_factory=None, device_lookup=None, sync_setter=None,
                 camera_start_stagger_s=2.0, parent=None):
        super().__init__(parent)
        self._camera_specs = camera_specs
        self._thread_factory = thread_factory or SessionEngineThread
        self._device_lookup = device_lookup or find_device_by_serial
        self._sync_setter = sync_setter or set_inter_cam_sync_mode
        # Real-hardware finding: two cameras sharing a USB hub/controller
        # (e.g. an Acroname hub) can disrupt each other's device enumeration
        # if their rs.pipeline().start() calls (already documented elsewhere
        # in this codebase as having unpredictable USB-level side effects)
        # happen at nearly the same moment - starting all N threads back-to-
        # back with zero delay hit this every time on the rig this was found
        # on (the second camera's resolve_and_group failed with "no matching
        # profile found... after a reconnect"). This settle delay, applied
        # before starting every camera AFTER the first, gives the previous
        # camera's own noisy open/negotiate window time to finish - same
        # "give the hardware a moment" pattern as hardware_reset_settle_s/
        # hub_switch_settle_s elsewhere in this project. 2.0s is a
        # real-hardware-tunable starting guess, not a proven-correct value -
        # keep raising it if two-camera collisions are still observed.
        self._camera_start_stagger_s = camera_start_stagger_s

        pair_specs = build_cross_camera_pair_specs(camera_specs, pairing_gap_outlier_threshold_us)
        self._reconciler = CrossCameraReconciler(pair_specs) if pair_specs else None
        self._latest_cross_row_by_pair = {}

        self._threads = {}
        self._finished_rows_by_camera = {}
        # Specs whose genlock role was ACTUALLY applied this attempt (never a
        # spec with inter_cam_sync_value None - nothing was ever written to
        # that camera, so nothing needs undoing). Populated by start_all's own
        # role-assignment loop, consumed by _reset_genlock_roles - see that
        # method's docstring for why a role must never be left lingering on a
        # real device past this controller's own use of it.
        self._applied_genlock_specs = []
        self._ctx = None

    @property
    def threads(self):
        return dict(self._threads)

    def match_diagnostics(self):
        """Per-spec cross-camera match/no-match counts - see
        CrossCameraReconciler.match_diagnostics. Empty list when no
        cross-camera pairs exist (no shared stream identities configured,
        or effectively a single-camera rig)."""
        return self._reconciler.match_diagnostics() if self._reconciler is not None else []

    def start_all(self, ctx):
        """1. Hardware-reset every camera that needs it, sequentially - a
        reset drops the device off USB and plausibly clears whatever
        inter_cam_sync_mode was previously set, so it must fully finish
        before ANY role assignment, not race it from inside a camera's own
        thread later. 2. Assign every camera's genlock role, master first,
        all synchronously on this (GUI) thread, all-or-nothing - abort with
        a clear RuntimeError and start NOTHING if any device fails to apply
        its role, rather than a silent partial run (a real error reaching
        the operator beats guessing whether a half-genlocked rig is safe to
        run). 3. Only once every role is confirmed applied, construct and
        start one thread per camera.

        Also enforces: at most one configured camera may use dual-panel
        mode. engine.dual_panel_control's relay/hub singletons
        (_dual_panel_primed, _relay_connection, _dual_panel_lock) represent
        exactly ONE shared relay/hub for the whole app - confirmed real
        wiring on the rig this was designed for is that ALL panels across
        ALL cameras share one relay, so two cameras' threads both calling
        start_scanning()/stop_scanning() concurrently would corrupt each
        other's state. Checked here, before anything else starts, rather
        than left to fail unpredictably mid-run."""
        dual_panel_camera_count = sum(
            1 for spec in self._camera_specs if spec.thread_kwargs.get("dual_panel_config") is not None
        )
        if dual_panel_camera_count > 1:
            raise RuntimeError(
                "{} cameras are configured for dual-panel mode, but this rig's panels all share one "
                "relay - at most one camera may use dual-panel mode per multi-camera run.".format(
                    dual_panel_camera_count
                )
            )

        self._ctx = ctx
        for spec in self._camera_specs:
            if spec.hardware_reset_before_start:
                device = self._device_lookup(ctx, spec.device_serial)
                device.hardware_reset()
                time.sleep(spec.hardware_reset_settle_s)

        self._applied_genlock_specs = []
        ordered_specs = sorted(self._camera_specs, key=lambda spec: not spec.is_master)
        for spec in ordered_specs:
            if spec.inter_cam_sync_value is None:
                continue
            device = self._device_lookup(ctx, spec.device_serial)
            if not self._sync_setter(device, spec.inter_cam_sync_value):
                # An earlier camera in this same attempt may already have had
                # its own role applied successfully - reset it back to
                # default now rather than aborting with that role left
                # lingering on a real device (see _reset_genlock_roles).
                self._reset_genlock_roles()
                raise RuntimeError(
                    "Camera {} does not support inter_cam_sync_mode - cannot "
                    "genlock this rig as configured".format(spec.camera_id)
                )
            self._applied_genlock_specs.append(spec)

        self._finished_rows_by_camera = {}
        for index, spec in enumerate(self._camera_specs):
            # See __init__'s own comment - staggered so each camera's
            # rs.pipeline().start() gets a moment to finish its own noisy
            # USB open/negotiate window before the next camera starts its
            # own, if they share a USB hub/controller. No delay before the
            # very first camera - nothing else is starting concurrently
            # with it yet.
            if index > 0 and self._camera_start_stagger_s > 0:
                time.sleep(self._camera_start_stagger_s)
            thread = self._thread_factory(
                ctx=ctx,
                device_serial=spec.device_serial,
                # Already handled above, sequentially, for every camera that
                # wanted it - a thread redoing this internally could race or
                # undo the genlock role just applied.
                hardware_reset_before_start=False,
                **spec.thread_kwargs,
            )
            self._wire_thread(spec.camera_id, thread)
            self._threads[spec.camera_id] = thread
            thread.start()

    def stop_all(self):
        for thread in self._threads.values():
            thread.request_stop()

    def _wire_thread(self, camera_id, thread):
        thread.frame_ready.connect(
            lambda stream_name, image, pair_index, mask, cid=camera_id:
                self.camera_frame_ready.emit(cid, stream_name, image, pair_index, mask)
        )
        thread.row_ready.connect(lambda row, cid=camera_id: self._on_row_ready(cid, row))
        thread.stats_ready.connect(lambda row, cid=camera_id: self._on_stats_ready(cid, row))
        thread.session_finished.connect(
            lambda rows, cid=camera_id: self._on_session_finished_rows(cid, rows)
        )
        thread.error.connect(lambda message, cid=camera_id: self.camera_error.emit(cid, message))
        thread.finished.connect(lambda cid=camera_id: self._on_thread_finished(cid))

    def _on_row_ready(self, camera_id, row):
        self.camera_row_ready.emit(camera_id, row)
        if self._reconciler is None:
            return
        for cross_row in self._reconciler.ingest_row(camera_id, row):
            key = (cross_row["slave_camera_id"], cross_row["stream_identity"])
            self._latest_cross_row_by_pair[key] = cross_row
            self.cross_pair_ready.emit(cross_row)

    def _on_stats_ready(self, camera_id, row):
        self.camera_stats_ready.emit(camera_id, row)
        if self._latest_cross_row_by_pair:
            self.cross_stats_ready.emit(dict(self._latest_cross_row_by_pair))

    def _on_thread_finished(self, camera_id):
        # session_finished isn't guaranteed to have fired for a thread that
        # errored before ever reaching AcquisitionLoop - default to an empty
        # row list rather than KeyError-ing on a camera that never produced any.
        self._finished_rows_by_camera.setdefault(camera_id, [])
        if set(self._finished_rows_by_camera) == set(self._threads):
            # Only NOW, once every camera thread's own hardware cleanup has
            # genuinely finished (not merely stop_all() having been called -
            # request_stop() is non-blocking) is it safe to touch these
            # devices again - see _reset_genlock_roles's own docstring.
            self._reset_genlock_roles()
            self.all_sessions_finished.emit(dict(self._finished_rows_by_camera))

    def _reset_genlock_roles(self):
        """Resets every spec THIS attempt actually applied a genlock role to
        (self._applied_genlock_specs) back to INTER_CAM_SYNC_DEFAULT, via the
        same injectable device_lookup/sync_setter start_all's own
        role-assignment loop already uses - no new collaborator, so this
        stays just as mockable as the existing role-assignment tests. A spec
        whose inter_cam_sync_value was None is never in this list in the
        first place (start_all's loop skips it before ever calling
        sync_setter), so this deliberately never touches a camera nothing
        was ever applied to.

        Called from exactly two places: (a) start_all's own failure path, so
        a role already applied to an earlier camera never lingers after a
        later camera fails to apply its own; (b) _on_thread_finished, only
        once every started camera thread's own Qt finished has genuinely
        fired - never synchronously from stop_all() itself, since
        request_stop() is non-blocking and the underlying rs.pipeline() may
        still be mid-stop on another thread when stop_all() returns;
        touching the same device concurrently with that teardown is exactly
        the kind of race engine.dual_panel_control's _dual_panel_lock exists
        to avoid elsewhere in this project. Leaving a real device stuck in
        e.g. "slave" mode is the same class of risk CLAUDE.md already
        documents for gain surviving across app restarts in camera firmware.

        Best-effort: a single device_lookup/sync_setter failure is swallowed,
        not raised, so it can't stop the REST of the applied specs from
        being reset, and - when called from _on_thread_finished - can't
        suppress all_sessions_finished firing. Whatever real error triggered
        this reset (a failed start, or just a normal finish) has already
        happened; losing the ability to report the run as finished on top of
        that would only make recovery harder, not safer."""
        for spec in self._applied_genlock_specs:
            try:
                device = self._device_lookup(self._ctx, spec.device_serial)
                self._sync_setter(device, INTER_CAM_SYNC_DEFAULT)
            except Exception:
                continue
        self._applied_genlock_specs = []

    def _on_session_finished_rows(self, camera_id, rows):
        self._finished_rows_by_camera[camera_id] = rows
        self.camera_session_finished.emit(camera_id, rows)
