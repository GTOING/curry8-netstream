from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QAbstractItemView, QGraphicsView, QSplitter

from curry_netstream.models import DataBlock, SessionInfo
from sleep_stim_controller.ui import MainWindow


def _send_wheel(
    qapp,
    target,
    *,
    angle_delta: QPoint | None = None,
    pixel_delta: QPoint | None = None,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> QWheelEvent:
    local = target.rect().center()
    event = QWheelEvent(
        QPointF(local),
        QPointF(target.mapToGlobal(local)),
        pixel_delta or QPoint(),
        angle_delta or QPoint(),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(target, event)
    qapp.processEvents()
    return event


def _center_in_settings(window, control, qapp) -> None:
    scroll = _scroll_for(window, control)
    window.pages.setCurrentWidget(scroll)
    qapp.processEvents()
    viewport = scroll.viewport()
    bar = scroll.verticalScrollBar()
    center = control.mapTo(viewport, control.rect().center())
    bar.setValue(bar.value() + center.y() - viewport.height() // 2)
    qapp.processEvents()


def _scroll_for(window, control):
    for scroll in (window.settings_scroll, window.workflow_scroll, window.diagnostics_scroll):
        if scroll.isAncestorOf(control):
            return scroll
    raise AssertionError("control has no page scroll")


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


def test_stage_csv_option_requires_recording_locks_and_reports_failure(qapp) -> None:
    window = MainWindow()
    try:
        assert not window.stage_csv_checkbox.isChecked()
        assert not window.stage_csv_checkbox.isEnabled()
        assert window.configuration()["stage_csv_enabled"] is False
        window.recording_checkbox.setChecked(True)
        assert window.stage_csv_checkbox.isEnabled()
        window.stage_csv_checkbox.setChecked(True)
        assert window.configuration()["stage_csv_enabled"] is True
        window.set_busy(True)
        assert not window.recording_checkbox.isEnabled()
        assert not window.stage_csv_checkbox.isEnabled()
        window.set_stage_csv_status(
            "自动 CSV 导出失败/不完整：fixture disk failure；权威 JSONL/NPY 仍继续记录"
        )
        assert "fixture disk failure" in window.stage_csv_status_label.text()
        window.set_busy(False)
        window.recording_checkbox.setChecked(False)
        assert not window.stage_csv_checkbox.isChecked()
        assert not window.stage_csv_checkbox.isEnabled()
        assert window.configuration()["stage_csv_enabled"] is False
    finally:
        window.close()


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
        assert "ONNX 模型" in window.overview_empty_detail.text()
        assert "NoModel" in window.run_model_label.text()
        assert "API 回复不证明物理输出" in window.hint_label.text()
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
        assert "本机模拟：真实 Rally 不会收到请求" in window.stimulation_warning_label.text()
        assert window.connect_button.isEnabled()
        assert not window.disconnect_button.isEnabled()
        assert window.configuration()["recording_enabled"] is False
        assert window.configuration()["stage_csv_enabled"] is False
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


def test_paradigm_profile_and_replay_are_explicitly_read_only(qapp) -> None:
    window = MainWindow()
    try:
        assert window.rally_profile_combo.currentData() == "binary"
        window.set_rally_mode("real")
        window.set_rally_profile("paradigm")
        assert window.rally_profile_combo.currentData() == "paradigm"
        assert "Start + 最新协议 Apply" in window.stimulation_auto_checkbox.text()
        assert "基础协议与所选范式包" in window.real_control_confirm_checkbox.text()
        window.set_rally_status(
            {
                "mode": "real",
                "profile": "paradigm",
                "runtime_state": "SWITCHING(A,C)",
                "enabled": True,
                "latest_desired_protocol": "C",
                "api_confirmed_protocol": "A",
                "paradigm_name": "fixture",
                "paradigm_version": "test-only-1",
                "paradigm_classification": "synthetic/test-only",
                "endpoint": "127.0.0.1:8801",
                "physical_output_confirmed": False,
            }
        )
        assert "SWITCHING(A,C)" in window.rally_control_status_label.text()
        assert "latest desired=C" in window.rally_control_status_label.text()
        window.set_replay_mode(True, "回放只读")
        window.set_replay_control_events(
            [
                {
                    "event_type": "paradigm_control",
                    "block_id": None,
                    "payload": {
                        "phase": "outcome",
                        "action": "apply",
                        "desired_protocol": "C",
                        "api_confirmed_protocol": "C",
                        "outcome": "api_success",
                    },
                }
            ]
        )
        assert "仅展示，不发送" in window.stimulation_recent_label.text()
        assert "范式 apply" in window.stimulation_recent_label.text()
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
        window.pages.setCurrentWidget(window.workflow_scroll)
        window.workflow_scroll.ensureWidgetVisible(window.recording_root_label)
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


def test_wheel_over_settings_controls_scrolls_without_changing_values_or_signals(
    qapp,
) -> None:
    window = MainWindow()
    try:
        assert (window.size().width(), window.size().height()) == (1000, 720)
        window.set_rally_mode("real")
        window.set_rally_profile("paradigm")
        window.stimulation_strategy_combo.setCurrentIndex(1)
        window.stage_checkboxes["N2"].setChecked(True)
        window.min_interval_edit.setText("0")
        window.max_result_age_edit.setText("5")
        window.port_spin.setValue(5432)
        for section in (
            window.model_section,
            window.stimulation_section,
            window.workflow_section,
            window.connection_section,
            window.session_section,
            window.diagnostics_section,
        ):
            section.set_expanded(True)
        window.set_error("测试错误：滚动内容。\n" * 40)
        window.set_diagnostics("诊断行：只读滚动内容。\n" * 120)
        window.show()
        qapp.processEvents()
        window.resize(800, 600)
        qapp.processEvents()
        assert (window.width(), window.height()) == (800, 600)
        assert window.connect_button.isVisible()
        assert window.settings_scroll.verticalScrollBar().maximum() > 0

        mode_spy = QSignalSpy(window.rally_mode_requested)
        profile_spy = QSignalSpy(window.rally_profile_requested)
        config_spy = QSignalSpy(window.stimulation_configuration_changed)
        values = (
            window.rally_mode_combo.currentData(),
            window.rally_profile_combo.currentData(),
            window.stimulation_strategy_combo.currentData(),
            window.port_spin.value(),
            window.stage_checkboxes["N2"].isChecked(),
        )
        controls = (
            window.rally_mode_combo,
            window.rally_profile_combo,
            window.stimulation_strategy_combo,
            window.port_spin,
        )

        for control_index, control in enumerate(controls):
            for focused in (True, False):
                _center_in_settings(window, control, qapp)
                scroll_bar = _scroll_for(window, control).verticalScrollBar()
                assert scroll_bar.maximum() > 0
                if focused:
                    control.setFocus()
                    qapp.processEvents()
                    assert control.hasFocus()
                else:
                    control.clearFocus()
                    qapp.processEvents()
                    assert not control.hasFocus()

                before_scroll = scroll_bar.value()
                angle_y = 120 if before_scroll > 0 else -120
                angle_modifiers = (
                    Qt.KeyboardModifier.NoModifier
                    if control_index == 0
                    else Qt.KeyboardModifier.ControlModifier
                )
                _send_wheel(
                    qapp,
                    control,
                    angle_delta=QPoint(0, angle_y),
                    modifiers=angle_modifiers,
                )
                assert scroll_bar.value() != before_scroll

                _center_in_settings(window, control, qapp)
                before_scroll = scroll_bar.value()
                pixel_y = 32 if before_scroll > 0 else -32
                receiver = (
                    control.lineEdit()
                    if control is window.port_spin
                    else control
                )
                _send_wheel(
                    qapp,
                    receiver,
                    pixel_delta=QPoint(0, pixel_y),
                    modifiers=(
                        Qt.KeyboardModifier.ShiftModifier
                        if focused
                        else Qt.KeyboardModifier.ControlModifier
                    ),
                )
                assert scroll_bar.value() != before_scroll
                assert (
                    window.rally_mode_combo.currentData(),
                    window.rally_profile_combo.currentData(),
                    window.stimulation_strategy_combo.currentData(),
                    window.port_spin.value(),
                    window.stage_checkboxes["N2"].isChecked(),
                ) == values
                assert mode_spy.count() == 0
                assert profile_spy.count() == 0
                assert config_spy.count() == 0

        combo = window.stimulation_strategy_combo
        original_strategy = combo.currentData()
        original_strategy_index = combo.currentIndex()
        alternate_strategy = combo.itemData(2)
        combo.blockSignals(True)
        for index in range(60):
            combo.addItem(f"滚轮测试策略 {index}", alternate_strategy)
        combo.blockSignals(False)
        window.pages.setCurrentWidget(window.settings_scroll)
        window.settings_scroll.ensureWidgetVisible(combo)
        qapp.processEvents()
        combo.showPopup()
        qapp.processEvents()
        view = combo.view()
        popup_scroll_bar = view.verticalScrollBar()
        assert view.isVisible()
        assert popup_scroll_bar.maximum() > 0
        popup_receivers = (view.viewport(), view, view.window())
        for receiver in popup_receivers:
            popup_scroll_bar.setValue(0)
            before_scroll = popup_scroll_bar.value()
            _send_wheel(
                qapp,
                receiver,
                angle_delta=QPoint(0, -120),
                modifiers=Qt.KeyboardModifier.ShiftModifier,
            )
            assert popup_scroll_bar.value() > before_scroll
            assert combo.currentData() == original_strategy
            assert config_spy.count() == 0

        selected_index = combo.model().index(12, 0)
        view.scrollTo(selected_index, QAbstractItemView.ScrollHint.PositionAtCenter)
        qapp.processEvents()
        click_pos = view.visualRect(selected_index).center()
        assert view.viewport().rect().contains(click_pos)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=click_pos)
        qapp.processEvents()
        assert combo.currentIndex() != original_strategy_index
        assert combo.currentData() == alternate_strategy
        assert config_spy.count() == 1

        window.set_rally_mode("simulation")
        assert mode_spy.count() == 0
        window.rally_mode_combo.setFocus()
        QTest.keyClick(window.rally_mode_combo, Qt.Key.Key_Down)
        qapp.processEvents()
        assert window.rally_mode_combo.currentData() == "real"
        assert mode_spy.count() == 1
        window.set_rally_mode("simulation")
        window.set_rally_profile("binary")
        assert window.rally_mode_combo.currentData() == "simulation"
        assert window.rally_profile_combo.currentData() == "binary"
        assert mode_spy.count() == 1
        assert profile_spy.count() == 0
        port_before_key = window.port_spin.value()
        window.port_spin.setFocus()
        QTest.keyClick(window.port_spin, Qt.Key.Key_Up)
        qapp.processEvents()
        assert window.port_spin.value() == port_before_key + 1

        error_bar = window.error_scroll.verticalScrollBar()
        assert error_bar.maximum() > 0
        error_bar.setValue(0)
        error_before = error_bar.value()
        _send_wheel(
            qapp,
            window.error_label,
            angle_delta=QPoint(0, -120),
        )
        assert error_bar.value() > error_before

        detail_bar = window.details.verticalScrollBar()
        assert detail_bar.maximum() > 0
        window.pages.setCurrentWidget(window.diagnostics_scroll)
        window.diagnostics_scroll.ensureWidgetVisible(window.details)
        qapp.processEvents()
        detail_bar.setValue(0)
        detail_before = detail_bar.value()
        _send_wheel(
            qapp,
            window.details.viewport(),
            angle_delta=QPoint(0, -120),
        )
        assert detail_bar.value() > detail_before
    finally:
        window.close()


def test_replay_index_wheel_is_ignored_but_buttons_keyboard_and_updates_work(
    qapp,
) -> None:
    window = MainWindow()
    try:
        window.show()
        qapp.processEvents()
        window.resize(800, 600)
        window.diagnostics_section.set_expanded(True)
        qapp.processEvents()
        window.set_replay_mode(True, "回放测试")
        window.set_replay_ready(
            SimpleNamespace(
                session_id="wheel-replay",
                block_count=4,
                status="complete",
                incomplete=False,
                issues=(),
            )
        )
        window.set_recording_root("/test/replay/" + "long-directory-" * 60)
        qapp.processEvents()
        control = window.replay_index_spin
        assert control.isEnabled()
        replay_spy = QSignalSpy(window.replay_index_requested)
        window.pages.setCurrentWidget(window.workflow_scroll)
        qapp.processEvents()
        scroll_bar = window.workflow_scroll.verticalScrollBar()
        assert scroll_bar.maximum() > 0

        for focused in (True, False):
            _center_in_settings(window, control, qapp)
            if focused:
                control.setFocus()
                qapp.processEvents()
                assert control.hasFocus()
            else:
                control.clearFocus()
                qapp.processEvents()
                assert not control.hasFocus()
            before_scroll = scroll_bar.value()
            angle_y = 120 if before_scroll > 0 else -120
            _send_wheel(
                qapp,
                control,
                angle_delta=QPoint(0, angle_y),
                modifiers=Qt.KeyboardModifier.ControlModifier,
            )
            assert scroll_bar.value() != before_scroll
            _center_in_settings(window, control, qapp)
            before_scroll = scroll_bar.value()
            pixel_y = 32 if before_scroll > 0 else -32
            _send_wheel(
                qapp,
                control.lineEdit(),
                pixel_delta=QPoint(0, pixel_y),
                modifiers=Qt.KeyboardModifier.ShiftModifier,
            )
            assert scroll_bar.value() != before_scroll
            assert control.value() == 1
            assert replay_spy.count() == 0

        block = DataBlock(
            data=np.zeros((2, 300), dtype=np.float32),
            start_sample=900,
            sample_rate_hz=10.0,
            labels=["C3", "C4"],
        )
        entry = SimpleNamespace(
            block_id=4,
            start_sample=900,
            end_sample_exclusive=1200,
            labels=["C3", "C4"],
            sample_rate_hz=10.0,
            received_utc="2026-09-24T00:00:00Z",
        )
        QTest.mouseClick(window.replay_next_button, Qt.MouseButton.LeftButton)
        assert control.value() == 2
        assert replay_spy.count() == 1
        window.show_replay_block(block, entry, index=1, total=4)
        assert control.value() == 2
        assert replay_spy.count() == 1
        QTest.mouseClick(window.replay_previous_button, Qt.MouseButton.LeftButton)
        assert control.value() == 1
        assert replay_spy.count() == 2
        control.setFocus()
        QTest.keyClick(control, Qt.Key.Key_Up)
        qapp.processEvents()
        assert control.value() == 2
        assert replay_spy.count() == 3
        window.show_replay_block(block, entry, index=3, total=4)
        assert control.value() == 4
        assert replay_spy.count() == 3
        QTest.mouseClick(window.replay_previous_button, Qt.MouseButton.LeftButton)
        assert control.value() == 3
        assert replay_spy.count() == 4
    finally:
        window.close()


def test_four_pages_navigation_and_style_are_passive(qapp) -> None:
    window = MainWindow()
    try:
        window.resize(800, 600)
        window.show()
        qapp.processEvents()
        assert [window.pages.tabText(i) for i in range(window.pages.count())] == [
            "运行总览", "刺激控制", "记录与回放", "连接／模型／诊断"
        ]
        assert window.run_bar.isVisible()
        assert window.run_api_label.isVisible()
        assert window.disable_automatic_button.isVisible()
        assert not window.disable_automatic_button.isEnabled()
        assert "不是硬件急停" in window.disable_automatic_button.accessibleName()
        assert "NoModel" in window.run_model_label.text()
        assert "无期别" in window.run_model_label.text()

        auto_spy = QSignalSpy(window.stimulation_auto_requested)
        mode_spy = QSignalSpy(window.rally_mode_requested)
        config_spy = QSignalSpy(window.stimulation_configuration_changed)
        replay_spy = QSignalSpy(window.replay_index_requested)
        window.set_error("合成错误：切页后仍需可读")
        for index in range(window.pages.count()):
            rect = window.pages.tabBar().tabRect(index)
            QTest.mouseClick(window.pages.tabBar(), Qt.MouseButton.LeftButton, pos=rect.center())
            qapp.processEvents()
            assert window.pages.currentIndex() == index
            assert window.error_scroll.isVisible()
            assert window.error_label.text() == "合成错误：切页后仍需可读"
            assert window.run_bar.isVisible()
        window.pages.setCurrentIndex(0)
        window.pages.tabBar().setFocus()
        QTest.keyClick(window.pages.tabBar(), Qt.Key.Key_Right)
        qapp.processEvents()
        assert window.pages.currentIndex() == 1
        window.setStyleSheet(window.styleSheet())
        qapp.processEvents()
        assert auto_spy.count() == 0
        assert mode_spy.count() == 0
        assert config_spy.count() == 0
        assert replay_spy.count() == 0
    finally:
        window.close()


def test_run_bar_tracks_real_status_and_fixed_close_uses_existing_path(qapp) -> None:
    from sleep_stim_controller.epoching import EpochAssemblySnapshot

    window = MainWindow()
    try:
        window.show()
        window.set_state("connecting", "握手中")
        assert "等待 Curry 握手" in window.run_progress_label.text()
        window.set_session(SessionInfo(2, 10.0, ["C3", "C4"]))
        window.set_assembly_progress(
            EpochAssemblySnapshot(30.0, 300, 7, 212, 0, 0, 212, 0)
        )
        assert "21.2 / 30" in window.run_progress_label.text()
        window.set_processing_result(
            {"status": "success", "stage": "N2", "model": {"is_test_double": True}}
        )
        assert "最新期别：N2 · 测试替身" in window.run_model_label.text()
        window.set_processing_result(
            {"status": "failed", "reason": "合成模型错误", "model": {}}
        )
        assert "分期失败 · 无期别" in window.run_model_label.text()

        window.set_rally_mode("real")
        assert "默认本机模拟" not in window.hint_label.text()
        window.set_rally_status(
            {"mode": "real", "runtime_state": "ARMED/IDLE",
             "expected_state": "停止", "confirmed_state": "IDLE", "enabled": False}
        )
        assert "ARMED/IDLE" in window.run_control_label.text()
        assert "IDLE" in window.run_api_label.text()
        window.set_stimulation_auto_status(True, "合成启用状态")
        window.set_rally_status(
            {"mode": "real", "runtime_state": "RUNNING", "enabled": True,
             "expected_state": "Start", "confirmed_state": "RUNNING",
             "last_outcome": {"command": "Start", "status": "success",
                              "raw_text": "合成回复" * 100}}
        )
        assert window.disable_automatic_button.isEnabled()
        assert "物理输出未验证" in window.run_api_label.text()
        assert "合成回复" not in window.run_api_label.text()
        assert "合成回复" in window.rally_control_status_label.text()
        window.pages.setCurrentWidget(window.diagnostics_scroll)
        qapp.processEvents()
        assert window.disable_automatic_button.isVisible()
        auto_spy = QSignalSpy(window.stimulation_auto_requested)
        QTest.mouseClick(window.disable_automatic_button, Qt.MouseButton.LeftButton)
        assert auto_spy.count() == 1
        assert auto_spy.at(0) == [False]
        window.set_stimulation_auto_status(False, "合成关闭状态")
        assert not window.disable_automatic_button.isEnabled()

        window.set_rally_status(
            {"mode": "real", "runtime_state": "FAULT/UNKNOWN",
             "enabled": False, "confirmed_state": "未知",
             "independent_stop_required": True}
        )
        qapp.processEvents()
        assert window.stop_warning_label.isVisible()
        assert "独立停止" in window.stop_warning_label.text()
        assert "FAULT/UNKNOWN" in window.run_control_label.text()
        assert "物理输出未验证" in window.run_api_label.text()
        window.set_replay_mode(True, "只读合成回放")
        qapp.processEvents()
        assert window.stop_warning_label.isVisible()
        assert "独立停止" in window.stop_warning_label.text()
    finally:
        window.close()


def test_recording_fault_and_incomplete_replay_stay_read_only(qapp) -> None:
    window = MainWindow()
    try:
        window.show()
        window.set_recording_status("记录失败/不完整：合成磁盘错误")
        window.set_stage_csv_status("自动 CSV 导出失败：合成磁盘错误")
        assert "记录：失败／不完整" in window.run_recording_label.text()
        assert "CSV：失败" in window.run_recording_label.text()
        window.set_replay_mode(True, "合成回放")
        window.set_replay_ready(
            SimpleNamespace(
                session_id="synthetic-incomplete", block_count=0,
                status="incomplete", incomplete=True, issues=("missing block",),
            )
        )
        assert "不完整" in window.run_recording_label.text()
        assert "只读" in window.run_control_label.text()
        assert "不发送命令" in window.run_api_label.text()
        assert not window.stimulation_auto_checkbox.isEnabled()
        assert not window.recording_checkbox.isEnabled()
        assert not window.replay_index_spin.isEnabled()
        before = window.run_model_label.text()
        connection_before = window.state_label.text()
        window.set_processing_result(
            {"status": "success", "stage": "N3", "model": {"is_test_double": True}}
        )
        window.set_rally_status(
            {"mode": "real", "runtime_state": "RUNNING", "enabled": True,
             "confirmed_state": "RUNNING"}
        )
        window.set_state("streaming", "过时实时状态")
        window.set_session(SessionInfo(2, 10.0, ["old-C3", "old-C4"]))
        assert window.run_model_label.text() == before
        assert window.state_label.text() == connection_before
        assert "old-C3" not in window.session_info.toPlainText()
        assert "回放只读" in window.run_api_label.text()
        assert not window.stop_warning_label.isVisible()
    finally:
        window.close()


def test_recording_and_csv_brief_follow_session_lifecycle(qapp) -> None:
    window = MainWindow()
    try:
        assert window.run_recording_label.text() == "记录：未启用 · CSV：未启用"
        window.recording_checkbox.setChecked(True)
        window.stage_csv_checkbox.setChecked(True)
        assert "记录：已选择，待连接" in window.run_recording_label.text()
        assert "CSV：已选择，待连接" in window.run_recording_label.text()

        window.set_busy(True)
        window.begin_session()
        window.set_state("connecting", "合成握手")
        assert "记录：初始化中" in window.run_recording_label.text()
        window.set_state("streaming", "合成采集")
        window.set_recording_status("记录中：/合成/session-1")
        window.set_stage_csv_status("自动 CSV 已写入 2 行：/合成/session-1/stage_labels.csv")
        assert "记录：正在写入" in window.run_recording_label.text()
        assert "CSV：正在写入" in window.run_recording_label.text()

        # The writer can report rows before the terminal signal; close text is authoritative.
        window.set_recording_status("会话已关闭：保存 2 块；/合成/session-1")
        assert "记录：已关闭" in window.run_recording_label.text()
        assert "CSV：已写入完成" in window.run_recording_label.text()
        window.set_busy(False)
        window.mark_session_finished(False, None)
        assert "待连接" not in window.run_recording_label.text()
        assert "正在写入" not in window.run_recording_label.text()
        assert "自动 CSV 已写入 2 行" in window.run_recording_label.toolTip()

        # A new session must not inherit the previous session's completed summary.
        window.set_busy(True)
        window.begin_session()
        assert "已关闭" not in window.run_recording_label.text()
        window.set_stage_csv_status("自动 CSV 已写入 0 行：/合成/session-2/stage_labels.csv")
        window.set_recording_status("会话已关闭：保存 0 块；/合成/session-2")
        window.set_busy(False)
        window.mark_session_finished(False, None)
        assert "记录：已关闭" in window.run_recording_label.text()
        assert "CSV：已写入完成" in window.run_recording_label.text()

        window.set_busy(True)
        window.begin_session()
        window.set_recording_status("会话已关闭：保存 0 块；/合成/session-3")
        window.set_busy(False)
        window.mark_session_finished(False, None)
        assert "CSV：已关闭（写入未确认）" in window.run_recording_label.text()

        window.set_busy(True)
        window.begin_session()
        window.set_recording_status("记录未完整：已保存 0 块；合成错误")
        window.set_stage_csv_status("自动 CSV 导出失败/不完整：合成磁盘错误")
        window.set_busy(False)
        window.mark_session_finished(False, RuntimeError("合成错误"))
        assert "记录：失败／不完整" in window.run_recording_label.text()
        assert "CSV：失败／不完整" in window.run_recording_label.text()
        window.set_replay_mode(True, "合成回放")
        window.set_replay_ready(SimpleNamespace(
            session_id="synthetic-complete", block_count=0, status="complete",
            incomplete=False, issues=(),
        ))
        assert window.run_recording_label.text() == "记录回放：完整"
        window.set_replay_ready(SimpleNamespace(
            session_id="synthetic-incomplete", block_count=0, status="incomplete",
            incomplete=True, issues=("missing block",),
        ))
        assert window.run_recording_label.text() == "记录回放：不完整"
    finally:
        window.close()


def test_preconnect_onnx_selection_does_not_impersonate_processing(qapp) -> None:
    window = MainWindow()
    try:
        assert "NoModel" in window.run_model_label.text()
        window.model_enabled_checkbox.setChecked(True)
        assert "文件未选" in window.run_model_label.text()
        assert "无期别" in window.processing_status_label.text()
        window.model_path_edit.setText("/合成/未校验-model.onnx")
        assert "ONNX 已选择" in window.run_model_label.text()
        assert "待连接校验／加载" in window.run_model_label.text()
        assert "尚未校验或加载" in window.overview_empty_detail.text()
        assert "ONNX 已选择" in window.processing_status_label.text()
        assert "最新期别" not in window.run_model_label.text()
        window.set_processing_result(None)
        assert "NoModel" not in window.run_model_label.text()
        window.model_enabled_checkbox.setChecked(False)
        assert "NoModel" in window.run_model_label.text()

        window.model_enabled_checkbox.setChecked(True)
        window.set_busy(True)
        window.begin_session()
        window.set_state("connecting", "合成连接")
        assert "待连接校验" not in window.run_model_label.text()
        window.set_processing_result({
            "status": "success", "stage": "N2", "confidence": 0.8,
            "model": {"is_test_double": True},
        })
        assert "最新期别：N2 · 测试替身" in window.run_model_label.text()
        assert window.overview_stage_primary.text().startswith("N2")
        window.model_enabled_checkbox.setChecked(False)
        assert "最新期别：N2" in window.run_model_label.text()
        window.set_processing_result({
            "status": "failed", "reason": "合成推理错误", "model": {},
        })
        assert "分期失败" in window.run_model_label.text()
        assert "合成推理错误" in window.overview_stage_reason.text()
        window.set_busy(False)
        window.mark_session_finished(False, None)
        window.model_enabled_checkbox.setChecked(True)
        assert "分期失败" in window.run_model_label.text()

        window.set_replay_mode(True, "合成回放")
        window.set_recorded_processing_result({
            "status": "success", "stage": "REM", "model": {"is_test_double": True},
        }, 1)
        assert "回放 · 最新期别：REM" in window.run_model_label.text()
        window.model_enabled_checkbox.setChecked(False)
        assert "回放 · 最新期别：REM" in window.run_model_label.text()
    finally:
        window.close()


def test_overview_empty_navigation_and_live_primary_state(qapp) -> None:
    from sleep_stim_controller.epoching import EpochAssemblySnapshot

    window = MainWindow()
    try:
        window.resize(800, 600)
        window.show()
        qapp.processEvents()
        assert window.overview_empty.isVisible()
        assert not window.overview_hero.isVisible()
        assert window.open_connection_settings_button.isVisible()
        assert window.run_bar.isVisible()
        assert window.connect_button.isVisible()
        auto_spy = QSignalSpy(window.stimulation_auto_requested)
        mode_spy = QSignalSpy(window.rally_mode_requested)
        connect_spy = QSignalSpy(window.connect_requested)
        QTest.mouseClick(window.open_connection_settings_button, Qt.MouseButton.LeftButton)
        assert window.pages.currentWidget() is window.diagnostics_scroll
        assert auto_spy.count() == mode_spy.count() == connect_spy.count() == 0

        window.set_busy(True)
        window.begin_session()
        window.set_state("connecting", "合成握手")
        window.pages.setCurrentWidget(window.overview_scroll)
        qapp.processEvents()
        assert not window.overview_empty.isVisible()
        assert window.overview_hero.isVisible()
        assert window.overview_progress_primary.text() == "等待握手"
        window.set_session(SessionInfo(2, 10.0, ["C3", "C4"]))
        window.set_assembly_progress(
            EpochAssemblySnapshot(30.0, 300, 7, 212, 0, 0, 212, 0)
        )
        assert window.overview_progress_primary.text() == "21.2 / 30 秒"
        assert "已收 212 点" in window.overview_progress_detail.text()
        window.set_processing_result({
            "status": "unavailable", "reason": "合成 NoModel", "model": {},
        })
        assert window.overview_stage_primary.text() == "无期别"
        assert "合成 NoModel" in window.overview_stage_reason.text()

        window.set_error("合成错误：" + "长中文错误。" * 40)
        window.set_rally_mode("real")
        window.set_rally_status({
            "mode": "real", "runtime_state": "FAULT/UNKNOWN", "enabled": True,
            "confirmed_state": "未知", "independent_stop_required": True,
        })
        window.set_stimulation_auto_status(True, "合成自动控制状态")
        qapp.processEvents()
        assert window.state_label.isVisible()
        assert window.run_api_label.isVisible()
        assert window.stop_warning_label.isVisible()
        assert window.disable_automatic_button.isVisible()
        assert window.error_scroll.isVisible()
        assert "物理输出未验证" in window.run_api_label.text()
        assert window.settings_scroll.horizontalScrollBar().maximum() == 0
        window.setStyleSheet(window.styleSheet())
        assert auto_spy.count() == mode_spy.count() == connect_spy.count() == 0
    finally:
        window.close()
