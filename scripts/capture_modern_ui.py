"""Capture explicitly synthetic, offscreen GUI review states without I/O to devices."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication

from curry_netstream.models import DataBlock
from sleep_stim_controller.ui import MainWindow


OUT = Path(__file__).resolve().parents[1] / "reports"


def capture(name: str, configure, *, size: tuple[int, int] = (1000, 720)) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.resize(*size)
        configure(window)
        window.statusBar().showMessage("离屏合成场景 · 无真实 Curry / Rally / 受试者")
        window.show()
        app.processEvents()
        path = OUT / f"P_GUI_MODERNIZATION_{name}.png"
        if not window.grab().save(str(path)):
            raise RuntimeError(f"cannot save screenshot: {path}")
        print(path)
    finally:
        window.close()
        app.processEvents()


def small(window: MainWindow) -> None:
    window.pages.setCurrentWidget(window.settings_scroll)


def model_selected(window: MainWindow) -> None:
    window.model_enabled_checkbox.setChecked(True)
    window.model_path_edit.setText("/合成路径/待校验-model.onnx")


def recording_closed(window: MainWindow) -> None:
    window.recording_checkbox.setChecked(True)
    window.stage_csv_checkbox.setChecked(True)
    window.set_busy(True)
    window.begin_session()
    window.set_stage_csv_status("自动 CSV 已写入 0 行：/合成/session/stage_labels.csv")
    window.set_recording_status("会话已关闭：保存 0 块；/合成/session")
    window.set_busy(False)
    window.set_state("disconnected", "合成会话已关闭")
    window.mark_session_finished(False, None)
    window.pages.setCurrentWidget(window.workflow_scroll)


def fault(window: MainWindow) -> None:
    window.set_rally_mode("real")
    window.set_rally_status(
        {
            "mode": "real",
            "enabled": False,
            "runtime_state": "FAULT/UNKNOWN",
            "expected_state": "停止",
            "desired_state": "停止",
            "confirmed_state": "未知",
            "independent_stop_required": True,
            "start_sent_responsibility": True,
            "last_request": {"command": "Stop", "status": "sent"},
            "last_outcome": {"command": "Stop", "status": "timeout"},
        }
    )
    window.set_error("合成故障：Stop API 未取得确认；请在 Rally／硬件侧独立停止。")


def replay(window: MainWindow) -> None:
    window.set_replay_mode(True, "正在打开合成离线会话")
    window.set_replay_worker_busy(True)
    window.set_replay_ready(
        SimpleNamespace(
            session_id="synthetic-review-session",
            block_count=1,
            status="complete",
            incomplete=False,
            issues=(),
        )
    )
    block = DataBlock(
        data=np.zeros((2, 300), dtype=np.float32),
        start_sample=0,
        sample_rate_hz=10.0,
        labels=["C3", "C4"],
    )
    entry = SimpleNamespace(
        block_id=1,
        start_sample=0,
        end_sample_exclusive=300,
        labels=["C3", "C4"],
        sample_rate_hz=10.0,
        received_utc="合成时间",
    )
    window.show_replay_block(block, entry, 0, 1)
    window.set_recorded_processing_result(None, 1)
    window.pages.setCurrentWidget(window.workflow_scroll)


def long_text(window: MainWindow) -> None:
    window.set_error("合成长错误：" + "连接失败，详细原因保留。" * 30)
    window.set_recording_root("/合成路径/" + "long-directory-" * 50)
    window.set_recording_status("记录失败/不完整：合成磁盘错误")
    window.set_stage_csv_status("导出/重建失败：合成磁盘错误")
    window.pages.setCurrentWidget(window.workflow_scroll)


if __name__ == "__main__":
    capture("DEFAULT", lambda window: None)
    capture("800X600", small, size=(800, 600))
    capture("MODEL_SELECTED_SYNTHETIC", model_selected)
    capture("RECORDING_CLOSED_SYNTHETIC", recording_closed)
    capture("FAULT_SYNTHETIC", fault)
    capture("REPLAY_SYNTHETIC", replay)
    capture("LONG_TEXT_SYNTHETIC", long_text, size=(800, 600))
