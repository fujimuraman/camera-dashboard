"""返品詳細を日本語で表示（文字化け回避のための簡易スクリプト）"""
import io
import sys
import csv
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sp_api.api import Reports  # noqa: E402
from scripts.common.sp_api_client import get_client  # noqa: E402

JST = timezone(timedelta(hours=9))
reports = get_client("fuji", Reports)

created_since = (
    (datetime.now(JST) - timedelta(days=30))
    .astimezone(timezone.utc)
    .replace(microsecond=0)
    .strftime("%Y-%m-%dT%H:%M:%SZ")
)

resp = reports.get_reports(
    reportTypes=["GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA"],
    processingStatuses=["DONE"],
    createdSince=created_since,
    pageSize=5,
)
latest = sorted(resp.payload["reports"], key=lambda r: r["createdTime"], reverse=True)[0]
doc = reports.get_report_document(latest["reportDocumentId"]).payload
url = doc["url"]

with urllib.request.urlopen(url, timeout=30) as r:
    raw_bytes = r.read()
    content_type = r.headers.get("content-type", "")
charset = "cp932"
if "charset=" in content_type.lower():
    charset = content_type.split("charset=")[-1].strip().lower()
    if charset == "windows-31j":
        charset = "cp932"
try:
    raw = raw_bytes.decode(charset, errors="replace")
except LookupError:
    raw = raw_bytes.decode("cp932", errors="replace")

# 返品理由の日本語マッピング
REASON_JP = {
    "DEFECTIVE": "不良品",
    "CUSTOMER_DAMAGED": "顧客破損",
    "DAMAGED_BY_CARRIER": "配送中破損",
    "SWITCHEROO": "すり替え",
    "UNWANTED_ITEM": "不要になった",
    "NOT_AS_DESCRIBED": "説明と異なる",
    "UNAUTHORIZED_PURCHASE": "不正購入",
    "OVERSHIPPED": "過剰配送",
    "WRONG_ITEM": "異なる商品",
    "QUALITY_UNACCEPTABLE": "品質不満",
    "EXPIRED_ITEM": "期限切れ",
    "MISSED_ESTIMATED_DELIVERY": "配達予定超過",
    "MISSING_PARTS": "部品欠損",
    "DID_NOT_LIKE_ITEM": "好みでない",
    "APPAREL_STYLE": "スタイル（アパレル）",
    "APPAREL_TOO_LARGE": "サイズが大きい",
    "APPAREL_TOO_SMALL": "サイズが小さい",
    "NO_REASON_GIVEN": "理由不明",
    "ORDERED_WRONG_ITEM": "誤注文",
}


def jp_reason(code: str) -> str:
    code = (code or "").strip()
    return REASON_JP.get(code, code)


out_path = Path(__file__).resolve().parent / "logs" / "returns_detail.txt"
with open(out_path, "w", encoding="utf-8") as f:
    reader = csv.DictReader(raw.splitlines(), delimiter="\t")
    returns = list(reader)
    f.write(f"# フジカメラ 返品レポート（{len(returns)}件）\n\n")

    # 表形式で出力
    headers = [
        "返品日",
        "注文番号",
        "SKU",
        "ASIN",
        "FNSKU",
        "商品",
        "返品理由",
        "購入者コメント",
    ]
    f.write("| " + " | ".join(headers) + " |\n")
    f.write("|" + "|".join(["---"] * len(headers)) + "|\n")


    for r in returns:
        date = (r.get("return-date", "") or "").split("T")[0]
        title = (r.get("product-name", "") or "").replace("|", "/").replace("\n", " ")
        comment = (r.get("customer-comments", "") or "(なし)").replace("|", "/").replace("\n", " ")
        row = [
            date,
            r.get("order-id", ""),
            r.get("sku", ""),
            r.get("asin", ""),
            r.get("fnsku", ""),
            title,
            jp_reason(r.get("reason", "")),
            comment,
        ]
        f.write("| " + " | ".join(row) + " |\n")

print(f"出力: {out_path}")
