# Backtest：唯一正式回测目录

## 1. 目录和职责

```text
backtest/
  __main__.py       validate / calibrate / run / compare / demo / reproduce
  config.py         所有参数、校验与配置加载
  domain.py         Bar / Pair / Spec / Model / Signal / Position
  data.py           CSV/Parquet、时间/身份适配、PIT规格、输入哈希
  contracts.py      主次合约在线识别、训练段长度、窗口选择
  features.py       严格左侧统计、VR、CL/F25、趋势特征
  gates.py          VR/MRE/CER和有可见时间的F25历史
  strategy.py       路由与逐日退出状态
  training.py       MR500/Trend96、年度模型、历史成本门槛
  engine.py         唯一连续事件状态机，不按年清仓
  allocation.py     现金扫入、出资旧仓、显式预热等权
  ledger.py         数量、现金损益、费用、OPEN/已平仓账本
  metrics.py        同一账本派生的净值与指标
  reporting.py      CSV/JSON/JSONL/Markdown/图表
  pipeline.py       校准、哈希验证、编排、失败产物与比较
  demo.py           固定种子的合成集成数据
  configs/          默认配置与显式历史语义研究配置
  assets/           仅规格/注册表格式模板，不含虚构市场参数
  docs/             策略、数据、迁移、审查问题与验证说明
  tests/            公式、因果、数据、账本、全流程回归
```

`data.py → contracts/features → strategy/gates → engine/ledger → metrics/reporting` 是阅读主线。`training.py` 使用同一个引擎训练历史前缀，`pipeline.py` 不反向侵入领域对象。

## 2. 安装和自检

在仓库根目录执行：

```bash
python -m pip install -e '.[dev,charts]'
python -m compileall -q backtest
python -m pytest -q
python -m backtest demo --output backtest/demo_output --run
```

需要 Python 3.11+。包依赖 pandas、pyarrow；图表额外使用 matplotlib，开发测试使用 pytest。当前依赖声明是兼容下界，不是精确环境锁；CI 的两版本测试结果也不能替代部署机器的依赖快照。

演示目录存在时命令拒绝覆盖。再次运行选择一个新的目录。合成生成器保存 `DATA_NOTICE.json`，明确标注 SYNTHETIC。

## 3. 真实输入

先阅读 [DATA_CONTRACT.md](docs/DATA_CONTRACT.md)。行情支持单个 CSV/Parquet 或目录内多个合约文件；规格需独立 CSV。默认要求完整 YYYYMM/YYMM 合约标识，不接受旧月序列的隐式身份假设。所有路径通过 CLI 传入，无个人 Windows 路径，无缓存数据根自动搜索。

```bash
python -m backtest validate --data-root /data/market --specs /data/specs.csv
python -m backtest calibrate --data-root /data/market --specs /data/specs.csv --output backtest/runs/models_001.json
python -m backtest run --data-root /data/market --specs /data/specs.csv --models backtest/runs/models_001.json --output backtest/runs/run_001
```

省略 `--models` 时，`run` 自动校准。`--config` 默认 `backtest/configs/default.json`，`--no-charts` 关闭图表，`--quiet` 隐藏校准进度。模型复用会验证源码、校准配置、每年部署前行情和规格前缀；历史前缀变化必须重新校准。追加未来数据只有在模型年份已有覆盖、配置一致、过去数据未变时才能复用。

## 4. 输出

每次输出是新目录，成功后从同级临时目录重命名发布，不覆盖现有结果。

- 顶层：`manifest.json`、`models.json`、`training_selection.csv`、`cost_training_selection.csv`、`metrics_by_cost.csv`、`REPORT.md`、`charts/`。
- 各成本目录：`cost_0bp/`、`cost_1bp/`、`cost_2bp/`。其中 `ledger.jsonl` 为事件账本，`fills.csv` 为实际数量改变；另外保留 orders、candidates、gate_decisions、allocation_events、daily_portfolio、phase_equity、closed_trades、metrics、yearly_returns、open_positions 和 state。
- 主指标由 `primary_cost_bps` 指定，所有收益/回撤字段为小数比例，报告展示为百分比。
- 失败结果保存至 `<run>.failed-<id>/`；存在引擎异常时，保留 `partial_state.json`、`partial_ledger.json` 和 `partial_fills.json`。不能将其当成完整回测。

比较两个运行：

```bash
python -m backtest compare backtest/runs/run_001 backtest/runs/run_002
```

结果包含配置、源码、输入、模型差异，以及主成本情景 orders/fills/daily_portfolio 的首个不同记录。

## 5. 历史语义与旧版复现

`configs/legacy_semantics_research.json` 显式启用历史经验名单、月序列适配、追溯规格、连续手数、稀疏F25注册表和同开盘代理。必须额外提供 `--origins`。**它仍运行修正后的 v2 引擎，不保证旧版逐笔一致。**

原版精确复现通过独立命令调用用户本机的冻结原仓库，而不是将旧运行代码复制进新包：

```bash
python -m backtest reproduce --legacy-root /path/to/Future-Spread-Trader --data-root /path/to/legacy/parquet --output /path/to/new-legacy-output
```

该命令要求原仓库 HEAD 为 `f7bff2b90087ea85a38449aec878c53e3d0bbe68`，原策略目录干净，原注册表存在；历史原始数据仍由用户提供。它是明示的档案验证工具，正常 run 不导入旧包。

## 6. 应先知道的限制

本包是日频研究引擎，不是可直接接实盘的执行系统。官方日线开盘并不证明双腿同时可成交；默认延迟开盘也只是一种日频执行情景。无夜盘时钟、部分成交、整数腿配比之外的对冲估计、历史交易所费率、强平模型或状态恢复。当前真实市场的修正后收益没有在本仓库预置或认证。
