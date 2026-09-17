"""Capture the P-CURRY-WINDOW progress view through real synthetic TCP wiring."""
import sys
import time
from pathlib import Path

from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sleep_stim_controller.app import build_application  # noqa: E402
from tests.test_controller_synthetic import SyntheticCurryServer  # noqa: E402


def pump_until(application, predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return
    application.processEvents()
    if not predicate():
        raise RuntimeError("synthetic TCP screenshot setup timed out")


application = QApplication.instance() or QApplication([])
with SyntheticCurryServer("short_packets_hold") as server:
    application_window, window, controller = build_application()
    window.host_edit.setText("127.0.0.1")
    window.port_spin.setValue(server.port)
    window.resize(800, 600)
    window.show()
    try:
        if not controller.connect("127.0.0.1", server.port):
            raise RuntimeError("controller.connect rejected synthetic TCP setup")
        pump_until(
            application,
            lambda: (
                controller.assembly_snapshot is not None
                and window._last_assembly_snapshot is not None
                and "收包 5 个" in window.assembly_progress_label.text()
            ),
        )
        destination = ROOT / "reports" / "P_CURRY_WINDOW_800X600.png"
        if not window.grab().save(str(destination)):
            raise SystemExit(f"failed to save {destination}")
        print(destination)
    finally:
        if controller.is_busy():
            controller.disconnect()
            pump_until(application, controller.resources_released)
        window.close()
