"""Reproduce P-GUI offscreen screenshots using temporary synthetic records."""
from pathlib import Path
import runpy
import tempfile

from PySide6.QtWidgets import QApplication
from sleep_stim_controller.recording import SessionReader
from sleep_stim_controller.ui import MainWindow

ROOT = Path(__file__).resolve().parents[1]
app = QApplication.instance() or QApplication([])
window = MainWindow()
window.show()


def capture(name, width=800, height=600):
    window.resize(width, height)
    app.processEvents()
    assert window.grab().save(str(ROOT / "reports" / f"P_GUI_{name}.png"))


try:
    capture("DEFAULT", 1000, 720)
    capture("800X600")
    window.set_error("测试替身 · 长错误展示：" + "连接失败，详情仍可滚动阅读。" * 60)
    window.set_recording_root("/测试替身/" + "long_directory_" * 50)
    app.processEvents()
    window.settings_scroll.ensureWidgetVisible(window.recording_root_label)
    capture("LONG_TEXT")
    window.set_error("")
    window.recording_root_label.setText("未选择保存父目录")
    window.recording_root_label.setProperty("path", None)
    fixture = runpy.run_path(str(ROOT / "tests" / "test_p2_recording.py"))
    with tempfile.TemporaryDirectory(prefix="p-gui-") as folder:
        reader = SessionReader(fixture["make_saved_session"](Path(folder)))
        window.set_replay_mode(True)
        window.set_replay_worker_busy(True)
        window.set_replay_ready(reader.overview)
        block, entry = reader.read_block(1)
        window.show_replay_block(block, entry, 1, reader.overview.block_count)
        window.set_recorded_processing_result(entry.processing_result, entry.block_id)
        window.set_replay_stimulation_events(entry.stimulation_events)
        window.hint_label.setText("测试替身 · 合成记录回放截图 · 仅模拟，真实刺激未接入")
        window.stimulation_section.set_expanded(False)
        window.settings_scroll.verticalScrollBar().setValue(0)
        capture("REPLAY", 1000, 720)
finally:
    window.set_replay_worker_busy(False)
    window.close()
print("P-GUI offscreen screenshots: default, 800x600, long text, synthetic replay")
