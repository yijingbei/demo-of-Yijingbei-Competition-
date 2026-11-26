# Yijingbei Competition 选手示例

这是一个可独立运行的竞赛示例仓库，包含六个拦截交战仿真场景、OpenMDBench 引擎、参赛程序接口、规则与 Hybrid 参考方法，以及离线可视化回放。选手可以先看对局，再在同一场景下运行基线，最后通过同一接口接入自己的程序。

每局由选手程序控制红方，蓝方使用场景自带的对手策略。评测器逐 tick 向选手程序发送授权观测和上步回执，接收动作，再交由引擎推进世界、判定任务并计分。

- [看六场规则基线回放](visualization/demo-replay.html) · [看六场 Hybrid 回放](visualization/hybrid-replay.html)
- [安装和运行](#安装和运行) · [仓库结构](#仓库结构) · [选手接口](docs/participant-guide.md)

## 示例场景

| 场景 ID | 场景 | 规则基线 | Hybrid |
| --- | --- | --- | --- |
| [`IE-01-SINGLE-TARGET`][ie01] | 双向协同拦截 | 支持 | 支持 |
| [`IE-05-MULTI-AXIS`][ie05] | 多方向接近 | 支持 | 支持 |
| [`IE-13-DEEP-STRIKE`][ie13] | 纵深双目标突击 | 支持 | 支持 |
| [`IE-S2-LURE`][s2] | 调虎离山 | 支持 | 支持 |
| [`IE-S10-TWO-AXIS`][s10] | 双轴同时施压 | 支持 | 支持 |
| [`IE-15-BUOY-IDENTIFICATION`][ie15] | 浮标阵中的水面威胁识别 | 专用轨迹判断 | DeepSeek 复查调度 |

[ie01]: scenarios/ie/ie-01-single-target/public_brief.json
[ie05]: scenarios/ie/ie-05-multi-axis/public_brief.json
[ie13]: scenarios/ie/ie-13-deep-strike/public_brief.json
[s2]: scenarios/ie/ie-s2-lure/public_brief.json
[s10]: scenarios/ie/ie-s10-two-axis/public_brief.json
[ie15]: scenarios/ie/ie-15-buoy-identification/public_brief.json

点击场景 ID 可读任务简报，查看保护目标、己方装备、公开波次和任务规则。CLI 接受的完整场景 ID 以 [场景清单](scenarios/suite.json) 为准。

## 已有方法

### 规则基线

五种模式在六个场景里均可用。IE01、IE05、IE13、S2、S10 走常规拦截分支，IE15 走独立的轨迹判断分支。[基线入口](omd_competition/agent.py) · [常规规划器](omd_competition/intercept_planner.py) · [IE15 策略](omd_competition/buoy_baselines.py)

| 模式 | 常规拦截 | IE15 轨迹判断 |
| --- | --- | --- |
| `idle` | 不主动发出动作，作为对照 | 不主动巡查或交战 |
| `naive` | 按简单接触优先级行动，结合基础避碰规则 | 优先就近处理观察到的接触 |
| `coordinated` | 对编队分配接触，采用协调距离和航向规则 | 优先选择有接近风险且尝试较少的轨迹，采用固定重试间隔 |
| `greedy` | 使用空域规划器处理当前观测 | 优先处理满足最短轨迹要求的近距离接触 |
| `adaptive` | 结合接触更新、区域巡查、公开波次与余量做动态分配 | 根据接近速度和到达保护目标的时间估计排序，并根据回执和预计在途时间更新决策 |

`run.py evaluate` 默认使用 `coordinated`。仓库里的规则回放录制使用 `adaptive`。

### Hybrid 基线

Hybrid 默认用 **Qwen3.8-27B** 每 30 tick 对公开观测摘要做一次规划，`adaptive` 执行器每 tick 生成动作。IE15 可用 **DeepSeek-V4-Flash** 选择侦察复查节奏，由 IE15 专用轨迹判断策略继续执行。入口使用 OpenAI 兼容的 Chat Completions 服务，调用记录和失败回退统计写到本地结果目录。[运行入口](run_hybrid.py) · [常规配置](configs/hybrid.example.json) · [IE15 配置](configs/hybrid.ie15.deepseek.example.json) · [使用说明](docs/hybrid-baseline.md)

### 自定义参赛程序

参赛程序用 UTF-8 JSONL 处理 `reset`、`act`、`close`。可先用最小程序验证接口，再参考 IE15 的独立基线程序替换决策逻辑。[选手接口](docs/participant-guide.md) · [最小程序](examples/minimal_agent.py) · [IE15 参考程序](examples/buoy_identify_reference.py)

## 仓库结构

```text
.
├── run.py                       # 查看场景、评测规则基线或参赛程序
├── run_hybrid.py                # 运行 LLM + adaptive 基线
├── replay.py                    # 把本地结果导出为离线回放
├── check_environment.py         # 检查解释器和依赖
├── requirements-lock.txt        # 固定的 CPU 运行依赖
├── configs/                     # Hybrid 模型服务示例配置
├── scenarios/
│   ├── suite.json               # 场景 ID、目录和资产目录的索引
│   ├── ie/                      # 六个场景包
│   └── catalog/                 # 这些场景使用的平台、地图和其他资源
├── omd_competition/             # 策略、评测、选手协议和引擎接入
├── engine/
│   ├── source_codes/            # OpenMDBench 引擎及动力学依赖
│   └── patches/                 # 随包引擎补丁及其来源记录
├── plugins/html_replay/         # 逐帧记录、离线导出与播放器源码
├── visualization/               # 规则和 Hybrid 的现成回放
├── examples/                    # 最小 Agent、提交清单和 IE15 参考程序
└── docs/                        # 选手接口和 Hybrid 运行说明
```

每个场景目录包含三个入口文件。读题先看 `public_brief.json`，查引擎参数看 `scenario.yaml`，查对手行为看 `opponent.json`。

| 要找什么 | 从这里进入 |
| --- | --- |
| 场景加载、对局运行和评分结果 | [runtime.py](omd_competition/runtime.py) · [evaluate.py](omd_competition/evaluate.py) |
| 自己的程序如何读观测与交动作 | [participant_brief.py](omd_competition/participant_brief.py) · [transport.py](omd_competition/transport.py) |
| 写规则基线或理解 IE15 | [agent.py](omd_competition/agent.py) · [intercept_planner.py](omd_competition/intercept_planner.py) · [buoy_baselines.py](omd_competition/buoy_baselines.py) |
| 查看 Hybrid 的规划与请求 | [hybrid_agent.py](omd_competition/hybrid_agent.py) · [hybrid_llm_client.py](omd_competition/hybrid_llm_client.py) |
| 定位回放记录和网页展示 | [回放插件](plugins/html_replay/README.md) · [player.js](plugins/html_replay/player.js) |

更多模块的职责和调用关系见 [代码导航](omd_competition/README.md)。

## 安装和运行

推荐 Python 3.11，仿真和规则基线可在 CPU 上运行。克隆或下载仓库后，在仓库根目录执行：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.venv\Scripts\python.exe check_environment.py
.venv\Scripts\python.exe run.py suite
```

Linux 可用 `python3.11 -m venv .venv` 创建环境，后续命令换成 `.venv/bin/python`。

运行一局规则基线：

```powershell
.venv\Scripts\python.exe run.py evaluate --scenario IE-01-SINGLE-TARGET --mode adaptive --seed 2026102302 --output local-results/ie01-rule --replay-format html
```

启动模型服务并核对配置后，运行一局 Hybrid 基线：

```powershell
.venv\Scripts\python.exe run_hybrid.py --config configs/hybrid.example.json --scenario IE-01-SINGLE-TARGET --seed 2026102302 --output local-results/ie01-hybrid --replay-format html
```

换场景时替换 `--scenario`，重复实验时使用新的 `--output` 路径。`result.json` 中的 `status` 是运行状态，`task_success` 是任务是否成功，`utility` 是得分。`replay.html` 可以直接离线打开。

## 看回放

从 GitHub 下载 HTML 原文件，在 Chrome 或 Edge 里打开，从下拉菜单切换场景。播放器使用内嵌录制数据，观看时无需启动仿真或模型服务。

| 回放入口 | 收录范围 | 录制来源 |
| --- | --- | --- |
| [规则基线](visualization/demo-replay.html) | 六场 | IE01、IE05、IE13 为原有能耗版实录，种子 `2026102302`。S2、S10 为历史实录，种子 `2026102801`。IE15 录于无人机能耗对齐之前，种子 `2026111201`。 |
| [Hybrid 基线](visualization/hybrid-replay.html) | 六场 | IE01、IE05、IE13 的种子为 `2026102302`，有模型调用核验记录。S2、S10 的种子为 `2026102801`，模型来自原回放策略声明。IE15 是 DeepSeek-V4-Flash 实跑，种子为 `2026111201`。 |

两份合集都保留各局录制时的运行时摘要。用当前配置重新实验后，可以通过[回放插件](plugins/html_replay/README.md)把本地结果导出到新的 HTML：

```powershell
.venv\Scripts\python.exe replay.py local-results/ie01-rule local-results/ie01-hybrid -o local-results/ie01-comparison.html
```
