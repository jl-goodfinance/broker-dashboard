#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
證券儀表板 — 自動更新腳本
讀 data.json → 抓 TWSE OpenAPI → 更新「auto」欄位 → 寫回 data.json + 產生 data.js

自動更新（每月可跑）：
  • 加權指數最新點數/漲跌      ← exchangeReport/FMTQIK
  • 本月累計成交值（兆元）      ← exchangeReport/FMTQIK
  • 各券商 最新季 收益/淨利/EPS ← opendata/t187ap06_X_bd + _L_bd（季更）
維持手動（腳本不動）：
  • 全台開戶數、券商市佔率share、p24 去年基準、美好證券、元富(無API)
用法：  python3 update.py
"""
import json, os, sys, ssl, datetime, urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
API = "https://openapi.twse.com.tw/v1"
TIMEOUT = 40

def fetch(path):
    url = path if path.startswith("http") else f"{API}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "dash-updater/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        # macOS python 可能缺 TPEX 憑證鏈；公開資料改用未驗證 context 重試
        if "CERTIFICATE" in str(e):
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
                return json.loads(r.read().decode("utf-8"))
        raise

def to_yi(thousand):           # 千元 → 億元
    return round(float(thousand) / 1e5, 1)

def roc_to_date(roc):          # "1150601" → "2026/06/01"
    s = str(roc)
    return f"{int(s[:3])+1911}/{s[3:5]}/{s[5:7]}"

def main():
    with open(os.path.join(BASE, "data.json"), encoding="utf-8") as f:
        data = json.load(f)

    log = []

    # ---- 1. 成交值（月序列）+ 指數：FinMind 大盤 TAIEX ----
    try:
        import collections
        start = (datetime.date.today() - datetime.timedelta(days=800)).isoformat()
        fu = ("https://api.finmindtrade.com/api/v4/data"
              f"?dataset=TaiwanStockPrice&data_id=TAIEX&start_date={start}")
        req = urllib.request.Request(fu, headers={"User-Agent": "dash-updater/1.0"})
        rows = json.loads(urllib.request.urlopen(req, timeout=60).read())["data"]
        bymon = collections.OrderedDict()
        for r in rows:
            ym = r["date"][:7]
            bymon[ym] = bymon.get(ym, 0) + (r.get("Trading_money") or 0)
        last = rows[-1]
        cur_ym = last["date"][:7]
        m = data["market"]
        # 完整月（剔除 0 與當前未完月）
        complete = [(ym, v) for ym, v in bymon.items() if v > 0 and ym != cur_ym]
        m["turnoverSeries"] = [{"ym": ym, "v": round(v / 1e12, 2)} for ym, v in complete[-12:]]
        m["turnoverMonth"] = round(bymon.get(cur_ym, 0) / 1e12, 2)   # 本月累計（含未完月）
        m["turnoverMonthLabel"] = cur_ym.replace("-", "/") + " 本月累計"
        # 近12個月(TTM) 與 前12個月，給頭條 + YoY
        last12, prev12 = complete[-12:], complete[-24:-12]
        m["turnoverTTM"] = round(sum(v for _, v in last12) / 1e12, 1)
        m["turnoverTTMPrev"] = round(sum(v for _, v in prev12) / 1e12, 1) if len(prev12) >= 12 else None
        m["turnoverTTMLabel"] = (last12[0][0].replace("-", "/") + "–" + last12[-1][0].replace("-", "/")) if last12 else ""
        m["taiex"] = round(float(last["close"]), 2)
        m["taiexChg"] = round(float(last.get("spread") or 0), 2)
        m["taiexDate"] = last["date"]
        log.append(f"FinMind 月序列 {len(m['turnoverSeries'])} 點；近12月(TTM) {m['turnoverTTM']} 兆"
                   f"（前12月 {m['turnoverTTMPrev']}）；本月 {m['turnoverMonth']} 兆；"
                   f"指數 {m['taiex']} ({m['taiexChg']:+}) @ {m['taiexDate']}")
    except Exception as e:
        log.append(f"[警告] FinMind 成交值/指數失敗：{e}")

    # ---- 2. 各券商 最新季 收益/淨利/EPS ----
    fin = {}   # code -> record
    for ds in ("/opendata/t187ap06_X_bd", "/opendata/t187ap06_L_bd",
               "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_bd"):  # 上櫃（美好6021）
        try:
            for r in fetch(ds):
                fin[str(r["公司代號"])] = r
        except Exception as e:
            log.append(f"[警告] {ds} 抓取失敗：{e}")

    period = ""
    hit = []
    for b in data["brokers"]:
        code = b.get("code")
        if not code or code not in fin:
            continue
        r = fin[code]
        b["qRev"]    = to_yi(r["收益"])
        b["qProfit"] = to_yi(r["本期淨利（淨損）"])
        eps = r.get("基本每股盈餘（元）")
        b["qEps"]    = float(eps) if eps not in (None, "") else None
        period = f'{int(r["年度"])+1911}Q{r["季別"]}'
        b["qPeriod"] = period
        hit.append(b["name"])
        # 若已是全年(Q4)，順手把年淨利/營收與YoY基準更新
        if str(r["季別"]) == "4":
            new_annual_profit = to_yi(r["本期淨利（淨損）"])
            new_year = int(r["年度"]) + 1911
            if b.get("_annualYear") != new_year:
                b["p24"] = b["p25"]                 # 去年←原本的今年
                b["p25"] = new_annual_profit        # 今年←最新全年
                b["rev"] = to_yi(r["收益"])
                b["_annualYear"] = new_year
                log.append(f"  ↳ {b['name']} 全年數字滾動更新：p25={b['p25']} (YoY 基準 p24={b['p24']})")

    if period:
        data["meta"]["finPeriod"] = period
    log.append(f"券商財報期別 {period or '—'}；更新 {len(hit)} 家：{'、'.join(hit)}")
    miss = [b["name"] for b in data["brokers"] if b.get("code") and b["code"] not in fin]
    if miss:
        log.append(f"（無 API 對應，維持手動：{'、'.join(miss)}）")

    # ---- 3. 各券商「當月營收」(證券層級)：t187ap05_P 公發 + _L 上市 ----
    rev_m = {}
    for ds in ("/opendata/t187ap05_P", "/opendata/t187ap05_L",
               "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"):  # 上櫃（美好6021）
        try:
            for r in fetch(ds):
                rev_m[str(r["公司代號"])] = r
        except Exception as e:
            log.append(f"[警告] {ds} 抓取失敗：{e}")

    def _f(x):
        try: return float(x)
        except Exception: return 0.0

    mhit, mym = [], ""
    for b in data["brokers"]:
        code = b.get("code")
        if not code or code not in rev_m:
            continue
        r = rev_m[code]
        cur, prev, yago = _f(r.get("營業收入-當月營收")), _f(r.get("營業收入-上月營收")), _f(r.get("營業收入-去年當月營收"))
        b["mRev"]    = round(cur / 1e5, 1)                                 # 億
        b["mRevMoM"] = round((cur / prev - 1) * 100, 1) if prev else None
        b["mRevYoY"] = round((cur / yago - 1) * 100, 1) if yago else None
        # 今年累計營收（YTD）＋ 對去年同期累計 YoY（去年基期≤0 則不算）
        ycur, yprev = _f(r.get("累計營業收入-當月累計營收")), _f(r.get("累計營業收入-去年累計營收"))
        b["yRev"]    = round(ycur / 1e5, 1)
        b["yRevYoY"] = round((ycur / yprev - 1) * 100, 1) if yprev > 0 else None
        ym = str(r.get("資料年月", ""))
        b["mRevYM"] = f"{int(ym[:3]) + 1911}/{ym[3:5]}" if len(ym) >= 5 else ""
        mym = b["mRevYM"]
        mhit.append(b["name"])
    log.append(f"當月營收({mym}) 證券層級更新 {len(mhit)} 家：{'、'.join(mhit)}")

    data["meta"]["updated"] = datetime.date.today().isoformat()

    # ---- 寫回 data.json + 產生 data.js ----
    with open(os.path.join(BASE, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    js = "/* 由 update.py 自動產生，請勿手改；要改手動值請改 data.json */\n"
    js += "window.DASH_DATA = " + json.dumps(data, ensure_ascii=False, indent=2) + ";\n"
    with open(os.path.join(BASE, "data.js"), "w", encoding="utf-8") as f:
        f.write(js)

    # ---- 產生單檔版到 Dropbox 共享資料夾（同事打開即最新） ----
    try:
        html = open(os.path.join(BASE, "index.html"), encoding="utf-8").read()
        bundle = html.replace('<script src="data.js"></script>', "<script>\n" + js + "\n</script>")
        drop_dir = os.path.expanduser("~/Dropbox/證券Dashboard")
        os.makedirs(drop_dir, exist_ok=True)
        with open(os.path.join(drop_dir, "美好證券_同業儀表板.html"), "w", encoding="utf-8") as f:
            f.write(bundle)
        log.append(f"單檔已輸出至 Dropbox：{drop_dir}/美好證券_同業儀表板.html")
    except Exception as e:
        log.append(f"[警告] Dropbox 單檔輸出失敗：{e}")

    print("=== 更新完成 " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M") + " ===")
    for l in log:
        print(" -", l)

if __name__ == "__main__":
    main()
