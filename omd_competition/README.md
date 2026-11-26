# 代码导航

从仓库根目录的 [run.py](../run.py) 进入规则评测，或使用 [run_hybrid.py](../run_hybrid.py) 运行 Hybrid。两套方法在同一评测接口下接收观察和回执，按既有场景规则提交动作。

## 对局如何运行

1. [runtime.py](runtime.py) 从 [suite.json](../scenarios/suite.json) 找到场景包，加载相应资源表并创建引擎会话。
2. [evaluate.py](evaluate.py) 推进对局并收集原生评分；[participant_brief.py](participant_brief.py) 生成开局公开简报，[transport.py](transport.py) 与参赛程序交换 JSONL。
3. 红方策略按当前请求生成动作，蓝方由 [opponent_registry.py](opponent_registry.py) 按场景选用脚本。引擎应用动作并返回回执。
4. [回放记录器](../plugins/html_replay/recording.py) 收集帧数据，[回放导出器](../plugins/html_replay/exporter.py) 使用结果、记录和场景快照生成离线 HTML。

## 按职责找代码

| 职责 | 入口 | 相关文件 |
| --- | --- | --- |
| 规则基线 | [agent.py](agent.py) 处理 JSONL 并选择策略 | [intercept_planner.py](intercept_planner.py) 提供常规规划，[surface_patrol.py](surface_patrol.py) 管理水面巡查，[surface_safety.py](surface_safety.py) 处理航行约束。 |
| IE15 轨迹判断 | [buoy_baselines.py](buoy_baselines.py) 实现五种规则模式 | [trajectory_judgment.py](trajectory_judgment.py) 累积接触轨迹，[action_feedback.py](action_feedback.py) 处理动作回执。外部协议示例见 [buoy_identify_reference.py](../examples/buoy_identify_reference.py)。 |
| Hybrid 基线 | [hybrid_runner.py](hybrid_runner.py) 读取配置并启动评测 | [hybrid_agent.py](hybrid_agent.py) 调用模型规划并交给规则执行器，[hybrid_llm_client.py](hybrid_llm_client.py) 负责 HTTP 和调用审计，[hybrid_brief_compat.py](hybrid_brief_compat.py) 适配公开波次时间窗，[hybrid_buoy.py](hybrid_buoy.py) 负责 IE15 的侦察复查调度。 |
| 对手脚本 | [opponent_registry.py](opponent_registry.py) 选择脚本 | [opponent.py](opponent.py) 是常规 IE 脚本，[opponent_s_v16.py](opponent_s_v16.py) 用于 S2 和 S10，[buoy_navigation.py](buoy_navigation.py) 用于 IE15。各题参数在场景包的 `opponent.json`。 |
| 参赛接入 | [submission.py](submission.py) 校验提交清单 | [transport.py](transport.py) 管理选手进程和消息，[引擎接口类型](../engine/source_codes/openmdbench/schemas/interface_v2.py) 定义动作数据。 |
| 评测与证据 | [evaluate.py](evaluate.py) 推进、结算、写结果 | [score_evidence.py](score_evidence.py) 保存评分证据，[transport_evidence.py](transport_evidence.py) 保存通信证据，[integrity.py](integrity.py) 计算运行时摘要。 |

如需修改己方决策，先找到相应策略和[选手接口](../docs/participant-guide.md)。如需查看动力学、能量、碰撞与世界事件，读 [OpenMDBench 引擎源码](../engine/source_codes/openmdbench)。
