# 本地竞价选股器

这个程序把当前目录下三份聚宽策略的选股部分迁移到本地，运行时只读取行情并输出候选股票，不包含下单、持仓、止盈止损和盘后交易统计。

## 运行环境

```powershell
python -m pip install -r requirements.txt
```

当前机器已经安装 `akshare`、`pandas` 和 `numpy`，可以直接试跑。

## 每天运行

在交易日 09:26 之后执行：

```powershell
python .\local_selector.py
```

也可以双击 `run_selector.bat`，或给它追加参数。

指定日期或单个策略：

```powershell
python .\local_selector.py --date 2026-08-17
python .\local_selector.py --strategy 低位首阳必买
python .\local_selector.py --strategy 打板 竞价量比
python .\local_selector.py --refresh
```

结果文件：

- `output/picks_YYYYMMDD.csv`
- `output/picks_YYYYMMDD.json`

行情请求会缓存到 `cache/`，同一天重复运行时不会重复下载已经成功获取的数据。

## 三份策略的本地化边界

- `低位首阳必买`：保留基础股票池、首板、量价、5 日波动和竞价量价过滤。
- `竞价量比`：保留昨日强势、极端涨停、5 日波动、连板、近 100 日高位和 C/D/E/F 竞价规则。
- `打板`：保留 MA5/MA10/MA30、多板限制、年涨停次数、换手率、MA5 斜率、3% 高开和一字板过滤。
- 聚宽的概念接口没有本地直接等价物；`打板` 的“三板必须命中热点概念”使用昨日涨停池的“所属行业”做热点代理，并在结果 `notes` 中标注。
- 为控制每日请求量，候选范围默认从昨日涨停池开始收敛。这与原策略“扫描全市场后再过滤”并非完全相同，但更适合本地每日运行，也避免对行情接口造成大量请求。

## 注意事项

1. 必须在竞价结束后运行，程序读取 09:26 盘前分时的最后一条记录；如需忽略历史缓存并重新拉取，使用 `--refresh`。
2. AkShare 和上游行情接口可能临时限流；遇到请求失败可稍后重试，缓存不会覆盖成功数据。
3. 输出结果是策略条件的机械筛选，不代表投资建议；原聚宽策略中的买入价格、仓位、卖出和风控逻辑均已移除。
