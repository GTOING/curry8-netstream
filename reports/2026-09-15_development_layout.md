# 开发目录规范化记录

日期：2026-09-15。依据：用户明确要求主线程整理当前开发目录。

## 完成内容

- 新增根级 README.md，明确目标、目录职责、环境重建、测试入口和业务尚未实现的边界。
- 新增 pyproject.toml，采用 src 布局及 setuptools；声明 Python 3.11、Curry 本地可编辑依赖、NumPy、PySide6、PyQtGraph 和 pytest 开发依赖。
- 新增 .python-version、uv.lock、.gitignore；`.venv` 由根级 uv 项目管理。
- 新增 src/sleep_stim_controller/__init__.py，只建立导入入口，没有提前实现 UI、模型或电刺激控制。
- 保留 curry8-netstream 独立 Git 克隆的位置、历史和工作区，保留参考原件路径，已有文档链接不因移动失效。
- 配置根级 pytest 只发现现有 Curry 测试，排除参考 Demo 的意外导入。

根目录暂无 .git，本轮未初始化仓库、建立子模块、提交或推送。根 .gitignore 排除独立 Curry 克隆；未来迁移时按 README 记录的上游 URL 和提交准备该目录。uv.lock 中本地依赖不锁定 Git 工作区内容，需结合提交记录复现。

## 验证结果

- uv lock：成功生成锁文件。
- uv sync --locked：成功，现有环境中新增根项目可编辑安装。
- 控制器包、Curry、PySide6、PyQtGraph 导入通过，路径指向预期 src 目录。
- 根目录 `.venv/bin/python -m pytest`：11 passed，0.05 秒，均为 Curry 既有合成/协议测试。
- 文档链接及 Curry 工作区状态通过收尾检查。

初次沙箱内 uv 调用因默认缓存访问受限失败；随后使用获准的 uv 权限完成锁定和同步。未运行硬件脚本、连接设备或进行真实实验。

## 后续入口

[项目 README](../README.md)、[当前队列](../decision_records/ACTIVE_QUEUE.md)。

本次只完成目录与开发环境整理，等待后续业务开发说明。控制器测试、开发脚本目录待实际代码任务需要时建立，不引入空功能或模板测试。
