"""
核心逻辑测试：
  连续价口径、缺价提示、改价联动、原子提交、快照隔离、Fenwick 对数复杂度、CSV、Web 冒烟。
运行：PYTHONPATH=. python -m unittest discover -s tests -v
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

from app import db


CAL = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
       "2026-01-09", "2026-01-12", "2026-01-13"]

# (合约, 日期, 价格)  单位：分
PRICES = [
    ("C0", "2026-01-05", 1000), ("C0", "2026-01-06", 1010),
    ("C0", "2026-01-07", 1020),
    ("C1", "2026-01-07", 1030), ("C1", "2026-01-08", 1040),
    ("C1", "2026-01-09", 1050),
    ("C2", "2026-01-09", 1075), ("C2", "2026-01-12", 1080),
    ("C2", "2026-01-13", 1090),
]


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        self.conn = db.connect(self.db_path)
        db.init_db(self.conn)
        db.replace_calendar(self.conn, CAL)
        db.setup_plan(self.conn, "C0",
                      [("2026-01-08", "C1"), ("2026-01-12", "C2")])
        cid = {r["code"]: r["contract_id"] for r in db.list_contracts(self.conn)}
        for code, day, px in PRICES:
            db.upsert_price(self.conn, cid[code], day, px)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def fetch(self, start, end, base):
        return db.fetch_range(self.conn, start, end, base)


class TestContinuousPrice(BaseCase):
    def test_delta_values(self):
        rolls = {r["ord"]: r["delta"] for r in db.list_rolls(self.conn)}
        self.assertEqual(rolls[1], 1030 - 1020)   # +10
        self.assertEqual(rolls[2], 1075 - 1050)   # +25

    def test_continuous_full_range_base_latest(self):
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        got = {r["date"]: r["continuous"] for r in rows}
        expected = {
            "2026-01-05": 1000 + 10 + 25,   # 两次换月都在该日之后
            "2026-01-06": 1010 + 10 + 25,
            "2026-01-07": 1020 + 10 + 25,
            "2026-01-08": 1040 + 25,        # roll1 当日已生效，只加 roll2
            "2026-01-09": 1050 + 25,
            "2026-01-12": 1080,             # 基准日前最后一次换月当日起不再调整
            "2026-01-13": 1090,
        }
        self.assertEqual(got, expected)
        self.assertTrue(all(r["status"] == "ok" for r in rows))

    def test_continuous_base_inside_range(self):
        # 基准日 = 结束日 01-09：roll2(01-12) 在基准日之后，不计入
        rows = self.fetch("2026-01-05", "2026-01-09", "2026-01-09")
        got = {r["date"]: r["continuous"] for r in rows}
        self.assertEqual(got["2026-01-05"], 1010)   # 1000 + 10
        self.assertEqual(got["2026-01-08"], 1040)
        self.assertEqual(got["2026-01-09"], 1050)

    def test_base_before_end_rejected(self):
        with self.assertRaises(db.AppError):
            self.fetch("2026-01-05", "2026-01-13", "2026-01-08")

    def test_missing_raw_price(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        self.conn.execute("DELETE FROM prices WHERE contract_id=? AND date='2026-01-06'",
                          (cid,))
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        row = next(r for r in rows if r["date"] == "2026-01-06")
        self.assertIsNone(row["continuous"])
        self.assertEqual(row["status"], "missing_raw")
        self.assertIn("C0", row["message"])
        self.assertIn("2026-01-06", row["message"])

    def test_missing_roll_quote_points_earliest_and_contracts(self):
        # 删掉 roll1 参考日(01-07) 的新合约报价 → delta1 置空
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C1")
        self.conn.execute("DELETE FROM prices WHERE contract_id=? AND date='2026-01-07'",
                          (cid,))
        # 直接重算 roll1 模拟该报价缺失下的状态
        with db.write_tx(self.conn):
            db._recompute_roll_delta(self.conn, 1)
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        early = next(r for r in rows if r["date"] == "2026-01-05")
        self.assertIsNone(early["continuous"])
        self.assertEqual(early["status"], "missing_roll")
        # 指出最早缺价换月（ord=1）及待补的具体合约报价
        self.assertIn("C0 → C1", early["message"])
        self.assertIn("C1(2026-01-07)", early["message"])
        # roll1 之后的日子不受该换月缺失影响（01-12 仍正常）
        last = next(r for r in rows if r["date"] == "2026-01-12")
        self.assertEqual(last["continuous"], 1080)

    def test_correction_recomputes_delta_and_continuous(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C1")
        preview = db.upsert_price(self.conn, cid, "2026-01-07", 1035)  # 1030 → 1035
        self.assertTrue(preview["committed"])
        self.assertTrue(preview["is_correction"])
        self.assertEqual(preview["new_delta"], 15)
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(rows[0]["continuous"], 1000 + 15 + 25)
        # 连续价不落地：库中不存在任何连续价表/列
        tables = {r["name"] for r in
                  self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("continuous", tables)

    def test_non_trade_date_rejected(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        with self.assertRaises(db.AppError):
            db.upsert_price(self.conn, cid, "2026-01-10", 999)  # 周六，非交易日

    def test_integer_price_required(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        with self.assertRaises(db.AppError):
            db.upsert_price(self.conn, cid, "2026-01-05", 1000.5)


class TestAtomicityAndSnapshot(BaseCase):
    def test_save_failure_rolls_back_but_returns_preview(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C1")
        before = db.get_price(self.conn, cid, "2026-01-07")

        # 让 Fenwick 更新在事务中途失败（模拟保存失败）
        orig = db._bit_add
        def boom(conn, n, pos, v):
            raise sqlite3.OperationalError("simulated disk failure")
        db._bit_add = boom
        try:
            preview = db.upsert_price(self.conn, cid, "2026-01-07", 1111)
        finally:
            db._bit_add = orig

        self.assertFalse(preview["committed"])          # 仍返回预期结果
        self.assertEqual(preview["price"], 1111)
        self.assertEqual(db.get_price(self.conn, cid, "2026-01-07"), before)  # 已回滚
        roll = self.conn.execute("SELECT delta FROM rolls WHERE ord=1").fetchone()
        self.assertEqual(roll["delta"], 10)             # 价差也保持旧值

    def test_snapshot_sees_one_committed_state(self):
        other = db.connect(self.db_path)
        cid = next(r["contract_id"] for r in db.list_contracts(other)
                   if r["code"] == "C0")
        with db.snapshot(self.conn):
            seen_before = db.get_price(self.conn, cid, "2026-01-05")
            db.upsert_price(other, cid, "2026-01-05", 2222)   # 另一连接已提交
            seen_in_snap = db.get_price(self.conn, cid, "2026-01-05")
        seen_after = db.get_price(self.conn, cid, "2026-01-05")
        self.assertEqual(seen_before, 1000)
        self.assertEqual(seen_in_snap, 1000)   # 快照内不被外部提交影响
        self.assertEqual(seen_after, 2222)
        other.close()


class TestFenwickLogGrowth(unittest.TestCase):
    def _build(self, n_rolls):
        tmp = tempfile.TemporaryDirectory()
        conn = db.connect(os.path.join(tmp.name, "b.db"))
        db.init_db(conn)
        # 跨月生成连续自然日，避免硬编码月份天数
        start = date(2026, 1, 5)
        days = [(start + timedelta(days=i)).isoformat()
                for i in range(2 + (n_rolls + 1) * 2)]
        db.replace_calendar(conn, days)
        switches = [(days[2 * k + 1], f"K{k+1}") for k in range(n_rolls)]
        db.setup_plan(conn, "K0", switches)
        return tmp, conn, days

    def test_matches_brute_force_and_aux_size(self):
        n = 20
        tmp, conn, days = self._build(n)
        try:
            cid = {r["code"]: r["contract_id"] for r in db.list_contracts(conn)}
            # 每个交易日给所有合约报价，价差取确定性序列
            for k in range(n + 1):
                for di, day in enumerate(days):
                    db.upsert_price(conn, cid[f"K{k}"], day, 100 + k * 7 + di)
            # 辅助表行数 ≤ 换月次数（稀疏 Fenwick，上界 n）
            bit_rows = conn.execute("SELECT COUNT(*) AS c FROM bit_sums").fetchone()["c"]
            self.assertLessEqual(bit_rows, n)

            rows = db.fetch_range(conn, days[0], days[-1], days[-1])
            # 暴力重算对照
            rolls = db.list_rolls(conn)
            for r in rows:
                day = r["date"]
                ord_ = db.active_contract_ord(conn, day)
                code = db.contract_by_ord(conn, ord_)["code"]
                raw = db.get_price(conn, cid[code], day)
                s = 0
                for roll in rolls:
                    if day < roll["switch_date"] <= days[-1]:
                        s += roll["delta"]
                self.assertEqual(r["continuous"], raw + s, msg=day)
        finally:
            conn.close()
            tmp.cleanup()

    def test_query_cost_is_log_steps(self):
        # Fenwick 前缀和的节点访问数 = 二进制置位数 ≤ log2(n)+1
        n = 64
        tmp, conn, days = self._build(n)
        try:
            # 填入报价使所有 delta 非空、Fenwick 节点被实际写入
            cid = {r["code"]: r["contract_id"] for r in db.list_contracts(conn)}
            for k in range(n + 1):
                for di, day in enumerate(days):
                    db.upsert_price(conn, cid[f"K{k}"], day, 100 + k * 7 + di)

            steps = 0
            orig_bit_sum = db._bit_sum

            def counting_bit_sum(c, pos):
                nonlocal steps
                steps += 1
                return orig_bit_sum(c, pos)

            db._bit_sum = counting_bit_sum
            try:
                db.range_delta_sum(conn, 1, n)  # 两个前缀和，各 O(log n)
            finally:
                db._bit_sum = orig_bit_sum
            self.assertLessEqual(steps, 2 * (n.bit_length()))
        finally:
            conn.close()
            tmp.cleanup()


class TestPlanValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "v.db"))
        db.init_db(self.conn)
        db.replace_calendar(self.conn, CAL)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_switch_must_be_trade_day(self):
        with self.assertRaises(db.AppError):
            db.setup_plan(self.conn, "C0", [("2026-01-10", "C1")])

    def test_switch_dates_strictly_increasing(self):
        with self.assertRaises(db.AppError):
            db.setup_plan(self.conn, "C0",
                          [("2026-01-08", "C1"), ("2026-01-08", "C2")])

    def test_rebuild_blocked_after_prices(self):
        db.setup_plan(self.conn, "C0", [("2026-01-08", "C1")])
        cid = db.list_contracts(self.conn)[0]["contract_id"]
        db.upsert_price(self.conn, cid, "2026-01-05", 100)
        with self.assertRaises(db.AppError):
            db.setup_plan(self.conn, "C0", [("2026-01-09", "X1")])


class TestCalendarChange(BaseCase):
    """改历联动：参考日重锚、删除保护、缺价提示、原子性、交错提交。"""

    def _rolls(self):
        return {r["ord"]: dict(r) for r in db.list_rolls(self.conn)}

    def _delta(self, ord_):
        return self._rolls()[ord_]["delta"]

    def test_insert_day_reanchors_pre_date_and_delta_and_continuous(self):
        # 在 01-09 与 01-12 之间补进一日 01-10（日历只登记 ISO 日期，不校验星期）
        new_cal = CAL + ["2026-01-10"]
        result = db.replace_calendar(self.conn, new_cal)
        # roll1(切换日 01-08) 不受影响；roll2(切换日 01-12) 的前一交易日 01-09 → 01-10
        changed = {a["ord"]: a for a in result["reanchored"]}
        self.assertEqual(sorted(changed), [2])
        self.assertEqual(changed[2]["old_pre_date"], "2026-01-09")
        self.assertEqual(changed[2]["new_pre_date"], "2026-01-10")
        rolls = self._rolls()
        self.assertEqual(rolls[1]["pre_date"], "2026-01-07")
        self.assertEqual(rolls[2]["pre_date"], "2026-01-10")
        # 新参考日尚无报价 → delta2 置空，而 delta1 保持 +10
        self.assertIsNone(rolls[2]["delta"])
        self.assertEqual(rolls[1]["delta"], 10)

        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        by_day = {r["date"]: r for r in rows}
        # 受 roll2 缺价差影响的日期连续价留空，并指出待补合约与新参考日
        early = by_day["2026-01-05"]
        self.assertIsNone(early["continuous"])
        self.assertEqual(early["status"], "missing_roll")
        self.assertIn("C1 → C2", early["message"])
        self.assertIn("(2026-01-10)", early["message"])
        # 切换日 01-12 当天起 roll2 已生效，不再需要该价差，仍正常出数
        self.assertEqual(by_day["2026-01-12"]["continuous"], 1080)
        self.assertEqual(by_day["2026-01-13"]["continuous"], 1090)

    def test_reanchor_picks_up_quotes_at_new_reference_day(self):
        # 补入 01-10，并在新参考日 01-10 上补齐两个合约的报价
        cid = {r["code"]: r["contract_id"] for r in db.list_contracts(self.conn)}
        db.replace_calendar(self.conn, CAL + ["2026-01-10"])
        p1 = db.upsert_price(self.conn, cid["C1"], "2026-01-10", 1060)
        p2 = db.upsert_price(self.conn, cid["C2"], "2026-01-10", 1095)
        self.assertEqual(p1["affected_roll_ord"], 2)
        self.assertEqual(p2["affected_roll_ord"], 2)
        # 新价差 = 1095 - 1060 = 35（旧参考日 01-09 上是 25）
        self.assertEqual(self._delta(2), 35)
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        first = rows[0]
        self.assertEqual(first["continuous"], 1000 + 10 + 35)  # 1045
        # Fenwick 与暴力口径一致
        rolls = db.list_rolls(self.conn)
        for r in rows:
            ord_ = db.active_contract_ord(self.conn, r["date"])
            s = sum(rr["delta"] for rr in rolls
                    if r["date"] < rr["switch_date"] <= "2026-01-13")
            self.assertEqual(r["continuous"],
                             db.get_price(self.conn,
                                          db.contract_by_ord(self.conn, ord_)["contract_id"],
                                          r["date"]) + s)

    def test_cannot_delete_trade_day_with_prices(self):
        with self.assertRaises(db.AppError) as ctx:
            db.replace_calendar(self.conn, [d for d in CAL if d != "2026-01-06"])
        self.assertIn("2026-01-06", str(ctx.exception))
        self.assertIn("已有报价", str(ctx.exception))
        # 原数据原样供数
        self.assertEqual(db.list_calendar(self.conn), CAL)
        self.assertEqual(self._delta(1), 10)
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(rows[0]["continuous"], 1035)

    def test_can_delete_day_without_prices(self):
        # 01-13 无任何报价依赖（不是任何 pre_date，且当天 C2 有报价 → 换一个无价日）
        # 先删掉 01-13 的报价，再删除该日应成功
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C2")
        with db.write_tx(self.conn):
            self.conn.execute("DELETE FROM prices WHERE contract_id=? AND date='2026-01-13'",
                              (cid,))
        new_cal = [d for d in CAL if d != "2026-01-13"]
        result = db.replace_calendar(self.conn, new_cal)
        self.assertEqual(result["reanchored"], [])
        self.assertEqual(db.list_calendar(self.conn), new_cal)

    def test_calendar_must_keep_switch_dates_and_their_prev_days(self):
        # 缺切换日
        with self.assertRaises(db.AppError) as ctx:
            db.replace_calendar(self.conn, [d for d in CAL if d != "2026-01-12"])
        self.assertIn("切换日", str(ctx.exception))
        # 删掉切换日 01-08 之前的全部交易日（01-05/06/07，先移除其报价以绕过删除保护），
        # 且不补入替代日 → 01-08 在新日历中没有前一交易日
        with db.write_tx(self.conn):
            self.conn.execute("DELETE FROM prices WHERE date < '2026-01-08'")
        with self.assertRaises(db.AppError) as ctx:
            db.replace_calendar(
                self.conn,
                [d for d in CAL if d >= "2026-01-08"],
            )
        self.assertIn("前一交易日", str(ctx.exception))
        # 失败整体回滚
        self.assertEqual(db.list_calendar(self.conn), CAL)

    def test_resaving_same_calendar_repairs_legacy_pre_date(self):
        # 模拟历史遗留：roll2 的参考日被错锚到 01-08（正确应为 01-09）
        with db.write_tx(self.conn):
            self.conn.execute("UPDATE rolls SET pre_date='2026-01-08' WHERE ord=2")
            db._recompute_roll_delta(self.conn, 2)
        stale = self._delta(2)
        # 01-08 上 C2 无报价 → 遗留价差为 NULL
        self.assertIsNone(stale)
        # 重存同一份合法日历即修复
        result = db.replace_calendar(self.conn, CAL)
        changed = {a["ord"]: a for a in result["reanchored"]}
        self.assertEqual(changed[2]["new_pre_date"], "2026-01-09")
        self.assertEqual(self._rolls()[2]["pre_date"], "2026-01-09")
        self.assertEqual(self._delta(2), 25)
        # 再次重存：没有参考日变化，不重算
        again = db.replace_calendar(self.conn, CAL)
        self.assertEqual(again["reanchored"], [])
        self.assertEqual(self._delta(2), 25)

    def test_only_changed_rolls_are_recomputed(self):
        calls = []
        orig = db._recompute_roll_delta

        def spy(conn, ord_):
            calls.append(ord_)
            return orig(conn, ord_)

        db._recompute_roll_delta = spy
        try:
            db.replace_calendar(self.conn, CAL + ["2026-01-10"])
        finally:
            db._recompute_roll_delta = orig
        # 仅 roll2 参考日变化 → 只重算 roll2，roll1 不触碰
        self.assertEqual(calls, [2])

    def test_failed_calendar_save_rolls_back_everything(self):
        # 先补入 01-10 并在该新参考日补齐报价，使 roll2 重锚后会产生非空价差、
        # 从而真正触发 Fenwick 写入
        cid = {r["code"]: r["contract_id"] for r in db.list_contracts(self.conn)}
        db.replace_calendar(self.conn, CAL + ["2026-01-10"])
        db.upsert_price(self.conn, cid["C1"], "2026-01-10", 1060)
        db.upsert_price(self.conn, cid["C2"], "2026-01-10", 1095)
        self.assertEqual(self._delta(2), 35)

        # 让重锚后的 Fenwick 更新在事务中途失败（模拟保存失败）
        orig = db._bit_add

        def boom(conn, n, pos, v):
            raise sqlite3.OperationalError("simulated disk failure")

        db._bit_add = boom
        try:
            with self.assertRaises(sqlite3.OperationalError):
                # 再补一日 01-11：roll2 参考日 01-10 → 01-11，旧价差 35 需先撤下
                db.replace_calendar(self.conn, CAL + ["2026-01-10", "2026-01-11"])
        finally:
            db._bit_add = orig
        # 日历、参考日、价差全部保持原状
        self.assertEqual(db.list_calendar(self.conn), sorted(CAL + ["2026-01-10"]))
        rolls = self._rolls()
        self.assertEqual(rolls[2]["pre_date"], "2026-01-10")
        self.assertEqual(rolls[2]["delta"], 35)
        # 取数仍按原数据
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(rows[0]["continuous"], 1000 + 10 + 35)

    def test_interleaved_price_after_calendar_uses_latest_calendar(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        # 改历先提交：01-06 被移出日历（先移除其报价以满足删除保护）
        with db.write_tx(self.conn):
            self.conn.execute("DELETE FROM prices WHERE date='2026-01-06'")
        db.replace_calendar(self.conn, [d for d in CAL if d != "2026-01-06"])
        # 后提交的报价按最新日历校验 → 拒绝
        with self.assertRaises(db.AppError):
            db.upsert_price(self.conn, cid, "2026-01-06", 999)

    def test_interleaved_calendar_after_price_sees_latest_prices(self):
        # 先在一个（当前无报价的）交易日补报价，随后改历试图删除它 → 必须被拦截
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C2")
        db.upsert_price(self.conn, cid, "2026-01-08", 1044)
        with self.assertRaises(db.AppError) as ctx:
            db.replace_calendar(self.conn, [d for d in CAL if d != "2026-01-08"])
        self.assertIn("2026-01-08", str(ctx.exception))
        self.assertEqual(db.list_calendar(self.conn), CAL)

    def test_concurrent_interleaving_uses_latest_committed_state(self):
        # 两个连接交错提交：连接 A 改历（删除一个交易日），连接 B 在 A 提交后
        # 才尝试写该日报价 → B 必须按最新日历被拒（校验在 B 的写事务内完成）
        other = db.connect(self.db_path)
        try:
            cid = next(r["contract_id"] for r in db.list_contracts(other)
                       if r["code"] == "C0")
            with db.write_tx(self.conn):
                self.conn.execute("DELETE FROM prices WHERE date='2026-01-06'")
            db.replace_calendar(self.conn, [d for d in CAL if d != "2026-01-06"])
            with self.assertRaises(db.AppError):
                db.upsert_price(other, cid, "2026-01-06", 999)

            # 反向交错：B 先在一个（当前无报价的）交易日补报价并提交，
            # A 随后删该日 → 删除保护按最新报价拦截 A
            cid_c2 = next(r["contract_id"] for r in db.list_contracts(other)
                          if r["code"] == "C2")
            db.upsert_price(other, cid_c2, "2026-01-08", 1044)
            with self.assertRaises(db.AppError):
                db.replace_calendar(self.conn,
                                    [d for d in CAL if d != "2026-01-08"])
        finally:
            other.close()

    def test_concurrent_write_tx_serializes_with_busy_timeout(self):
        # 两个 IMMEDIATE 事务真正并发时，后到者等待先到者提交（busy_timeout=30s），
        # 不会读到中间态；先到者提交的内容对后到者立即可见
        import threading
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        barrier = threading.Event()
        errors = []

        def writer():
            try:
                wconn = db.connect(self.db_path)
                try:
                    with db.write_tx(wconn):
                        barrier.set()  # 已取得写锁并进入事务
                        # 持锁期间插入一笔新日历日报价（先确保该日在日历中）
                        wconn.execute(
                            "INSERT OR IGNORE INTO calendar(date) VALUES ('2026-03-02')")
                        wconn.execute(
                            "INSERT INTO prices(contract_id, date, price) VALUES (?, '2026-03-02', 500)",
                            (cid,))
                        import time
                        time.sleep(0.5)
                finally:
                    wconn.close()
            except Exception as e:  # pragma: no cover - 仅用于暴露线程内错误
                errors.append(e)

        t = threading.Thread(target=writer)
        t.start()
        self.assertTrue(barrier.wait(timeout=5))
        # 主线程此时开启写事务必须等待，不会报 locked；提交后读到最新状态
        result = db.replace_calendar(self.conn, CAL + ["2026-03-02"])
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(db.get_price(self.conn, cid, "2026-03-02"), 500)
        self.assertIn("2026-03-02", db.list_calendar(self.conn))
        self.assertEqual(result["reanchored"], [])

    def test_calendar_replace_does_not_full_scan_prices(self):
        # 改历过程中对 prices 的访问只能是按被删日期的索引点查：
        # 用大量无关报价 + 仅删无价日的改历，统计该事务内各表的变化行数
        cid = {r["code"]: r["contract_id"] for r in db.list_contracts(self.conn)}
        start = date(2026, 2, 2)  # 避开 02-01，保留它作为最后要删的“无报价日”
        extra = [(start + timedelta(days=i)).isoformat() for i in range(60)]
        db.replace_calendar(self.conn, CAL + extra)
        for code, days in (("C0", extra[:20]), ("C1", extra[20:40]),
                           ("C2", extra[40:60])):
            for j, day in enumerate(days):
                db.upsert_price(self.conn, cid[code], day, 2000 + j)
        target = "2026-02-01"
        db.replace_calendar(self.conn, CAL + ["2026-02-01"] + extra)  # 先把 02-01 纳入
        before = self.conn.total_changes
        # 删除无报价的 02-01：prices 上仅 1 次 LIMIT 1 点查（0 行命中），
        # 不允许出现与报价总量同阶的写入/删除
        db.replace_calendar(self.conn, CAL + extra)
        changed = self.conn.total_changes - before
        # calendar 删除 m+1 行 + 插入 m 行；prices/rolls/bit_sums 均 0 变化
        m = len(CAL) + 1 + len(extra)
        self.assertEqual(changed, m + (m - 1))
        # 报价一笔未动
        n_prices = self.conn.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
        self.assertEqual(n_prices, len(PRICES) + 60)


class TestLegacyMigration(unittest.TestCase):
    """旧库（pre_date 无唯一约束）启动时自动迁移，数据与价差不丢。"""

    def test_migrate_legacy_rolls_table(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "legacy.db")
        conn = db.connect(path)
        db.init_db(conn)
        db.replace_calendar(conn, CAL)
        db.setup_plan(conn, "C0",
                      [("2026-01-08", "C1"), ("2026-01-12", "C2")])
        cid = {r["code"]: r["contract_id"] for r in db.list_contracts(conn)}
        for code, day, px in PRICES:
            db.upsert_price(conn, cid[code], day, px)
        before = [tuple(r) for r in conn.execute(
            "SELECT ord, switch_date, pre_date, new_contract_id, delta "
            "FROM rolls ORDER BY ord")]
        conn.close()

        # 直接改回旧结构：去掉 pre_date 的唯一约束
        legacy = sqlite3.connect(path)
        legacy.executescript(
            "ALTER TABLE rolls RENAME TO rolls_new_schema;"
            "CREATE TABLE rolls ("
            "ord INTEGER PRIMARY KEY, switch_date TEXT NOT NULL UNIQUE, "
            "pre_date TEXT NOT NULL, new_contract_id INTEGER NOT NULL, delta INTEGER);"
            "INSERT INTO rolls SELECT * FROM rolls_new_schema;"
            "DROP TABLE rolls_new_schema;"
            "CREATE INDEX idx_rolls_predate ON rolls(pre_date);"
        )
        legacy.commit()
        legacy.close()

        # 重新打开并执行启动迁移
        conn = db.connect(path)
        db.init_db(conn)
        db.init_db(conn)  # 幂等：第二次不应再重建
        after = [tuple(r) for r in conn.execute(
            "SELECT ord, switch_date, pre_date, new_contract_id, delta "
            "FROM rolls ORDER BY ord")]
        self.assertEqual(after, before)
        # 唯一性生效：重复 pre_date 写入被拒
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO rolls(ord, switch_date, pre_date, new_contract_id, delta) "
                "VALUES (99, '2099-01-01', '2026-01-07', 1, NULL)")
        # 旧普通索引已移除
        names = {r["name"] for r in
                 conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertNotIn("idx_rolls_predate", names)
        # 迁移后业务功能正常
        rows = db.fetch_range(conn, "2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(rows[0]["continuous"], 1035)
        conn.close()
        tmp.cleanup()


class TestCSVAndWeb(BaseCase):
    def test_csv_columns_and_blank_for_missing(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        self.conn.execute("DELETE FROM prices WHERE contract_id=? AND date='2026-01-06'",
                          (cid,))
        rows = self.fetch("2026-01-05", "2026-01-08", "2026-01-13")
        text = db.to_csv(rows)
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "交易日,在用合约,原价(分),连续价(分),状态,说明")
        blank = next(l for l in lines if l.startswith("2026-01-06"))
        self.assertIn(",,", blank)          # 连续价留空
        self.assertIn("缺原价", blank)

    def test_flask_routes_and_download(self):
        os.environ["CONT_DB_PATH"] = self.db_path  # 让 Web 请求落到本用例的临时库
        from app.app import create_app
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        for path in ("/", "/calendar", "/plan", "/prices", "/query"):
            self.assertEqual(client.get(path).status_code, 200)

        # Web 表单更正一笔报价
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C1")
        resp = client.post("/prices", data={
            "contract_id": cid, "date": "2026-01-07", "price": 1038})
        self.assertEqual(resp.status_code, 302)

        resp = client.get("/download?start=2026-01-05&end=2026-01-13&base=2026-01-13")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers["Content-Type"])
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        body = resp.data.decode("utf-8-sig")
        self.assertIn("2026-01-05", body)
        # 1038 → delta1 = 18，首日连续价 = 1000+18+25 = 1043
        self.assertIn("1043", body)


if __name__ == "__main__":
    unittest.main()
