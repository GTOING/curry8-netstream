#!/usr/bin/env python3
"""CLI：连接 Curry NetStreaming Server，打印数据块，并演示发送控制指令。

示例::

    python scripts/run_client.py --host 127.0.0.1 --port 4455 \
        --log-level DEBUG --dump capture.bin --max-blocks 20

    # 演示控制流：连接后先发阻抗检测，收满 10 块后发停止录制
    python scripts/run_client.py --send-impedance --stop-after 10
"""
from __future__ import annotations

import argparse
import pathlib
import sys

# 允许直接 `python scripts/run_client.py` 运行（把 src/ 加入搜索路径）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from curry_netstream.client import CurryClient  # noqa: E402
from curry_netstream.logging_util import setup_logging  # noqa: E402
from curry_netstream.models import DataBlock  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Curry 8 NetStreaming 调试客户端")
    p.add_argument("--host", default="127.0.0.1", help="Curry Server IP / 主机名")
    p.add_argument("--port", type=int, default=4455, help="NetStreaming 端口")
    p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    p.add_argument("--dump", metavar="PATH", help="把原始字节流写入文件（逆向协议用）")
    p.add_argument("--max-blocks", type=int, default=None, help="收到 N 块后停止")
    p.add_argument(
        "--expected-seconds", type=float, default=30.0,
        help="每个 EEG 包预期包含的秒数（默认 30；设为 0 则不检查）",
    )
    p.add_argument(
        "--peek", type=float, metavar="SECONDS",
        help="【诊断模式】连接后只 hexdump 原始字节、不分帧，持续 N 秒，"
             "用来判断服务器是否在发数据",
    )
    p.add_argument(
        "--peek-send-start", action="store_true",
        help="配合 --peek：监听过半后主动发『开始推流』再继续监听",
    )
    p.add_argument(
        "--send-impedance", action="store_true",
        help="连接后立即发送一次阻抗检测指令（演示控制流）",
    )
    p.add_argument(
        "--stop-after", type=int, metavar="N",
        help="收到 N 块后发送『停止录制』指令（演示控制流）",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)

    state: dict = {"n": 0, "client": None}

    def on_data(block: DataBlock) -> None:
        state["n"] += 1
        ev = f", events={len(block.events)}" if block.events else ""
        duration = (
            block.n_samples / block.sample_rate_hz
            if block.sample_rate_hz > 0 else 0.0
        )
        print(
            f"[block {state['n']:>4}] start_sample={block.start_sample} "
            f"shape={tuple(block.data.shape)} sr={block.sample_rate_hz:.0f}Hz "
            f"duration={duration:.3f}s{ev}"
        )
        if args.expected_seconds > 0:
            expected = round(block.sample_rate_hz * args.expected_seconds)
            if block.n_samples != expected:
                print(
                    f"警告：该包有 {block.n_samples} 个采样/通道，"
                    f"预期 {expected}（{args.expected_seconds:g} 秒）。",
                    file=sys.stderr,
                )
        if (
            args.stop_after is not None
            and state["n"] == args.stop_after
            and state["client"] is not None
        ):
            state["client"].stop_recording()

    try:
        with CurryClient(args.host, args.port, dump_path=args.dump) as client:
            state["client"] = client
            if args.peek is not None:
                # 诊断模式：只看原始字节，不进入正常分帧/解析
                client.peek_raw(args.peek, send_start=args.peek_send_start)
                return 0
            if args.send_impedance:
                client.start_impedance()
            client.stream(on_data, max_blocks=args.max_blocks)
    except ConnectionRefusedError:
        print(
            f"无法连接 {args.host}:{args.port} —— "
            f"请确认 Curry Server 已启动并启用 NetStreaming（端口/IP 正确）。",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError) as exc:
        print(f"Curry 数据接收失败：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n用户中断。")

    print(f"共收到 {state['n']} 块数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
