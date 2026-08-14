# Vibe Center for Windows

Windows 版 Vibe Center —— 把 macOS 刘海面板移植到 Windows：置顶悬浮在
屏幕顶部中央的深色胶囊面板，实时展示各 AI coding agent 的会话状态、
配额用量，并在 Claude Code 需要写操作审批时弹出决定卡片。

与 macOS 版共用同一套协议（HMAC 签名的 TCP IPC + relay.py hooks +
usage-daemon.py），扫描器支持全部 9 种 agent：

claude / zcode / codex / gemini（Antigravity 与原生 CLI）/ qwen /
kimi / opencode / deepseek

## 安装与启动

### 方式 A：官方构建（推荐，无需 Python）

从 GitHub **Releases** 下载 `VibeCenter-windows-x64.zip`（每次打 tag 或在
Actions 页面均可获取），解压后：

- `VibeCenter.exe` — 面板（双击运行，托盘出现 V 图标）
- `VibeCenterRelay.exe` — hook 中继（无需手动运行；在面板“设置”里安装
  Hook 后会自动被复制到 `~/.vibe-island/bin/` 并由 Claude Code 调用）

两个 exe 都是 PyInstaller 单文件打包，目标机器**不需要安装 Python**；
用量监测在 exe 内以进程内线程运行。

### 方式 B：从源码运行

前置要求：Windows 10/11 + [Python 3.9+](https://python.org)
（安装时勾选 "Add python.exe to PATH"）。

```powershell
cd windows
.\run.bat
```

`run.bat` 首次运行会自动 `pip install PySide6`，然后启动面板。
也可以手动：

```powershell
python -m pip install -r requirements.txt
python vibecenter\main.py
```

启动后系统托盘出现 "V" 图标：显示面板 / 刷新会话 / 设置 / 退出。

## 启用 Claude Code 审批卡片

在面板 **设置 → Claude Code Hook → 安装 / 修复 Hook**（或运行
`install-hook.ps1`）。之后 Claude Code 的写操作（Bash / Edit / Write）
会先在面板弹出审批卡：风险分级（低/中/高/严重）+ 原因 + diff 预览 +
倒计时，超时自动拒绝（fail-closed）。

移除：设置里点“移除”，或
`powershell -ExecutionPolicy Bypass -File install-hook.ps1 -Uninstall`。

## 用量监测

设置里开启“自动监测用量”后，面板会以 `~/.zcode/v2/config.json` 里的
API key 轮询 Z.ai（及其他配置的 provider）配额并轮播显示；
未配置时显示“未找到 Z.ai API 配置”。

## 功能对照（相对 macOS 版）

| 功能 | 状态 |
|------|------|
| 顶部中央 notch 面板（悬停展开 / 点击固定） | ✅ |
| 9 种 agent 会话扫描（含标题 / 预览 / 运行状态） | ✅ |
| 文件监听实时刷新（QFileSystemWatcher） | ✅ |
| HMAC-SHA256 签名 IPC（TCP 14321，与 relay.py 互通） | ✅ |
| 审批卡片：风险分级 / 原因 / diff / 倒计时 / fail-closed | ✅ |
| AskUserQuestion 多问题卡片（单选/多选） | ✅ |
| 隐私决策历史（只存类别与结果，上限 30 条） | ✅ |
| 用量轮播（多 provider） | ✅ |
| 托盘菜单 + 原生通知 + 设置面板 | ✅ |
| Hook 一键安装/修复/移除 | ✅ |
| 返回工作现场 | 简化：双击会话打开项目目录 |
| 多显示器放置 | 简化：跟随主显示器 |

## 开发与测试（任何平台可跑）

```bash
python -m pytest windows/tests -q          # scanner/IPC/风险规则/协议互通
python windows/vibecenter/main.py --self-test   # 离屏渲染 4 种 UI 状态到 windows/build/*.png
```

协议层与 relay.py 的字节级兼容由 `test_scanner.py::AuthParityTests`
保证（用 relay.py 的真实签名函数交叉验证）。

## CI / 自动构建

`.github/workflows/build.yml` 在每次 push / PR 时运行：

- **windows-latest**：跑 `windows/tests`，然后用 PyInstaller 打出
  `VibeCenter.exe`（面板，含 relay.py / usage-daemon.py / 字体）和
  `VibeCenterRelay.exe`（hook 中继），对两个 exe 做冒烟自检后压成
  `VibeCenter-windows-x64.zip` 上传为 artifact；
- 推送 `v*` tag 时自动创建 GitHub Release 并附上双平台安装包：
  `git tag v1.0.0 && git push origin v1.0.0`。

本地复现打包（任意平台）：

```bash
python -m pip install PySide6 pyinstaller
python -m PyInstaller --onefile --windowed --name VibeCenter \
  --add-data "relay.py:." --add-data "usage-daemon.py:." \
  --add-data "DepartureMono-Regular.otf:." windows/launch.py   # Windows 上分隔符用 ;
python -m PyInstaller --onefile --console --name VibeCenterRelay relay.py
./dist/VibeCenter --self-test   # 冒烟：离屏渲染 4 种 UI 状态
```

## 目录结构

| 文件 | 职责 |
|------|------|
| `vibecenter/main.py` | 入口：组装 IPC / 扫描 / 托盘 / UI + self-test |
| `vibecenter/ui.py` | notch 窗口、会话行、审批/提问卡片、设置、托盘 |
| `vibecenter/store.py` | 状态层：会话 upsert / 对账 / 请求队列 / 历史 |
| `vibecenter/scanner.py` | scan-agents.sh 的纯 Python 移植（9 种 agent） |
| `vibecenter/ipc.py` | TCP IPC 服务器（签名验证 / 审批挂起 / 超时拒绝） |
| `vibecenter/auth.py` | 与 relay.py 字节兼容的 HMAC 签名 |
| `vibecenter/models.py` | 会话/请求/风险规则/用量模型 |
| `vibecenter/hooks.py` | Claude Code hook 安装器（install-hook.sh 的移植） |
| `tests/` | 跨平台测试（fixture 驱动，无需 Windows） |

打包独立 exe（可选）：`pip install pyinstaller && pyinstaller --onefile
--windowed --name VibeCenter windows/vibecenter/main.py`
