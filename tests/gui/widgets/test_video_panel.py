import numpy as np
from PySide6.QtWidgets import QSizePolicy

from gui.widgets.video_panel import VideoPanel


# --- Responsive sizing (both force_square and default use Expanding, not
# Fixed - a smaller/lower-resolution screen needs to be able to shrink
# these panels below their preferred size, not clip/overflow past the
# window with no way to reach it) ---

def test_force_square_panel_is_expanding_not_fixed(qapp):
    panel = VideoPanel(force_square=True)
    assert panel.sizePolicy().horizontalPolicy() == QSizePolicy.Expanding
    assert panel.sizePolicy().verticalPolicy() == QSizePolicy.Expanding
    assert panel.minimumSize().width() == 160
    assert panel.minimumSize().height() == 160
    assert panel.sizeHint().width() == 320
    assert panel.sizeHint().height() == 320


def test_force_square_panel_never_grows_past_its_natural_square_size(qapp):
    # Regression: on a page whose video row has nothing else competing for
    # leftover vertical space (unlike Live Session's busier graphs/stats
    # layout), an uncapped Expanding policy stretched this panel tall,
    # rendering as a non-square rectangle instead of the mockup's fixed
    # square. A maximum size caps growth while the minimum above still
    # allows shrinking on a smaller screen.
    panel = VideoPanel(force_square=True)
    assert panel.maximumSize().width() == 320
    assert panel.maximumSize().height() == 320


def test_default_panel_is_expanding(qapp):
    panel = VideoPanel()
    assert panel.sizePolicy().horizontalPolicy() == QSizePolicy.Expanding
    assert panel.sizePolicy().verticalPolicy() == QSizePolicy.Expanding
    assert panel.minimumSize().width() == 160
    assert panel.minimumSize().height() == 120


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
