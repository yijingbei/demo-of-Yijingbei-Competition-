# Delta 大模型 API 调用

准备 Python 3 和正式服 API Key。以下示例无需安装额外依赖。

Base URL：`https://delta-router.yangtzeailab.com/v1`

示例模型：`DeepSeek-V4-Flash`。

将以下代码保存为 `delta_call.py`：

```python
import getpass
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

base_url = "https://delta-router.yangtzeailab.com/v1"
api_key = getpass.getpass("Delta API Key: ").strip()
payload = {
    "model": "DeepSeek-V4-Flash",
    "messages": [{"role": "user", "content": "你好"}],
    "max_tokens": 128,
    "stream": False,
}
request = Request(
    f"{base_url}/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Authorization": f"Bearer {api_key}",
             "Content-Type": "application/json"},
    method="POST",
)
try:
    with urlopen(request, timeout=60) as response:
        result = json.load(response)
    print(result["choices"][0]["message"]["content"])
except HTTPError as error:
    raise SystemExit(f"HTTP {error.code}: {error.read().decode('utf-8', errors='replace')}")
```

运行：

```bash
python -X utf8 delta_call.py
```

按提示粘贴 API Key，输入不回显。成功后显示模型回复；Key 不写入文件。

常见错误：`401` 检查是否使用有效的正式服 Key；`403` 检查额度和模型权限；`429` 或 `5xx` 稍后重试。具体原因以返回的错误信息为准。
