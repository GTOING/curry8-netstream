from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QGraphicsView, QSplitter

from curry_netstream.models import DataBlock, SessionInfo
from sleep_stim_controller.ui import MainWindow


def test_application_starts_and_closes_with_no_model_selected(qapp) -> None:
    from sleep_stim_controller.app import build_application

    application, window, controller = build_application()
    window.show()
    qapp.processEvents()
    assert not window.model_enabled_checkbox.isChecked()
    assert window.model_path_edit.text() == ""
    assert "NoModel" in window.processing_status_label.text()
    window.close()
    qapp.processEvents()
    assert not controller.is_busy()


def test_model_configuration_rejects_missing_or_wrong_extension(qapp, tmp_path) -> None:
    window = MainWindow()
    try:
        assert window.model_path_edit.text() == ""
        with pytest.raises(ValueError, match="选择 ONNX"):
            window.model_configuration()
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
        assert "模型默认关闭（NoModel）" in window.hint_label.text()
        assert "真实刺激未接入" in window.hint_label.text()
        assert "当前累计 0.0 / 30 秒" in window.assembly_progress_label.text()
        assert not window.model_enabled_checkbox.isChecked()
        assert not window.model_path_edit.isEnabled()
        assert not window.choose_model_button.isEnabled()
        assert not window.model_channel_edit.isEnabled()
        assert window.model_path_edit.text() == ""
        with pytest.raises(ValueError, match="选择 ONNX"):
            window.model_configuration()
        from sleep_stim_controller.onnx_staging import default_model_path

        window.model_path_edit.setText(str(default_model_path()))
        model_config = window.model_configuration()
        assert model_config["model_path"].endswith("litesleepnet_edf20_fp32_6000.onnx")
        assert model_config["channel_name"] == "Fpz-Cz"
        window.model_enabled_checkbox.setChecked(True)
        assert window.model_path_edit.isEnabled()
        assert window.choose_model_button.isEnabled()
        assert window.model_channel_edit.isEnabled()
        window.set_busy(True)
        assert not window.model_enabled_checkbox.isEnabled()
        assert not window.model_path_edit.isEnabled()
        window.set_busy(False)
        window.model_enabled_checkbox.setChecked(False)
        assert not window.model_path_edit.isEnabled()
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


def test_live_assembly_progress_is_readable_and_replay_does_not_overwrite(qapp):
    from sleep_stim_controller.epoching import EpochAssemblySnapshot

    window = MainWindow()
    try:
        snapshot = EpochAssemblySnapshot(
            window_seconds=30.0,
            window_samples=300,
            received_packets=7,
            received_samples=512,
            completed_windows=1,
            accepted_windows=1,
            partial_samples=212,
            partial_start_sample=400,
        )
        window.set_state("streaming", "正在接收")
        window.set_assembly_progress(snapshot)
        assert "收包 7 个" in window.assembly_progress_label.text()
        assert "当前累计 21.2 / 30 秒" in window.assembly_progress_label.text()
        before = window.assembly_progress_label.text()
        window.set_replay_mode(True, "回放")
        window.set_assembly_progress(
            EpochAssemblySnapshot(30.0, 300, 99, 999, 3, 3, 0, None)
        )
        assert window.assembly_progress_label.text() != before
        assert "离线回放" in window.assembly_progress_label.text()
    finally:
        window.close()


def test_live_session_shows_waiting_until_handshake_and_terminal_copy(qapp) -> None:
    window = MainWindow()
    try:
        window.begin_session()
        assert "正在连接 Curry" in window.data_status_label.text()
        assert "已连接" not in window.assembly_progress_label.text()

        window.set_state("connecting", "正在等待握手")
        assert "等待 BasicInfo / ChannelInfo 握手" in window.data_status_label.text()

        window.set_session(SessionInfo(2, 10.0, ["C3", "C4"]))
        assert "已连接" in window.data_status_label.text()
        window.set_state("streaming", "握手完成")
        assert "已连接" in window.data_status_label.text()

        window.mark_session_finished(True, None)
        assert "实时会话已停止" in window.data_status_label.text()
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
