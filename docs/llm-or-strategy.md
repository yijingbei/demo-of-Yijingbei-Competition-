# LLM＋运筹策略

这是一套独立于规则基线和 Hybrid 基线的参赛策略，迁移自 ORV11 的 C3 容错与预算控制版本。六个场景共用一套程序，通过既有 JSONL 选手接口读取公开简报、授权观测和己方动作回执。

LLM 为每个可控作战单位给出任务、目标、交战授权、弹药预留及计划有效期。运筹执行器每 tick 检查武器、射程、接触时效、冷却和库存，再计算单位与目标槽位的分配、导航和飞行高度。小规模分配沿用有界精确搜索，较大编队采用精确二分匹配；不需要 OR-Tools、商业求解器或模型训练。分配的最优性只针对当前有限候选的子问题，导航和高度采用短时近似模型，不代表整局得分最优。

## 运行

使用仓库已有的 Python 3.11 环境和 `requirements-lock.txt`，无需额外安装策略依赖。

先查看 `configs/llm-or.example.json`。默认地址是 Delta 测试服，模型为 `DeepSeek-V4-Flash`。使用其他 OpenAI 兼容服务时修改 `base_url`、`model`。测试服和正式服的地址、Key需要匹配。个人配置建议放在被 Git 忽略的 `configs/llm-or.local.json` 中。密钥只通过环境变量 `OPENMD_LLM_API_KEY` 或 `--prompt-key` 输入，不要写进配置或提交清单。

```powershell
python run_llm_or.py --config configs/llm-or.example.json --scenario IE-01-SINGLE-TARGET --seed 2026102302 --output local-results/ie01-llm-or --replay-format html --prompt-key
```

如果已在自己的终端中设置好 `OPENMD_LLM_API_KEY`，可以省略 `--prompt-key`。换场景时替换 `--scenario`，每次使用新的 `--output` 路径。可用场景由仓库的 `scenarios/suite.json` 决定，策略没有单独保存或修改场景。

快速检查接入可加 `--max-ticks 3`，这会截断对局，只能作为接口检查，不能当作完整对局成绩。策略接入阶段的离线模型或本地桩服务测试也不能证明实际模型性能。

## 决策节奏与容错

- 正常规划间隔为100 tick，LLM刷新至少间隔20 tick，并根据公开对局时长和已用请求时间进一步分摊预算；运筹执行仍逐tick进行。
- 原生API组的单次动作30秒、整局累计1800秒限制保持不变。策略默认将LLM请求累计时间控制在1200秒附近，为本地计算和通信保留余量；这只是策略侧预算，不改变原生计时或评分。
- 单次规划最多尝试两次，共享25秒预算；剩余时间不足时不再发起无望完成的纠错请求。
- 可选数值字段的 `null` 会规范化为省略字段，采用任务协议的默认值。正数预留约束不会被丢弃或猜测。
- 目标已不在当前授权观测中时，只让对应单位短暂保持、禁止开火并等待重新规划，不替它猜另一个目标。其他单位继续执行仍然有效的LLM授权。
- 临时超时或输出错误后，复核并短暂沿用未过期指令；过期的开火授权不会被自动续期。尚未获得可用LLM计划的单位保持不射击，随后再次请求LLM。
- 没有使用 `adaptive`、`greedy` 等规则基线作为动作回退。认证、额度或显式调用次数上限错误仍会清楚报出。

这些机制降低的是接口异常导致整局退出的风险，不保证任务成功或得分提升。新仓库的装备、能耗和终止规则有更新，旧仓库成绩不能直接作为这里的评测成绩。

## 输出与复现

| 文件 | 内容 |
| --- | --- |
| `result.json` | 原生运行状态、得分、任务结果、计时和API用量 |
| `strategy-provenance.json` | 生效配置、策略版本、源代码摘要 |
| `strategy/decisions.jsonl.gz` | 每tick的LLM计划、运筹诊断、动作和执行记忆 |
| `strategy/planner-events.jsonl` | 计划接收、格式规范化、纠错与应急续行记录 |
| `strategy/progress.json` | 最近tick、决策数、确认发射数、请求用量和累计请求时间 |
| `strategy/strategy-summary.json` | 策略与模型调用汇总 |
| `replay.html` | 使用 `--replay-format html` 生成的离线回放 |

结果使用现有 `local-results/` 目录，并被 Git 忽略。模型价格未明确时，配置中的两个单价保持 `null`，元数据标记 `api_prices_known=false`；协议中的 `cost_usd=0` 占位不代表免费。

## 通过提交清单运行

可以生成包含策略源码、公开数据处理辅助函数、无密钥配置及SHA-256摘要的本地提交目录：

```powershell
python examples/create_llm_or_submission.py --team-id my-team
python run.py evaluate --scenario IE-01-SINGLE-TARGET --submission local-results/llm-or-submission/submission.json --output local-results/ie01-llm-or-submission --replay-format html
```

提交方式读取终端中的 `OPENMD_LLM_API_KEY`。提交清单使用 `resource_group=api` 并声明 `api_models`，每次回复包含累计调用数、输入输出token和费用字段。源码变动后重新生成目录与摘要，不手工修改旧摘要。这个清单是本地运行示例，容器评测仍遵循主办方既有规则。

## 代码与离线检查

实现全部位于 `omd_competition/llm_or/`，公共入口为 `run_llm_or.py`。只复用现有协议、简报投影、接触关联、岸线几何与动作序列化辅助函数，不读取对手配置、裁判状态或回放中的隐藏信息。

```powershell
python -m unittest discover -s omd_competition/llm_or/tests -v
```

检查使用测试用计划、模拟请求和构造的公开观测，不会调用付费模型。它们验证指令约束、分配合法性和预算恢复流程，不替代完整原生对局评估。
