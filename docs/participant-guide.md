# 本地选手接口

六个示例都使用 `interception` 赛道。参赛程序控制红方，蓝方按本题对手配置运行。读取 `observation.observer_faction_id` 和 `observation.controlled_entity_ids` 确认当前请求授予的控制范围。

程序通过 UTF-8 JSONL 通信。每次从 stdin 读一行请求，向 stdout 写一行回复并立即刷新。调试信息写 stderr。

| 请求 | 处理 |
| --- | --- |
| `reset` | 读取 `public_brief`、`observation` 与 `last_receipt`，初始化状态并返回首步动作。 |
| `act` | 根据当前 `observation` 和上步 `last_receipt` 返回本步动作。 |
| `close` | 退出程序。 |

每个回复包含 `protocol` 和 `batch`。`batch` 为 `null` 表示本步没有动作。指令的会话身份、阵营、控制器和观察 tick 应来自当前请求，可参考 [navigation_batch](../omd_competition/agent.py) 的构造。开局的 `public_brief` 提供选手有权限读取的任务、己方装备、地图、能量与计分信息。规则基线使用这些请求的数据做决策。

## 本地提交示例

```text
python examples/create_submission.py
python run.py evaluate --scenario IE-01-SINGLE-TARGET --submission examples/submission.json --output local-results/ie01-submission
```

生成的 `submission.json` 声明了 `team_id`、`track`、`resource_group`、`protocol`、`entrypoint` 与 `artifacts`。`entrypoint` 是可执行命令的参数数组，`artifacts` 以相对于清单目录的文件名为键、SHA-256 为值。更新程序后同步更新摘要。

[minimal_agent.py](../examples/minimal_agent.py) 演示空动作响应。[IE15 参考程序](../examples/buoy_identify_reference.py) 演示如何把现有规则基线接到同一个 JSONL 接口。动作结构见 [引擎接口类型](../engine/source_codes/openmdbench/schemas/interface_v2.py)。
