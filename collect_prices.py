# -*- coding: utf-8 -*-
"""
איסוף מחירים ומבצעים לחנויות באזור כפר חב"ד, ובניית קובץ catalog-kfar-chabad.json.

מקורות:
  * אושר עד (סניפים 020 לוד, 032 קריית אונו) - קובצי שקיפות רשמיים, publishedprices.co.il
    (שם משתמש ציבורי osherad, בלי סיסמה, לפי אתר משרד הכלכלה)
  * יש חסד בארות יצחק (סניף 300 של שופרסל) - קובצי שקיפות רשמיים, prices.shufersal.co.il
  * ליפסקר, שוליק - רק המוצרים שברשימת הקניות + דף המבצעים, בקצב איטי, פעם בשבוע

הרצה:  python collect_prices.py            (כל החנויות)
       python collect_prices.py --skip-web (בלי ליפסקר ושוליק)
"""
import gzip, io, json, re, sys, time, datetime, argparse, os
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "data")
UA = "KfarChabadPriceCompare/1.0 (personal price comparison)"
WEB_DELAY = 3  # שניות בין בקשות לליפסקר/שוליק

OSHER_AD_BRANCHES = {"020": "osherad_lod", "032": "osherad_kiryat_ono"}
SHUFERSAL_STORES = {"300": "yeshchesed"}
WEB_STORES = {
    "lipsker": ("https://www.lipsker.net", 1276, 1677),
    "sholik": ("https://www.sholik.net", 3, 49),
}


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- עזרים ל-XML
def decode_bytes(raw: bytes) -> str:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace")


def parse_xml(text: str) -> ET.Element:
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text.strip())
    return ET.fromstring(text)


def child(el, *names):
    """מחזיר טקסט של תת-אלמנט לפי שם, בלי תלות באותיות גדולות/קטנות."""
    low = {n.lower() for n in names}
    for c in el:
        if c.tag.lower() in low:
            return (c.text or "").strip()
    return ""


def to_float(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_price_file(text):
    """PriceFull -> {barcode: {...}}"""
    root = parse_xml(text)
    items = {}
    for it in root.iter():
        if it.tag.lower() != "item":
            continue
        code = child(it, "ItemCode")
        price = to_float(child(it, "ItemPrice"))
        if not code or not price:
            continue
        weighted = child(it, "bIsWeighted") == "1"
        qty = child(it, "Quantity")
        uom = child(it, "UnitOfMeasure", "UnitQty")
        base_uom = re.sub(r"^[\d.]+\s*", "", uom) if uom and uom != "לא ידוע" else ""
        q = qty.rstrip("0").rstrip(".") if qty and "." in qty else qty
        unit = 'ק"ג (שקיל)' if weighted else " ".join(x for x in [q, base_uom] if x)
        items[code] = {
            "barcode": code,
            "name": child(it, "ItemName", "ItemNm"),
            "unit": unit or None,
            "price": price,
            "internal": child(it, "ItemType") == "0",  # קוד פנימי של החנות
        }
    return items


def parse_promo_file(text, today):
    """PromoFull -> {barcode: deal}. מבצעי מועדון (ClubID != 0) מסומנים."""
    root = parse_xml(text)
    deals = {}
    for pr in root.iter():
        if pr.tag.lower() != "promotion":
            continue
        end = child(pr, "PromotionEndDateTime", "PromotionEndDate")[:10]
        start = child(pr, "PromotionStartDateTime", "PromotionStartDate")[:10]
        if end and end < today or start and start > today:
            continue
        desc = child(pr, "PromotionDescription")
        club = child(pr, "ClubID", "ClubId")
        if club in ("", "0", "00"):
            club_only = False
        else:
            club_only = True
        for pi in pr.iter():
            if pi.tag.lower() not in ("promotionitem", "item"):
                continue
            code = child(pi, "ItemCode")
            if not code:
                continue
            min_qty = to_float(child(pi, "MinQty")) or to_float(child(pr, "MinQty")) or 1
            dprice = to_float(child(pi, "DiscountedPrice")) or to_float(child(pr, "DiscountedPrice"))
            unit_price = None
            if dprice:
                unit_price = round(dprice / min_qty, 2) if min_qty and min_qty > 1 else dprice
            deal = {
                "price": unit_price,
                "terms": desc,
                "min_qty": int(min_qty) if min_qty and float(min_qty).is_integer() else min_qty,
                "until": end or None,
            }
            if club_only:
                deal["club_only"] = True
            old = deals.get(code)
            # אם יש כמה מבצעים - שומרים את הזול ביותר שלא דורש מועדון
            if old is None or (
                (old.get("club_only") and not club_only)
                or (bool(old.get("club_only")) == club_only and (unit_price or 9e9) < (old["price"] or 9e9))
            ):
                deals[code] = deal
    return deals


# ---------------------------------------------------------------- אושר עד
def fetch_osher_ad(today_compact):
    base = "https://url.publishedprices.co.il"
    s = requests.Session()
    s.headers["User-Agent"] = UA
    r = s.get(base + "/login", timeout=60)
    m = re.search(r'name="csrftoken"\s+content="([^"]+)"', r.text)
    token = m.group(1) if m else ""
    r = s.post(base + "/login/user", data={"r": "", "username": "osherad", "password": "", "Submit": "Sign in", "csrftoken": token}, timeout=60)
    m = re.search(r'name="csrftoken"\s+content="([^"]+)"', s.get(base + "/file", timeout=60).text)
    token = m.group(1) if m else token
    result = {}
    for branch, key in OSHER_AD_BRANCHES.items():
        files = {}
        for kind in ("PriceFull", "PromoFull"):
            r = s.post(base + "/file/json/dir", data={
                "sEcho": 1, "iColumns": 5, "iDisplayStart": 0, "iDisplayLength": 100,
                "sSearch": f"{kind}7290103152017-001-{branch}-", "csrftoken": token, "cd": "/",
            }, timeout=60)
            names = sorted(row["name"] for row in r.json().get("aaData", []) if row.get("name", "").startswith(kind))
            if not names:
                raise RuntimeError(f"Osher Ad: no {kind} file for branch {branch}")
            latest = names[-1]
            log(f"  אושר עד {branch}: {latest}")
            files[kind] = decode_bytes(s.get(base + "/file/d/" + latest, timeout=180).content)
        result[key] = files
    return result


# ---------------------------------------------------------------- שופרסל / יש חסד
def fetch_shufersal():
    base = "https://prices.shufersal.co.il"
    s = requests.Session()
    s.headers["User-Agent"] = UA
    result = {}
    for store, key in SHUFERSAL_STORES.items():
        files = {}
        for kind, cat in (("PriceFull", 2), ("PromoFull", 4)):
            html = s.get(f"{base}/FileObject/UpdateCategory?catID={cat}&storeId={store}", timeout=60).text
            links = re.findall(r'href="(https://[^"]+\.gz[^"]*)"', html)
            if not links:
                raise RuntimeError(f"Shufersal: no {kind} for store {store}")
            url = links[0].replace("&amp;", "&")
            log(f"  יש חסד {store}: {kind}")
            files[kind] = decode_bytes(s.get(url, timeout=180).content)
        result[key] = files
    return result


# ---------------------------------------------------------------- ליפסקר / שוליק
WEB_FILTER = json.dumps({"must": {"exists": ["family.id", "family.categoriesPaths.id", "branch.regularPrice"],
                                  "term": {"branch.isActive": True, "branch.isVisible": True}},
                         "mustNot": {"term": {"branch.regularPrice": 0}}}, separators=(",", ":"))


def web_barcode(p):
    url = ((p.get("image") or {}).get("url") or "")
    m = re.search(r"gs1-products/\d+/[^/]+/\d{7,14}-\d+/(\d{7,14})/", url)
    return m.group(1) if m else None


def web_deal(p):
    sp = ((p.get("branch") or {}).get("specials") or [])
    if not sp:
        return None
    s = sp[0]
    fl = s.get("firstLevel") or {}
    gift = fl.get("firstGift") or {}
    qty = fl.get("firstPurchaseTotal")
    price = None
    if gift.get("total") is not None and qty:
        price = round(gift["total"] / qty, 2)
    names = s.get("names") or {}
    terms = (names.get("1") or {}).get("promotionTag") or s.get("description")
    return {"price": price, "terms": terms, "min_qty": qty, "until": (s.get("endDate") or "")[:10] or None}


def fetch_web_store(key, base, R, B, terms, per_term):
    s = requests.Session()
    s.headers["User-Agent"] = UA
    items = []
    for t in terms:
        url = f"{base}/v2/retailers/{R}/branches/{B}/products"
        params = {"appId": 4, "filters": WEB_FILTER, "from": 0, "isSearch": "true", "languageId": 1, "query": t, "size": per_term}
        try:
            r = s.get(url, params=params, timeout=60)
            r.raise_for_status()
            prods = r.json().get("products") or []
        except Exception as e:
            log(f"  {key}: שגיאה בחיפוש '{t}' ({e}) - עוצר את החנות הזו")
            break
        for p in prods:
            br = p.get("branch") or {}
            if not br.get("regularPrice"):
                continue
            uom = ((p.get("unitOfMeasure") or {}).get("names") or {}).get("1")
            w = p.get("weight")
            unit = 'ק"ג (שקיל)' if p.get("isWeighable") else (" ".join(str(x) for x in [w, uom] if x) or None)
            items.append({"term": t, "barcode": web_barcode(p), "name": p.get("localName") or ((p.get("names") or {}).get("1") or {}).get("short"),
                          "unit": unit, "price": br["regularPrice"], "deal": web_deal(p)})
        time.sleep(WEB_DELAY)
    log(f"  {key}: {len(items)} מוצרים")
    return items


# ---------------------------------------------------------------- התאמה לפי שם
UNIT_WORDS = {"גרם": "g", "גר": "g", "ג": "g", "קג": "kg", "ליטר": "l", "ל": "l", "מל": "ml", "יח": "u", "יחידות": "u"}


def norm(name):
    s = (name or "").replace("`", "'").replace("״", '"').replace("׳", "'")
    s = re.sub(r"['\"*.,\-/()%+]", " ", s)
    s = re.sub(r"(\d)([^\d\s.])", r"\1 \2", s)
    s = re.sub(r"([^\d\s.])(\d)", r"\1 \2", s)
    toks = []
    for t in s.split():
        if len(t) > 3 and t[0] in "בהול" and not t.isdigit():
            t = t[1:]
        toks.append(UNIT_WORDS.get(t, t))
    return [t for t in toks if t]


def numbers(toks):
    return {t for t in toks if re.fullmatch(r"\d+(\.\d+)?", t)}


def similarity(a, b):
    ta, tb = norm(a), norm(b)
    if not ta or not tb:
        return 0.0
    na, nb = numbers(ta), numbers(tb)
    if na and nb and not (na & nb):  # מספרים שונים (5% מול 9%, 250 מול 500) => לא אותו מוצר
        return 0.0
    sa, sb = set(ta), set(tb)
    jac = len(sa & sb) / len(sa | sb)
    seq = SequenceMatcher(None, " ".join(sorted(sa)), " ".join(sorted(sb))).ratio()
    return round(0.6 * jac + 0.4 * seq, 3)


MATCH_OK = 0.72      # התאמה בטוחה
MATCH_REVIEW = 0.55  # התאמה שצריך לבדוק


def term_hit(term, name):
    toks = norm(name)
    tt = norm(term)
    return all(any(x.startswith(y[:max(3, len(y) - 2)]) for x in toks) for y in tt)


# ---------------------------------------------------------------- בנייה
def build(skip_web=False):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=2)))
    today = now.strftime("%Y-%m-%d")
    terms = [l.strip() for l in open(os.path.join(HERE, "shopping_list.txt"), encoding="utf-8") if l.strip() and not l.startswith("#")]
    store_rows = {}   # store -> list of {name, unit, price, deal, barcode, term}
    status = {}

    def official(fetcher, label):
        try:
            data = fetcher()
        except Exception as e:
            log(f"!! {label}: {e}")
            return
        for key, files in data.items():
            prices = parse_price_file(files["PriceFull"])
            deals = parse_promo_file(files["PromoFull"], today)
            rows = []
            for code, it in prices.items():
                hit = next((t for t in terms if term_hit(t, it["name"])), None)
                if not hit:
                    continue
                rows.append({**it, "term": hit, "deal": deals.get(code)})
            store_rows[key] = rows
            status[key] = {"updated": now.isoformat(timespec="minutes"), "items_in_file": len(prices), "deals_in_file": len(deals), "matched_list": len(rows)}

    log("אושר עד...")
    official(lambda: fetch_osher_ad(now.strftime("%Y%m%d")), "Osher Ad")
    log("יש חסד...")
    official(fetch_shufersal, "Shufersal")

    if not skip_web:
        for key, (base, R, B) in WEB_STORES.items():
            log(f"{key}...")
            rows = fetch_web_store(key, base, R, B, terms, per_term=10)
            if rows:
                store_rows[key] = rows
                status[key] = {"updated": now.isoformat(timespec="minutes"), "matched_list": len(rows)}

    # שמירת נתונים קודמים לחנויות שלא עודכנו הפעם (למשל ליפסקר/שוליק בין ריצות שבועיות)
    prev_path = os.path.join(OUT_DIR, "store_rows.json")
    if os.path.exists(prev_path):
        prev = json.load(open(prev_path, encoding="utf-8"))
        for k, v in prev.get("rows", {}).items():
            if k not in store_rows:
                store_rows[k] = v
                status[k] = prev.get("status", {}).get(k, {})
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump({"rows": store_rows, "status": status}, open(prev_path, "w", encoding="utf-8"), ensure_ascii=False)

    # מיזוג: אותו ברקוד = אותו מוצר; אחרת לפי דמיון שם
    products = []
    for store, rows in store_rows.items():
        for r in rows:
            entry = {"price": r["price"], "deal": r.get("deal")}
            best, best_score, how = None, 0.0, None
            for p in products:
                if store in p["prices"] or p["category"] != r["term"]:
                    continue
                if r.get("barcode") and r["barcode"] in p["_barcodes"]:
                    best, best_score, how = p, 1.0, "barcode"
                    break
                sc = similarity(r["name"], p["name"])
                if sc > best_score:
                    best, best_score, how = p, sc, "name"
            if best and best_score >= MATCH_REVIEW:
                if how == "name":
                    entry["match"] = {"by": "name", "score": best_score, "name_in_store": r["name"]}
                    if best_score < MATCH_OK:
                        entry["match"]["review"] = True
                best["prices"][store] = entry
                if r.get("barcode"):
                    best["_barcodes"].add(r["barcode"])
                    best["barcode"] = best["barcode"] or r["barcode"]
            else:
                products.append({"barcode": r.get("barcode"), "name": r["name"], "unit": r.get("unit"), "category": r["term"],
                                 "prices": {store: entry}, "_barcodes": {r["barcode"]} if r.get("barcode") else set()})
    for p in products:
        p.pop("_barcodes")
    products.sort(key=lambda p: (-len(p["prices"]), p["category"], p["name"]))

    out = {"collected": today, "updated_at": now.isoformat(timespec="minutes"), "stores": status, "products": products}
    path = os.path.join(OUT_DIR, "catalog-kfar-chabad.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    multi = sum(1 for p in products if len(p["prices"]) > 1)
    log(f"נשמר {path}: {len(products)} מוצרים, {multi} מופיעים ביותר מחנות אחת")
    for k, v in status.items():
        log(f"  {k}: {v}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-web", action="store_true", help="בלי ליפסקר ושוליק (לריצות היומיות)")
    build(**vars(ap.parse_args()))
