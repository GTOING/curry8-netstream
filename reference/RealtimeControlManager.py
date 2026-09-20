import subprocess
import time
import signal
import sys
import os
from datetime import datetime
import threading
import socket
import platform


class RealtimeControlManager:
    """
    电刺激协议调度管理器（极简版）
    只管什么时间切换/停止协议
    """

    def __init__(self):
        self.current_process = None
        self.current_protocol = None
        self.is_running = False
        self.timers = []

        # ========================================
        # 🔧 协议脚本注册
        # ========================================
        self.protocol_scripts = {
            'nosleep': {
                'name': '睡前协议',
                'script': 'RealtimeControlAPIDemo3.py',
            },
            'sleep': {
                'name': '睡中协议',
                'script': 'RealtimeControlAPIDemo4.py',
            },
            'nodian': {
                'name': '无电流协议',
                'script': 'RealtimeControlAPIDemo5.py',
            }
        }

        # ========================================
        # 🔧 时间线（只配什么时间做什么动作）
        # ========================================
        # self.timeline = [
        #     # (启动后多少秒, 动作, 协议名)
        #     # 动作: 'start' / 'switch' / 'stop'
        #
        #     (0, 'start', 'nosleep'),  # 0秒：启动高电流
        #     (600, 'switch', 'nodian'),  # 10分钟后：切低电流
        #     (2400, 'switch', 'sleep'),  # 40分钟后：切回高电流
        #     (3000, 'switch', 'nodian'),  # 70分钟后：停止
        #     (3100, 'stop', None)
        # ]
        self.timeline = [
            # (启动后多少秒, 动作, 协议名)
            # 动作: 'start' / 'switch' / 'stop'

            (0, 'start', 'nosleep'),  # 0秒：启动高电流
            (60, 'switch', 'nodian'),  # 10分钟后：切低电流
            (80, 'switch', 'sleep'),  # 40分钟后：切回高电流
            (200, 'switch', 'nodian'),  # 70分钟后：停止
            (240, 'stop', None)
        ]
        # ⚠️ 每个协议刺激多久，去各自的 RealtimeControlAPIDemo.py 里改 SD！

        self.python_path = sys.executable
        self._check_environment()

    def _check_environment(self):
        """检查环境"""
        print("\n" + "=" * 50)
        print("🔍 检查协议脚本:")
        for key, proto in self.protocol_scripts.items():
            path = os.path.join(os.getcwd(), proto['script'])
            status = "✅" if os.path.exists(path) else "❌"
            print(f"   {status} [{key}] {proto['name']} -> {proto['script']}")

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.1)
            sock.bind(('127.0.0.1', 0))
            sock.close()
            print("   ✅ UDP正常")
        except:
            print("   ⚠️  UDP可能有问题")
        print("=" * 50)

    # ========================================
    # 启动实验
    # ========================================

    def start_session(self):
        """一键启动"""
        if self.is_running:
            print("⚠️  已在运行中")
            return

        print("\n" + "⚡" * 20)
        print("   实验开始")
        print("⚡" * 20)

        self._print_timeline()

        start_time = datetime.now()
        print(f"\n⏱  启动时间: {start_time.strftime('%H:%M:%S')}")

        self._cancel_all_timers()

        # 按时间线设置定时器
        for offset, action, protocol in self.timeline:
            if offset == 0:
                # 立即执行第一个动作
                self._do_action(action, protocol, 0)
            else:
                timer = threading.Timer(offset, self._do_action, args=[action, protocol, offset])
                timer.daemon = True
                timer.start()
                self.timers.append(timer)

        # 等待实验结束
        total_time = max(offset for offset, _, _ in self.timeline)

        try:
            while self.is_running or any(t.is_alive() for t in self.timers):
                elapsed = (datetime.now() - start_time).total_seconds()
                current = self.current_protocol or "空闲"
                remaining = total_time - elapsed

                if remaining > 0:
                    print(f"\r⏱  运行中 [{current}] | 已过{self._fmt(elapsed)} | 剩余{self._fmt(remaining)}",
                          end='', flush=True)

                time.sleep(1)

                if elapsed > total_time + 5:
                    break

            print(f"\n\n✅ 实验完成！结束时间: {datetime.now().strftime('%H:%M:%S')}")

        except KeyboardInterrupt:
            print(f"\n\n⚠️  手动中断")
            self.stop_session()

        self._cancel_all_timers()
        if self.is_running:
            self._kill()

    def _do_action(self, action, protocol, offset):
        """执行动作"""
        now = datetime.now().strftime('%H:%M:%S')

        if action == 'start':
            print(f"\n▶️  [{self._fmt(offset)}] {now} 启动: {self.protocol_scripts[protocol]['name']}")
            self._launch(protocol)

        elif action == 'switch':
            print(f"\n🔄 [{self._fmt(offset)}] {now} 切换: {self.protocol_scripts[protocol]['name']}")
            self._kill()
            time.sleep(1)
            self._launch(protocol)

        elif action == 'stop':
            print(f"\n⏹️  [{self._fmt(offset)}] {now} 停止")
            self._kill()

    def _launch(self, protocol_key):
        """启动协议脚本"""
        script = os.path.join(os.getcwd(), self.protocol_scripts[protocol_key]['script'])

        try:
            if platform.system() == 'Windows':
                self.current_process = subprocess.Popen(
                    [self.python_path, script],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                self.current_process = subprocess.Popen(
                    [self.python_path, script],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, preexec_fn=os.setsid
                )

            self.is_running = True
            self.current_protocol = protocol_key
            print(f"   ✅ 已启动 [{protocol_key}] PID:{self.current_process.pid}")

            # 监控线程
            t = threading.Thread(target=self._monitor, daemon=True)
            t.start()

        except Exception as e:
            print(f"   ❌ 启动失败: {e}")

    def _kill(self):
        """终止进程"""
        if not self.current_process:
            return
        try:
            if platform.system() == 'Windows':
                self.current_process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(self.current_process.pid), signal.SIGTERM)
            try:
                self.current_process.wait(timeout=3)
            except:
                try:
                    if platform.system() == 'Windows':
                        self.current_process.kill()
                    else:
                        os.killpg(os.getpgid(self.current_process.pid), signal.SIGKILL)
                except:
                    self.current_process.kill()
                self.current_process.wait()
        except:
            try:
                self.current_process.kill()
            except:
                pass

        self.current_process = None
        self.is_running = False
        self.current_protocol = None

    def stop_session(self):
        """手动停止"""
        self._cancel_all_timers()
        self._kill()
        print("⏹️  已停止")

    def _cancel_all_timers(self):
        for t in self.timers:
            t.cancel()
        self.timers.clear()

    def _monitor(self):
        """监控输出"""
        if not self.current_process:
            return
        while self.is_running and self.current_process.poll() is None:
            try:
                if self.current_process.stdout:
                    line = self.current_process.stdout.readline()
                    if line:
                        msg = line.strip()
                        if any(kw in msg for kw in ['结果', 'error', 'Error', '成功', '失败']):
                            print(f"   📤 {msg}")
                time.sleep(0.1)
            except:
                break

        if self.current_process and self.current_process.poll() is not None:
            if self.is_running:
                print(f"   ⚠️  进程退出")
            self.is_running = False
            self.current_protocol = None

    def _print_timeline(self):
        """打印时间线"""
        print("\n📋 时间线:")
        print("-" * 40)
        for offset, action, protocol in self.timeline:
            t = self._fmt(offset)
            if action == 'start':
                print(f"   {t:>8}  ▶️  启动 {self.protocol_scripts[protocol]['name']}")
            elif action == 'switch':
                print(f"   {t:>8}  🔄  切换 {self.protocol_scripts[protocol]['name']}")
            elif action == 'stop':
                print(f"   {t:>8}  ⏹️  停止")
        total = max(o for o, _, _ in self.timeline)
        print(f"\n   总时长: {self._fmt(total)}")
        print("-" * 40)

    def _fmt(self, seconds):
        """格式化 - 精确到秒"""
        if seconds < 60:
            return f"{seconds:.0f}秒"
        elif seconds < 3600:
            m = int(seconds // 60)
            s = int(seconds % 60)
            if s == 0:
                return f"{m}分钟"
            else:
                return f"{m}分{s}秒"
        else:
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = int(seconds % 60)
            if s == 0:
                return f"{h}小时{m}分钟"
            else:
                return f"{h}小时{m}分{s}秒"


if __name__ == "__main__":
    manager = RealtimeControlManager()

    print("\n" + "=" * 50)
    print("  电刺激协议调度器")
    print("=" * 50)

    manager._print_timeline()

    print("\n1. 🚀 启动实验")
    print("2. ⏹️  停止")
    print("3. 🚪 退出")

    c = input("\n选: ").strip()
    if c == '1':
        manager.start_session()
    elif c == '2':
        manager.stop_session()
    else:
        print("退出")