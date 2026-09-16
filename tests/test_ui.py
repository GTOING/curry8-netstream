from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QGraphicsView, QSplitter

from curry_netstream.models import DataBlock, SessionInfo
from sleep_stim_controller.ui import MainWindow


def test_model_configuration_rejects_missing_or_wrong_extension(qapp, tmp_path) -> None:
    window = MainWindow()
    try:
        window.model_path_edit.setText(str(tmp_path / "missing.onnx"))
        with pytest.raises(FileNotFoundError):
            window.model_configuration()
        text_file = tmp_path / "model.txt"
        text_file.write_text("not an ONNX model", encoding="utf-8")
        window.model_path_edit.setText(str(text_file))
        with pytest.raises(ValueError, match=".onnx"):
            window.model_configuration()
    finally:
        window.close()


def test_console_has_fixed_controls_and_metadata_without_waveforms(qapp) -> None:
    window = MainWindow()
    try:
        assert window.minimumSize().width() == 800
        assert window.minimumSize().height() == 600
        assert window.host_edit.text() == "127.0.0.1"
        assert window.port_spin.value() == 4455
        assert not window.findChildren(QGraphicsView)
        assert not window.findChildren(QSplitter)
        assert "ONNX 睡眠分期" in window.hint_label.text()
        assert "真实刺激未接入" in window.hint_label.text()
        model_config = window.model_configuration()
        assert model_config["model_path"].endswith("litesleepnet_edf20_fp32_6000.onnx")
        assert model_config["channel_name"] == "Fpz-Cz"
        assert not window.recording_checkbox.isChecked()
        assert not window.replay_index_spin.isEnabled()
        assert all(not checkbox.isChecked() for checkbox in window.stage_checkboxes.values())
        assert window.stimulation_strategy_combo.currentData() is None
        assert window.min_interval_edit.text() == ""
        assert window.max_result_age_edit.text() == ""
        assert window.request_timeout_edit.text() == "1.0"
        assert not window.stimulation_auto_checkbox.isChecked()
        assert "仅模拟，真实刺激未接入" in window.stimulation_warning_label.text()
        assert window.connect_button.isEnabled()
        assert not window.disconnect_button.isEnabled()
        assert window.configuration()["recording_enabled"] is False
        assert window.configuration()["recording_root"] is None

        window.show()
        window.resize(800, 600)
        qapp.processEvents()
        assert window.connect_button.isVisible()
        assert window.disconnect_button.isVisible()
        window.set_error("连接失败：这是一个需要持续可见的错误提示。")
        qapp.processEvents()
        assert window.error_label.isVisible()
        assert window.error_scroll.mapTo(window, QPoint()).y() < window.settings_scroll.mapTo(window, QPoint()).y()
        window.set_stimulation_auto_status(True, "模拟自动决策已启用")
        assert not window.stage_checkboxes["N2"].isEnabled()
        assert not window.min_interval_edit.isEnabled()
        window.set_stimulation_auto_status(False, "自动决策已关闭")
        assert window.stage_checkboxes["N2"].isEnabled()

        window.set_session(SessionInfo(2, 10.0, ["C3", "C4"]))
        assert "C3, C4" in window.session_info.toPlainText()
        assert window.available_model_channels_label.text() == "C3, C4"
        assert not window._has_displayed_block

        block = DataBlock(
            data=np.arange(600, dtype=np.float32).reshape(2, 300),
            start_sample=100,
            sample_rate_hz=10.0,
            labels=["C3", "C4"],
        )
        window.show_block(block, 1, 0)
        assert "[100, 400)" in window.block_metadata_label.text()
        assert "C3, C4" in window.block_metadata_label.text()
        assert "30.000 秒" in window.data_status_label.text()
        assert "最新有效块 #1" in window.data_status_label.text()

        window.mark_session_finished(True, None)
        assert "历史数据" in window.data_status_label.text()
        assert not window.settings_scroll.isAncestorOf(window.error_label)
    finally:
        window.close()


def test_small_console_long_text_and_scrolling_keep_controls_accessible(qapp):
    window = MainWindow()
    try:
        window.resize(800, 600)
        window.show()
        error = "测试错误：" + "连接失败，详细原因保留。" * 100
        path = "/测试路径/" + "long_directory_" * 50
        window.set_error(error)
        window.set_recording_root(path)
        window.protocol_summary_label.setText(path)
        qapp.processEvents()
        assert window.size().width() == 800
        assert window.size().height() == 600
        assert window.error_label.text() == error
        assert window.error_scroll.verticalScrollBar().maximum() > 0
        assert window.settings_scroll.horizontalScrollBar().maximum() == 0
        window.settings_scroll.ensureWidgetVisible(window.stimulation_auto_checkbox)
        qapp.processEvents()
        rect = window.settings_scroll.viewport().rect()
        point = window.stimulation_auto_checkbox.mapTo(window.settings_scroll.viewport(), QPoint())
        assert rect.contains(point)
        window.settings_scroll.ensureWidgetVisible(window.recording_root_label)
        qapp.processEvents()
        assert window.recording_root_label.text() == path
        assert window.recording_root_label.verticalScrollBar().maximum() > 0
        assert window.recording_root_label.horizontalScrollBar().maximum() == 0
        assert window.connect_button.isVisible()
        assert window.error_scroll.isVisible()
        assert not window.settings_scroll.isAncestorOf(window.connect_button)
        assert not window.settings_scroll.isAncestorOf(window.error_scroll)
    finally:
        window.close()
