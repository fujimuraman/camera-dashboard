"""楽ロジの発送依頼スプレッドシート（2140藤村薫）を You and Me の注文で更新する

楽ロジ側の列（W〜AD）・No（B列）・見出しは保護されていて書けない。ここで書くのは C〜V の決められた列だけ。
お届け先（郵便番号・住所・名前・電話）と画像 URL は SP-API で取れない（個人情報のロールが無い）ので、
定期タスクがセラーセントラルの注文詳細画面から読んで add に渡す。

使い方:
  python rakulogi_sheet.py candidates [--days 30]
      未出荷の新しい注文（シートの注文番号に無いもの）を1行1件の JSON で出す（注文日・期限・ASIN・商品名・数量）
  python rakulogi_sheet.py add <JSONファイル>
      candidates の各件に postal / address / name / phone / image_url を足した JSON の配列を、
      「yy年度」タブの記入済みの一番下に追記する。書いた行番号を出す
  python rakulogi_sheet.py note <行> <Amazon.comの追跡番号 か -> <商品代ドル か ->
      備考欄（V）の期限の下に商品代「45.99」、その下に追跡番号を書く。購入を確認したら商品代だけ（追跡番号は -）、発送後に追跡番号だけ足せる（見出しも $ も付けない。金額と分からないように＝かおる指示）
  python rakulogi_sheet.py tracking <行> <MyUSの追跡番号>
      楽ロジ倉庫までの追跡番号（R）を書く
  python rakulogi_sheet.py rows
      今年度タブで国内発送前（楽ロジ使用国内追跡番号 X が空）の行を JSON で出す。段階（stage）と、その段階で要る確認（check:
      amazon_com / myus_packages / myus_shipments をカンマ区切り・空＝確認不要）・期限・控え・知らせるべきこと付き。MyUS の到着一覧と追跡番号で自動照合する
  python rakulogi_sheet.py state <注文番号> <ASIN> key=value ...
      行ごとの控え（amazon_com_order / delivered / myus_package / myus_status など）を更新する
"""
import argparse
import io
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import gspread  # noqa: E402

from scripts.yandme_daily_check import fetch_order_detail, fetch_orders  # noqa: E402

JST = timezone(timedelta(hours=9))
SPREADSHEET_ID = "1_2GpK8yR7KEyhRAK17RX-TDY3_VAS3X6bHFRtJ54Un8"
SHOP_NAME = "輸入雑貨店\nYou and Me"
STATE = _PROJECT_ROOT / "logs" / "rakulogi_state.json"

# 遅れの知らせ（2026-11-02 かおる決定）
ALERT_BEFORE_MYUS = 10   # MyUS から出る前: 配達期限まで10日
ALERT_AFTER_MYUS = 5     # MyUS から出た後: 配達期限まで5日
ALERT_SHIP_NOTICE = 2    # 出荷通知の期限の2日前から
MYUS_ARRIVAL_DAYS = 3    # Amazon.com で配達済みになって3日たっても MyUS に入らなければ問い合わせ


def sheet(year=None):
    gc = gspread.service_account(filename=str(_PROJECT_ROOT / "secrets" / "gcp-sa.json"))
    year = year or datetime.now(JST).year
    return gc.open_by_key(SPREADSHEET_ID).worksheet(f"{year % 100}年度")


def mmdd(iso):
    if not iso:
        return ""
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST)
    return f"{d.month}/{d.day}"


def load_state():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save_state(st):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_candidates(days):
    values = sheet().get_all_values()
    known = {r[12] for r in values if len(r) > 12 and r[12]}
    orders = [o for o in fetch_orders(days) if o.get("OrderStatus") in ("Unshipped", "PartiallyShipped")
              and o["AmazonOrderId"] not in known]
    for o in sorted(orders, key=lambda o: o.get("PurchaseDate", "")):
        items, _ = fetch_order_detail(o["AmazonOrderId"])
        for it in items:
            print(json.dumps({"order_id": o["AmazonOrderId"], "purchase": mmdd(o.get("PurchaseDate")),
                              "asin": it.get("ASIN", ""), "title": it.get("Title", ""),
                              "qty": int(it.get("QuantityOrdered", 1)),
                              "deliver_by": mmdd(o.get("LatestDeliveryDate")), "ship_by": mmdd(o.get("LatestShipDate"))},
                             ensure_ascii=False))


def cmd_add(path):
    recs = json.loads(Path(path).read_text(encoding="utf-8"))
    ws = sheet()
    values = ws.get_all_values()
    known = {(r[12], r[13]) for r in values if len(r) > 13 and r[12]}
    rows = [i + 1 for i, r in enumerate(values) if i >= 2 and len(r) > 2 and r[2].strip()]
    row = rows[-1] if rows else 2
    for x in recs:
        if (x["order_id"], x["asin"]) in known:
            print(f"既にある: {x['order_id']} {x['asin']}")
            continue
        for k in ("postal", "address", "name", "phone", "image_url"):
            if not x.get(k):
                sys.exit(f"{x['order_id']}: {k} が空なので書かない")
        row += 1
        cells = {
            "C": x["purchase"], "D": SHOP_NAME, "F": x["postal"], "G": x["address"], "J": x["name"],
            "L": re.sub(r"[^0-9]", "", x["phone"]), "M": x["order_id"], "N": x["asin"], "O": x["title"],
            "Q": x["image_url"], "V": f"未購入({today_md()})\n{deadline_line(x['deliver_by'], x['ship_by'])}",
        }
        # P（=IMAGE()）と R〜U は触らない。電話番号の頭の0が消えないよう RAW で書く
        ws.batch_update([{"range": f"{c}{row}", "values": [[v]]} for c, v in cells.items()], value_input_option="RAW")
        print(json.dumps({"row": row, "order_id": x["order_id"], "asin": x["asin"]}, ensure_ascii=False))


PRICE_RE = re.compile(r"^\d+\.\d{2}$")
TRACK_RE = re.compile(r"^[A-Za-z0-9]{8,40}$")
DEADLINE_RE = re.compile(r"\d{1,2}/\d{1,2}")
STATUS_RE = re.compile(r"^(未購入|発送待ち|発送中|MyUS配送中)\((\d{1,2}/\d{1,2})\)$")


def parse_note(note):
    """備考を分ける。並びは「段階(M/D) → 配達期限 → 出荷通知 → 商品代 → 追跡番号」（2026-11-02 かおる指示）。
    どれにも当てはまらない行（かおるの手書きのメモ等）は others に残し、書き直す時も消さない"""
    p = {"status": "", "head": [], "deliver": "", "ship": "", "price": "", "track": "", "others": []}
    for x in note.split("\n"):
        if not x:
            continue
        if not p["status"] and STATUS_RE.match(x):
            p["status"] = x
        elif x.startswith(("配達", "出荷通知", "発送")) and DEADLINE_RE.search(x):
            # 1行の形「配達11/8 発送10/30」と、前の2行の形「配達期限:11/8」「出荷通知(10/30)」の両方を読む
            p["head"].append(x)
            m = re.search(r"配達(?:期限:)?(\d{1,2}/\d{1,2})", x)
            p["deliver"] = m[1] if m else p["deliver"]
            m = re.search(r"(?:出荷通知\(|発送)(\d{1,2}/\d{1,2})", x)
            p["ship"] = m[1] if m else p["ship"]
        elif not p["price"] and PRICE_RE.match(x):
            p["price"] = x
        elif not p["track"] and TRACK_RE.match(x):
            p["track"] = x
        else:
            p["others"].append(x)
    return p


def deadline_line(deliver, ship):
    return f"配達{deliver} 発送{ship}"


def build_note(p):
    """4行に収める（はみ出さないように＝かおる指示）: 段階(M/D) / 配達M/D 発送M/D / 商品代 / 追跡番号"""
    head = deadline_line(p["deliver"], p["ship"]) if p["deliver"] or p["ship"] else ""
    return "\n".join([x for x in [p["status"], head, p["price"], p["track"], *p["others"]] if x])


def split_note(note):
    p = parse_note(note)
    return p["head"], p["track"], p["price"]


def status_of(p, myus_tracking):
    """シートの中身から段階を決める（かおるが手で書いても合うように、控えではなくシートを見る）"""
    if myus_tracking:
        return "MyUS配送中"
    if p["track"]:
        return "発送中"
    if p["price"]:
        return "発送待ち"
    return "未購入"


def today_md():
    d = datetime.now(JST)
    return f"{d.month}/{d.day}"


def cmd_note(row, tracking, price):
    """商品代と追跡番号を備考に書く。購入を確認した時点で商品代だけ（tracking に - ）、発送後に追跡番号を足す"""
    ws = sheet()
    cur = ws.acell(f"V{row}").value or ""
    p = parse_note(cur)
    if tracking != "-":
        p["track"] = tracking
    if price not in ("", "-"):
        p["price"] = f"{float(price.lstrip('$')):.2f}"
    new = build_note(p)
    if new == cur:
        print("既に書いてある")
        return
    ws.update_acell(f"V{row}", new)
    print("OK（段階の行は rows が直す）")


def cmd_tracking(row, tracking):
    sheet().update(range_name=f"R{row}", values=[[tracking]], value_input_option="RAW")
    print("OK")


def parse_md(s, ref):
    m = re.match(r"(\d{1,2})/(\d{1,2})", s or "")
    if not m:
        return None
    d = datetime(ref.year, int(m[1]), int(m[2]), tzinfo=JST)
    return d.replace(year=ref.year + 1) if d < ref - timedelta(days=60) else d


def order_status(order_id):
    from sp_api.api import Orders
    from scripts.common.sp_api_client import get_client
    try:
        return get_client("yandme", Orders).get_order(order_id).payload.get("OrderStatus", "")
    except Exception:
        return ""


def stage_of(s, us_track, myus_out, us_price=""):
    """行の段階と、その段階でだけ要る確認（amazon_com / myus_shipments / なし）

    ordered      注文をシートに記入。Amazon.com の購入待ち → 購入履歴で注文を探し、見つかったら商品代を書く
    purchased    Amazon.com で購入済み・未発送（商品代は備考に書く）→ 購入履歴で発送（追跡番号）を見る
    us_shipping  Amazon.com から発送済み → 購入履歴で配達済みを見る＋MyUS の Your Packages に届いてないか見る
    us_delivered Amazon.com で配達済み・MyUS 未着 → MyUS の Your Packages を見る（到着メールが来ない時の保険。3日で問い合わせを促す）
    at_myus      MyUS に到着。転送は myus-forward とかおるの OK → 確認不要
    myus_shipped MyUS の Ship 済み・R列が空 → MyUS の発送一覧で追跡番号を見る
    to_rakulogi  R列記入済み。楽ロジの国内発送待ち → 確認不要（期限だけ見張る）
    """
    if myus_out:
        return "to_rakulogi", ""
    if s.get("myus_package"):
        return ("myus_shipped", "myus_shipments") if s.get("myus_status") == "shipped" else ("at_myus", "")
    if s.get("delivered"):
        return "us_delivered", "myus_packages"
    if us_track:
        return "us_shipping", "amazon_com,myus_packages"
    if s.get("amazon_com_order") or us_price:
        return "purchased", "amazon_com"
    return "ordered", "amazon_com"


def cmd_rows():
    ws = sheet()
    values = ws.get_all_values()
    status_updates = []
    st = load_state()
    q = Path(r"C:\claude\logs\myus\queue.json")
    myus = json.loads(q.read_text(encoding="utf-8")).get("packages", {}) if q.exists() else {}
    today = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    for i, r in enumerate(values):
        if i < 2 or len(r) < 24 or not r[2].strip() or r[23].strip():
            continue  # 注文日なし、または国内発送済み（X 楽ロジ使用国内追跡番号）
        order_date = parse_md(r[2], today - timedelta(days=300)) or today
        note = r[21]
        p = parse_note(note)
        deliver = parse_md(p["deliver"], order_date)
        ship = parse_md(p["ship"], order_date)
        _, us_track, us_price = split_note(note)
        key = f"{r[12]}|{r[13]}"
        s = st.setdefault(key, {})
        if us_track and not s.get("myus_package"):
            # MyUS 側の追跡番号は頭に英数字が付くことがあるので「含まれていれば一致」
            hit = next((p for p in myus.values() if us_track in (p.get("tracking") or "")), None)
            if hit:
                s["myus_package"] = hit["package_id"]
        if s.get("myus_package"):
            s["myus_status"] = myus.get(s["myus_package"], {}).get("status", s.get("myus_status", ""))
        myus_out = bool(r[17].strip())
        stage, check = stage_of(s, us_track, myus_out, us_price)
        # 備考の一番上の「段階(変わった日)」を直す。段階の行以外は触らない
        label = status_of(p, r[17].strip())
        if not p["status"].startswith(label + "("):
            p["status"] = f"{label}({today_md()})"
        if build_note(p) != note:
            status_updates.append({"range": f"V{i + 1}", "values": [[build_note(p)]]})
        alerts = []
        if deliver:
            left = (deliver - today).days
            if left <= (ALERT_AFTER_MYUS if myus_out else ALERT_BEFORE_MYUS):
                alerts.append(f"配達期限まで{left}日（{'MyUS発送済み' if myus_out else 'MyUSから未発送'}）→ お客様に遅延の連絡を")
        if ship and order_status(r[12]) not in ("Shipped", "Canceled"):
            left = (ship - today).days
            if left <= ALERT_SHIP_NOTICE:
                alerts.append(f"出荷通知の期限まで{left}日" if left >= 0 else f"出荷通知の期限を{-left}日過ぎた")
        if s.get("delivered") and not s.get("myus_package") and not s.get("myus_inquired"):
            waited = (today - datetime.fromisoformat(s["delivered"]).replace(tzinfo=JST)).days
            if waited >= MYUS_ARRIVAL_DAYS:
                alerts.append(f"Amazon.com で配達済みから{waited}日、MyUS に未着 → MyUS に問い合わせを")
        print(json.dumps({"row": i + 1, "order_id": r[12], "asin": r[13], "title": r[14][:60],
                          "status": p["status"], "stage": stage, "check": check,
                          "amazon_com_tracking": us_track, "price": us_price,
                          "myus_tracking": r[17], "deliver_by": deliver and f"{deliver.month}/{deliver.day}",
                          "ship_by": ship and f"{ship.month}/{ship.day}", "state": s, "alerts": alerts},
                         ensure_ascii=False))
    save_state(st)
    if status_updates:
        ws.batch_update(status_updates, value_input_option="RAW")
        print(f"# 段階を更新: {', '.join(u['range'] for u in status_updates)}", file=sys.stderr)


def cmd_state(order_id, asin, pairs):
    st = load_state()
    s = st.setdefault(f"{order_id}|{asin}", {})
    for p in pairs:
        k, _, v = p.partition("=")
        s[k] = v
    save_state(st)
    print(json.dumps(s, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("candidates").add_argument("--days", type=int, default=30)
    sub.add_parser("add").add_argument("path")
    p = sub.add_parser("note"); p.add_argument("row", type=int); p.add_argument("tracking"); p.add_argument("price")
    p = sub.add_parser("tracking"); p.add_argument("row", type=int); p.add_argument("tracking")
    sub.add_parser("rows")
    p = sub.add_parser("state"); p.add_argument("order_id"); p.add_argument("asin"); p.add_argument("pairs", nargs="*")
    a = ap.parse_args()
    if a.cmd == "candidates":
        cmd_candidates(a.days)
    elif a.cmd == "add":
        cmd_add(a.path)
    elif a.cmd == "note":
        cmd_note(a.row, a.tracking, a.price)
    elif a.cmd == "tracking":
        cmd_tracking(a.row, a.tracking)
    elif a.cmd == "rows":
        cmd_rows()
    else:
        cmd_state(a.order_id, a.asin, a.pairs)


if __name__ == "__main__":
    main()
