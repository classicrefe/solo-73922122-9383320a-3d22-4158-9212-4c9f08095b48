# 期货连续合约结算价系统

把多个合约的期货结算价，按固定换月计划统一到**指定基准日的连续合约口径**。
Flask + SQLite 单体应用，Docker Compose 一键启动。

## 口径定义

- 合约序列：一个**初始合约**（`ord=0`）+ 每次换月启用的新合约（第 k 次换月 → `ord=k`）。
- 换月价差：取**切换日前一交易日**的 `新合约结算价 − 旧合约结算价`（单位：整数分）。
- 某日 d 的连续价（基准日 B，B 不早于查询结束日）：

```
连续价(d, B) = 当日在用合约结算价
             + Σ { 各次换月价差 | d < 切换日 <= B }
```

- 当日原价缺失 → 连续价留空，注明缺哪个合约哪天的原价。
- 区间内某次换月缺报价 → 连续价留空，注明**最早**缺价换月及其待补的合约/日期。
- 连续价**实时计算、不落库**；改价不逐日回写。

## 数据结构与复杂度

| 表 | 作用 |
|---|---|
| `calendar` | 交易日历 |
| `contracts` | 初始合约及每次换月启用的合约（`ord` 即启用次序） |
| `rolls` | 固定换月计划：切换日、前一交易日、新合约、价差（缺报价为 NULL） |
| `prices` | 按合约 + 交易日登记的整数分结算价（联合主键，重复提交即更正） |
| `bit_sums` | 换月价差的 **Fenwick 树**辅助表 |

- 价差区间求和、单点更新均为 **O(log n)**（n=换月次数）；辅助记录数 ≤ n，随换月次数只对数级增长。
- 单笔报价更新：写 1 行 `prices` + 至多重算 1 个价差（前一交易日唯一）+ Fenwick 单点 O(log n)，
  在一个 `BEGIN IMMEDIATE` 事务内**原子提交**，原价与受影响价差一起生效；保存失败整体回滚，
  预期计算结果仍随返回值可供展示。
- 一次取数在一个只读快照事务内完成，只看到同一已提交状态（SQLite 读快照）。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

数据库持久化在宿主机 `./data/continuous.db`（含 WAL 文件）。

## 本地运行（无 Docker 时）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export CONT_DB_PATH=$(pwd)/data/continuous.db
python -m flask --app app.app run --host 0.0.0.0 --port 8000
```

## 使用流程

1. **交易日历**：每行一个 `YYYY-MM-DD`，全量替换保存。
2. **换月计划**：填初始合约代码，逐行添加「切换日 + 当日启用的新合约」后保存。
   切换日必须在交易日历内且严格递增；已有报价后禁止整体重建（防历史错配）。
3. **报价补录/更正**：选合约、交易日，填整数分价格。重复提交即更正；
   若该日是某换月的价差参考日，同事务自动联动重算价差。
4. **连续价取数**：选开始日、结束日、基准日（不得早于结束日）→ 查询 / 下载 CSV
   （UTF-8 BOM，Excel 直开不乱码；缺价行连续价列留空并附说明）。

## 测试

```bash
PYTHONPATH=. python -m unittest discover -s tests -v      # 18 个单元/集成测试
# 端到端（先启动服务）：
BASE_URL=http://127.0.0.1:8000 CONT_DB_PATH=./data/continuous.db \
  python tests/e2e_http.py
```

## 目录

```
app/schema.sql      建表脚本（幂等）
app/db.py           数据访问、Fenwick 树、连续价计算、事务与快照
app/app.py          Flask 路由与 CSV 下载
app/templates/      页面模板
tests/              单元/集成测试 + HTTP 端到端试跑脚本
Dockerfile / docker-compose.yml
```
