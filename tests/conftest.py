import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _reset_single_panel_target():
    # engine.dual_panel_control's single-panel target is module-level state
    # MainWindow sets on every Stream Config commit - never let one test's
    # target make a later test attempt a real Acroname hub switch.
    from engine import dual_panel_control
    dual_panel_control.set_single_panel_target(None, None)
    yield
    dual_panel_control.set_single_panel_target(None, None)
