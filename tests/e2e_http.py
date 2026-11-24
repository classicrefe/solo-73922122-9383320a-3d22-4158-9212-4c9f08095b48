"""
端到端试跑脚本：对运行中的 Flask 服务（默认 http://127.0.0.1:8000）
走一遍完整业务流，并打印关键校验结果。
用法：python tests/e2e_http.py
"""
import os
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
CAL = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
       "2026-01-09", "2026-01-12", "2026-01-13"]
# (code, date, price)
PRICES = [
    ("IF2601", "2026-01-05", 38000), ("IF2601", "2026-01-06", 38100),
    ("IF2601", "2026-01-07", 38200),
    ("IF2602", "2026-01-07", 38300), ("IF2602", "2026-01-08", 38400),
    ("IF2602", "2026-01-09", 38500),
    ("IF2603", "2026-01-09", 38750), ("IF2603", "2026-01-12", 38800),
    ("IF2603", "2026-01-13", 38900),
]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 不自动跟随，让调用方直接看到 302


opener = urllib.request.build_opener(NoRedirect)


def post(path, data):
    body = urllib.parse.urlencode(data, doseq=True).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, r.read().decode("utf-8-sig")


def main():
    checks = []

    # 1) 交易日历
    st, _ = post("/calendar", {"dates": "\n".join(CAL)})
    checks.append(("保存交易日历", st == 302))

    # 2) 换月计划：初始 IF2601，01-08 切 IF2602，01-12 切 IF2603
    st, _ = post("/plan", {
        "initial_code": "IF2601",
        "switch_date": ["2026-01-08", "2026-01-12"],
        "switch_code": ["IF2602", "IF2603"],
    })
    checks.append(("登记换月计划", st == 302))

    # 取合约 id（从 /prices 页面解析 option value）
    st, html = get("/prices")
    import re
    cid = {code: cid_ for cid_, code, _ in re.findall(
        r'value="(\d+)">([^（]+)（ord=(\d+)）', html)}
    assert len(cid) == 3, f"合约数异常: {cid}"

    # 3) 补录全部报价
    for code, day, px in PRICES:
        st, _ = post("/prices", {"contract_id": cid[code], "date": day, "price": px})
        assert st == 302, (code, day, st)
    checks.append(("补录 9 笔报价", True))

    # 4) 取数：基准日 01-13
    q = "/query?start=2026-01-05&end=2026-01-13&base=2026-01-13"
    st, html = get(q)
    checks.append(("首次取数 HTTP 200", st == 200))
    # 首日连续价 = 38000 +(38300-38200)+(38750-38500)= 38000+100+250=38350
    checks.append(("首日连续价 38350", "38350" in html))
    checks.append(("末日连续价 38900", "38900" in html))

    # 5) 下载 CSV
    st, csv_text = get("/download?start=2026-01-05&end=2026-01-13&base=2026-01-13")
    lines = csv_text.strip().splitlines()
    checks.append(("CSV 下载", st == 200 and len(lines) == 8 and lines[0].startswith("交易日")))
    first = lines[1].split(",")
    checks.append(("CSV 首行连续价 38350", first[3] == "38350"))

    # 6) 改价：IF2602 在 01-07（roll1 参考日）38300 → 38360
    st, _ = post("/prices", {"contract_id": cid["IF2602"],
                             "date": "2026-01-07", "price": 38360})
    checks.append(("更正报价", st == 302))
    # 新 delta1 = 160，首日连续价 = 38000+160+250 = 38410
    st, html = get(q)
    checks.append(("改价后联动连续价 38410", "38410" in html))
    st, csv_text = get("/download?start=2026-01-05&end=2026-01-13&base=2026-01-13")
    checks.append(("改价后 CSV 同步", "38410" in csv_text))

    # 7) 缺价场景：删除 IF2603 在 01-09 的报价（roll2 参考日新合约价）
    #    通过直接操作库模拟“尚未补录”，再取数验证留空与提示
    import sqlite3
    db_path = os.environ.get("CONT_DB_PATH", "data/continuous.db")
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM prices WHERE date='2026-01-09' "
                 "AND contract_id=(SELECT contract_id FROM contracts WHERE code='IF2603')")
    conn.execute("UPDATE rolls SET delta=NULL WHERE ord=2")
    conn.commit()
    conn.close()
    st, html = get(q)
    checks.append(("缺换月报价时留空", "待补报价：IF2603(2026-01-09)" in html))
    checks.append(("指出最早缺价换月 IF2602 → IF2603", "IF2602 → IF2603" in html))
    # 01-12、01-13 不受影响仍有连续价
    st, csv_text = get("/download?start=2026-01-12&end=2026-01-13&base=2026-01-13")
    rows = [l for l in csv_text.strip().splitlines()[1:]]
    checks.append(("换月后日期仍正常", rows[0].split(",")[3] == "38800"))

    # 8) 基准日早于结束日应被拒（页面渲染错误提示，HTTP 200 + flash）
    st, html = get("/query?start=2026-01-05&end=2026-01-13&base=2026-01-08")
    checks.append(("拒绝基准日早于结束日", st == 200 and "不能早于结束日" in html))

    print("\n===== 端到端试跑结果 =====")
    ok = True
    for name, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print("==========================")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
