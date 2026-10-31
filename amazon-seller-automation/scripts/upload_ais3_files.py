"""AIS3 の出品ファイル（〇〇shipping.tsv）と削除ファイル（〇〇deleting.tsv）を You and Me に反映する

使い方: python upload_ais3_files.py [--dry-run] <ファイル or フォルダ> [...]
  - shipping: リードタイム・在庫数・説明文を fix_ais3_listing_file のルールで直してから
    在庫数/リードタイム・価格（販売価格・最低/最高価格）・商品の状態の説明を PATCH
    （出品に無い SKU は LISTING_OFFER_ONLY で新規作成）
  - deleting: DELETE。ただし未出荷の注文がある SKU は消さない
  - JSON_LISTINGS_FEED で 1 フィード 10,000 件まで。結果は logs/ais3_upload_<日時>.log
"""
import io, json, os, sys, time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.sp_api_client import get_client
from fix_ais3_listing_file import ENC, ATTR_ROW, DATA_ROW, LEAD_MAP, QTY, NOTE_OLD, NOTE_NEW
from sp_api.api import Feeds, Orders, Reports

MP = "A1VC38T7YXB528"
SELLER = os.getenv("YANDME_SELLER_ID")
CHUNK = 10000
LOG_DIR = Path(__file__).resolve().parents[1] / "logs"


def read_rows(path):
    lines = path.read_bytes().decode(ENC).split("\r\n")
    attrs = lines[ATTR_ROW].split("\t")
    return [dict(zip(attrs, l.split("\t"))) for l in lines[DATA_ROW:] if l.strip()]


def lead(v):
    v = int(v) if str(v).strip() else 0
    return LEAD_MAP.get(v, 30 if v > 13 else max(v, 20))


def price_attr(r):
    p = "purchasable_offer[marketplace_id=A1VC38T7YXB528][audience=ALL]#1."
    sched = lambda k: [{"schedule": [{"value_with_tax": float(r[p + k + "#1.schedule#1.value_with_tax"])}]}]
    return [{"marketplace_id": MP, "currency": "JPY", "audience": "ALL",
             "our_price": sched("our_price"),
             "minimum_seller_allowed_price": sched("minimum_seller_allowed_price"),
             "maximum_seller_allowed_price": sched("maximum_seller_allowed_price")}]


def shipping_attrs(r):
    return {
        "fulfillment_availability": [{"fulfillment_channel_code": "DEFAULT", "quantity": int(QTY),
                                      "lead_time_to_ship_max_days": lead(r["fulfillment_availability#1.lead_time_to_ship_max_days"])}],
        "purchasable_offer": price_attr(r),
        "condition_note": [{"value": r["condition_note#1.value"].replace(NOTE_OLD, NOTE_NEW),
                            "language_tag": "ja_JP", "marketplace_id": MP}],
    }


def payload_of(res):
    return res.payload if hasattr(res, "payload") else res


def current_skus():
    """出品中の SKU（GET_MERCHANT_LISTINGS_ALL_DATA）"""
    rp = get_client("yandme", Reports)
    rid = rp.create_report(reportType="GET_MERCHANT_LISTINGS_ALL_DATA", marketplaceIds=[MP]).payload["reportId"]
    while True:
        p = rp.get_report(rid).payload
        if p["processingStatus"] in ("DONE", "FATAL", "CANCELLED"):
            break
        time.sleep(30)
    doc = payload_of(rp.get_report_document(p["reportDocumentId"], download=True))["document"]
    lines = doc.splitlines()
    i = lines[0].split("\t").index("出品者SKU")
    return {l.split("\t")[i] for l in lines[1:] if l.strip()}


def open_order_skus():
    o = get_client("yandme", Orders)
    after = (datetime.utcnow() - timedelta(days=60)).isoformat()
    orders = o.get_orders(CreatedAfter=after, OrderStatuses=["Unshipped", "PartiallyShipped"], MarketplaceIds=[MP]).payload.get("Orders", [])
    skus = set()
    for od in orders:
        skus |= {it["SellerSKU"] for it in o.get_order_items(od["AmazonOrderId"]).payload["OrderItems"]}
        time.sleep(2)
    return skus


def build_messages(files, listed, keep):
    msgs, stat = [], Counter()
    for f in files:
        rows = read_rows(f)
        if "deleting" in f.name:
            for r in rows:
                sku = r["contribution_sku#1.value"]
                if sku in keep:
                    stat["削除せず(未出荷の注文あり)"] += 1
                    continue
                msgs.append({"sku": sku, "operationType": "DELETE"})
                stat["削除"] += 1
        else:
            for r in rows:
                sku, at = r["contribution_sku#1.value"], shipping_attrs(r)
                if sku in listed:
                    msgs.append({"sku": sku, "operationType": "PATCH", "productType": "PRODUCT",
                                 "patches": [{"op": "replace", "path": f"/attributes/{k}", "value": v} for k, v in at.items()]})
                    stat["更新"] += 1
                else:
                    at["merchant_suggested_asin"] = [{"value": r["merchant_suggested_asin#1.value"], "marketplace_id": MP}]
                    at["condition_type"] = [{"value": "new_new", "marketplace_id": MP}]
                    msgs.append({"sku": sku, "operationType": "UPDATE", "productType": "PRODUCT",
                                 "requirements": "LISTING_OFFER_ONLY", "attributes": at})
                    stat["新規"] += 1
    return msgs, stat


def submit(msgs, log):
    f = get_client("yandme", Feeds)
    ids = []
    for n in range(0, len(msgs), CHUNK):
        ch = [dict(m, messageId=i + 1) for i, m in enumerate(msgs[n:n + CHUNK])]
        body = {"header": {"sellerId": SELLER, "version": "2.0", "issueLocale": "ja_JP"}, "messages": ch}
        for _ in range(10):
            try:
                _, resp = f.submit_feed("JSON_LISTINGS_FEED", io.BytesIO(json.dumps(body).encode()),
                                        content_type="application/json; charset=UTF-8")
                break
            except Exception as e:
                print("retry", str(e)[:120], flush=True)
                time.sleep(130)
        ids.append(resp.payload["feedId"])
        log.write(f"feed {ids[-1]} size={len(ch)}\n"); log.flush()
        print(f"送信 {len(ids)}: feed {ids[-1]}（{len(ch)}件）", flush=True)
    total = Counter()
    for fid in ids:
        while True:
            try:
                p = f.get_feed(fid).payload
                if p["processingStatus"] in ("DONE", "FATAL", "CANCELLED"):
                    break
            except Exception:
                pass
            time.sleep(60)
        res = None
        for _ in range(10):
            try:
                res = f.get_feed_document(p["resultFeedDocumentId"], download=True)
                break
            except Exception:
                time.sleep(60)
        doc = payload_of(res)
        doc = doc.get("document", doc) if isinstance(doc, dict) else doc
        doc = json.loads(doc) if isinstance(doc, str) else doc
        s = doc.get("summary", {}) if isinstance(doc, dict) else {}
        total.update({k: v for k, v in s.items() if isinstance(v, int)})
        for iss in (doc.get("issues", []) if isinstance(doc, dict) else []):
            if iss.get("severity") == "ERROR":
                log.write(json.dumps(iss, ensure_ascii=False) + "\n")
        print(f"結果 feed {fid}: {p['processingStatus']} {s}", flush=True)
    return total


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    files = []
    for a in (x for x in args if x != "--dry-run"):
        p = Path(a)
        files += sorted(p.glob("*shipping.tsv")) + sorted(p.glob("*deleting.tsv")) if p.is_dir() else [p]
    print("対象:", ", ".join(f.name for f in files))
    listed = current_skus()
    keep = open_order_skus()
    print(f"出品中 {len(listed)}件 / 未出荷の注文の SKU {len(keep)}件")
    msgs, stat = build_messages(files, listed, keep)
    print("内訳:", dict(stat))
    if dry:
        print(json.dumps(msgs[:1], ensure_ascii=False)[:1500])
        return
    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / f"ais3_upload_{datetime.now():%Y%m%d_%H%M%S}.log", "w", encoding="utf-8") as log:
        log.write("files: " + ", ".join(f.name for f in files) + f"\n{dict(stat)}\n")
        total = submit(msgs, log)
        log.write(f"TOTAL {dict(total)}\n")
    print("合計:", dict(total))


if __name__ == "__main__":
    main()
