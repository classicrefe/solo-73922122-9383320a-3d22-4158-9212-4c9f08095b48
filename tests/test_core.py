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
