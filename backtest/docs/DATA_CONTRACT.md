# 数据契约与清洗细节

## 行情组织

输入为 UTF-8 CSV 或 Parquet，CLI 接受单文件或递归目录。目录应只放本次行情，不混入规格、模型产物等 CSV。一行是某一真实交割合约的一个**交易日**，键为 `(product, contract, trading_date)`。同日同合约重复直接失败，禁止靠文件排序覆盖，禁止将分钟数据压成最后一根冒充日线。

日期列优先级：`trading_date`、`date`、`time`。支持 `YYYY-MM-DD`、`YYYYMMDD`、数值年月日和 Unix 秒/毫秒/微秒/纳秒。数值时间戳先 UTC 再转 Asia/Shanghai；带时区日期也转上海。没有交易所夜盘日历，不能把墙上时间的自然日转换等同于交易日分配。CSV 无合约列时，可用 `A202801_DF.csv` 等合约文件名推断；合约列存在时以列值为准。

## 字段

|字段|读入与用途|缺失行为|
|---|---|---|
|trading_date/date/time|交易日、排序与可见性|无法解析失败|
|contract|实际交割身份；移除 -/_ 并大写|不能解析失败|
|product|可省略，按合约字母前缀推导|显式值与合约不一致失败|
|open|下一开盘观察/成交与开盘盯市|数值无效保留该行，但该开盘不可执行|
|close|主次排名资格、MR/VR/Trend信号|无效不产生该合约当日信号；不删除有效 open|
|high/low|信号日CL/F25|无效导致F25缺失，按显式政策处理|
|settlement/settle|日末估值；优先 settlement 列|无效时可显式回退有效 close；二者均无效且持仓则运行失败并保存账本|
|volume|D收盘活跃条件 >0|缺失/无穷/非法转0|
|open_interest/oi|D收盘主次排名，>=minimum_oi|缺失/无穷/非法转0|
|can_buy_open/can_sell_open|外部提供的**该开盘已知**买卖可执行状态|默认True是乐观原子成交假设；不要用当日最终高低价推导这两个标志|

数值不提前统一四舍五入到两位；保持输入精度。成交滑点以 `slippage_ticks × tick_size` 应用于每腿。费用按实际模拟成交价计名义金额。涨跌停、深度、成交量参与率不由日线OHLC自动猜测。

## 实际合约与月序列

默认接受 `A202801` 或 `A2801`；四位年月按2000世纪解释。三位郑商所历史代码因年代歧义拒绝，需输入方用合约主数据映射到完整YYYYMM。月份必须1–12。

`allow_month_series=true` 才接受 `a01`：按观察日期推断最近未过期交割月份；交割月15日及之前属于当年，16日起属于次年。这只是与旧代码相近的**日历假设**，不保证符合供应商真实换月规则。记录 `identity_source=legacy_month_series_calendar_assumption` 并在manifest披露。正式身份验证应改用真实合约主数据，不能把“按日期因果计算”写成“真实身份已验证”。

主次选择避开实际交割月前 `delivery_guard_days` 天，并排除交割月份早于当前年月的合约。这不是各交易所最后交易日/交割资格规则的完整实现。

## 合约规格

单独提供CSV：

```text
product,effective_from,known_at,multiplier,margin_rate,tick_size,source
```

所有字段必需。`effective_from` 是生效日；`known_at` 是策略最早可知日，日频日期粒度假定该日开盘前已知。严格模式只选择两者都不晚于当前日的最新版本。历史同合约持仓中乘数改变会失败，不能静默换算。保证金按当前可用价格和当前有效保证金率更新。

未知品种没有默认10倍、12%保证金等回退。`assets/specs_template.csv` 故意只含表头。真实历史规格需要可追溯来源，不允许为了跑通而填入看似合理的数字。`allow_retro_specs=true` 会明确标记为追溯研究，不再约束known_at，不应称为历史保证金可执行验证。

## F25 起点注册表

仅 `f25_coverage=registry` 使用，另传 `--origins` CSV/Parquet。四列 `product,date,main_contract,secondary_contract` 必须完整且规范后唯一；两合约用完整交割身份。注册表不在包中伪造。使用未证明事前可知的冻结名单需要 `allow_retrospective=true`。未匹配行只按配置决定中性保留或拒绝，报告必须披露覆盖限制。

## 复现边界

运行前后哈希包含实际读取的行情文件及CLI配置/规格/模型/注册表文件；模型另保存逐年历史数据前缀摘要。哈希证明本次输入一致，不能证明原数据无错误、身份正确、时间戳真正当时可见。数据修改后应生成新快照和新运行，不覆盖旧产物。
