"""Capture the Issue 5 ARMED/IDLE UI evidence without a Rally endpoint."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication

from sleep_stim_controller.ui import MainWindow


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports" / "ISSUE5_RALLY_ARMING_800X600.png"


def main() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.set_rally_mode("real")
        window.real_control_confirm_checkbox.setChecked(True)
        window.set_stimulation_auto_status(
            True,
            "真实 Rally 自动控制已 armed；启用不发送基线，等待启用后的新合格结果。",
        )
        window.set_rally_status(
            {
                "mode": "real",
                "enabled": True,
                "armed": True,
                "operator_confirmed": True,
                "arming_state": "ARMED/IDLE",
                "runtime_state": "ARMED/IDLE",
                "expected_state": "STOPPED",
                "desired_state": "STOPPED",
                "confirmed_state": None,
                "confirmed_utc": None,
                "last_request": None,
                "last_outcome": None,
                "independent_stop_required": False,
                "start_sent_responsibility": False,
                "start_send_in_flight": False,
                "endpoint": "127.0.0.1:8801",
                "handshake_ready": True,
                "model_configured": True,
            }
        )
        window.rally_mode_hint_label.setText(
            "Issue 5 合成 UI 证据 · ARMED/IDLE · 启用零 UDP · 不是生产 Rally 证据"
        )
        window.hint_label.setText(
            "操作者确认协议已加载、已检查且当前未进行刺激；仅启用后的新合格结果可控制。"
        )
        window.workflow_section.set_expanded(False)
        window.resize(800, 600)
        window.show()
        window.settings_scroll.ensureWidgetVisible(window.rally_control_status_label)
        app.processEvents()
        if not window.grab().save(str(OUTPUT)):
            raise RuntimeError(f"failed to save {OUTPUT}")
        print(f"saved {OUTPUT}")
    finally:
        window.close()
        app.processEvents()


if __name__ == "__main__":
    main()
