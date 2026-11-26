# 离线回放插件

在仓库根目录运行一次示例评测后，可从已有结果重新导出回放。

```text
python replay.py local-results/ie01 -o local-results/ie01.html
python replay.py local-results/ie01 local-results/ie15 -o local-results/comparison.html
python replay.py local-results/ie15 --participant-only -o local-results/ie15-observations.html
```

在 Chrome 或 Edge 中打开导出的 HTML。独立 HTML 内嵌播放器和对局数据，可以离线播放。多场导出可在左侧切换对局。`--participant-only` 导出参赛方授权观测视图。

运行评测时也可直接选择 `--replay-format html`，或使用默认的 `--replay-format shared`。共用模式下，打开结果目录上一级的 `replay-viewer.html`，再导入本局的 `replay.pack.json.gz`。

保留原始对局目录时，请一并保留结果、原生记录和场景快照，便于重建来源一致的回放。
