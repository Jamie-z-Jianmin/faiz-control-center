# Faiz Control Center (`faiz-dash`)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776ab.svg)](pyproject.toml)
[![Textual TUI](https://img.shields.io/badge/Textual-8.0%2B-green.svg)](https://textual.textualize.io/)

**专为 [Antigravity CLI](https://antigravity.google) (`agy`) 打造的极速 (`<80ms`) TrueColor 状态栏、两位小数 `/usage` 实时配额同步、无占屏对话气泡诊断 (`/faiz-dash`) 与按需 Textual 控制台。**

[English](README.md) | 简体中文

![Faiz StatusLine Themes](docs/assets/hero-statusline-themes.png)

---

## 核心特性

- **与 `/usage` 两位小数逐位对齐（`21.36% · 51.66%`）**：通过隔离 PTY 探针采集 5 小时与每周剩余配额（带 `flock` 并发锁与 `rfind()` 尾帧防重绘撕裂）；当检测到活跃对话消耗配额时自动切换为 **45 秒高频后台刷新**（空闲保持 180 秒零消耗）。
- **`<80ms` 纯 Bash + `jq` 热路径渲染**：永不阻塞输入提示符。支持 **90 列分屏 4x2 对齐矩阵**、宽屏（`>=155` 列）自动单行展开、`Apple_Terminal` 16 色自动降级；未配置云端环境时自动零噪音隐藏云模块。
- **Prompt Cache 命中率与实时输出速率 (`t/s`)**：仅增量读取当前会话 `transcript.jsonl` 尾行，实时呈现缓存命中率、3m/4m TTL 冷却预警与真实输出速度 (`t/s`)。
- **零常驻挤压的双通道检视**：
  - **会话内气泡式诊断（`/faiz-dash` / `faiz-dash inspect`）**：不占用终端列宽，直接输出配额四分组与重置倒计时、24 小时各模型 Token Burn、Cloud/Drive 退避状态、Mailbox 队列核验与只读 APM 遥测摘要。
  - **Rex 按需右侧边栏（`faiz-dash side`）**：在 Rex 内一键开关右侧 28% 宽度实时看板（`--focus=false` 不抢焦点，按 `q` 或再次执行 `faiz-dash side` 关闭），默认关闭启动自动 Dock（`faiz-dash off`）。
- **中英双语设置工作室（`faiz-dash config`）**：交互式配置 8 个信息模块、5 种视觉风格（`capsule` / `powerline` / `pill` / `ghost` / `minimal`）、4 套主题配色、`ring`/`bar` 指示器、配额精度（`exact` 2 位 / `1dp` / `int`）、启动开关及分屏宽度比例，底部提供实时 TrueColor 预览。
- **可选云算力与异步队列扩展**：配置 `rclone.conf` 后自动接入 Colab 心跳状态、60 分钟闲置计时、GCE Spot 实例监控、Google Drive 水位（含持久指数退避）与产物防假完成核验。

---

## TUI 控制中心与设置工作室

![Control Center Dock and Rex Sidebar](docs/assets/control-center-dock-side.png)

![Faiz Config Studio](docs/assets/config-studio.png)

---

## 系统架构

```mermaid
flowchart LR
    Agy["agy CLI 会话"] -->|"statusLine JSON stdin"| SL["scripts/statusline.sh (<80ms)"]
    Transcript["transcript.jsonl 尾行"] -->|"Cache 命中, TTL, t/s"| SL
    SL -->|"毫秒级只读"| Cache["~/.agents/cache/*.json"]
    SL -.->|"活跃 45s / 空闲 180s (--quota-only)"| Daemon["faiz-dash --once-json"]
    Daemon -->|"PTY rfind() + flock"| Usage["UsageProbe (/usage)"]
    Daemon -->|"检测到 rclone.conf 时启用"| Cloud["CloudProbe + MailboxProbe"]
    Usage --> Store["StateStore (密钥脱敏)"]
    Cloud --> Store
    Store --> Cache
    Cache --> Inspect["faiz-dash inspect (/faiz-dash 技能)"]
    Cache --> TUI["FaizControlCenterApp (faiz-dash side / --dock)"]
```

---

## 30 秒快速上手

### 环境依赖
- **macOS 或 Linux**，Python **3.12+**、`bash`、`jq`
- **Antigravity CLI**（`agy` 位于 `PATH`）
- 可选：**Rex Terminal**（支持 `faiz-dash side` / `dock` 按需分屏）；仅需监控 Colab/GCE/Drive 时才需要 `rclone` / `gcloud`

### 安装步骤

#### 一行命令自动安装（macOS & Linux）

```bash
curl -fsSL https://faiz-control-center.pages.dev/install.sh | bash
```

*（Cloudflare Pages 全球边缘节点加速；GitHub 备用源：`https://raw.githubusercontent.com/Jamie-z-Jianmin/faiz-control-center/main/docs/site/install.sh`）*

#### 手动 / 开发模式安装

```bash
git clone https://github.com/Jamie-z-Jianmin/faiz-control-center.git
cd faiz-control-center

# 1. 安装 Python 包与 Textual 依赖（建议在虚拟环境或 pipx 中执行）
python3 -m pip install -e .

# 2. 将 statusline、CLI 包装器、Rex Hook 与 /faiz-dash Skill 软链至 ~/.agents 与 ~/.local/bin
./install.sh --link   # 外部用户如需独立拷贝安装可传 --copy
```

确认 `~/.local/bin` 已加入 `PATH`，随后在 `~/.gemini/antigravity-cli/settings.json` 中配置 `statusLine`：

```json
{
  "statusLine": {
    "type": "command",
    "command": "~/.agents/scripts/statusline.sh"
  }
}
```

### 快速验证

```bash
faiz-dash inspect         # 读取缓存毫秒输出 Markdown 诊断报告
faiz-dash inspect --live  # 立即触发最新探针后输出诊断报告
faiz-dash config          # 打开双语实时预览设置中心
```

---

## 常用指令与快捷键

| 指令 | 作用 |
| :--- | :--- |
| `faiz-dash inspect [--live] [--json]` | 非交互式输出配额、24h Burn、云端退避、Mailbox 与 APM 遥测报告 |
| `faiz-dash side` | 在 Rex 中按需打开/关闭右侧边栏（保持主对话焦点；非 Rex 终端会提示替代方案） |
| `faiz-dash dock` | 在 Rex 中按需打开/关闭底部三栏 Dock |
| `faiz-dash config` | 启动设置 UI（也支持 CLI 参数：`--style`、`--theme`、`--lang zh\|en`、`--quota-precision exact\|1dp\|int`、`--side-ratio`、`--auto-dock on\|off`、`--show`、`--reset`） |
| `faiz-dash off` / `on` / `status` | 关闭/开启新会话自动弹出底部 Dock（`~/.agents/cache/dash_disabled`）及查看缓存状态 |
| `faiz-dash --once-json [--quota-only] [--no-pty]` | 单次采集快照并输出脱敏 JSON |
| `faiz-dash --preset 1 \| 2` | 在当前终端直接以横排 Dock (`1`) 或竖排侧栏 (`2`) 启动全屏 TUI |

### TUI 面板快捷键

- `Tab` / `Shift+Tab`：切换焦点面板
- `1` / `2`：切换横排 Dock / 竖排侧栏视图
- `m`：放大 / 还原当前焦点面板
- `[` / `]`：微调当前面板占比（`-0.1fr` / `+0.1fr`）
- `v` / `0` / `!` `@` `#`：隐藏或恢复面板
- `s`：交换面板顺序；`u` / `r`：手动刷新配额或云端探针；`q`：退出关闭分屏

---

## 配置与可选云端扩展

所有配置保存在 `~/.agents/statusline_config.json`（纯本地模式零配置即可开箱使用）。若你有 Colab / Drive (`rclone`) / GCE Spot 监控需求：
1. 在 `~/.config/rclone/rclone.conf` 中配置目标 remote（默认 `gdrive`）。
2. 通过 `~/.agents/statusline_config.json` 的 `"cloud"` 字段或环境变量（`FAIZ_GCP_PROJECT`、`FAIZ_RCLONE_REMOTE`、`FAIZ_COLAB_STATUS_PATH`、`FAIZ_MAILBOX_ROOT`、`FAIZ_PROJECTS_ROOT`）覆盖路径即可；无 `rclone.conf` 时系统保持 `NOT_CONFIGURED` 零告警降级。

---

## 测试与开源协议

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

[GitHub Actions CI](.github/workflows/ci.yml) 在 Ubuntu 与 macOS 上自动校验 Shell 语法、`styles.tcss` 打包完整性以及全部 36 项单元与 Textual Pilot 集成测试。采用 [MIT License](LICENSE) 开源。
