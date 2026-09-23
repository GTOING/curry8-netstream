"""Capture the synthetic Issue 6 profile in an offscreen 800x600 GUI."""
from __future__ import annotations

from pathlib import Path

from sleep_stim_controller.app import build_application
from sleep_stim_controller.paradigm import load_paradigm_package


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "tests" / "fixtures" / "issue6_paradigm_test_only"
OUTPUT = ROOT / "reports" / "ISSUE6_PARADIGM_800X600.png"


def main() -> None:
    application, window, _controller = build_application(request_timeout_seconds=0.2)
    runtime = window._p3_runtime
    snapshot = load_paradigm_package(PACKAGE)
    try:
        runtime.select_paradigm(snapshot)
        runtime.set_real_profile("paradigm")
        runtime.set_control_mode("real")
        window.set_rally_mode("real")
        window.set_rally_profile("paradigm")
        window.set_paradigm_package_summary(
            str(PACKAGE),
            name=snapshot.name,
            version=snapshot.version,
            sha256=snapshot.sha256,
            classification=snapshot.classification,
        )
        status = runtime.real_status
        status.update(
            {
                "latest_desired_protocol": "A",
                "api_confirmed_protocol": None,
                "runtime_state": "DISARMED/UNKNOWN",
                "enabled": False,
                "desired_state": "RUNNING",
                "confirmed_state": None,
            }
        )
        window.set_rally_status(status)
        window.rally_mode_hint_label.setText(
            "synthetic/test-only 包仅用于软件演示；真实自动启用会被拒绝。"
        )
        window.stimulation_auto_status_label.setText(
            "无生产范式包；A/B/C 参数未批准。截图为离屏界面，不含 Rally/物理输出。"
        )
        window.workflow_section.set_expanded(False)
        window.resize(800, 600)
        window.show()
        window.settings_scroll.ensureWidgetVisible(window.rally_control_status_label)
        application.processEvents()
        if not window.grab().save(str(OUTPUT)):
            raise RuntimeError(f"无法保存截图：{OUTPUT}")
        print(f"saved {OUTPUT}")
    finally:
        window.close()
        application.processEvents()
        if not runtime.join(2.0):
            raise RuntimeError("GUI 关闭后 Rally 控制 worker 未退出")


if __name__ == "__main__":
    main()
