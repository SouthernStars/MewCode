# Deterministic Eval 基线

该基线不调用真实模型，使用固定输入、固定工具约束和固定观测结果，适合在 CI 或本地快速验证 Eval Runner 行为。

运行：

```bash
python scripts/run_deterministic_eval.py
```

当前基线包含 4 个场景：文件读取、文件修改、安全回答、定时任务。验收条件是成功率 100%，且每个场景不得超过声明的 Token 预算。

当前结果：

- 场景数：4
- 成功数：4
- 成功率：100%
- 平均 Token：14

原始结果保存在 [`docs/eval-baseline.json`](eval-baseline.json)。
