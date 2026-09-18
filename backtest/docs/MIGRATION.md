# 重构来源、模块映射与策略差异

## 来源与交付形式

参考对象：`Lorien-LAB/Future-Spread-Trader`，提交 `f7bff2b90087ea85a38449aec878c53e3d0bbe68`，路径 `backtest/strategy/v1-10_Final`。目标仓库原先仅有README和空占位文件，因此本次在根目录直接建立backtest，不搬动原仓库，也不复制实盘代码。

本次不是承诺逐行保持旧行为的机械重命名，而是根据审查结果重建统一的因果事件引擎。算法意图、500/96网格、MR/Trend、VR/MRE/CER/F25和现金扫入进入同一正式包；已确认不合理的历史生命周期/账户模式不再作为正式实现保留。原样参考只通过外部冻结checkout的reproduce命令调用。

## 旧新映射

|旧模块/职责|新位置|处理|
|---|---|---|
|run_backtest.py + reproduce.ps1|__main__.py|跨平台统一命令行；run和档案reproduce分开|
|config.py + default.json|config.py + configs/|所有有效参数集中校验，无绝对Windows路径|
|core/data.py 文件读取/规格|data.py|只保留必要适配；open与mark独立；PIT规格不猜参数|
|core/data.py 主次识别|contracts.py|在线收盘状态、实际合约身份、稳定排序|
|EntryFeatureSnapshot与滚动统计|domain.py + features.py|强类型、纯计算、严格左窗口|
|core/product_types.py|strategy.py|默认训练期路由；经验名单只显式回顾研究|
|core/training.py + experts.py|training.py + strategy/features|500/96网格，单一引擎评价；去掉未启用替代专家|
|family_selection.py + candidate_engine.py|training.py|训练质量门和结构化拒绝原因|
|source_pipeline.py|training.py + engine.py|年度模型训练与连续交易状态解耦|
|core/execution.py 整笔结果回看|engine.py + strategy.py|先成交后形成结果，OPEN/EXIT_PENDING保留|
|gates.py|gates.py + features.py|门只接Signal/当前价格；F25可见性时钟明确|
|portfolio/policies.py + costs.py|allocation.py + ledger.py|数量目标、每次真实fill费用，旧仓不隐式再投资|
|portfolio/replay.py|engine.py + ledger.py|唯一账户事件流，不再重放事后筛过的完整结果|
|metrics.py + reporting.py|metrics.py + reporting.py|统一close和open/close指标，无首开盘遗漏|
|pipeline.py 固定REFERENCE/指纹断言|pipeline.py|运行输入不变量；旧业绩断言只留给外部档案验证|
|F25冻结资产|外部--origins + assets/格式模板|默认不依赖历史稀疏名单，不伪造或无声跳过|

## 语义决策记录

### 保留的基本算法

near-minus-far价差；D收盘主次信息；MR严格左侧总体标准差；sigma/T/S 500组；Trend96组；VR部分窗口；MRE剩余残差；年度成本阈值；默认MR绝对残差屏障；影子基础策略占位；新仓事件触发现金扫入。

### 修正后必然可能改变结果的部分

|变化|理由|是否保证旧业绩一致|
|---|---|---|
|保留截至年末/样本末OPEN|避免用未来是否完成决定过去是否入场|否|
|未来缺mark时保留历史fills并失败|缺失数据不能倒删成交|否|
|一个数量账本，close/settle口径显式|禁止两套PnL并行漂移|否|
|入场费用在fill当刻入账|不能在close才扣费用或预支资金|否|
|旧仓盈亏不自动转为新手数|融资调仓和再投资不是同义词|否|
|真实前一close作为评估基线|包含首日开盘损益/费用|否|
|MAE/MFE只在持仓生命周期更新|禁止读取退出后行情|否|
|配置完整传递、日期动态生成|禁止日志参数和运行算法不一致|否|
|F25历史等上游开盘可见后更新|禁止未来入选集合反向参与早期门槛|否|

### 默认配置主动选择的新研究条件

训练期路由替代事后经验名单；all_valid F25替代历史注册表；delayed_open替代同开盘代理；整数手替代连续手数；5%保证金现金缓冲；PIT历史规格替代静态后备值。这些不是纯软件修复，必须作为研究版本改变进行比较。`legacy_semantics_research.json`只对部分条件提供明确开关，不复活错误生命周期或旧资本化账本。

原先0bp 68.0290%、2bp43.0781%以及711笔参考只用于识别旧研究，不写入正常运行的通过条件。本次没有取得原完整数据快照和历史合约规格来计算修正后的真实市场收益。

## 旧文件处理原则

没有把旧研究路由、未启用段斜率专家、R8双数据源工具、KKT分配器和多份回测副本迁入正式包。Trend仍在预热/训练比较里使用，不能简单删除。需要研究新专家时，在现有接口与配置注册，先加测试，不建第二套引擎。

旧策略源码保持在原仓库原commit中作为档案；新正常运行不访问该仓库、网络或外部agent。外部reproduce是可选的档案桥接，不是正常backtest的隐式依赖。

## 后续实验建议

在固定真实数据/规格快照上，依次比较生命周期修复、旧vs训练期路由、旧vs全覆盖F25、shadow vs actual、有符号vs绝对屏障、同开盘代理vs延迟执行、连续vs整数数量、无现金缓冲vs有缓冲。每次只改变一个明确研究条件，保存events差异与成本/风险，而不只看最终CAGR。
