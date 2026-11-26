# Hybrid 基线

常规 Hybrid 默认使用 Qwen3.8-27B 做规划，每 30 个仿真 tick 调用一次。模型读取选手接口提供的观测摘要，输出有限的 JSON 规划字段，由现有 `adaptive` 执行器生成每个 tick 的动作。评测仍通过同一个选手 JSONL 接口。

IE01、IE05、IE13、S2 和 S10 使用常规分支。IE15 的 Hybrid 分支根据授权观测的汇总计数选择普通或加密复查节奏，间隔分别为 120 或 60 tick。侦察、轨迹判断和动作仍由 [IE15 专用规则执行器](../omd_competition/buoy_baselines.py) 处理。

## 运行

先按[仓库说明](../README.md#安装和运行)安装 Python 依赖，再启动支持 OpenAI Chat Completions 接口的模型服务。默认配置连接本机 `http://127.0.0.1:8101/v1`，请求的模型名为 `Qwen3.8-27B`。可复制[示例配置](../configs/hybrid.example.json)，按服务修改 `base_url` 和 `model`。服务需要鉴权时，设置环境变量 `OPENMD_LLM_API_KEY`。

在仓库根目录运行：

```powershell
.venv\Scripts\python.exe run_hybrid.py --config configs/hybrid.example.json --scenario IE-01-SINGLE-TARGET --seed 2026102302 --output local-results/ie01-hybrid --replay-format html
```

Linux 使用 `.venv/bin/python`。每次使用新的输出目录。快速验证接口时可加 `--max-ticks 3`，这个选项会截断对局。

| 结果文件 | 内容 |
| --- | --- |
| `replay.html` | 可离线播放的对局 |
| `result.json` | 运行状态、评分与任务结果 |
| `hybrid-summary.json` | 模型调用、传输失败、JSON 解析和执行器错误统计 |
| `llm-audit.jsonl` | 每次规划的 tick、请求摘要、模型响应及 token 用量 |
| `hybrid-provenance.json` | 生效配置及基线源码摘要 |

模型调用异常时，执行器按基线的回退规则继续。运行入口会用退出码和 `hybrid-summary.json` 报告调用情况。

## IE15 与 DeepSeek-V4-Flash

IE15 的复现实验使用 [Delta Router 配置](../configs/hybrid.ie15.deepseek.example.json)。在运行环境中设置 `OPENMD_LLM_API_KEY` 后执行：

```powershell
.venv\Scripts\python.exe run_hybrid.py --config configs/hybrid.ie15.deepseek.example.json --scenario IE-15-BUOY-IDENTIFICATION --seed 2026111201 --output local-results/ie15-deepseek-v4-flash --replay-format html
```

每 30 tick 规划一次。`scout-plans.jsonl` 保存接受与回退情况以及实际复查间隔，`hybrid-summary.json` 保存模型网关返回的标识和调度统计。

## 离线回放

下载[六场 Hybrid 回放](../visualization/hybrid-replay.html)，用 Chrome 或 Edge 打开，在下拉菜单切换。IE01、IE05 和 IE13 采用种子 `2026102302`，S2 和 S10 采用种子 `2026102801`。IE15 使用种子 `2026111201` 和当前场景配置，网关返回的模型标识为 `neolink/program/deepseek-v4.1-flash`。其余五场保留原始录制版本。IE01、IE05、IE13 有模型调用核验记录，S2、S10 的模型来自原回放策略声明。

本次 IE15 的终局、得分、模型调用和复查模式计数见 [实验结果摘要](../visualization/ie15-hybrid-summary.json)。

## 源码

- [运行入口](../run_hybrid.py)与[评测接入](../omd_competition/hybrid_runner.py)
- [规划与执行](../omd_competition/hybrid_agent.py)
- [模型请求与审计](../omd_competition/hybrid_llm_client.py)
- [IE15 复查调度](../omd_competition/hybrid_buoy.py)
