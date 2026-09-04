#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
證券儀表板 — 自動更新腳本
讀 data.json → 抓 TWSE OpenAPI → 更新「auto」欄位 → 寫回 data.json + 產生 data.js

自動更新（每月可跑）：
  • 加權指數最新點數/漲跌      ← exchangeReport/FMTQIK
  • 本月累計成交值（兆元）      ← exchangeReport/FMTQIK
  • 各券商 最新季 收益/淨利/EPS ← opendata/t187ap06_X_bd + _L_bd（季更）
  • 全台開戶數（逐月累計/年度）   ← 證交所「投資人開戶人數變動統計表」月報 xlsx（次月首個營業日上架）
維持手動（腳本不動）：
  • 券商市佔率share、p24 去年基準、美好證券、元富(無API)
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
        start = datetime.date(datetime.date.today().year - 2, 1, 1).isoformat()  # 覆蓋兩個完整年度
        fu = ("https://api.finmindtrade.com/api/v4/data"
              f"?dataset=TaiwanStockPrice&data_id=TAIEX&start_date={start}")
        req = urllib.request.Request(fu, headers={"User-Agent": "dash-updater/1.0"})
        rows = json.loads(urllib.request.urlopen(req, timeout=60).read())["data"]
        bymon = collections.OrderedDict()
        daysmon = collections.OrderedDict()          # 各月「有成交金額」的交易日數 → 供日均計算
        for r in rows:
            ym = r["date"][:7]
            v = r.get("Trading_money") or 0
            bymon[ym] = bymon.get(ym, 0) + v
            if v > 0:
                daysmon[ym] = daysmon.get(ym, 0) + 1
        last = rows[-1]
        cur_ym = last["date"][:7]
        m = data["market"]
        # 完整月（剔除 0 與當前未完月）
        complete = [(ym, v) for ym, v in bymon.items() if v > 0 and ym != cur_ym]
        # 全部完整月（跨年度），附交易日數供日均計算
        m["turnoverSeries"] = [{"ym": ym, "v": round(v / 1e12, 2), "d": daysmon.get(ym, 0)} for ym, v in complete]
        m["turnoverMonthDays"] = daysmon.get(cur_ym, 0)
        m["turnoverMonth"] = round(bymon.get(cur_ym, 0) / 1e12, 2)   # 本月累計（含未完月）
        m["turnoverMonthLabel"] = cur_ym.replace("-", "/") + " 本月累計"
        # 近12個月(TTM) 與 前12個月（保留備用）
        last12, prev12 = complete[-12:], complete[-24:-12]
        m["turnoverTTM"] = round(sum(v for _, v in last12) / 1e12, 1)
        m["turnoverTTMPrev"] = round(sum(v for _, v in prev12) / 1e12, 1) if len(prev12) >= 12 else None
        m["turnoverTTMLabel"] = (last12[0][0].replace("-", "/") + "–" + last12[-1][0].replace("-", "/")) if last12 else ""
        # 今年累計 YTD（1月至今，含進行中月份）vs 去年同期
        cur_y, cur_mo = cur_ym[:4], int(cur_ym[5:7])
        ytd = sum(v for ym, v in complete if ym[:4] == cur_y) + bymon.get(cur_ym, 0)
        prev_y = str(int(cur_y) - 1)
        ytd_prev = sum(v for ym, v in bymon.items() if ym[:4] == prev_y and int(ym[5:7]) <= cur_mo)
        m["turnoverYTD"] = round(ytd / 1e12, 1)
        m["turnoverYTDPrev"] = round(ytd_prev / 1e12, 1) if ytd_prev > 0 else None
        m["turnoverYTDLabel"] = f"{cur_y}/01–{cur_mo:02d}"
        m["taiex"] = round(float(last["close"]), 2)
        m["taiexChg"] = round(float(last.get("spread") or 0), 2)
        m["taiexDate"] = last["date"]
        # 指數今年以來漲跌幅（去年最後收盤 vs 最新）
        cy = last["date"][:4]
        prev_rows = [r for r in rows if r["date"][:4] < cy]
        if prev_rows:
            m["taiexYearPct"] = round((last["close"] / prev_rows[-1]["close"] - 1) * 100, 1)
        # 今年每日指數序列（收盤/成交金額億）→ 指數 hero 圖
        m["taiexSeries"] = [
            {"d": r["date"][5:], "c": round(float(r["close"]), 2),
             "v": round((r.get("Trading_money") or 0) / 1e8)}
            for r in rows if r["date"][:4] == cy
        ]
        # 年度成交值（兆）：最近完整年 vs 前一年
        byyear = {}
        for r in rows:
            byyear[r["date"][:4]] = byyear.get(r["date"][:4], 0) + (r.get("Trading_money") or 0)
        y1, y2 = str(int(cy) - 1), str(int(cy) - 2)
        if y1 in byyear and y2 in byyear:
            m["turnoverYear"] = round(byyear[y1] / 1e12, 2)
            m["turnoverPrev"] = round(byyear[y2] / 1e12, 2)
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
        b["yRev"]     = round(ycur / 1e5, 1)
        b["yRevPrev"] = round(yprev / 1e5, 1)   # 去年同期累計（可為負，供「由負轉正」顯示）
        b["yRevYoY"]  = round((ycur / yprev - 1) * 100, 1) if yprev > 0 else None
        ym = str(r.get("資料年月", ""))
        b["mRevYM"] = f"{int(ym[:3]) + 1911}/{ym[3:5]}" if len(ym) >= 5 else ""
        mym = b["mRevYM"]
        mhit.append(b["name"])
    log.append(f"當月營收({mym}) 證券層級更新 {len(mhit)} 家：{'、'.join(mhit)}")

    # ---- 4. 全券商市佔率＋真實排名（經紀手續費收入口徑，t187ap21，月更）----
    try:
        fee = [r for r in fetch("/opendata/t187ap21") if r.get("會計科目名稱") == "經紀手續費收入"]
        total = sum(_f(r["本月金額"]) for r in fee)
        ranked = sorted(fee, key=lambda r: -_f(r["本月金額"]))
        pos = {}
        for i, r in enumerate(ranked, 1):
            pos[r["券商名稱"].replace(" ", "").replace("　", "")] = (i, _f(r["本月金額"]) / total * 100)
        NAME_MAP = {"元大證券": "元大", "凱基證券": "凱基", "富邦證券": "富邦", "永豐金證券": "永豐金",
                    "國泰證券": "國泰綜合", "群益金鼎證券": "群益金鼎", "統一證券": "統一",
                    "華南永昌證券": "華南永昌", "兆豐證券": "兆豐", "美好證券": "美好"}
        od = str(fee[0]["出表日期"])                       # 民國 1150625 → 資料月＝前一月
        yy, mo = int(od[:3]) + 1911, int(od[3:5]) - 1
        if mo == 0: yy, mo = yy - 1, 12
        share_ym = f"{yy}/{mo:02d}"
        shit = []
        for b in data["brokers"]:
            key = NAME_MAP.get(b["name"])
            if key and key in pos:
                b["rank"] = pos[key][0]
                b["share"] = round(pos[key][1], 2)
                b.pop("rankApprox", None)
                shit.append(b["name"])
        data["market"]["feeTotal"] = round(total / 1e8, 1)   # 全市場月手續費（億）
        data["market"]["shareYM"] = share_ym
        log.append(f"市佔/排名（手續費口徑 {share_ym}・全 {len(fee)} 家含外資）更新 {len(shit)} 家；"
                   f"全市場月手續費 {data['market']['feeTotal']} 億")
    except Exception as e:
        log.append(f"[警告] 市佔(t187ap21) 失敗：{e}")

    # ---- 5. 月自結損益（重大訊息 t187ap04；公告只在當日 API 出現，抓到即長存）----
    import re
    try:
        ann = []
        for ds in ("/opendata/t187ap04_L",
                   "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O"):
            try:
                ann += fetch(ds)
            except Exception as e:
                log.append(f"[警告] {ds} 失敗：{e}")
        by_code = {b.get("code"): b for b in data["brokers"] if b.get("code")}
        nhit = []
        for r in ann:
            code = str(r.get("公司代號") or r.get("SecuritiesCompanyCode") or "")
            subj = str(r.get("主旨") or "")
            body = str(r.get("說明") or "").replace("\n", " ")
            if code not in by_code or "自結" not in (subj + body):
                continue
            mm = re.search(r"(\d{3,4})\s*年\s*(\d{1,2})\s*月", subj + body)  # 支援西元與民國年
            nums = re.findall(r"稅後淨利[^\d\-]*(-?[\d,]+)\s*仟元[^\d\-]*(-?[\d,]+)\s*仟元", body)
            if not nums:
                continue
            b = by_code[code]
            single, cum = (float(x.replace(",", "")) for x in nums[0])
            b["sProfitM"] = round(single / 1e5, 2)   # 單月自結稅後（億）
            b["sProfit"] = round(cum / 1e5, 2)       # 今年累計自結稅後（億）
            if mm:
                yy4 = int(mm.group(1))
                if yy4 < 1900: yy4 += 1911          # 民國年轉西元
                b["sYM"] = f"{yy4}/{int(mm.group(2)):02d}"
            elif not b.get("sYM"):
                b["sYM"] = ""
            nhit.append(f'{b["name"]}(至{b["sYM"]})')
        if nhit:
            log.append(f"月自結稅後淨利更新：{'、'.join(nhit)}")
    except Exception as e:
        log.append(f"[警告] 自結損益解析失敗：{e}")

    # ---- 6. 全台開戶數：證交所「投資人開戶人數變動統計表」月報 xlsx ----
    #   URL 規律：/staticFiles/inspection/inspection/02/012/YYYYMM_C02012.xlsx，次月首個營業日上架。
    #   每份檔案含 2022/01 起完整逐月序列（期末累計開戶人數），只用標準庫解 xlsx，不需 openpyxl。
    try:
        import zipfile, io
        import xml.etree.ElementTree as ET
        NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

        def _acct_xlsx(ym):
            u = f"https://www.twse.com.tw/staticFiles/inspection/inspection/02/012/{ym}_C02012.xlsx"
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 dash-updater/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                raise

        ay, am = datetime.date.today().year, datetime.date.today().month
        blob = None
        for _ in range(4):                       # 從上月起往回最多找 4 個月
            am -= 1
            if am == 0:
                ay, am = ay - 1, 12
            blob = _acct_xlsx(f"{ay}{am:02d}")
            if blob:
                break

        if not blob:
            log.append("[警告] 開戶數：近 4 個月證交所月報皆不存在，維持既有值")
        else:
            z = zipfile.ZipFile(io.BytesIO(blob))
            ss = ["".join(t.text or "" for t in si.iter(NS + "t"))
                  for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(NS + "si")]
            monthly = {}                          # (西元年, 月) -> 期末累計開戶人數
            cur_y = None
            for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(NS + "row"):
                vals = []
                for c in row.iter(NS + "c"):
                    v = c.find(NS + "v")
                    if v is None:
                        continue
                    vals.append(ss[int(v.text)] if c.get("t") == "s" else v.text)
                if len(vals) < 5:
                    continue
                lab = vals[0].replace(" ", "").replace("　", "")
                my = re.fullmatch(r"(\d{3})年", lab)                 # 年度小計列 → 記住年份
                mm = re.fullmatch(r"(?:(\d{3})年)?(\d{1,2})月", lab)   # 月列（可能省略年）
                if my:
                    cur_y = int(my.group(1)) + 1911
                elif mm and vals[4].lstrip("-").isdigit():
                    if mm.group(1):
                        cur_y = int(mm.group(1)) + 1911
                    if cur_y:
                        monthly[(cur_y, int(mm.group(2)))] = int(vals[4])

            if not monthly:
                raise ValueError("xlsx 解析不到月資料")
            ly, lm = max(monthly)
            wan = lambda n: round(n / 1e4, 1)
            data["accountsMonthly"] = [{"ym": f"{ly}-{m:02d}", "v": wan(monthly[(ly, m)])}
                                       for (yy, m) in sorted(monthly) if yy == ly]
            acc = {a["y"]: a for a in data.get("accounts", [])}
            for (yy, m), n in monthly.items():
                if yy == ly or m != 12:
                    continue
                if yy not in acc:                 # 補缺的完整年度
                    acc[yy] = {"y": yy, "v": wan(n)}
                elif "p" in acc[yy]:              # 去年還掛著「進行中」→ 改成年底定值
                    acc[yy] = {"y": yy, "v": wan(n)}
            acc[ly] = {"y": ly, "v": wan(monthly[(ly, lm)]), "p": lm}
            data["accounts"] = [acc[k] for k in sorted(acc)]
            data["totalAccounts"] = acc[ly]["v"]
            log.append(f"開戶數（證交所月報 {ly}/{lm:02d}）：累計 {acc[ly]['v']} 萬人；"
                       f"{ly} 年逐月 {len(data['accountsMonthly'])} 點")
    except Exception as e:
        log.append(f"[警告] 開戶數更新失敗：{e}")

    # ---- 7. 超額儲蓄：主計總處「國民所得統計及國內經濟情勢展望」新聞稿 附表5〈儲蓄與投資〉xlsx ----
    #   每年 2/5/8/11 月發布。路徑：新聞稿列表 → 最新含「預測」之新聞稿 → 附件 t5.xlsx（標準庫解析）。
    #   失敗則維持 data.json 既有值。單位：百萬元 → 兆元；(f)=預測、(p)=初步、(r)=修正。
    try:
        import re as _re, zipfile as _zf, io as _io, html as _html
        import xml.etree.ElementTree as _ET
        _NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

        def _get(u, binary=False):
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 dash-updater/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    b = r.read()
            except urllib.error.URLError as e:
                # macOS python 對 stat.gov.tw / ws.dgbas.gov.tw 憑證鏈驗證失敗；公開資料改用未驗證 context 重試
                if "CERTIFICATE" not in str(e):
                    raise
                with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl._create_unverified_context()) as r:
                    b = r.read()
            return b if binary else b.decode("utf-8", "ignore")

        lst = _get("https://www.stat.gov.tw/News.aspx?n=2677&sms=10980")
        href = title = None
        for mm in _re.finditer(r'href="([^"]*News_Content\.aspx[^"]*)"[^>]*>(.*?)</a>', lst, _re.S):
            t = _html.unescape(_re.sub(r"<[^>]+>", "", mm.group(2))).strip()
            if "預測" in t:
                href, title = _html.unescape(mm.group(1)), t
                break
        if not href:
            raise RuntimeError("新聞稿列表找不到含「預測」之發布")
        if not href.startswith("http"):
            href = "https://www.stat.gov.tw/" + href.lstrip("/")
        page = _get(href)
        m5 = _re.search(r'href="([^"]+/t5\.xlsx)"', page)
        if not m5:
            raise RuntimeError("新聞稿頁面無 t5.xlsx 附件")
        t5url = _html.unescape(m5.group(1))
        md = _re.search(r'(1\d{2})[-/.](\d{2})[-/.](\d{2})', page)      # 發布日期（民國）
        rel_iso = (f"{int(md.group(1))+1911}-{md.group(2)}-{md.group(3)}" if md
                   else datetime.date.today().isoformat())

        z = _zf.ZipFile(_io.BytesIO(_get(t5url, binary=True)))
        ss = []
        if "xl/sharedStrings.xml" in z.namelist():
            ss = ["".join(t.text or "" for t in si.iter(_NS + "t"))
                  for si in _ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_NS + "si")]
        def _cell(c):
            t = c.get("t"); v = c.find(_NS + "v")
            if t == "s" and v is not None: return ss[int(v.text)]
            if t == "inlineStr": return "".join(x.text or "" for x in c.iter(_NS + "t"))
            return v.text if v is not None else ""
        def _num(x):
            try: return float(str(x).replace(",", ""))
            except Exception: return None
        def _tri(x):                    # 百萬元 → 兆元
            return None if x is None else round(x / 1e6, 2)

        annual, quarterly, cur_year = [], [], None
        sheet = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]
        for row in _ET.fromstring(z.read(sheet)).iter(_NS + "row"):
            cols = {_re.match(r"[A-Z]+", c.get("r")).group(0): _cell(c) for c in row.iter(_NS + "c")}
            a = (cols.get("A") or "").strip().replace(" ", "")
            my = _re.match(r"^(\d{3})年(?:\((\w)\))?$", a)
            mq = _re.match(r"^第(\d)季(?:\((\w)\))?$", a)
            if not (my or mq):
                continue
            sav, savR = _num(cols.get("B")), _num(cols.get("C"))
            inv, invR = _num(cols.get("D")), _num(cols.get("E"))
            ex,  exR  = _num(cols.get("F")), _num(cols.get("G"))
            if ex is None or exR is None:
                continue
            if my:
                cur_year = int(my.group(1)) + 1911
                annual.append({"y": cur_year, "f": (my.group(2) or "") == "f",
                               "sav": _tri(sav), "savRate": savR, "inv": _tri(inv), "invRate": invR,
                               "ex": _tri(ex), "exRate": exR})
            elif cur_year:
                quarterly.append({"q": f"{cur_year}Q{mq.group(1)}", "flag": mq.group(2) or "",
                                  "ex": _tri(ex), "exRate": exR, "savRate": savR, "invRate": invR})
        if len(annual) < 5:
            raise RuntimeError(f"表5 解析僅得 {len(annual)} 個年度，疑似格式變動")
        data["savings"] = {
            "source": "主計總處 國民所得統計及國內經濟情勢展望 新聞稿 附表5〈儲蓄與投資〉",
            "release": rel_iso, "releaseTitle": title[:80], "url": t5url,
            "annual": annual, "quarterly": quarterly,
        }
        fc = [a for a in annual if a["f"]]
        log.append(f"超額儲蓄（主計總處 {rel_iso} 發布）：年度 {len(annual)} 點、季 {len(quarterly)} 點；"
                   + "；".join(f"{a['y']}(f) {a['ex']} 兆／{a['exRate']}%" for a in fc))
    except Exception as e:
        log.append(f"[警告] 超額儲蓄更新失敗（維持既有值）：{e}")

    data["meta"]["updated"] = datetime.date.today().isoformat()
    data["meta"]["asLabel"] = (f"財報 {data['meta'].get('finPeriod','—')}・"
                               f"市佔 {data['market'].get('shareYM','—')}（手續費口徑）")

    # ---- 寫回 data.json + 產生 data.js ----
    with open(os.path.join(BASE, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    js = "/* 由 update.py 自動產生，請勿手改；要改手動值請改 data.json */\n"
    js += "window.DASH_DATA = " + json.dumps(data, ensure_ascii=False, indent=2) + ";\n"
    with open(os.path.join(BASE, "data.js"), "w", encoding="utf-8") as f:
        f.write(js)

    print("=== 更新完成 " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M") + " ===")
    for l in log:
        print(" -", l)

if __name__ == "__main__":
    main()
