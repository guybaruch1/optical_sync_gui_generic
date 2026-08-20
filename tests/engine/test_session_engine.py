"""Pure-Python-testable slice of SessionEngineThread: the recent-frame
ring buffer (_record_recent_frame/get_recent_frame_pair) that backs
cross-camera debug images (gui/pages/multi_camera_live_session_page.py).
Everything else about this class is hardware/Qt-facing and untested by
design (see CLAUDE.md) - constructing a SessionEngineThread with dummy
args is safe (no hardware/Qt event loop is touched until .start()/.run()
actually runs), so this file is scoped ONLY to the ring buffer, never
calling those."""

from engine.session_engine import SessionEngineThread, _RECENT_FRAMES_MAXLEN


def _make_thread(qapp):
    return SessionEngineThread(
        ctx=None, device_serial="SN1", pick_a={}, pick_b={}, camera_controls={}, test_session=None,
    )


def test_get_recent_frame_pair_returns_none_when_never_recorded(qapp):
    thread = _make_thread(qapp)
    assert thread.get_recent_frame_pair(5) is None


def test_get_recent_frame_pair_returns_the_recorded_images(qapp):
    thread = _make_thread(qapp)
    thread._record_recent_frame(5, "image_a_5", "image_b_5")

    assert thread.get_recent_frame_pair(5) == ("image_a_5", "image_b_5")


def test_get_recent_frame_pair_distinguishes_between_pair_indices(qapp):
    thread = _make_thread(qapp)
    thread._record_recent_frame(1, "a1", "b1")
    thread._record_recent_frame(2, "a2", "b2")

    assert thread.get_recent_frame_pair(1) == ("a1", "b1")
    assert thread.get_recent_frame_pair(2) == ("a2", "b2")
    assert thread.get_recent_frame_pair(3) is None


def test_recent_frame_buffer_evicts_oldest_past_its_maxlen(qapp):
    thread = _make_thread(qapp)

    for i in range(_RECENT_FRAMES_MAXLEN + 5):
        thread._record_recent_frame(i, "a{}".format(i), "b{}".format(i))

    # The first 5 pair_indices (0-4) must have aged out - only the most
    # recent _RECENT_FRAMES_MAXLEN entries survive.
    assert thread.get_recent_frame_pair(0) is None
    assert thread.get_recent_frame_pair(4) is None
    assert thread.get_recent_frame_pair(5) == ("a5", "b5")
    last_index = _RECENT_FRAMES_MAXLEN + 4
    assert thread.get_recent_frame_pair(last_index) == ("a{}".format(last_index), "b{}".format(last_index))
