"""
连续合约结算价 —— 数据访问与计算层

核心模型
========
合约序列：初始合约 ord=0；第 k 次换月启用的合约 ord=k（k>=1），换月记录在 rolls(ord=k)。
某日 d 在用合约 = ord 满足：roll ord r 的 switch_date > d 的最小 r（即 roll r-1 已生效、r 未生效），
等价于 ord = count(rolls.switch_date <= d)，该 ord 即 rolls 中严格大于 d 的最小 ord 再减 1。

连续价（基准日 B）
==================
cont(d, B) = price(contract(d), d) + Σ { delta(r) | d < switch_date(r) <= B }
其中 delta(r) = price(new_r, pre_date_r) - price(old_r, pre_date_r)，
pre_date_r 为切换前一交易日；任一所需报价缺失则 delta 为 NULL。
缺价时连续价留空，并指出：
  - 当天原价缺失；或
  - 区间内最早一个 delta 为空的换月，以及该换月待补的合约报价。

复杂度保证
==========
- 换月价差以 Fenwick 树（bit_sums 表）维护：区间求和、单点更新均为 O(log n)，
  辅助记录数 ≤ n+1（n=换月次数），随换月次数对数级增长。
- 连续价不落地：单笔报价更新只写 prices 与至多一个 delta + Fenwick 单点，
  绝不逐日回写连续价。
- 单次取数在一个只读快照事务内完成（同一已提交状态），逐日用对数级查询。

事务语义
========
- 报价更新（新增/更正）在单个 IMMEDIABLE 事务内提交：原价与受影响价差一起生效，
  不会出现“价已改、价差未改”的中间状态。
- 保存失败时事务整体回滚，调用方仍可拿到本次计算的预期结果用于页面展示。
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from contextlib import contextmanager
from datetime import date as date_cls, datetime
from typing import Optional


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

ISO_FMT = "%Y-%m-%d"


def iso(d: date_cls) -> str:
    return d.strftime(ISO_FMT)


def parse_iso(s: str) -> date_cls:
    return datetime.strptime(s, ISO_FMT).date()


def valid_iso(s: str) -> bool:
    try:
        parse_iso(s)
        return True
    except (ValueError, TypeError):
        return False


class AppError(Exception):
    """面向用户的业务错误（中文消息可直接展示）。"""


# --------------------------------------------------------------------------- #
# 数据库连接
# --------------------------------------------------------------------------- #

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def get_db_path() -> str:
    return os.environ.get("CONT_DB_PATH", os.path.join(os.getcwd(), "data", "continuous.db"))


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())


@contextmanager
def snapshot(conn: sqlite3.Connection):
    """
    只读快照事务：整次取数只看到同一个已提交状态。
    SQLite 默认 DEFERRED 事务的首条 SELECT 即获取读快照，事务结束前不变。
    """
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """立即写事务：保证“原价 + 受影响价差”原子提交，失败整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------- #
# 基础配置维护：交易日历 / 合约 / 换月计划
# --------------------------------------------------------------------------- #

def list_calendar(conn: sqlite3.Connection) -> list[str]:
    return [r["date"] for r in conn.execute("SELECT date FROM calendar ORDER BY date")]


def replace_calendar(conn: sqlite3.Connection, dates: list[str]) -> int:
    """全量替换交易日历（去重、校验格式、排序）。"""
    cleaned = sorted({d.strip() for d in dates if d and d.strip()})
    bad = [d for d in cleaned if not valid_iso(d)]
    if bad:
        raise AppError(f"交易日日期格式非法：{bad[0]}，应为 YYYY-MM-DD")
    with write_tx(conn):
        conn.execute("DELETE FROM calendar")
        conn.executemany("INSERT INTO calendar(date) VALUES (?)", [(d,) for d in cleaned])
    return len(cleaned)


def prev_trade_date(conn: sqlite3.Connection, day: str) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(date) AS d FROM calendar WHERE date < ?", (day,)
    ).fetchone()
    return row["d"] if row and row["d"] else None


def is_trade_date(conn: sqlite3.Connection, day: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM calendar WHERE date = ?", (day,)
    ).fetchone() is not None


def list_contracts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT c.contract_id, c.code, c.ord, "
        "  (SELECT COUNT(*) FROM prices p WHERE p.contract_id = c.contract_id) AS price_count "
        "FROM contracts c ORDER BY c.ord"
    ))


def get_contract(conn: sqlite3.Connection, contract_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM contracts WHERE contract_id = ?", (contract_id,)
    ).fetchone()


def contract_by_ord(conn: sqlite3.Connection, ord_: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM contracts WHERE ord = ?", (ord_,)
    ).fetchone()


def list_rolls(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT r.ord, r.switch_date, r.pre_date, r.new_contract_id, r.delta, "
        "       nc.code AS new_code, oc.code AS old_code "
        "FROM rolls r "
        "JOIN contracts nc ON nc.contract_id = r.new_contract_id "
        "JOIN contracts oc ON oc.ord = r.ord - 1 "
        "ORDER BY r.ord"
    ))


def setup_plan(conn: sqlite3.Connection, initial_code: str,
               switches: list[tuple[str, str]]) -> None:
    """
    （重新）登记初始合约与固定换月计划。
    switches: 按时间顺序的 [(switch_date, new_contract_code), ...]
    仅允许在尚无任何报价/价差数据时整体重建，避免历史数据错配。
    """
    initial_code = (initial_code or "").strip()
    if not initial_code:
        raise AppError("初始合约代码不能为空")
    plan: list[tuple[str, str]] = []
    seen_dates: set[str] = set()
    prev = None
    for sd, code in switches:
        sd = (sd or "").strip()
        code = (code or "").strip()
        if not sd or not code:
            continue
        if not valid_iso(sd):
            raise AppError(f"换月切换日格式非法：{sd}")
        if sd in seen_dates:
            raise AppError(f"换月切换日重复：{sd}")
        if prev is not None and sd <= prev:
            raise AppError(f"换月切换日必须严格递增：{prev} 之后不能是 {sd}")
        if not is_trade_date(conn, sd):
            raise AppError(f"换月切换日 {sd} 不在交易日历中")
        seen_dates.add(sd)
        plan.append((sd, code))
        prev = sd

    with write_tx(conn):
        n_prices = conn.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
        n_rolls = conn.execute("SELECT COUNT(*) AS n FROM rolls").fetchone()["n"]
        if n_prices or n_rolls:
            raise AppError("已存在报价或换月数据，不能重建计划；如需调整请先清空数据")
        conn.execute("DELETE FROM rolls")
        conn.execute("DELETE FROM contracts")
        conn.execute("DELETE FROM bit_sums")

        cur = conn.execute(
            "INSERT INTO contracts(code, ord) VALUES (?, 0)", (initial_code,)
        )
        init_id = cur.lastrowid
        conn.execute("UPDATE meta SET initial_contract_id = ? WHERE id = 1", (init_id,))
        for k, (sd, code) in enumerate(plan, start=1):
            cur = conn.execute(
                "INSERT INTO contracts(code, ord) VALUES (?, ?)", (code, k)
            )
            new_id = cur.lastrowid
            pre = prev_trade_date(conn, sd)
            if pre is None:
                raise AppError(f"切换日 {sd} 之前没有交易日，无法取换月价差")
            conn.execute(
                "INSERT INTO rolls(ord, switch_date, pre_date, new_contract_id, delta) "
                "VALUES (?, ?, ?, ?, NULL)",
                (k, sd, pre, new_id),
            )


# --------------------------------------------------------------------------- #
# Fenwick 树：换月价差的对数级前缀和
# --------------------------------------------------------------------------- #

def _bit_add(conn: sqlite3.Connection, n: int, pos: int, v: int) -> None:
    i = pos
    while i <= n:
        row = conn.execute("SELECT val FROM bit_sums WHERE pos = ?", (i,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO bit_sums(pos, val) VALUES (?, ?)", (i, v)
            )
        else:
            conn.execute(
                "UPDATE bit_sums SET val = val + ? WHERE pos = ?", (v, i)
            )
        i += i & -i


def _bit_sum(conn: sqlite3.Connection, pos: int) -> int:
    total = 0
    i = pos
    while i > 0:
        row = conn.execute("SELECT val FROM bit_sums WHERE pos = ?", (i,)).fetchone()
        if row is not None:
            total += row["val"]
        i -= i & -i
    return total


def range_delta_sum(conn: sqlite3.Connection, lo_ord: int, hi_ord: int) -> int:
    """换月序号闭区间 [lo_ord, hi_ord] 内已知价差之和（调用前需确认区间内无 NULL）。"""
    if lo_ord > hi_ord:
        return 0
    return _bit_sum(conn, hi_ord) - _bit_sum(conn, lo_ord - 1)


# --------------------------------------------------------------------------- #
# 报价维护
# --------------------------------------------------------------------------- #

def get_price(conn: sqlite3.Connection, contract_id: int, day: str) -> Optional[int]:
    row = conn.execute(
        "SELECT price FROM prices WHERE contract_id = ? AND date = ?",
        (contract_id, day),
    ).fetchone()
    return row["price"] if row else None


def list_prices(conn: sqlite3.Connection, contract_id: Optional[int] = None,
                limit: int = 500) -> list[sqlite3.Row]:
    if contract_id is None:
        return list(conn.execute(
            "SELECT p.contract_id, c.code, p.date, p.price FROM prices p "
            "JOIN contracts c ON c.contract_id = p.contract_id "
            "ORDER BY p.date DESC, c.ord LIMIT ?", (limit,)
        ))
    return list(conn.execute(
        "SELECT p.contract_id, c.code, p.date, p.price FROM prices p "
        "JOIN contracts c ON c.contract_id = p.contract_id "
        "WHERE p.contract_id = ? ORDER BY p.date DESC LIMIT ?",
        (contract_id, limit),
    ))


def _recompute_roll_delta(conn: sqlite3.Connection, ord_: int) -> Optional[int]:
    """
    依据当前 prices 重算第 ord_ 次换月的价差并更新 rolls/bit_sums。
    返回新的 delta（缺报价为 None）。
    """
    roll = conn.execute("SELECT * FROM rolls WHERE ord = ?", (ord_,)).fetchone()
    if roll is None:
        return None
    n = conn.execute("SELECT COUNT(*) AS n FROM rolls").fetchone()["n"]
    old = contract_by_ord(conn, ord_ - 1)
    new_id = roll["new_contract_id"]
    p_old = get_price(conn, old["contract_id"], roll["pre_date"])
    p_new = get_price(conn, new_id, roll["pre_date"])
    new_delta = (p_new - p_old) if (p_old is not None and p_new is not None) else None

    old_delta = roll["delta"]
    if new_delta != old_delta:
        conn.execute("UPDATE rolls SET delta = ? WHERE ord = ?", (new_delta, ord_))
        # Fenwick 只承载非空值：旧值撤下、新值加入，均为 O(log n)
        if old_delta is not None:
            _bit_add(conn, n, ord_, -old_delta)
        if new_delta is not None:
            _bit_add(conn, n, ord_, new_delta)
    return new_delta


def upsert_price(conn: sqlite3.Connection, contract_id: int, day: str,
                 price: int) -> dict:
    """
    新增/更正一笔整数分结算价。
    原子事务内完成：
      1) prices 写入（原价与价差一起生效）；
      2) 仅重算“价差参考日 = 该日”的换月（至多 1 个，因为 pre_date 唯一）；
      3) 不触碰任何连续价、不逐日回写。
    返回预览信息（即使提交失败，调用方也可展示计算结果）。
    """
    if not valid_iso(day):
        raise AppError(f"日期格式非法：{day}，应为 YYYY-MM-DD")
    if not isinstance(price, int) or isinstance(price, bool):
        raise AppError("结算价必须是整数（单位：分）")
    if not is_trade_date(conn, day):
        raise AppError(f"{day} 不在交易日历中，不能登记报价")

    contract = get_contract(conn, contract_id)
    if contract is None:
        raise AppError(f"合约不存在：id={contract_id}")

    affected_roll = conn.execute(
        "SELECT ord FROM rolls WHERE pre_date = ?", (day,)
    ).fetchone()
    before = get_price(conn, contract_id, day)
    preview = {
        "contract": contract["code"], "date": day, "price": price,
        "old_price": before, "is_correction": before is not None,
        "affected_roll_ord": affected_roll["ord"] if affected_roll else None,
        "committed": False,
    }

    try:
        with write_tx(conn):
            conn.execute(
                "INSERT INTO prices(contract_id, date, price) VALUES (?, ?, ?) "
                "ON CONFLICT(contract_id, date) DO UPDATE SET price = excluded.price",
                (contract_id, day, price),
            )
            if affected_roll is not None:
                new_delta = _recompute_roll_delta(conn, affected_roll["ord"])
                preview["new_delta"] = new_delta
        # 事务已由上下文管理器提交：原价与受影响价差同时生效
        preview["committed"] = True
    except sqlite3.Error as e:
        # 保存失败已整体回滚；预期结果仍随 preview 返回供页面展示
        preview["committed"] = False
        preview["error"] = str(e)
    return preview


# --------------------------------------------------------------------------- #
# 取数：逐日连续价
# --------------------------------------------------------------------------- #

def active_contract_ord(conn: sqlite3.Connection, day: str) -> int:
    """
    当日在用合约的 ord：切换日 <= day 的换月次数。
    走 switch_date 唯一索引的反向定位（LIMIT 1），单次 O(log n)，
    不做区间计数扫描；因 ord 稠密且与切换日同序，取到的最大 ord 即次数。
    """
    row = conn.execute(
        "SELECT ord FROM rolls WHERE switch_date <= ? "
        "ORDER BY switch_date DESC LIMIT 1",
        (day,),
    ).fetchone()
    return row["ord"] if row else 0


def _first_missing_roll(conn: sqlite3.Connection, lo_ord: int,
                        hi_ord: int) -> Optional[sqlite3.Row]:
    """区间 [lo_ord, hi_ord] 内最早一个缺价差的换月。"""
    if lo_ord > hi_ord:
        return None
    return conn.execute(
        "SELECT r.*, nc.code AS new_code, oc.code AS old_code "
        "FROM rolls r "
        "JOIN contracts nc ON nc.contract_id = r.new_contract_id "
        "JOIN contracts oc ON oc.ord = r.ord - 1 "
        "WHERE r.ord BETWEEN ? AND ? AND r.delta IS NULL "
        "ORDER BY r.ord LIMIT 1",
        (lo_ord, hi_ord),
    ).fetchone()


def _missing_quote_contracts(conn: sqlite3.Connection,
                             roll: sqlite3.Row) -> list[sqlite3.Row]:
    """某换月价差缺失时，具体缺 pre_date 上哪个合约的报价。"""
    missing = []
    old_c = contract_by_ord(conn, roll["ord"] - 1)
    new_c = get_contract(conn, roll["new_contract_id"])
    for c in (old_c, new_c):
        if get_price(conn, c["contract_id"], roll["pre_date"]) is None:
            missing.append(c)
    return missing


def fetch_range(conn: sqlite3.Connection, start: str, end: str,
                base: str) -> list[dict]:
    """
    取 [start, end] 交易日的连续价，基准日 base（不早于 end）。
    整个过程在一个只读快照事务中执行，看到的是同一已提交状态。
    逐日查询：在用合约定位 O(log n)（唯一索引反向定位）、区间价差和 O(log n)（Fenwick）。
    """
    for label, v in (("开始日", start), ("结束日", end), ("基准日", base)):
        if not valid_iso(v):
            raise AppError(f"{label}格式非法：{v}，应为 YYYY-MM-DD")
    if start > end:
        raise AppError("开始日不能晚于结束日")
    if base < end:
        raise AppError(f"基准日 {base} 不能早于结束日 {end}")

    rows: list[dict] = []
    with snapshot(conn):
        # 所有读取均在同一只读快照内：整次取数只看到一个已提交状态
        base_row = conn.execute(
            "SELECT ord FROM rolls WHERE switch_date <= ? "
            "ORDER BY switch_date DESC LIMIT 1",
            (base,),
        ).fetchone()
        base_roll_hi = base_row["ord"] if base_row else 0
        days = [r["date"] for r in conn.execute(
            "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date",
            (start, end),
        )]
        if not days:
            raise AppError(f"{start} 至 {end} 之间没有交易日")

        for day in days:
            ord_ = active_contract_ord(conn, day)
            contract = contract_by_ord(conn, ord_)
            raw = get_price(conn, contract["contract_id"], day)

            # 该日之后至基准日（含）的换月：switch_date in (day, base] → ord in (ord_, base_roll_hi]
            lo, hi = ord_ + 1, base_roll_hi
            miss_roll = _first_missing_roll(conn, lo, hi)

            row = {
                "date": day,
                "contract": contract["code"],
                "raw_price": raw,
                "continuous": None,
                "status": "ok",
                "message": "",
            }
            if raw is None:
                row["status"] = "missing_raw"
                row["message"] = f"缺少原价：合约 {contract['code']} 在 {day} 的结算价待补"
            elif miss_roll is not None:
                row["status"] = "missing_roll"
                miss_cs = _missing_quote_contracts(conn, miss_roll)
                names = "、".join(
                    f"{c['code']}({miss_roll['pre_date']})" for c in miss_cs
                ) or "价差所需报价"
                row["message"] = (
                    f"换月 {miss_roll['old_code']} → {miss_roll['new_code']} "
                    f"（切换日 {miss_roll['switch_date']}）缺价差，待补报价：{names}"
                )
            else:
                adj = range_delta_sum(conn, lo, hi)
                row["continuous"] = raw + adj
                row["adjustment"] = adj
            rows.append(row)
    return rows


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["交易日", "在用合约", "原价(分)", "连续价(分)", "状态", "说明"])
    status_cn = {"ok": "正常", "missing_raw": "缺原价", "missing_roll": "缺换月报价"}
    for r in rows:
        writer.writerow([
            r["date"], r["contract"],
            "" if r["raw_price"] is None else r["raw_price"],
            "" if r["continuous"] is None else r["continuous"],
            status_cn.get(r["status"], r["status"]),
            r["message"],
        ])
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# 状态总览
# --------------------------------------------------------------------------- #

def overview(conn: sqlite3.Connection) -> dict:
    n_contracts = conn.execute("SELECT COUNT(*) AS n FROM contracts").fetchone()["n"]
    n_rolls = conn.execute("SELECT COUNT(*) AS n FROM rolls").fetchone()["n"]
    n_missing_delta = conn.execute(
        "SELECT COUNT(*) AS n FROM rolls WHERE delta IS NULL"
    ).fetchone()["n"]
    n_prices = conn.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
    n_days = conn.execute("SELECT COUNT(*) AS n FROM calendar").fetchone()["n"]
    cal = list_calendar(conn)
    return {
        "n_contracts": n_contracts,
        "n_rolls": n_rolls,
        "n_missing_delta": n_missing_delta,
        "n_prices": n_prices,
        "n_days": n_days,
        "cal_min": cal[0] if cal else None,
        "cal_max": cal[-1] if cal else None,
    }
