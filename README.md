# Future-Strategy

商品期货跨期价差研究。正式回测代码直接位于 **[`backtest/`](backtest/)**；没有 `backtest/strategy/calendar_spread_mr/`，也不在版本目录间复制运行代码。

## 从这里开始

```bash
python -m pip install -e '.[dev,charts]'
python -m pytest -q
python -m backtest demo --output backtest/demo_output --run
```

Windows PowerShell 同样可以运行以上命令；安装依赖时可使用双引号 `".[dev,charts]"`。`demo` 生成固定随机种子的**合成数据**，然后执行年度训练、过滤、组合、0/1/2bp 重放和图表。它用于验证软件，不是收益证据。

真实数据运行：

```bash
python -m backtest validate --config backtest/configs/default.json --data-root /path/to/market --specs /path/to/specs.csv
python -m backtest run --config backtest/configs/default.json --data-root /path/to/market --specs /path/to/specs.csv --output backtest/runs/run_001
```

完整参数、数据格式和输出说明见 [回测入口](backtest/README.md)。新 agent 先读 [AGENTS.md](AGENTS.md)，再读 [策略说明](backtest/docs/STRATEGY.md) 和 [重构差异](backtest/docs/MIGRATION.md)。

## 版本与验证边界

本版本根据 `Lorien-LAB/Future-Spread-Trader` 的 `v1-10_Final` 审查结果实现，是**纠正因果/记账问题后的 v2 研究实现**，不是把旧代码换个目录后保证逐笔一致的机械迁移。原仓库、原冻结版本和实盘模块未被修改。

默认配置使用训练期路由、完整有效 F25 覆盖、整数手、5% 现金缓冲以及观察开盘后的延后开盘执行；旧经验路由与同开盘代理仅通过显式研究配置启用。旧版 43.0781% 年化、711 笔交易等不是本引擎的通过条件，也不被自动沿用。

GitHub Actions 工作流 `Backtest validation` 验证 Python 3.11/3.13、回归测试和带图表 CLI 演示。请以对应提交的实际工作流结果为准，不能把工作流文件存在当成已经通过。没有附带原始真实市场数据与历史规格，本仓库不宣称已经完成原市场的全历史数值验证。
