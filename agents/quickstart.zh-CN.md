# 快速使用：低 token 调用链分析 Agent

## 推荐用法
把 `agents/low-token-callchain-agent.md` 作为 system/developer 约束，再给一个非常具体的问题，例如：

- `分析 /api/payment/confirm 的调用链（从路由到 DB 写入）`
- `分析定时任务 sync_user_profile 的调用链（触发->service->外部API）`

## 可执行版本（shell-only）
仓库已提供可执行脚本：`agents/callchain_agent.py`。

1. 复制示例规格：`agents/spec.example.json`
2. 按你的项目修改 `question` / `phase_a_commands` / `hypotheses`
3. 运行：

```bash
python3 agents/callchain_agent.py --spec agents/spec.example.json --repo . --out report.md
```

## 参数建议
- 小仓库：文件预算 8、命令预算 12
- 中仓库：文件预算 12、命令预算 18
- 大仓库：先按“单入口场景”切分，再分别跑

## 你会看到的收益
- 输出更稳：不会无限探索
- token 更低：上下文不会滚雪球
- 速度更快：每轮都围绕假设收敛
