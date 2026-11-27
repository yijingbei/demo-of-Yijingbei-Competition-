# Delta 使用指南：比赛镜像与模型 API

选手可在本地或 Delta 云端开发、调试和运行策略。**推荐使用 Delta 云端比赛镜像**，其中已包含比赛代码、场景及运行依赖，可减少环境配置工作。本地运行方式见[仓库说明](https://github.com/yijingbei/demo-of-Yijingbei-Competition-#readme)。

**报名主赛道可领取专属额度**，具体领取方式以主办方通知为准。

沙箱管理服务与模型 API 使用不同的接口地址。可用镜像、模型及计算资源以账号查询结果为准。

## 1. 检查 Delta CLI 与账号配置

使用前，应在本地完成 Delta CLI 安装及账号登录。安装与登录说明见 [Delta CLI 官方文档](https://github.com/yzailab/delta-infra-cli#快速开始)。

沙箱管理服务地址为 `https://delta-infra-nacos.yangtzeailab.com`。在本地终端执行以下命令：

```bash
delta-cli --version
delta-cli config show
delta-cli auth status
delta-cli usage
delta-cli quota
```

确认 `base_url` 与上述地址一致，认证方式为 `apikey`。`usage` 用于查询剩余积分（credits），`quota` 用于查询硬件资源配额。若地址不符，应检查 `DELTA_INFRA_BASE_URL` 环境变量是否覆盖了配置。

## 2. 使用比赛镜像创建沙箱

查看当前账号可用的镜像：

```bash
delta-cli sandbox images
```

从列表中选择比赛镜像。以下以 `yjb-competition:latest` 为例，具体名称以平台当前列表为准。创建一个配置为 2 核 CPU、4 GiB 内存、无 GPU 的沙箱：

```bash
delta-cli sandbox create --image-name yjb-competition:latest --cpu 2 --memory 4Gi --gpu 0 --max-life 120 --shared-workspace=false
```

记录响应中的 `sandbox_id`，并将后续命令中的 `SANDBOX_ID` 替换为该值。`--max-life 120` 表示沙箱最长存活时间为 120 分钟。本示例禁用共享工作区，运行结果需主动下载保存。

示例镜像中的比赛项目位于 `/app`，包含运行入口、场景、示例策略及运行依赖。执行以下命令查看项目文件和评测参数：

```bash
delta-cli sandbox run SANDBOX_ID --command "ls /app" --timeout 60 --format json
delta-cli sandbox run SANDBOX_ID --command "python3 /app/run.py evaluate --help" --timeout 60 --format json
```

镜像中的代码版本取决于其构建时间，具体功能及策略以镜像内实际文件为准。

## 3. 运行镜像内的场景

使用内置 `adaptive` 规则基线执行 3 个仿真步（tick），验证场景运行流程并生成 HTML 回放：

```bash
delta-cli sandbox run SANDBOX_ID --command "cd /app && python3 run.py evaluate --scenario IE-01-SINGLE-TARGET --mode adaptive --seed 2026102302 --max-ticks 3 --output /app/local-results/cloud-ie01-baseline --replay-format html" --timeout 300 --format json
```

结果保存在 `/app/local-results/cloud-ie01-baseline`。本示例用于短程调试，完整对局的评测参数见仓库说明。每次实验应指定新的 `--output` 目录。

## 4. 接入自定义策略或 Agent

比赛环境已包含在镜像中，选手可上传自定义策略文件。以单文件 Python 策略为例，在本地创建 `my-agent` 目录，并放入以下文件：

- [`minimal_agent.py`](https://github.com/yijingbei/demo-of-Yijingbei-Competition-/blob/main/examples/minimal_agent.py)：根据参赛接口实现策略逻辑，并保留该文件名。
- [`create_submission.py`](https://github.com/yijingbei/demo-of-Yijingbei-Competition-/blob/main/examples/create_submission.py)：用于生成包含程序入口和文件校验值的提交清单。

未修改的 `minimal_agent.py` 返回空动作，适用于接口连通性验证。完整接口说明见[参赛指南](https://github.com/yijingbei/demo-of-Yijingbei-Competition-/blob/main/docs/participant-guide.md)。

在本地终端进入 `my-agent` 的上一级目录，上传文件，并在云端生成提交清单：

```bash
delta-cli sandbox upload SANDBOX_ID --source ./my-agent --target /app/my-agent
delta-cli sandbox run SANDBOX_ID --command "python3 /app/my-agent/create_submission.py" --timeout 60 --format json
```

在云端生成提交清单，可确保程序入口指向沙箱内的解释器和策略文件。策略包含多个文件或模型权重时，应按参赛接口补全提交清单中的文件及校验值。上传目录不应包含密钥、`.git` 目录及无关文件。

在后台启动策略调试任务：

```bash
delta-cli sandbox run-bg SANDBOX_ID --command "cd /app && python3 run.py evaluate --scenario IE-01-SINGLE-TARGET --submission /app/my-agent/submission.json --seed 2026102302 --max-ticks 3 --output /app/local-results/cloud-ie01-agent --replay-format html" --timeout 300
```

记录响应中的 `execution_id`，并将后续命令中的 `EXECUTION_ID` 替换为该值。策略需要额外依赖时，应完成依赖安装后再启动任务。

选手可基于该镜像安装开发工具、运行 Agent 或开展其他实验。开发 Agent 可部署在本地并通过 CLI 操作沙箱，也可安装在沙箱中运行。

## 5. 查看日志、下载结果并释放资源

实时查看后台任务日志，并查询沙箱状态和策略运行结果：

```bash
delta-cli sandbox logs SANDBOX_ID --execution-id EXECUTION_ID --stream
delta-cli sandbox status SANDBOX_ID
delta-cli sandbox read SANDBOX_ID --path /app/local-results/cloud-ie01-agent/result.json
```

`execution_id` 用于标识后台任务，执行结果应以日志和结果文件为准。`result.json` 中，`status` 表示运行状态，`task_success` 表示任务是否成功，`utility` 表示效用评分。上述 3 tick 调试正常结束时，状态为 `bounded_probe`。

本地等待超时或网络中断后，云端任务可能仍在运行。重新启动任务前，应先检查状态和日志。

长时间运行的任务应定期检查沙箱剩余存活时间，并按需延长：

```bash
delta-cli sandbox extend SANDBOX_ID
delta-cli sandbox extend SANDBOX_ID --max-life 240
```

`--max-life 240` 将沙箱总存活时间上限调整为 240 分钟。

实验结束后，应先下载结果并确认文件完整，再销毁沙箱：

```bash
delta-cli sandbox pull SANDBOX_ID --source /app/local-results --target ./cloud-results
delta-cli sandbox kill SANDBOX_ID
```

下载完成后，可在本地打开结果目录中的 `replay.html`。若在云端修改了策略，也应下载策略目录。沙箱到期或执行 `kill` 时不会自动保存运行结果。

## 6. 调用模型 API

模型 API 的 Base URL 为 `https://delta-router.yangtzeailab.com/v1`。接口兼容 OpenAI Chat Completions，示例模型为 `DeepSeek-V4-Flash`。

以下脚本使用 Python 标准库，无需额外安装依赖。保存为 `delta_call.py`：

```python
import getpass
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

api_key = getpass.getpass("Delta API Key: ").strip()
payload = {
    "model": "DeepSeek-V4-Flash",
    "messages": [{"role": "user", "content": "你好"}],
    "max_tokens": 128,
    "stream": False,
}
request = Request(
    "https://delta-router.yangtzeailab.com/v1/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    },
    method="POST",
)
try:
    with urlopen(request, timeout=60) as response:
        result = json.load(response)
    print(result["choices"][0]["message"]["content"])
except HTTPError as error:
    detail = error.read().decode("utf-8", errors="replace")
    raise SystemExit(f"HTTP {error.code}: {detail}")
```

在本地交互式终端执行以下命令，并按提示输入模型 API Key：

```bash
python -X utf8 delta_call.py
```

API Key 输入时不回显，脚本不保存密钥。收到 `401` 时应检查密钥，收到 `403` 时应检查权限与额度；`429` 或 `5xx` 可稍后重试。具体原因以接口返回的错误信息为准。

云端 Agent 调用模型时，需按所用工具的要求单独配置模型凭据。本地 CLI 登录不会自动向沙箱传递模型密钥。上述交互式输入示例适用于本地终端，`sandbox run` 不提供该输入方式。密钥不应写入公开代码或命令行参数。

更多命令见 [Delta CLI 官方文档](https://github.com/yzailab/delta-infra-cli)。

验证记录：2026-11-27，使用 Delta CLI v1.1.64 完成了本文所列沙箱命令及模型 API 示例的实际验证。
