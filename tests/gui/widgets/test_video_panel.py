import numpy as np
from gui.widgets.video_panel import VideoPanel


def test_set_frame_grayscale_sets_nonnull_pixmap(qapp):
    panel = VideoPanel()
    image = np.full((20, 30), 128, dtype=np.uint8)
    panel.set_frame(image)
    pixmap = panel.pixmap()
    assert pixmap is not None
    assert pixmap.width() == 30
    assert pixmap.height() == 20


def test_set_frame_bgr_sets_correct_size(qapp):
    panel = VideoPanel()
    image = np.zeros((15, 25, 3), dtype=np.uint8)
    panel.set_frame(image)
    pixmap = panel.pixmap()
    assert pixmap.width() == 25
    assert pixmap.height() == 15
