# Windows 环境配置

在完整项目副本中，双击根目录 **setup_windows.cmd**。需要联网下载 uv、Python 和锁定依赖；支持 64 位 Windows、Windows PowerShell 5.1 或 Windows 上的 PowerShell 7。通常无需管理员权限。

也可在项目根目录打开 PowerShell：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
```

脚本自动定位项目，支持目录包含空格或中文；检查 Curry 本地依赖完整性，复用已有 uv。缺少 uv 时从官方安装地址安装 0.12.1 到当前用户 `%LOCALAPPDATA%\sleep-stim-controller\uv-bin`，不修改永久 PATH。安装方式依据 [uv 官方文档](https://docs.astral.sh/uv/getting-started/installation/)。ExecutionPolicy Bypass 只作用于本次启动的进程，不修改系统策略；组织策略限制仍需遵守。

脚本读取 `.python-version`，安装 Python 3.11，用 `uv sync --locked` 创建/同步项目 `.venv`（包含测试依赖），再检查主程序、Curry、Qt、NumPy 及当前版本存在的 ONNX/SciPy 模块导入。锁文件过期会明确失败，不自动重写。重复运行复用安装和缓存；已有跨平台 `.venv` 会提示人工移开，不自行删除。不要把 macOS 的 `.venv` 拷贝到 Windows。

配置成功后，在项目根目录运行，无需激活虚拟环境或设置 uv PATH：

```powershell
.\.venv\Scripts\sleep-stim-controller.exe
```

可选：配置环境并运行离屏回归测试（测试会使用本机合成 TCP/UDP，不连接真实设备）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1 -RunTests
```

测试结束会恢复原 `QT_QPA_PLATFORM`；配置过程也恢复临时修改的进程环境变量。默认只安装和检查导入，不启动 GUI、不加载模型、不连接 Curry/Rally。安装运行时依赖不等于启用模型，正式默认仍为 NoModel。

失败时保留项目、缓存与失败环境，退出码为 1；依提示检查网络、写入权限或项目完整性后重新运行。该脚本不安装 Curry/Rally 软件、设备驱动或修改防火墙。

验证限制：脚本在 macOS 工作区生成并进行静态检查，尚未实际执行 Windows 安装流程；此前应用测试结果不代表此脚本已在 Windows 实测。
