"""
端到端试跑脚本：对运行中的 Flask 服务（默认 http://127.0.0.1:8000）
走一遍完整业务流，并打印关键校验结果。
用法：python tests/e2e_http.py
"""
import http.cookiejar
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
# 改历时补进的交易日（落在 roll2 切换日 01-12 的前一交易日位置）
EXTRA_DAY = "2026-01-10"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 不自动跟随，让调用方直接看到 302


opener = urllib.request.build_opener(NoRedirect)
# 带 cookie 存储的 opener：跟随 302 后能读到 flash 提示
cookie_jar = http.cookiejar.CookieJar()
follow_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(cookie_jar))


def post(path, data):
    body = urllib.parse.urlencode(data, doseq=True).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def post_and_follow(path, data):
    """提交表单并跟随重定向，返回最终页面 HTML（用于断言 flash 文案）。"""
    body = urllib.parse.urlencode(data, doseq=True).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    with follow_opener.open(req, timeout=10) as r:
        return r.status, r.read().decode("utf-8", "ignore")


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

    # 7) 缺价场景：删除 IF2603 在 01-09 的报价（roll2 参考日新合约价）。
    #    报价没有网页删除入口，这里直连库模拟“尚未补录”，并按同一口径把 Fenwick
    #    中的旧价差同步撤下（生产中正常流程由 upsert/改历事务维护，不会出现分叉）
    import sqlite3
    db_path = os.environ.get("CONT_DB_PATH", "data/continuous.db")
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM prices WHERE date='2026-01-09' "
                 "AND contract_id=(SELECT contract_id FROM contracts WHERE code='IF2603')")
    conn.execute("UPDATE rolls SET delta=NULL WHERE ord=2")
    # 撤下 Fenwick 中 ord=2 的旧值 250（bit(2) 承载 delta1+delta2）
    conn.execute("UPDATE bit_sums SET val = val - 250 WHERE pos = 2")
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

    # ===================================================================== #
    # 9) 网页改历：补进一日 01-10，roll2（切换日 01-12）参考日 01-09 → 01-10
    # ===================================================================== #
    # 先恢复步骤 7 删掉的 01-09 报价（由系统事务重建 delta2 与 Fenwick 节点）
    st, _ = post("/prices", {"contract_id": cid["IF2603"],
                             "date": "2026-01-09", "price": 38750})
    assert st == 302

    new_cal = CAL + [EXTRA_DAY]
    st, html = post_and_follow("/calendar", {"dates": "\n".join(new_cal)})
    checks.append(("改历保存成功并回显", st == 200 and "交易日历已保存" in html))
    checks.append(("flash 指出参考日重锚 01-09 → 01-10",
                   "重锚" in html and "2026-01-09 → 2026-01-10" in html))

    # 新参考日 01-10 尚无报价 → roll2 价差留空，连续价留空并指出待补合约与日期
    st, html = get(q)
    checks.append(("新参考日缺报价时连续价留空",
                   "待补报价" in html and "(2026-01-10)" in html))
    checks.append(("提示待补合约 IF2602、IF2603",
                   "IF2602(2026-01-10)" in html and "IF2603(2026-01-10)" in html))
    st, csv_text = get("/download?start=2026-01-05&end=2026-01-13&base=2026-01-13")
    csv_lines = csv_text.strip().splitlines()
    first_row = csv_lines[1].split(",")
    checks.append(("CSV 同步留空", first_row[3] == "" and "2026-01-10" in first_row[5]))

    # 在新参考日补齐两个合约报价 → 同事务联动重算 roll2 价差
    st, _ = post("/prices", {"contract_id": cid["IF2602"],
                             "date": EXTRA_DAY, "price": 38600})
    checks.append(("补录 IF2602 新参考日报价", st == 302))
    st, _ = post("/prices", {"contract_id": cid["IF2603"],
                             "date": EXTRA_DAY, "price": 38950})
    checks.append(("补录 IF2603 新参考日报价并联动价差", st == 302))
    # 新 delta2 = 38950 - 38600 = 350；delta1 = 160（38360-38200）
    # 首日连续价 = 38000 + 160 + 350 = 38510
    st, html = get(q)
    checks.append(("改历后取数使用新价差 38510", "38510" in html))
    st, csv_text = get("/download?start=2026-01-05&end=2026-01-13&base=2026-01-13")
    checks.append(("改历后 CSV 使用新价差", "38510" in csv_text))
    # 换月当日及之后不受 roll2 影响
    st, csv_text = get("/download?start=2026-01-12&end=2026-01-13&base=2026-01-13")
    tail = [l for l in csv_text.strip().splitlines()[1:]]
    checks.append(("换月后日期仍按原价出数", tail[0].split(",")[3] == "38800"))

    # 10) 删除保护：试图从日历删掉已有报价的 01-06，应被拒绝且原数据继续供数
    st, html = post_and_follow(
        "/calendar", {"dates": "\n".join(d for d in new_cal if d != "2026-01-06")})
    checks.append(("拒绝删除已有报价的交易日",
                   st == 200 and "已有报价登记，不能从日历删除" in html))
    st, html = get(q)
    checks.append(("拒绝后仍按原数据供数 38510", "38510" in html))

    # 11) 重存同一份合法日历：无参考日变化，数据不变
    st, html = post_and_follow("/calendar", {"dates": "\n".join(new_cal)})
    checks.append(("重存同一份日历成功",
                   st == 200 and "换月参考日均无变化" in html))
    st, html = get(q)
    checks.append(("重存后连续价不变 38510", "38510" in html))

    print("\n===== 端到端试跑结果 =====")
    ok = True
    for name, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print("==========================")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
