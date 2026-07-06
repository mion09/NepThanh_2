import os
from datetime import datetime, timedelta

from modules.utils import _parse_int


SHOP_TIME_OFFSET_HOURS = int(
    (os.environ.get("SHOP_TIME_OFFSET_HOURS") or "7").strip() or "7"
)


def apply_promotion_to_price(price, promotion):
    regular_price = max(_parse_int(price, 0), 0)
    if not promotion or regular_price <= 0:
        return regular_price

    discount_type = (promotion["discount_type"] or "").strip().lower()
    value = max(_parse_int(promotion["value"], 0), 0)
    if value <= 0:
        return regular_price

    if discount_type == "percent":
        discount = int(round(regular_price * min(value, 100) / 100))
    else:
        discount = value
    return max(regular_price - min(discount, regular_price), 0)


def get_product_promotion_map(conn, product_ids):
    product_ids = sorted({int(product_id) for product_id in product_ids if product_id})
    if not product_ids:
        return {}

    promotions = _load_active_promotions(conn)
    if not promotions:
        return {}

    category_map = _load_product_category_map(conn, product_ids)
    promo_map = {}
    for product_id in product_ids:
        product_categories = category_map.get(product_id, set())
        matches = [
            promotion
            for promotion in promotions
            if _promotion_matches_product(promotion, product_categories)
        ]
        if matches:
            promo_map[product_id] = matches
    return promo_map


def best_promotion_for_price(price, promotions):
    if not promotions:
        return None
    regular_price = max(_parse_int(price, 0), 0)
    best = None
    best_price = regular_price
    for promotion in promotions:
        promoted_price = apply_promotion_to_price(regular_price, promotion)
        if promoted_price < best_price:
            best = promotion
            best_price = promoted_price
    return best


def _load_active_promotions(conn):
    now = _promotion_now().isoformat()
    return conn.execute(
        """
        SELECT *
        FROM promotions
        WHERE is_active = 1
          AND (starts_at IS NULL OR starts_at = '' OR starts_at <= ?)
          AND (ends_at IS NULL OR ends_at = '' OR ends_at >= ?)
        ORDER BY created_at DESC
        """,
        (now, now),
    ).fetchall()


def active_promotion_cache_signature(conn):
    promotions = _load_active_promotions(conn)
    return "|".join(
        ":".join(
            str(promotion[key] or "")
            for key in (
                "id",
                "name",
                "promo_type",
                "discount_type",
                "value",
                "category_id",
                "starts_at",
                "ends_at",
            )
        )
        for promotion in promotions
    ) or "none"


def _promotion_now():
    return datetime.utcnow() + timedelta(hours=SHOP_TIME_OFFSET_HOURS)


def _load_product_category_map(conn, product_ids):
    placeholders = ",".join("?" for _ in product_ids)
    rows = conn.execute(
        f"""
        SELECT product_id, category_id
        FROM product_categories
        WHERE product_id IN ({placeholders})
        """,
        tuple(product_ids),
    ).fetchall()
    category_map = {product_id: set() for product_id in product_ids}
    for row in rows:
        category_map.setdefault(row["product_id"], set()).add(row["category_id"])
    return category_map


def _promotion_matches_product(promotion, product_categories):
    promo_type = (promotion["promo_type"] or "").strip().lower()
    category_id = promotion["category_id"]
    if promo_type == "flash" or not category_id:
        return True
    return category_id in product_categories
