"""P1 desktop presentation layer.

The widgets in this module are intentionally free of network and model
logic. The controller owns the Curry worker; this module only renders state
received on the Qt thread and emits explicit connection actions.
"""
from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QCheckBox,
    QFileDialog,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from curry_netstream.models import DataBlock, SessionInfo

from .epoching import EpochAssemblySnapshot
from .staging import STAGES
from .stimulation import (
    ProtocolScheme,
    StimulationConfig,
    TriggerMode,
    load_protocol_scheme,
)


class CollapsibleSection(QWidget):
    """A disclosure section that keeps its contents when collapsed."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.toggle = QToolButton()
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.content = QWidget()
        self.content.setVisible(False)
        layout.addWidget(self.toggle)
        layout.addWidget(self.content)
        self.toggle.toggled.connect(self.set_expanded)

    @Slot(bool)
    def set_expanded(self, expanded: bool) -> None:
        self.toggle.setChecked(expanded)
        self.toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.content.setVisible(expanded)


class PathSummary(QPlainTextEdit):
    """Read-only, selectable long paths with bounded height and word wrapping."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setFixedHeight(58)
        self.setPlainText(text)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, text: str) -> None:
        self.setPlainText(text)


class MainWindow(QMainWindow):
    """P1 connection/display, P2 recording/replay and P3 local simulation."""

    connect_requested = Signal()
    disconnect_requested = Signal()
    closing_requested = Signal()
    replay_open_requested = Signal()
    replay_exit_requested = Signal()
    replay_index_requested = Signal(int)
    replay_export_csv_requested = Signal()
    stimulation_configuration_changed = Signal(object)
    stimulation_auto_requested = Signal(bool)
    rally_mode_requested = Signal(str)
    rally_profile_requested = Signal(str)
    paradigm_package_requested = Signal(str)
    simulator_start_requested = Signal()
    simulator_stop_requested = Signal()
    request_timeout_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("睡眠分期与刺激控制台")
        self.resize(1000, 720)
        self.setMinimumSize(800, 600)
        self._state = "disconnected"
        self._busy = False
        self._pending_close = False
        self._has_displayed_block = False
        self._last_block_summary = "尚无有效 EEG 数据块"
        self._last_display_note = ""
        self._last_assembly_snapshot: EpochAssemblySnapshot | None = None
        self._handshake_ready = False
        self._session_finished = False
        self._replay_mode = False
        self._replay_worker_busy = False
        self._replay_block_count = 0
        self._replay_overview_ready = False
        self._stage_csv_export_busy = False
        self._p3_background_busy = False
        self._p3_close_requested = False
        self._p3_auto_enabled = False
        self._p3_simulator_active = False
        self._p3_replay_events: tuple[dict, ...] = ()
        self._p3_replay_control_events: tuple[dict, ...] = ()
        self._p3_session_id: str | None = None
        self._p3_protocol: ProtocolScheme | None = None
        self._p3_recent: list[str] = []
        self._rally_mode = "simulation"
        self._rally_profile = "binary"
        self._paradigm_package_summary = "未选择范式包；范式模式默认关闭"

        toolbar = QToolBar("程序信息")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        self.title_label = QLabel("睡眠分期脑电控制器")
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 700;")
        toolbar.addWidget(self.title_label)
        toolbar.addSeparator()
        self.status_summary = QLabel("未连接")
        toolbar.addWidget(self.status_summary)

        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)

        operator = QWidget()
        root_layout.addWidget(operator)
        operator_layout = QVBoxLayout(operator)
        operator_layout.setContentsMargins(0, 0, 0, 0)
        self.mode_label = QLabel("实时 · 等待连接")

        self.state_label = QLabel("状态：未连接")
        self.state_label.setObjectName("connectionState")
        self.state_label.setWordWrap(True)

        controls = QHBoxLayout()
        controls.addWidget(self.mode_label)
        controls.addWidget(self.state_label, 1)
        self.connect_button = QPushButton("连接")
        self.connect_button.setObjectName("connectButton")
        self.connect_button.clicked.connect(self.connect_requested)
        self.disconnect_button = QPushButton("断开")
        self.disconnect_button.setObjectName("disconnectButton")
        self.disconnect_button.clicked.connect(self.disconnect_requested)
        self.connect_button.setFixedWidth(100)
        self.disconnect_button.setFixedWidth(100)
        controls.addWidget(self.connect_button)
        controls.addWidget(self.disconnect_button)
        operator_layout.addLayout(controls)

        self.data_status_label = QLabel(
            "数据状态：等待连接；EEG 波形请在 Curry 8 查看。"
        )
        self.data_status_label.setObjectName("dataStatus")
        self.data_status_label.setWordWrap(True)

        self.block_metadata_label = QLabel("尚无有效数据块")
        self.block_metadata_label.setObjectName("blockMetadata")
        self.block_metadata_label.setWordWrap(True)

        self.assembly_progress_label = QLabel(
            "接收进度：尚未完成 Curry 握手；当前累计 0.0 / 30 秒"
        )
        self.assembly_progress_label.setObjectName("assemblyProgress")
        self.assembly_progress_label.setWordWrap(True)


        self.processing_status_label = QLabel(
            "分期状态：模型默认未启用（NoModel）"
        )
        self.processing_status_label.setObjectName("processingStatus")
        self.processing_status_label.setWordWrap(True)


        self.recording_status_label = QLabel("保存状态：未保存（记录已关闭）")
        self.recording_status_label.setObjectName("recordingStatus")
        self.recording_status_label.setWordWrap(True)


        self.hint_label = QLabel(
            "模型默认关闭（NoModel）；ONNX 睡眠分期可在连接前显式启用 · "
            "仅模拟，真实刺激未接入 · EEG 波形请在 Curry 8 查看"
        )
        self.hint_label.setWordWrap(True)
        self.hint_label.setTextFormat(Qt.TextFormat.PlainText)
        operator_layout.addWidget(self.hint_label)

        self.error_label = QLabel()
        self.error_label.setObjectName("persistentError")
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b42318; font-weight: 600;")
        self.error_label.hide()
        self.error_scroll = QScrollArea()
        self.error_scroll.setWidgetResizable(True)
        self.error_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.error_scroll.setFixedHeight(72)
        self.error_scroll.setWidget(self.error_label)
        self.error_scroll.hide()
        operator_layout.addWidget(self.error_scroll)

        # Only settings and diagnostics scroll; state and errors stay visible.
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setObjectName("settingsScroll")
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings = QWidget()
        settings_layout = QVBoxLayout(settings)
        settings_layout.setContentsMargins(2, 2, 12, 12)
        settings_layout.setSpacing(14)
        self.summary_section = QGroupBox("数据与分期摘要")
        self.summary_section.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        summary_layout = QGridLayout(self.summary_section)
        summary_layout.setVerticalSpacing(4)
        self.session_id_label = QLabel("当前会话：尚未连接")
        summary_layout.addWidget(self.session_id_label, 0, 0, 1, 2)
        summary_layout.addWidget(self.data_status_label, 1, 0)
        summary_layout.addWidget(self.processing_status_label, 1, 1)
        summary_layout.addWidget(self.block_metadata_label, 2, 0, 1, 2)
        summary_layout.addWidget(self.assembly_progress_label, 3, 0, 1, 2)
        summary_layout.setColumnStretch(0, 1)
        summary_layout.setColumnStretch(1, 1)
        settings_layout.addWidget(self.summary_section)

        self.model_section = CollapsibleSection("睡眠分期模型")
        model_layout = QFormLayout(self.model_section.content)
        self.model_enabled_checkbox = QCheckBox("启用 ONNX 分期（默认关闭）")
        self.model_enabled_checkbox.setObjectName("modelEnabled")
        self.model_enabled_checkbox.toggled.connect(self._update_model_controls)
        model_layout.addRow(self.model_enabled_checkbox)
        model_path_row = QWidget()
        model_path_layout = QHBoxLayout(model_path_row)
        model_path_layout.setContentsMargins(0, 0, 0, 0)
        self.model_path_edit = QLineEdit()
        self.model_path_edit.setObjectName("modelPathEdit")
        self.model_path_edit.setPlaceholderText("启用后显式选择一个 ONNX 模型文件")
        self.choose_model_button = QPushButton("选择…")
        self.choose_model_button.setObjectName("chooseOnnxModel")
        self.choose_model_button.clicked.connect(self._choose_model_path)
        model_path_layout.addWidget(self.model_path_edit, 1)
        model_path_layout.addWidget(self.choose_model_button)
        model_layout.addRow("ONNX 文件", model_path_row)
        self.model_channel_edit = QLineEdit("Fpz-Cz")
        self.model_channel_edit.setObjectName("modelChannelEdit")
        self.model_channel_edit.setPlaceholderText("与 Curry 标签精确匹配，如 Fpz-Cz")
        model_layout.addRow("单通道标签", self.model_channel_edit)
        self.available_model_channels_label = QLabel("完成 Curry 握手后显示可用通道")
        self.available_model_channels_label.setWordWrap(True)
        model_layout.addRow("可用通道", self.available_model_channels_label)
        self.model_contract_label = QLabel(
            "仅勾选启用后才校验或加载模型；失败会明确报错，不回退。"
            "输入按 µV 处理，50 Hz 工频处理 + 0.3–35 Hz 带通，"
            "重采样到 100 Hz，不做逐样本 z-score；时间长度由 ONNX 自动读取。"
        )
        self.model_contract_label.setWordWrap(True)
        model_layout.addRow("固定预处理", self.model_contract_label)
        settings_layout.addWidget(self.model_section)

        self.workflow_section = CollapsibleSection("会话记录与离线回放")
        workflow_layout = QVBoxLayout(self.workflow_section.content)
        workflow_layout.setContentsMargins(4, 4, 4, 4)
        self.recording_checkbox = QCheckBox("启用本次会话记录（默认关闭）")
        self.recording_checkbox.setObjectName("recordingEnabled")
        workflow_layout.addWidget(self.recording_status_label)
        workflow_layout.addWidget(self.recording_checkbox)
        self.stage_csv_checkbox = QCheckBox("同时自动生成分期 CSV（默认关闭）")
        self.stage_csv_checkbox.setObjectName("stageCsvEnabled")
        self.stage_csv_checkbox.setEnabled(False)
        self.recording_checkbox.toggled.connect(self._on_recording_toggled)
        self.stage_csv_checkbox.toggled.connect(self._on_stage_csv_toggled)
        workflow_layout.addWidget(self.stage_csv_checkbox)
        self.stage_csv_status_label = QLabel("分期 CSV：未启用")
        self.stage_csv_status_label.setObjectName("stageCsvStatus")
        self.stage_csv_status_label.setWordWrap(True)
        workflow_layout.addWidget(self.stage_csv_status_label)
        choose_path = QHBoxLayout()
        self.choose_recording_dir_button = QPushButton("选择保存目录…")
        self.choose_recording_dir_button.setObjectName("chooseRecordingDirectory")
        self.choose_recording_dir_button.clicked.connect(self._choose_recording_root)
        self.recording_root_label = PathSummary("未选择保存父目录")
        self.recording_root_label.setObjectName("recordingDirectory")
        choose_path.addWidget(self.choose_recording_dir_button)
        choose_path.addWidget(self.recording_root_label, 1)
        workflow_layout.addLayout(choose_path)

        replay_actions = QHBoxLayout()
        self.open_replay_button = QPushButton("打开会话回放…")
        self.open_replay_button.setObjectName("openReplay")
        self.open_replay_button.clicked.connect(self.replay_open_requested)
        self.exit_replay_button = QPushButton("退出回放")
        self.exit_replay_button.setObjectName("exitReplay")
        self.exit_replay_button.clicked.connect(self.replay_exit_requested)
        self.export_stage_csv_button = QPushButton("导出/重新生成 CSV")
        self.export_stage_csv_button.setObjectName("exportStageCsv")
        self.export_stage_csv_button.setEnabled(False)
        self.export_stage_csv_button.clicked.connect(self.replay_export_csv_requested)
        replay_actions.addWidget(self.open_replay_button)
        replay_actions.addWidget(self.exit_replay_button)
        replay_actions.addWidget(self.export_stage_csv_button)
        workflow_layout.addLayout(replay_actions)

        navigation = QHBoxLayout()
        self.replay_previous_button = QPushButton("上一块")
        self.replay_previous_button.setObjectName("replayPrevious")
        self.replay_next_button = QPushButton("下一块")
        self.replay_next_button.setObjectName("replayNext")
        self.replay_previous_button.setEnabled(False)
        self.replay_next_button.setEnabled(False)
        self.replay_index_spin = QSpinBox()
        self.replay_index_spin.setObjectName("replayBlockIndex")
        self.replay_index_spin.setRange(1, 1)
        self.replay_index_spin.setEnabled(False)
        navigation.addWidget(self.replay_previous_button)
        navigation.addWidget(self.replay_index_spin)
        navigation.addWidget(self.replay_next_button)
        workflow_layout.addLayout(navigation)
        self.replay_previous_button.clicked.connect(
            lambda: self.replay_index_spin.setValue(
                max(1, self.replay_index_spin.value() - 1)
            )
        )
        self.replay_next_button.clicked.connect(
            lambda: self.replay_index_spin.setValue(
                min(self._replay_block_count, self.replay_index_spin.value() + 1)
            )
        )
        self.replay_index_spin.valueChanged.connect(
            lambda value: self.replay_index_requested.emit(value - 1)
            if self._replay_mode and self._replay_block_count
            else None
        )
        self.replay_status_label = QLabel("离线回放未打开")
        self.replay_status_label.setObjectName("replayStatus")
        self.replay_status_label.setWordWrap(True)
        workflow_layout.addWidget(self.replay_status_label)

        self.workflow_section.set_expanded(True)

        self.stimulation_section = CollapsibleSection("睡眠期决策与本机模拟")
        stimulation_layout = QVBoxLayout(self.stimulation_section.content)
        stimulation_layout.setContentsMargins(4, 4, 4, 4)
        self.stimulation_warning_label = QLabel(
            "仅模拟，真实刺激未接入（当前默认状态）。真实模式只控制 Rally 已加载协议，"
            "不选择或修改刺激参数。"
        )
        self.stimulation_warning_label.setWordWrap(True)
        self.stimulation_warning_label.setTextFormat(Qt.TextFormat.PlainText)
        self.stimulation_warning_label.setStyleSheet(
            "color: #9a3412; font-weight: 600;"
        )
        stimulation_layout.addWidget(self.stimulation_warning_label)

        mode_row = QHBoxLayout()
        self.rally_mode_combo = QComboBox()
        self.rally_mode_combo.setObjectName("rallyControlMode")
        self.rally_mode_combo.addItem("本机模拟（P3）", "simulation")
        self.rally_mode_combo.addItem(
            "真实 Rally（127.0.0.1:8801；默认关闭）", "real"
        )
        self.rally_mode_combo.currentIndexChanged.connect(self._request_rally_mode)
        mode_row.addWidget(QLabel("控制模式"))
        mode_row.addWidget(self.rally_mode_combo, 1)
        stimulation_layout.addLayout(mode_row)

        profile_row = QHBoxLayout()
        self.rally_profile_combo = QComboBox()
        self.rally_profile_combo.setObjectName("rallyProtocolProfile")
        self.rally_profile_combo.addItem("二值启停（现有协议）", "binary")
        self.rally_profile_combo.addItem("版本化范式（Start + Apply）", "paradigm")
        self.rally_profile_combo.currentIndexChanged.connect(self._request_rally_profile)
        profile_row.addWidget(QLabel("真实协议配置"))
        profile_row.addWidget(self.rally_profile_combo, 1)
        stimulation_layout.addLayout(profile_row)

        paradigm_row = QHBoxLayout()
        self.choose_paradigm_button = QPushButton("选择范式包目录…")
        self.choose_paradigm_button.setObjectName("chooseParadigmPackage")
        self.choose_paradigm_button.clicked.connect(self._choose_paradigm_package)
        self.paradigm_package_summary_label = PathSummary(self._paradigm_package_summary)
        self.paradigm_package_summary_label.setObjectName("paradigmPackageSummary")
        paradigm_row.addWidget(self.choose_paradigm_button)
        paradigm_row.addWidget(self.paradigm_package_summary_label, 1)
        stimulation_layout.addLayout(paradigm_row)

        self.rally_mode_hint_label = QLabel(
            "真实模式要求操作者先在 Rally 中加载并检查协议；软件确认只表示收到 API 回复。"
        )
        self.rally_mode_hint_label.setObjectName("rallyModeHint")
        self.rally_mode_hint_label.setWordWrap(True)
        stimulation_layout.addWidget(self.rally_mode_hint_label)

        stage_row = QHBoxLayout()
        self.stage_checkboxes: dict[str, QCheckBox] = {}
        for stage in STAGES:
            checkbox = QCheckBox(stage)
            checkbox.setObjectName(f"stimTarget{stage}")
            checkbox.toggled.connect(self._request_stimulation_configuration)
            self.stage_checkboxes[stage] = checkbox
            stage_row.addWidget(checkbox)
        stimulation_layout.addLayout(stage_row)

        stimulation_form = QGridLayout()
        stimulation_form.setColumnStretch(1, 1)
        stimulation_form.setColumnStretch(3, 1)
        self.stimulation_strategy_combo = QComboBox()
        self.stimulation_strategy_combo.setObjectName("stimulationStrategy")
        self.stimulation_strategy_combo.addItem("未选择策略", None)
        self.stimulation_strategy_combo.addItem(
            "每个匹配目标期块", TriggerMode.EACH_MATCHING_BLOCK.value
        )
        self.stimulation_strategy_combo.addItem(
            "进入目标期集合", TriggerMode.ENTER_TARGET_SET.value
        )
        self.stimulation_strategy_combo.currentIndexChanged.connect(
            self._request_stimulation_configuration
        )
        stimulation_form.addWidget(QLabel("触发策略"), 0, 0)
        stimulation_form.addWidget(self.stimulation_strategy_combo, 0, 1)

        self.min_interval_edit = QLineEdit()
        self.min_interval_edit.setObjectName("stimulationMinInterval")
        self.min_interval_edit.setPlaceholderText("必填；秒；无预置实验值")
        self.min_interval_edit.editingFinished.connect(
            self._request_stimulation_configuration
        )
        stimulation_form.addWidget(QLabel("最小请求间隔（秒）"), 0, 2)
        stimulation_form.addWidget(self.min_interval_edit, 0, 3)

        self.max_result_age_edit = QLineEdit()
        self.max_result_age_edit.setObjectName("stimulationMaxResultAge")
        self.max_result_age_edit.setPlaceholderText("必填；秒；无预置实验值")
        self.max_result_age_edit.editingFinished.connect(
            self._request_stimulation_configuration
        )
        stimulation_form.addWidget(QLabel("最大结果年龄（秒）"), 1, 0)
        stimulation_form.addWidget(self.max_result_age_edit, 1, 1)

        self.request_timeout_edit = QLineEdit("1.0")
        self.request_timeout_edit.setObjectName("simulatedRequestTimeout")
        self.request_timeout_edit.setPlaceholderText("仅本机模拟通信超时")
        self.request_timeout_edit.editingFinished.connect(
            self._request_timeout_update
        )
        stimulation_form.addWidget(QLabel("模拟通信超时（秒）"), 1, 2)
        stimulation_form.addWidget(self.request_timeout_edit, 1, 3)
        stimulation_layout.addLayout(stimulation_form)

        scheme_row = QHBoxLayout()
        self.choose_protocol_button = QPushButton("选择模拟协议 JSON…")
        self.choose_protocol_button.setObjectName("chooseStimulationProtocol")
        self.choose_protocol_button.clicked.connect(self._choose_stimulation_protocol)
        self.protocol_summary_label = PathSummary("未选择协议；不会发送请求")
        self.protocol_summary_label.setObjectName("stimulationProtocolSummary")
        scheme_row.addWidget(self.choose_protocol_button)
        scheme_row.addWidget(self.protocol_summary_label, 1)
        stimulation_layout.addLayout(scheme_row)

        simulator_row = QHBoxLayout()
        self.simulator_start_button = QPushButton("启动本机模拟端")
        self.simulator_start_button.setObjectName("startRallySimulator")
        self.simulator_start_button.clicked.connect(self.simulator_start_requested)
        self.simulator_stop_button = QPushButton("关闭本机模拟端")
        self.simulator_stop_button.setObjectName("stopRallySimulator")
        self.simulator_stop_button.clicked.connect(self.simulator_stop_requested)
        self.simulator_stop_button.setEnabled(False)
        simulator_row.addWidget(self.simulator_start_button)
        simulator_row.addWidget(self.simulator_stop_button)
        stimulation_layout.addLayout(simulator_row)

        self.simulator_status_label = QLabel("模拟端未运行（不会发送）")
        self.simulator_status_label.setObjectName("simulatorStatus")
        self.simulator_status_label.setWordWrap(True)
        stimulation_layout.addWidget(self.simulator_status_label)

        self.stimulation_auto_checkbox = QCheckBox(
            "启用自动决策（仅发送至本应用模拟端）"
        )
        self.stimulation_auto_checkbox.setObjectName("stimulationAutomaticEnabled")
        self.stimulation_auto_checkbox.toggled.connect(
            self._request_automatic_decision
        )
        stimulation_layout.addWidget(self.stimulation_auto_checkbox)
        self.real_control_confirm_checkbox = QCheckBox(
            "我确认 Rally 已加载并检查协议，且当前未进行刺激；启用真实自动控制"
        )
        self.real_control_confirm_checkbox.setObjectName("realRallyConfirmation")
        self.real_control_confirm_checkbox.setChecked(False)
        self.real_control_confirm_checkbox.toggled.connect(
            self._request_stimulation_configuration
        )
        stimulation_layout.addWidget(self.real_control_confirm_checkbox)
        self.stimulation_auto_status_label = QLabel(
            "自动决策关闭；正常 NoModel 模式不会产生请求。"
        )
        self.stimulation_auto_status_label.setObjectName("stimulationAutoStatus")
        self.stimulation_auto_status_label.setWordWrap(True)
        stimulation_layout.addWidget(self.stimulation_auto_status_label)

        self.rally_control_status_label = QLabel(
            "Rally 控制状态：DISARMED/UNKNOWN · 操作者确认=否 · 尚未启用真实模式"
        )
        self.rally_control_status_label.setObjectName("rallyControlStatus")
        self.rally_control_status_label.setWordWrap(True)
        stimulation_layout.addWidget(self.rally_control_status_label)

        self.stimulation_recent_label = QLabel("尚无实时或回放刺激决策事件")
        self.stimulation_recent_label.setObjectName("stimulationRecentEvents")
        self.stimulation_recent_label.setTextFormat(Qt.TextFormat.PlainText)
        self.stimulation_recent_label.setWordWrap(True)
        stimulation_layout.addWidget(self.stimulation_recent_label)
        settings_layout.addWidget(self.stimulation_section)
        settings_layout.addWidget(self.workflow_section)
        self.stimulation_section.set_expanded(True)

        connection = CollapsibleSection("Curry 连接")
        connection_form = QFormLayout(connection.content)
        self.host_edit = QLineEdit("127.0.0.1")
        self.host_edit.setObjectName("hostEdit")
        self.host_edit.setPlaceholderText("IPv4 地址")
        connection_form.addRow("IPv4 地址", self.host_edit)
        self.port_spin = QSpinBox()
        self.port_spin.setObjectName("portSpin")
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(4455)
        connection_form.addRow("端口", self.port_spin)
        settings_layout.addWidget(connection)
        self.connection_section = connection
        connection.set_expanded(False)

        self.session_section = CollapsibleSection("会话元信息")
        session_layout = QVBoxLayout(self.session_section.content)
        self.session_info = QPlainTextEdit()
        self.session_info.setObjectName("sessionInfo")
        self.session_info.setReadOnly(True)
        self.session_info.setMinimumHeight(110)
        self.session_info.setPlaceholderText("完成 BasicInfo 和 ChannelInfo 握手后显示")
        session_layout.addWidget(self.session_info)
        settings_layout.addWidget(self.session_section)

        self.diagnostics_section = CollapsibleSection("诊断详情")
        diagnostic_layout = QVBoxLayout(self.diagnostics_section.content)
        self.details = QPlainTextEdit()
        self.details.setObjectName("diagnosticDetails")
        self.details.setReadOnly(True)
        self.details.setMinimumHeight(150)
        self.details.setPlaceholderText("连接、块校验和有界显示交接信息")
        diagnostic_layout.addWidget(self.details)
        settings_layout.addWidget(self.diagnostics_section)
        settings_layout.addStretch()
        self.settings_scroll.setWidget(settings)
        operator_layout.addWidget(self.settings_scroll, 1)

        self.setStyleSheet("""
            QMainWindow { background: #f3f5f7; }
            QWidget { font-size: 13px; color: #223142; }
            QGroupBox { font-weight: 600; border: 1px solid #d7dee5;
                        border-radius: 6px; margin-top: 10px; padding: 14px 10px 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; }
            QPushButton, QLineEdit, QComboBox, QSpinBox { min-height: 26px; }
            QToolButton { font-weight: 600; padding: 5px 0; }
            QPushButton#connectButton { background: #246b82; color: white;
                                       border-radius: 4px; padding: 3px 16px; }
            QWidget:disabled { color: #8a9299; }
            QPushButton#connectButton:disabled { background: #d7dee5; color: #78838c; }
        """)
        for label in self.findChildren(QLabel):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setWordWrap(True)
            label.setMinimumWidth(0)
        for label in (self.data_status_label, self.block_metadata_label,
                      self.assembly_progress_label, self.processing_status_label,
                      self.recording_root_label, self.stage_csv_status_label,
                      self.protocol_summary_label, self.stimulation_recent_label,
                      self.error_label, self.session_id_label, self.replay_status_label):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.statusBar().showMessage("就绪 · 等待 Curry 连接")

        self.set_state("disconnected", "等待连接")
        self.set_busy(False)

    def configuration(self) -> dict[str, str | int | bool | None]:
        return {
            "host": self.host_edit.text().strip(),
            "port": self.port_spin.value(),
            "recording_enabled": self.recording_checkbox.isChecked(),
            "recording_root": self._recording_root,
            "stage_csv_enabled": self.stage_csv_checkbox.isChecked(),
        }

    def model_configuration(self) -> dict[str, str]:
        model_path_text = self.model_path_edit.text().strip()
        channel_name = self.model_channel_edit.text().strip()
        if not model_path_text:
            raise ValueError("请选择 ONNX 模型文件")
        if not channel_name:
            raise ValueError("请输入一个 Curry 单通道标签")
        model_path = Path(model_path_text).expanduser().resolve(strict=True)
        if not model_path.is_file():
            raise ValueError(f"ONNX 路径不是文件：{model_path}")
        if model_path.suffix.casefold() != ".onnx":
            raise ValueError(f"模型文件扩展名必须是 .onnx：{model_path}")
        return {"model_path": str(model_path), "channel_name": channel_name}

    @Slot()
    def _choose_model_path(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择睡眠分期 ONNX 模型",
            self.model_path_edit.text().strip(),
            "ONNX 模型 (*.onnx);;所有文件 (*)",
        )
        if selected:
            self.model_path_edit.setText(selected)

    @property
    def _recording_root(self) -> str | None:
        path = self.recording_root_label.property("path")
        return str(path) if path else None

    def _choose_recording_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "选择会话保存父目录", self._recording_root or ""
        )
        if selected:
            self.set_recording_root(selected)

    def _optional_seconds(self, edit: QLineEdit, label: str) -> float | None:
        text = edit.text().strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError(f"{label}必须是有限数值") from exc
        if not math.isfinite(value):
            raise ValueError(f"{label}必须是有限数值")
        if label == "最小请求间隔" and value < 0:
            raise ValueError("最小请求间隔须 >=0 秒")
        if label == "最大结果年龄" and value <= 0:
            raise ValueError("最大结果年龄须 >0 秒")
        return value

    def stimulation_configuration(self) -> StimulationConfig:
        selected_mode = self.stimulation_strategy_combo.currentData()
        mode = TriggerMode(selected_mode) if selected_mode else None
        return StimulationConfig(
            target_stages=frozenset(
                stage
                for stage, checkbox in self.stage_checkboxes.items()
                if checkbox.isChecked()
            ),
            trigger_mode=mode,
            min_request_interval_seconds=self._optional_seconds(
                self.min_interval_edit, "最小请求间隔"
            ),
            max_result_age_seconds=self._optional_seconds(
                self.max_result_age_edit, "最大结果年龄"
            ),
            protocol=self._p3_protocol,
        )

    def _request_stimulation_configuration(self, *_args) -> None:
        try:
            config = self.stimulation_configuration()
        except ValueError as exc:
            self.stimulation_auto_status_label.setText(f"配置输入无效：{exc}")
            return
        self.stimulation_configuration_changed.emit(config)

    @Slot()
    def _choose_stimulation_protocol(self) -> None:
        if self._p3_auto_enabled or self._replay_mode:
            return
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择本地模拟协议 JSON",
            "",
            "JSON 文件 (*.json);;所有文件 (*)",
        )
        if not selected:
            return
        try:
            scheme = load_protocol_scheme(selected)
        except (OSError, ValueError) as exc:
            self.set_error(f"模拟协议结构无效：{exc}")
            return
        self._p3_protocol = scheme
        self.set_error("")
        self.protocol_summary_label.setText(
            f"{scheme.name} · {scheme.protocol_version} · "
            f"刺激通道 {', '.join(scheme.channel_names)} · "
            f"来源 {scheme.source_path} · 模拟配置，未核实真机限制"
        )
        self._request_stimulation_configuration()

    @Slot()
    def _request_timeout_update(self) -> None:
        try:
            value = self._optional_seconds(
                self.request_timeout_edit, "模拟通信超时"
            )
            if value is None or value <= 0:
                raise ValueError("模拟通信超时须为有限正数")
        except ValueError as exc:
            self.stimulation_auto_status_label.setText(f"通信参数无效：{exc}")
            return
        self.request_timeout_changed.emit(value)

    @Slot(int)
    def _request_rally_mode(self, _index: int) -> None:
        mode = str(self.rally_mode_combo.currentData() or "simulation")
        self._rally_mode = mode
        self._update_rally_mode_copy(mode)
        self.stimulation_auto_checkbox.setText(
            "启用真实 Rally 自动控制（需明确确认）"
            if mode == "real"
            else "启用自动决策（仅发送至本应用模拟端）"
        )
        self.rally_mode_requested.emit(mode)
        self._update_rally_profile_copy()
        self._update_stimulation_controls()

    def _request_rally_profile(self, _index: int) -> None:
        profile = str(self.rally_profile_combo.currentData() or "binary")
        self._rally_profile = profile
        self.rally_profile_requested.emit(profile)
        self._update_rally_profile_copy()
        self._update_stimulation_controls()

    def _choose_paradigm_package(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择包含 paradigm.json 的范式包目录")
        if selected:
            self.paradigm_package_requested.emit(selected)

    def set_rally_profile(self, profile: str) -> None:
        normalized = "paradigm" if str(profile).strip().lower() == "paradigm" else "binary"
        self._rally_profile = normalized
        self.rally_profile_combo.blockSignals(True)
        self.rally_profile_combo.setCurrentIndex(
            max(0, self.rally_profile_combo.findData(normalized))
        )
        self.rally_profile_combo.blockSignals(False)
        self._update_rally_profile_copy()
        self._update_stimulation_controls()

    def set_paradigm_package_summary(
        self,
        path: str,
        *,
        name: str,
        version: str,
        sha256: str,
        classification: str,
    ) -> None:
        self.paradigm_package_summary_label.setText(
            f"{path}\n{name} · {version} · {classification}\nSHA-256 {sha256}"
        )

    def _update_rally_profile_copy(self) -> None:
        if self._rally_profile == "paradigm":
            self.stimulation_auto_checkbox.setText(
                "启用范式自动控制（Start + 最新协议 Apply；需人工确认）"
            )
            self.real_control_confirm_checkbox.setText(
                "我已核对 Rally 基础协议与所选范式包，且当前未刺激；申请范式自动控制"
            )
        else:
            self.stimulation_auto_checkbox.setText(
                "启用真实 Rally 自动控制（需明确确认）"
                if self._rally_mode == "real"
                else "启用自动决策（仅发送至本应用模拟端）"
            )
            self.real_control_confirm_checkbox.setText(
                "我确认 Rally 已加载并检查协议，且当前未进行刺激；启用真实自动控制"
            )

    def set_rally_mode(self, mode: str) -> None:
        normalized = "real" if str(mode).strip().lower() == "real" else "simulation"
        index = self.rally_mode_combo.findData(normalized)
        if index < 0:
            return
        self.rally_mode_combo.blockSignals(True)
        self.rally_mode_combo.setCurrentIndex(index)
        self.rally_mode_combo.blockSignals(False)
        self._rally_mode = normalized
        self._update_rally_mode_copy(normalized)
        self._update_rally_profile_copy()
        self.stimulation_auto_checkbox.setText(
            "启用真实 Rally 自动控制（需明确确认）"
            if normalized == "real"
            else "启用自动决策（仅发送至本应用模拟端）"
        )
        self._update_rally_profile_copy()
        self._update_stimulation_controls()

    def _update_rally_mode_copy(self, mode: str) -> None:
        if mode == "real":
            self.stimulation_warning_label.setText(
                "真实 Rally 模式：只控制 Rally 当前已加载协议，不选择或修改刺激参数；"
                "API 确认不等于物理输出确认。"
            )
        else:
            self.stimulation_warning_label.setText(
                "仅模拟，真实刺激未接入（当前默认状态）。真实模式只控制 Rally 已加载协议，"
                "不选择或修改刺激参数。"
            )

    @Slot(bool)
    def _request_automatic_decision(self, enabled: bool) -> None:
        if enabled:
            if self._rally_mode == "real" and not self.real_control_confirm_checkbox.isChecked():
                self.stimulation_auto_checkbox.blockSignals(True)
                self.stimulation_auto_checkbox.setChecked(False)
                self.stimulation_auto_checkbox.blockSignals(False)
                self.set_stimulation_auto_status(
                    False,
                    "真实 Rally 控制未启用：请先确认协议已加载并检查，且当前未进行刺激。",
                )
                return
            if not (self._rally_mode == "real" and self._rally_profile == "paradigm"):
                try:
                    config = self.stimulation_configuration()
                except ValueError as exc:
                    self.set_stimulation_auto_status(False, f"配置输入无效：{exc}")
                    return
                self.stimulation_configuration_changed.emit(config)
        self.stimulation_auto_requested.emit(enabled)

    @Slot(bool, str)
    def set_stimulation_auto_status(self, enabled: bool, detail: str) -> None:
        self._p3_auto_enabled = enabled
        self.stimulation_auto_checkbox.blockSignals(True)
        self.stimulation_auto_checkbox.setChecked(enabled)
        self.stimulation_auto_checkbox.blockSignals(False)
        self.stimulation_auto_status_label.setText(detail)
        self._update_stimulation_controls()

    @Slot(object)
    def set_rally_status(self, status: dict) -> None:
        if not isinstance(status, dict):
            return
        mode = status.get("mode", self._rally_mode)
        enabled = bool(status.get("enabled", False))
        runtime_state = status.get("runtime_state", "DISARMED/UNKNOWN")
        expected = status.get("expected_state") or "—"
        desired = status.get("desired_state") or "—"
        confirmed = status.get("confirmed_state") or "未知"
        last_request = status.get("last_request") or {}
        last_outcome = status.get("last_outcome") or {}
        independent = bool(status.get("independent_stop_required", False))
        operator_confirmed = bool(status.get("operator_confirmed", False))
        start_responsibility = bool(status.get("start_sent_responsibility", False))
        endpoint = status.get("endpoint") or "未知"
        profile = str(status.get("profile") or "binary")
        if profile in {"binary", "paradigm"} and profile != self._rally_profile:
            self.set_rally_profile(profile)
        desired_protocol = status.get("latest_desired_protocol") or "无"
        confirmed_protocol = status.get("api_confirmed_protocol") or "未知"
        package = ""
        if status.get("paradigm_name"):
            package = (
                f"\n范式={status.get('paradigm_name')}@{status.get('paradigm_version')} "
                f"· {status.get('paradigm_classification')}"
            )
        expiry = status.get("estimated_expiry_utc")
        request_text = "无"
        if isinstance(last_request, dict):
            request_text = (
                f"{last_request.get('command', '?')} / "
                f"{last_request.get('status', '?')}"
            )
        outcome_text = "无"
        if isinstance(last_outcome, dict):
            outcome_text = (
                f"{last_outcome.get('command', '?')} / "
                f"{last_outcome.get('status', '?')} / "
                f"{last_outcome.get('raw_text') or last_outcome.get('message', '')}"
            )
        detail = (
            f"Rally 控制状态：{runtime_state} · 自动控制={'开' if enabled else '关'} · "
            f"端点={endpoint}\n"
            f"操作者确认未刺激={'是' if operator_confirmed else '否'} · "
            f"Start 收尾责任={'有' if start_responsibility else '无'}\n"
            f"期望={expected} · 最新期望={desired} · API最近确认={confirmed}\n"
            f"最近请求={request_text}\n最近回复={outcome_text}"
            f"{package}"
        )
        if profile == "paradigm":
            detail += (
                f"\n范式 latest desired={desired_protocol} · API confirmed={confirmed_protocol}"
                f"\n估计到期={expiry or '不可估计'} · 物理输出确认=否"
            )
        if independent:
            detail += "\n⚠ 未获得停止确认：请在 Rally/硬件侧独立停止。"
        if mode == "simulation":
            detail = "Rally 控制状态：模拟模式；真实端点不会发送\n" + detail
        self.rally_control_status_label.setText(detail)
        self._update_stimulation_controls()

    @Slot(bool, str)
    def set_simulator_status(self, active: bool, detail: str) -> None:
        self._p3_simulator_active = active
        self.simulator_status_label.setText(detail)
        self._update_stimulation_controls()

    def _update_stimulation_controls(self) -> None:
        editable = (
            not self._p3_auto_enabled
            and not self._replay_mode
            and not self._p3_close_requested
        )
        for checkbox in self.stage_checkboxes.values():
            checkbox.setEnabled(editable)
        self.stimulation_strategy_combo.setEnabled(editable)
        self.min_interval_edit.setEnabled(editable)
        self.max_result_age_edit.setEnabled(editable)
        self.request_timeout_edit.setEnabled(editable)
        self.choose_protocol_button.setEnabled(
            editable and self._rally_mode == "simulation"
        )
        self.rally_profile_combo.setEnabled(
            editable and self._rally_mode == "real" and not self._busy
        )
        self.choose_paradigm_button.setEnabled(
            editable and self._rally_profile == "paradigm"
        )
        self.paradigm_package_summary_label.setVisible(self._rally_profile == "paradigm")
        simulation_widgets_visible = self._rally_mode == "simulation"
        self.choose_protocol_button.setVisible(simulation_widgets_visible)
        self.protocol_summary_label.setVisible(simulation_widgets_visible)
        self.simulator_start_button.setVisible(simulation_widgets_visible)
        self.simulator_stop_button.setVisible(simulation_widgets_visible)
        self.simulator_status_label.setVisible(simulation_widgets_visible)
        self.rally_mode_combo.setEnabled(
            not self._p3_auto_enabled
            and not self._replay_mode
            and not self._p3_close_requested
            and not self._busy
        )
        self.real_control_confirm_checkbox.setEnabled(
            self._rally_mode == "real"
            and not self._p3_auto_enabled
            and not self._replay_mode
            and not self._p3_close_requested
        )
        self.stimulation_auto_checkbox.setEnabled(
            not self._replay_mode and not self._p3_close_requested
        )
        self.simulator_start_button.setEnabled(
            not self._p3_simulator_active
            and self._rally_mode == "simulation"
            and not self._replay_mode
            and not self._p3_close_requested
        )
        self.simulator_stop_button.setEnabled(
            self._p3_simulator_active
            and self._rally_mode == "simulation"
            and not self._replay_mode
            and not self._p3_close_requested
        )

    def _append_stimulation_recent(self, text: str) -> None:
        self._p3_recent.append(text)
        self._p3_recent = self._p3_recent[-20:]
        self.stimulation_recent_label.setText("\n".join(self._p3_recent))

    @Slot(object)
    def set_stimulation_decision(self, decision) -> None:
        if self._replay_mode or self._p3_session_id is None:
            return
        if decision.session_id != self._p3_session_id:
            return
        if self._rally_mode == "real":
            return
        verdict = "候选允许（仅模拟）" if decision.allowed else "抑制"
        model = decision.model
        marker = " · 测试替身" if decision.test_double else ""
        self._append_stimulation_recent(
            f"实时 block={decision.block_id} stage={decision.stage or '无'} · "
            f"{verdict} · {decision.reason} · "
            f"model={model.get('model_id', '?')}@{model.get('version', '?')}"
            f"{marker}"
        )

    @Slot(object)
    def set_stimulation_request(self, update: dict) -> None:
        if self._replay_mode or self._p3_session_id is None:
            return
        if update.get("mode") == "real":
            source = update.get("response_source") or "未确认来源"
            self._append_stimulation_recent(
                f"实时 Rally {update.get('command', '?')} · "
                f"{update.get('status', '?')} · {update.get('message', '')} · "
                f"来源 {source}"
            )
            return
        request_id = update.get("request_id", "?")
        status = update.get("status", "?")
        self._append_stimulation_recent(
            f"实时 block={update.get('block_id', '?')} 请求 {str(request_id)[:8]} · "
            f"{status} · {update.get('message', '')}"
        )

    def set_live_stimulation_session(self, session_id: str | None) -> None:
        if session_id != self._p3_session_id:
            self._p3_recent.clear()
            self.stimulation_recent_label.setText("尚无实时或回放刺激决策事件")
        self._p3_session_id = session_id
        self.real_control_confirm_checkbox.blockSignals(True)
        self.real_control_confirm_checkbox.setChecked(False)
        self.real_control_confirm_checkbox.blockSignals(False)
        self.session_id_label.setText(f"当前会话：{session_id}")

    def set_replay_stimulation_events(self, events: Sequence[dict]) -> None:
        if not self._replay_mode:
            return
        self.stimulation_recent_label.setText("此回放块尚无已记录的刺激事件")
        self._p3_replay_events = tuple(events)
        self._p3_recent.clear()
        for event in (*self._p3_replay_control_events, *events):
            event_type = event.get("event_type")
            payload = event.get("payload", {})
            block_id = event.get("block_id", "?")
            if not isinstance(payload, dict):
                continue
            if event_type == "decision":
                verdict = "候选允许" if payload.get("allowed") else "抑制"
                model = payload.get("model", {})
                marker = " · 测试替身" if payload.get("test_double") else ""
                detail = (
                    f"回放 block={block_id} stage={payload.get('stage') or '无'} · "
                    f"{verdict} · {payload.get('reason', '')} · "
                    f"model={model.get('model_id', '?')}@{model.get('version', '?')}"
                    f"{marker}"
                )
            elif event_type == "request_sent":
                detail = f"回放 block={block_id} 请求已发送 · 等待响应"
            elif event_type == "request_outcome":
                detail = (
                    f"回放 block={block_id} 请求结果={payload.get('status', '?')} · "
                    f"{payload.get('message', '')}"
                )
            elif event_type in {"rally_control", "paradigm_control"}:
                phase = payload.get("phase", "?")
                label = (
                    f"范式 {payload.get('action') or payload.get('operation') or '配置'}"
                    if event_type == "paradigm_control"
                    else payload.get("command") or "配置/无命令"
                )
                detail = (
                    f"回放 Rally phase={phase} · {label} · "
                    f"期望={payload.get('desired_protocol') or payload.get('desired_state') or '无'} · "
                    f"API确认={payload.get('api_confirmed_protocol') or payload.get('confirmed_state') or '未知'} · "
                    f"{payload.get('outcome', '?')} · 仅展示，不发送"
                )
            else:
                continue
            self._append_stimulation_recent(detail)

    def set_replay_control_events(self, events: Sequence[dict]) -> None:
        if not self._replay_mode:
            return
        self._p3_replay_control_events = tuple(
            event
            for event in events
            if event.get("block_id") is None
            and event.get("event_type") in {"rally_control", "paradigm_control"}
        )
        self.set_replay_stimulation_events(self._p3_replay_events)

    def set_recording_root(self, path: str) -> None:
        self.recording_root_label.setProperty("path", path)
        self.recording_root_label.setText(path)

    @Slot(bool)
    def _on_recording_toggled(self, enabled: bool) -> None:
        if not enabled and self.stage_csv_checkbox.isChecked():
            self.stage_csv_checkbox.setChecked(False)
        self.stage_csv_checkbox.setEnabled(
            enabled and not self._busy and not self._replay_mode
        )
        if not enabled:
            self.stage_csv_status_label.setText("分期 CSV：未启用")
        elif self.stage_csv_checkbox.isChecked():
            self.stage_csv_status_label.setText(
                "分期 CSV：已选择；连接后写入本次新会话的 stage_labels.csv"
            )

    @Slot(bool)
    def _on_stage_csv_toggled(self, enabled: bool) -> None:
        if enabled:
            self.stage_csv_status_label.setText(
                "分期 CSV：已选择；连接后写入本次新会话的 stage_labels.csv"
            )
        elif not self._replay_mode:
            self.stage_csv_status_label.setText("分期 CSV：未启用")

    @Slot(str)
    def set_recording_status(self, text: str) -> None:
        self.recording_status_label.setText("保存状态：" + text)

    @Slot(str)
    def set_stage_csv_status(self, text: str) -> None:
        self.stage_csv_status_label.setText("分期 CSV：" + text)

    def set_stage_csv_export_busy(self, busy: bool) -> None:
        self._stage_csv_export_busy = busy
        self._update_button_state()

    def _format_processing_result(self, result) -> str:
        if result is None:
            return "分期状态：模型默认未启用（NoModel）"
        if isinstance(result, dict):
            model = result.get("model", {})
            status = str(result.get("status", "failed"))
            block_id = result.get("block_id", "?")
            start = result.get("start_sample", "?")
            end = result.get("end_sample_exclusive", "?")
            stage = result.get("stage")
            confidence = result.get("confidence")
            elapsed = result.get("elapsed_ms")
            reason = result.get("reason")
        else:
            status = result.status.value
            model = result.model.to_dict()
            block_id = result.block_id
            start = result.start_sample
            end = result.end_sample_exclusive
            stage = result.stage
            confidence = result.confidence
            elapsed = result.elapsed_ms
            reason = result.reason
        if status == "unavailable":
            detail = "模型未接入"
        elif status == "success":
            detail = f"{stage}"
            if confidence is not None:
                detail += f" · confidence={float(confidence):.3f}"
        elif status == "cancelled":
            detail = "分期已取消"
        else:
            detail = f"分期失败：{reason or '原因未知'}"
        if elapsed is not None:
            detail += f" · {float(elapsed):.2f} ms"
        if model.get("is_test_double"):
            detail += " · 测试替身"
        elif model.get("model_id") and model.get("model_id") != "none":
            detail += f" · {model['model_id']}"
        return (
            f"分期状态：block_id={block_id} · samples [{start}, {end}) · "
            f"{detail}"
        )

    @Slot(object)
    def set_processing_result(self, result) -> None:
        self.processing_status_label.setText(self._format_processing_result(result))

    @Slot(str, str)
    def set_state(self, state: str, detail: str) -> None:
        labels = {
            "disconnected": "未连接",
            "connecting": "连接中",
            "streaming": "推流中",
            "stopping": "停止中",
            "error": "错误",
        }
        self._state = state
        state_text = labels.get(state, state)
        self.state_label.setTextFormat(Qt.TextFormat.PlainText)
        self.state_label.setText(f"状态：{state_text} · {detail}")
        self.status_summary.setText(f"{state_text} · {detail}")
        self.statusBar().showMessage(f"{state_text} · {detail}")
        if not self._replay_mode and not self._session_finished and not self._has_displayed_block:
            if state == "connecting" and not self._handshake_ready:
                self.data_status_label.setText(
                    "数据状态：正在连接 Curry，等待 BasicInfo / ChannelInfo 握手"
                )
                self.assembly_progress_label.setText(
                    "接收进度：正在连接/等待握手；尚未收到合法网络包"
                )
            elif state == "streaming" and self._handshake_ready:
                self.data_status_label.setText(
                    "数据状态：已连接；正在接收短包并按采样点累积分析窗口"
                )
                if self._last_assembly_snapshot is None:
                    self.assembly_progress_label.setText(
                        "接收进度：已连接；收包 0 个 · 完整窗口 0 个 · "
                        "当前累计 0.0 / 30 秒"
                    )
            elif state == "stopping":
                self.data_status_label.setText("数据状态：正在停止实时会话")
            elif state == "error":
                self.data_status_label.setText("数据状态：连接/会话失败，正在收尾")
        self._update_button_state()

    @Slot(bool)
    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.host_edit.setEnabled(not busy)
        self.port_spin.setEnabled(not busy)
        self.model_enabled_checkbox.setEnabled(not busy and not self._replay_mode)
        self._update_model_controls()
        self.recording_checkbox.setEnabled(not busy and not self._replay_mode)
        self.stage_csv_checkbox.setEnabled(
            self.recording_checkbox.isChecked() and not busy and not self._replay_mode
        )
        self.choose_recording_dir_button.setEnabled(not busy and not self._replay_mode)
        self._update_button_state()
        self._maybe_finish_pending_close()

    def _update_button_state(self) -> None:
        self.connect_button.setEnabled(
            not self._busy
            and not self._replay_mode
            and self._state in {"disconnected", "error"}
        )
        self.disconnect_button.setEnabled(
            self._busy and not self._replay_mode and self._state in {"connecting", "streaming"}
        )
        self.open_replay_button.setEnabled(
            not self._busy and not self._replay_mode and not self._replay_worker_busy
        )
        self.exit_replay_button.setEnabled(self._replay_mode and self._replay_worker_busy)
        self.export_stage_csv_button.setEnabled(
            self._replay_mode
            and self._replay_overview_ready
            and not self._stage_csv_export_busy
        )

    def set_replay_worker_busy(self, busy: bool) -> None:
        self._replay_worker_busy = busy
        self._update_button_state()
        self._maybe_finish_pending_close()

    @Slot(bool)
    def set_p3_background_busy(self, busy: bool) -> None:
        self._p3_background_busy = busy
        self._maybe_finish_pending_close()

    def _maybe_finish_pending_close(self) -> None:
        if (
            self._pending_close
            and not self._busy
            and not self._replay_worker_busy
            and not self._p3_background_busy
        ):
            self._pending_close = False
            QTimer.singleShot(0, self.close)

    def set_replay_mode(self, active: bool, message: str = "") -> None:
        entering = active and not self._replay_mode
        self._replay_mode = active
        self.host_edit.setEnabled(not active and not self._busy)
        self.port_spin.setEnabled(not active and not self._busy)
        self.model_enabled_checkbox.setEnabled(not active and not self._busy)
        self._update_model_controls()
        self.recording_checkbox.setEnabled(not active and not self._busy)
        self.stage_csv_checkbox.setEnabled(
            not active and not self._busy and self.recording_checkbox.isChecked()
        )
        self.choose_recording_dir_button.setEnabled(not active and not self._busy)
        self._update_stimulation_controls()

        if entering:
            self._replay_overview_ready = False
            self._stage_csv_export_busy = False
            self.mode_label.setText("离线回放 · 加载中")
            self.session_id_label.setText("回放会话：加载中")
            self._last_block_summary = "尚无回放块"
            self.session_info.clear()
            self._has_displayed_block = False
            self._session_finished = False
            self._last_display_note = ""
            self._last_assembly_snapshot = None
            self._handshake_ready = False
            self._p3_session_id = None
            self._p3_replay_events = ()
            self._p3_replay_control_events = ()
            self._p3_recent.clear()
            self.stimulation_recent_label.setText("此回放块尚无已记录的刺激事件")
            self.block_metadata_label.setText("等待离线回放数据块")
            self.assembly_progress_label.setText("接收进度：离线回放不显示实时累积进度")
            self.data_status_label.setText("数据状态：正在打开离线回放")
            self.processing_status_label.setText("离线回放：等待已记录分期结果")
        if not active:
            self._replay_overview_ready = False
            self._handshake_ready = False
            self.mode_label.setText("历史离线回放" if self._has_displayed_block else "实时 · 未连接")
            if not self._has_displayed_block:
                self.session_id_label.setText("当前会话：尚未连接")
            self._replay_block_count = 0
            self.replay_index_spin.setEnabled(False)
            self.replay_previous_button.setEnabled(False)
            self.replay_next_button.setEnabled(False)
            self.replay_status_label.setText("离线回放未打开")
            if self._has_displayed_block:
                self.data_status_label.setText(
                    "数据状态：历史离线回放数据（回放已退出）"
                )
            else:
                self.assembly_progress_label.setText(
                    "接收进度：尚未连接；当前累计 0.0 / 30 秒"
                )
        elif message:
            self.replay_status_label.setText(message)
        self._update_button_state()
        self._update_stimulation_controls()

    @Slot()
    def _update_model_controls(self) -> None:
        enabled = (
            self.model_enabled_checkbox.isChecked()
            and not self._busy
            and not self._replay_mode
        )
        self.model_path_edit.setEnabled(enabled)
        self.choose_model_button.setEnabled(enabled)
        self.model_channel_edit.setEnabled(enabled)

    def set_replay_ready(self, overview) -> None:
        self._replay_overview_ready = True
        self.session_id_label.setText(f"回放会话：{overview.session_id}")
        self._replay_block_count = overview.block_count
        if not overview.block_count:
            self.clear_block_summary("空会话：0 个已记录块")
        self.replay_index_spin.blockSignals(True)
        self.replay_index_spin.setRange(1, max(1, overview.block_count))
        self.replay_index_spin.setValue(1)
        self.replay_index_spin.blockSignals(False)
        self._update_replay_navigation(0)
        detail = (
            f"离线回放 · {overview.session_id} · {overview.block_count} 个已记录块 · "
            f"状态={overview.status}"
        )
        if overview.incomplete:
            preview = "；".join(overview.issues[:3])
            extra = len(overview.issues) - 3
            if extra > 0:
                preview += f"；另有 {extra} 项"
            detail += " · 不完整：" + preview
        self.replay_status_label.setText(detail)
        self.status_summary.setText("离线回放")
        self.statusBar().showMessage(detail)
        self._update_button_state()

    def show_replay_block(self, block: DataBlock, entry, index: int, total: int) -> None:
        self.replay_index_spin.blockSignals(True)
        self.replay_index_spin.setValue(index + 1)
        self.replay_index_spin.blockSignals(False)
        self._update_replay_navigation(index)
        self.show_block(block, entry.block_id, 0)
        self.block_metadata_label.setText(
            f"离线回放 · block_id={entry.block_id} · 样本区间 "
            f"[{entry.start_sample}, {entry.end_sample_exclusive})\n"
            f"标签：{', '.join(entry.labels)} · {entry.sample_rate_hz:g} Hz · "
            f"units=unknown · 接收时间 {entry.received_utc}"
        )
        self.replay_status_label.setText(
            f"离线回放 · 第 {index + 1}/{total} 块 · block_id={entry.block_id}"
        )

    def _update_replay_navigation(self, index: int) -> None:
        has_blocks = self._replay_mode and self._replay_block_count > 0
        self.replay_index_spin.setEnabled(has_blocks)
        self.replay_previous_button.setEnabled(has_blocks and index > 0)
        self.replay_next_button.setEnabled(
            has_blocks and index < self._replay_block_count - 1
        )

    @Slot(str)
    def set_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))
        self.error_scroll.setVisible(bool(text))

    @Slot(str)
    def set_diagnostics(self, text: str) -> None:
        if self._last_display_note:
            text = f"{text}\n{self._last_display_note}"
        self.details.setPlainText(text)

    @Slot(object)
    def set_assembly_progress(self, snapshot: EpochAssemblySnapshot) -> None:
        """Render throttled live progress without allowing replay overwrite."""
        if self._replay_mode or self._session_finished:
            return
        if not isinstance(snapshot, EpochAssemblySnapshot):
            return
        self._last_assembly_snapshot = snapshot
        self.assembly_progress_label.setText(
            f"接收进度：收包 {snapshot.received_packets} 个 · "
            f"样本 {snapshot.received_samples} 点 · "
            f"完整窗口 {snapshot.completed_windows} 个（已接纳 "
            f"{snapshot.accepted_windows} 个）· 当前累计 "
            f"{snapshot.pending_seconds:.1f} / {snapshot.window_seconds:g} 秒"
        )
        if not self._has_displayed_block and self._handshake_ready:
            self.data_status_label.setText(
                "数据状态：已连接；正在接收短包并按采样点累积分析窗口"
            )

    @Slot(object)
    def set_session(self, session: SessionInfo | None) -> None:
        if session is None:
            self._handshake_ready = False
            self.session_info.setPlainText("尚未完成 BasicInfo / ChannelInfo 握手")
            self.available_model_channels_label.setText("完成 Curry 握手后显示可用通道")
            return
        self._handshake_ready = True
        self.available_model_channels_label.setText(", ".join(session.labels))
        text = (
            f"EEG 通道数：{session.n_channels}\n"
            f"采样率：{session.sample_rate_hz:g} Hz\n"
            f"标签：{', '.join(session.labels)}\n"
            "显示单位：原始值（单位未确认）\n"
            "模型支路：选中通道按 µV 合同解释\n"
            "EEG 波形在 Curry 8 查看；接收时间不是精确采样时刻"
        )
        self.session_info.setPlainText(text)
        if not self._replay_mode and not self._session_finished and not self._has_displayed_block:
            self.data_status_label.setText(
                "数据状态：已连接；正在接收短包并按采样点累积分析窗口"
            )

    @Slot()
    def begin_session(self) -> None:
        self.set_error("")
        self._session_finished = False
        self._last_assembly_snapshot = None
        self._handshake_ready = False
        self.mode_label.setText("实时 · 正在连接")
        self.clear_block_summary("正在连接 Curry，等待 BasicInfo / ChannelInfo 握手")
        self.assembly_progress_label.setText(
            "接收进度：正在连接/等待握手；尚未收到合法网络包"
        )

    @Slot(object, int, int, bool, object)
    def show_block(
        self,
        block: DataBlock,
        block_number: int,
        replaced_count: int,
        historical: bool = False,
        history_reason: str | None = None,
    ) -> None:
        historical = historical or self._session_finished
        n_samples = block.n_samples
        duration = n_samples / float(block.sample_rate_hz)
        prefix = "离线回放" if self._replay_mode else "最新有效块"
        summary = (
            f"{prefix} #{block_number} · start_sample={block.start_sample} · "
            f"通道数={block.n_channels} · 采样率={block.sample_rate_hz:g} Hz · "
            f"每通道样本数={n_samples} · 持续时间={duration:.3f} 秒"
        )
        self._last_block_summary = summary
        self._has_displayed_block = True
        if self._replay_mode:
            self.data_status_label.setText("数据状态：离线回放 · " + summary)
        elif historical:
            reason = history_reason or "会话已结束"
            self.data_status_label.setText(
                f"数据状态：历史数据 · {summary} · {reason}"
            )
        else:
            self.data_status_label.setText("数据状态：" + summary)
        self.mode_label.setText("离线回放" if self._replay_mode else ("历史数据" if historical else "实时"))
        self.block_metadata_label.setText(
            f"block_id={block_number} · 样本区间 [{block.start_sample}, {block.start_sample + n_samples})\n"
            "标签：" + ", ".join(block.labels) + " · 原始值（单位未确认）"
        )
        self._last_display_note = "界面仅更新块摘要；完整 EEG 仍用于处理及可选保存。"
        if replaced_count:
            self._last_display_note += (
                f"\n界面摘要累计替换过期更新：{replaced_count} 次。"
            )

    def clear_block_summary(self, reason: str) -> None:
        """Invalidate all per-block presentation while loading or after failure."""
        self._has_displayed_block = False
        self._last_block_summary = reason
        self._last_display_note = ""
        self.data_status_label.setText("数据状态：" + reason)
        self.block_metadata_label.setText("尚无有效数据块")
        self.processing_status_label.setText("分期状态：等待该块结果")
        self._p3_replay_events = ()
        self._p3_recent.clear()
        self.stimulation_recent_label.setText("当前块尚无可显示的刺激事件")

    def set_recorded_processing_result(self, result, block_id: int) -> None:
        if result is None:
            self.processing_status_label.setText(
                f"分期状态：block_id={block_id} · 未记录/处理未完成"
            )
        else:
            self.processing_status_label.setText(
                "离线回放 · " + self._format_processing_result(result).removeprefix("分期状态：")
            )

    @Slot(bool, object)
    def mark_session_finished(self, cancelled: bool, error: BaseException | None) -> None:
        self._session_finished = True
        self.mode_label.setText("历史 · 会话已结束")
        if self._last_assembly_snapshot is not None:
            snapshot = self._last_assembly_snapshot
            tail = (
                f" · 尾段 {snapshot.partial_samples} 点未用于分期"
                if snapshot.partial_samples
                else " · 无未完成尾段"
            )
            self.assembly_progress_label.setText(
                self.assembly_progress_label.text() + " · 会话结束" + tail
            )
        if self._has_displayed_block:
            if cancelled:
                reason = "用户主动断开"
            elif error is not None:
                reason = f"会话错误：{error}"
            else:
                reason = "会话已结束"
            self.data_status_label.setText(
                f"数据状态：历史数据 · {self._last_block_summary} · {reason}"
            )
        elif error is not None:
            self.data_status_label.setText(f"数据状态：未收到完整 30 秒窗口 · {error}")
        elif cancelled:
            self.data_status_label.setText(
                "数据状态：实时会话已停止 · 未收到完整 30 秒窗口"
            )
        else:
            self.data_status_label.setText(
                "数据状态：实时会话已结束 · 未收到完整 30 秒窗口"
            )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API name
        if not self._p3_close_requested:
            self._p3_close_requested = True
            self.closing_requested.emit()
            self._update_stimulation_controls()
        if self._busy or self._replay_worker_busy or self._p3_background_busy:
            self._pending_close = True
            event.ignore()
            return
        event.accept()


def main() -> int:
    """Development fallback; the production wiring lives in ``app.py``."""
    from .app import main as application_main

    return application_main(sys.argv)
