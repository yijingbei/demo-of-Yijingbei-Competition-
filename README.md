# Yijingbei Competition 选手示例

本仓库提供 6 个可在本地运行的拦截交战示例。每个示例包含场景配置、任务简报、参赛方规则基线和用于复现的对手配置。参赛程序控制红方，蓝方由场景自带策略控制。

| 场景 ID | 场景 | 规则基线 |
| --- | --- | --- |
| `IE-01-SINGLE-TARGET` | 双向协同拦截 | [空域协同规划][air] |
| `IE-05-MULTI-AXIS` | 多方向接近 | [空域协同规划][air] |
| `IE-13-DEEP-STRIKE` | 纵深双目标突击 | [空域协同规划][air] |
| `IE-S2-LURE` | 调虎离山 | [空域协同规划][air] |
| `IE-S10-TWO-AXIS` | 双轴同时施压 | [空域协同规划][air] |
| `IE-15-BUOY-IDENTIFICATION` | 浮标阵中的水面威胁识别 | [轨迹判断基线][buoy] · [外部 JSONL 示例][buoy-example] |

[air]: omd_competition/intercept_planner.py
[buoy]: omd_competition/buoy_baselines.py
[buoy-example]: examples/buoy_identify_reference.py

## 安装和运行

使用 Python 3.11，CPU 即可。以下为 Windows PowerShell 命令。Linux 可用 `python3.11 -m venv .venv` 创建环境，后续用 `.venv/bin/python`。

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.venv\Scripts\python.exe check_environment.py
.venv\Scripts\python.exe run.py suite
.venv\Scripts\python.exe run.py evaluate --scenario IE-01-SINGLE-TARGET --mode adaptive --seed 2026102302 --output local-results/ie01 --replay-format html
```

替换 `--scenario` 即可运行表里的任意示例。每次运行使用新的输出目录。`result.json` 记录运行状态、任务结果和得分，`replay.html` 记录对局。[agent.py](omd_competition/agent.py) 是内置基线入口。上例使用 `adaptive`，也可用 `idle`、`naive`、`coordinated` 或 `greedy` 做对照。

## 看回放

下载 [六场规则基线回放 HTML](visualization/demo-replay.html)，用 Chrome 或 Edge 打开。可在同一播放器中切换 IE01、IE05、IE13、IE-S2-LURE、IE-S10-TWO-AXIS 和 IE15。

| 回放 | 种子 | 录制版本 |
| --- | --- | --- |
| IE01、IE05、IE13 | 2026102302 | 原有能耗版 |
| IE-S2-LURE、IE-S10-TWO-AXIS | 2026102801 | S 系列历史规则基线 |
| IE15 | 2026111201 | 无人机能耗对齐前的历史记录 |

每局保留录制时的运行时摘要。可用上面的评测命令按仓库当前配置生成新回放。

用 [回放插件](plugins/html_replay/README.md) 可将自己的实验目录导出为独立 HTML。

```powershell
.venv\Scripts\python.exe replay.py local-results/ie01 -o local-results/ie01.html
```

## Hybrid 基线

[五场 Hybrid 回放 HTML](visualization/hybrid-replay.html) 可切换 IE01、IE05、IE13、IE-S2-LURE 和 IE-S10-TWO-AXIS。Qwen3.8-27B 负责周期性规划，`adaptive` 执行器以现有规则生成动作。参见 [Hybrid 使用说明](docs/hybrid-baseline.md) 和 [运行入口](run_hybrid.py)。IE15 提供轨迹判断规则基线。

## 接入自己的程序

[选手接口说明](docs/participant-guide.md) 提供 JSONL 请求、回执与提交入口。示例配置见 [场景清单](scenarios/suite.json)。[引擎](engine/source_codes/openmdbench) 随仓库提供，启动器直接加载其源码。
