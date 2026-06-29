from datetime import datetime, timezone

from flask import session

from modules.db import _get_db
from modules.utils import (
    _find_static_asset,
    _normalize_static_path,
    _normalize_product_color,
    _parse_int,
    _static_path_exists,
)

SESSION_CART_KEY = "guest_cart_items"
SESSION_COUPON_KEY = "guest_cart_coupon"
SESSION_SHIPPING_KEY = "guest_cart_shipping_zone"
DEFAULT_SHIPPING_FEE = 0
_CART_TABLES_READY = False

SHIPPING_ZONES = {
    "city": {"label": "Nội thành (1-2 ngày)", "multiplier": 1.0},
    "province": {"label": "Liên tỉnh (2-4 ngày)", "multiplier": 1.2},
    "remote": {"label": "Vùng xa (4-6 ngày)", "multiplier": 1.45},
}


def _image_stems(slug):
    normalized = (slug or "").strip()
    if not normalized:
        return []
    return [normalized.replace("-", "_"), normalized]


def _image_stem_from_path(path):
    normalized = _normalize_static_path(path)
    if not normalized:
        return None
    return normalized.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _resolve_product_image(image_url, product_slug, character_slug=None):
    image = _normalize_static_path(image_url)
    if image and _static_path_exists(image):
        return image
    stems = []
    for stem in (
        [_image_stem_from_path(image)]
        + _image_stems(character_slug)
        + _image_stems(product_slug)
    ):
        if stem and stem not in stems:
            stems.append(stem)
    for stem in list(stems):
        if stem.startswith("ao_"):
            stripped = stem[3:]
            if stripped and stripped not in stems:
                stems.append(stripped)
    return (
        _find_static_asset(("images/shirt", "images/products", "uploads/products", "images"), stems)
        or "images/logo/logo.png"
    )


def ensure_cart_tables():
    global _CART_TABLES_READY
    if _CART_TABLES_READY:
        return
    conn = _get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cart_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            variant_id INTEGER NOT NULL,
            qty INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, variant_id),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(variant_id) REFERENCES product_variants(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cart_states (
            user_id INTEGER PRIMARY KEY,
            coupon_code TEXT,
            shipping_zone TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cart_items_user_id ON cart_items(user_id)"
    )
    conn.commit()
    conn.close()
    _CART_TABLES_READY = True


def get_shipping_options():
    return [{"value": key, "label": value["label"]} for key, value in SHIPPING_ZONES.items()]


def get_cart_item_count(user):
    user_id = user["id"] if user else None
    if user_id:
        ensure_cart_tables()
        conn = _get_db()
        row = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) AS total_qty FROM cart_items WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        conn.close()
        return row["total_qty"] if row else 0
    if SESSION_CART_KEY not in session:
        return 0
    guest_items = _get_guest_item_map()
    return sum(guest_items.values())


def get_cart_snapshot(user):
    ensure_cart_tables()
    user_id = user["id"] if user else None
    conn = _get_db()
    try:
        if user_id:
            item_map = _get_user_item_map(conn, user_id)
            state = _get_user_state(conn, user_id)
        else:
            item_map = _get_guest_item_map()
            state = _get_guest_state()

        items = _build_items(conn, item_map)
        subtotal = sum(item["line_total"] for item in items)

        coupon_code = state.get("coupon_code")
        coupon_result = None
        coupon_warning = None
        discount_amount = 0
        if coupon_code:
            coupon_result = _evaluate_coupon(conn, coupon_code, items, subtotal)
            if coupon_result.get("valid"):
                discount_amount = coupon_result["discount"]
                coupon_code = coupon_result["coupon"]["code"]
            else:
                coupon_warning = coupon_result.get("error")
                coupon_code = None
                if user_id:
                    _save_user_state(conn, user_id, None, state.get("shipping_zone"))
                    conn.commit()
                else:
                    session.pop(SESSION_COUPON_KEY, None)
                    session.modified = True

        shipping_zone = state.get("shipping_zone")
        if shipping_zone and shipping_zone not in SHIPPING_ZONES:
            shipping_zone = None
            if user_id:
                _save_user_state(conn, user_id, coupon_code, None)
                conn.commit()
            else:
                session.pop(SESSION_SHIPPING_KEY, None)
                session.modified = True

        shipping_result = _estimate_shipping(conn, subtotal - discount_amount, shipping_zone)
        total = max(subtotal - discount_amount + shipping_result["fee"], 0)

        return {
            "items": items,
            "item_count": sum(item["qty"] for item in items),
            "subtotal": subtotal,
            "discount_amount": discount_amount,
            "coupon_code": coupon_code,
            "coupon_warning": coupon_warning,
            "shipping_zone": shipping_zone,
            "shipping_fee": shipping_result["fee"],
            "shipping_label": shipping_result["label"],
            "shipping_estimated": shipping_result["estimated"],
            "total": total,
            "shipping_options": get_shipping_options(),
        }
    finally:
        conn.close()


def add_item_to_cart(user, variant_id, quantity=1):
    ensure_cart_tables()
    user_id = user["id"] if user else None
    try:
        variant_id = int(variant_id)
    except (TypeError, ValueError):
        return False, "Sản phẩm không hợp lệ."
    quantity = max(1, _parse_int(quantity, 1))

    conn = _get_db()
    try:
        variant = _fetch_active_variant(conn, variant_id)
        if variant is None:
            return False, "Phiên bản sản phẩm không tồn tại."
        stock_qty = max(_parse_int(variant["stock_qty"], 0), 0)
        if stock_qty <= 0:
            return False, "Size này tạm hết hàng."

        if user_id:
            item_map = _get_user_item_map(conn, user_id)
        else:
            item_map = _get_guest_item_map()

        current_qty = item_map.get(variant_id, 0)
        new_qty = min(current_qty + quantity, stock_qty)
        item_map[variant_id] = new_qty

        if user_id:
            _save_user_item_qty(conn, user_id, variant_id, new_qty)
            conn.commit()
        else:
            _save_guest_item_map(item_map)

        if new_qty < current_qty + quantity:
            return True, f"Đã thêm vào giỏ. Size này còn tối đa {stock_qty} sản phẩm."
        return True, "Đã thêm vào giỏ hàng."
    finally:
        conn.close()


def update_cart_item(user, variant_id, quantity):
    ensure_cart_tables()
    user_id = user["id"] if user else None
    try:
        variant_id = int(variant_id)
    except (TypeError, ValueError):
        return False, "Sản phẩm không hợp lệ."
    quantity = _parse_int(quantity, 1)
    if quantity <= 0:
        return remove_cart_item(user, variant_id)

    conn = _get_db()
    try:
        variant = _fetch_active_variant(conn, variant_id)
        if variant is None:
            return False, "Phiên bản sản phẩm không tồn tại."
        stock_qty = max(_parse_int(variant["stock_qty"], 0), 0)
        if stock_qty <= 0:
            return False, "Size này đã hết hàng."

        new_qty = min(quantity, stock_qty)
        if user_id:
            _save_user_item_qty(conn, user_id, variant_id, new_qty)
            conn.commit()
        else:
            item_map = _get_guest_item_map()
            item_map[variant_id] = new_qty
            _save_guest_item_map(item_map)
        if new_qty < quantity:
            return True, f"Số lượng được cập nhật tối đa {stock_qty}."
        return True, "Đã cập nhật số lượng."
    finally:
        conn.close()


def remove_cart_item(user, variant_id):
    ensure_cart_tables()
    user_id = user["id"] if user else None
    try:
        variant_id = int(variant_id)
    except (TypeError, ValueError):
        return False, "Sản phẩm không hợp lệ."

    conn = _get_db()
    try:
        if user_id:
            conn.execute(
                "DELETE FROM cart_items WHERE user_id = ? AND variant_id = ?",
                (user_id, variant_id),
            )
            conn.commit()
        else:
            item_map = _get_guest_item_map()
            item_map.pop(variant_id, None)
            _save_guest_item_map(item_map)
        return True, "Đã xoá sản phẩm khỏi giỏ."
    finally:
        conn.close()


def apply_coupon_to_cart(user, code):
    ensure_cart_tables()
    user_id = user["id"] if user else None
    code = (code or "").strip().upper()
    if not code:
        return False, "Vui lòng nhập mã giảm giá."

    conn = _get_db()
    try:
        if user_id:
            item_map = _get_user_item_map(conn, user_id)
            state = _get_user_state(conn, user_id)
        else:
            item_map = _get_guest_item_map()
            state = _get_guest_state()
        items = _build_items(conn, item_map)
        if not items:
            return False, "Giỏ hàng trống, không thể áp mã."
        subtotal = sum(item["line_total"] for item in items)
        result = _evaluate_coupon(conn, code, items, subtotal)
        if not result.get("valid"):
            return False, result.get("error") or "Mã giảm giá không hợp lệ."
        if user_id:
            _save_user_state(conn, user_id, result["coupon"]["code"], state.get("shipping_zone"))
            conn.commit()
        else:
            session[SESSION_COUPON_KEY] = result["coupon"]["code"]
            session.modified = True
        return True, "Đã áp dụng mã giảm giá."
    finally:
        conn.close()


def clear_cart_coupon(user):
    ensure_cart_tables()
    user_id = user["id"] if user else None
    if user_id:
        conn = _get_db()
        state = _get_user_state(conn, user_id)
        _save_user_state(conn, user_id, None, state.get("shipping_zone"))
        conn.commit()
        conn.close()
    else:
        session.pop(SESSION_COUPON_KEY, None)
        session.modified = True
    return True, "Đã gỡ mã giảm giá."


def set_shipping_zone(user, zone):
    ensure_cart_tables()
    user_id = user["id"] if user else None
    zone = (zone or "").strip()
    if zone and zone not in SHIPPING_ZONES:
        return False, "Tuỳ chọn vận chuyển không hợp lệ."

    if user_id:
        conn = _get_db()
        state = _get_user_state(conn, user_id)
        _save_user_state(conn, user_id, state.get("coupon_code"), zone or None)
        conn.commit()
        conn.close()
    else:
        if zone:
            session[SESSION_SHIPPING_KEY] = zone
        else:
            session.pop(SESSION_SHIPPING_KEY, None)
        session.modified = True
    return True, "Đã cập nhật ước tính vận chuyển."


def merge_guest_cart_into_user(user_id):
    ensure_cart_tables()
    if not user_id:
        return
    guest_items = _get_guest_item_map()
    guest_coupon = (session.get(SESSION_COUPON_KEY) or "").strip().upper() or None
    guest_shipping = session.get(SESSION_SHIPPING_KEY)

    if not guest_items and not guest_coupon and not guest_shipping:
        return

    conn = _get_db()
    try:
        user_items = _get_user_item_map(conn, user_id)
        for variant_id, qty in guest_items.items():
            variant = _fetch_active_variant(conn, variant_id)
            if variant is None:
                continue
            stock_qty = max(_parse_int(variant["stock_qty"], 0), 0)
            if stock_qty <= 0:
                continue
            current_qty = user_items.get(variant_id, 0)
            merged_qty = min(current_qty + qty, stock_qty)
            _save_user_item_qty(conn, user_id, variant_id, merged_qty)
            user_items[variant_id] = merged_qty

        state = _get_user_state(conn, user_id)
        coupon_code = state.get("coupon_code") or guest_coupon
        shipping_zone = state.get("shipping_zone") or (
            guest_shipping if guest_shipping in SHIPPING_ZONES else None
        )
        _save_user_state(conn, user_id, coupon_code, shipping_zone)
        conn.commit()
    finally:
        conn.close()

    session.pop(SESSION_CART_KEY, None)
    session.pop(SESSION_COUPON_KEY, None)
    session.pop(SESSION_SHIPPING_KEY, None)
    session.modified = True


def clear_cart(user):
    ensure_cart_tables()
    user_id = user["id"] if user else None
    if user_id:
        conn = _get_db()
        conn.execute("DELETE FROM cart_items WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM cart_states WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
    else:
        session.pop(SESSION_CART_KEY, None)
        session.pop(SESSION_COUPON_KEY, None)
        session.pop(SESSION_SHIPPING_KEY, None)
        session.modified = True


def _utcnow_iso():
    return datetime.utcnow().isoformat()


def _parse_iso_datetime(value):
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _get_guest_item_map():
    raw = session.get(SESSION_CART_KEY, {})
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for key, value in raw.items():
        variant_id = _parse_int(key, 0)
        qty = _parse_int(value, 0)
        if variant_id > 0 and qty > 0:
            cleaned[variant_id] = qty
    return cleaned


def _save_guest_item_map(item_map):
    clean = {str(variant_id): int(qty) for variant_id, qty in item_map.items() if qty > 0}
    session[SESSION_CART_KEY] = clean
    session.modified = True


def _get_guest_state():
    coupon_code = (session.get(SESSION_COUPON_KEY) or "").strip().upper() or None
    shipping_zone = session.get(SESSION_SHIPPING_KEY)
    if shipping_zone and shipping_zone not in SHIPPING_ZONES:
        shipping_zone = None
    return {"coupon_code": coupon_code, "shipping_zone": shipping_zone}


def _get_user_item_map(conn, user_id):
    rows = conn.execute(
        "SELECT variant_id, qty FROM cart_items WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    return {row["variant_id"]: max(_parse_int(row["qty"], 0), 0) for row in rows if row["qty"] > 0}


def _get_user_state(conn, user_id):
    row = conn.execute(
        "SELECT coupon_code, shipping_zone FROM cart_states WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return {"coupon_code": None, "shipping_zone": None}
    coupon_code = (row["coupon_code"] or "").strip().upper() or None
    shipping_zone = row["shipping_zone"]
    if shipping_zone and shipping_zone not in SHIPPING_ZONES:
        shipping_zone = None
    return {"coupon_code": coupon_code, "shipping_zone": shipping_zone}


def _save_user_item_qty(conn, user_id, variant_id, qty):
    now = _utcnow_iso()
    if qty <= 0:
        conn.execute(
            "DELETE FROM cart_items WHERE user_id = ? AND variant_id = ?",
            (user_id, variant_id),
        )
        return
    conn.execute(
        """
        INSERT INTO cart_items (user_id, variant_id, qty, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, variant_id)
        DO UPDATE SET qty = excluded.qty, updated_at = excluded.updated_at
        """,
        (user_id, variant_id, qty, now, now),
    )


def _save_user_state(conn, user_id, coupon_code, shipping_zone):
    now = _utcnow_iso()
    conn.execute(
        """
        INSERT INTO cart_states (user_id, coupon_code, shipping_zone, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            coupon_code = excluded.coupon_code,
            shipping_zone = excluded.shipping_zone,
            updated_at = excluded.updated_at
        """,
        (user_id, coupon_code, shipping_zone, now),
    )


def _fetch_active_variant(conn, variant_id):
    return conn.execute(
        """
        SELECT
            v.id,
            v.product_id,
            v.stock_qty,
            v.is_active,
            p.status AS product_status
        FROM product_variants v
        JOIN products p ON p.id = v.product_id
        WHERE v.id = ? AND v.is_active = 1 AND p.status = 'active'
        """,
        (variant_id,),
    ).fetchone()


def _build_items(conn, item_map):
    if not item_map:
        return []
    variant_rows = _load_variant_rows(conn, list(item_map.keys()))
    items = []
    for variant_id, requested_qty in item_map.items():
        variant = variant_rows.get(variant_id)
        if variant is None:
            continue
        if variant["product_status"] != "active" or not variant["is_active"]:
            continue
        stock_qty = max(_parse_int(variant["stock_qty"], 0), 0)
        if stock_qty <= 0:
            continue
        qty = min(max(_parse_int(requested_qty, 1), 1), stock_qty)
        regular_unit_price = (
            _parse_int(variant["price"], 0)
            if variant["price"] is not None
            else _parse_int(variant["base_price"], 0)
        )
        unit_price = regular_unit_price
        image = _resolve_product_image(
            variant["image"],
            variant["product_slug"],
            variant["character_slug"],
        )
        items.append(
            {
                "variant_id": variant_id,
                "product_id": variant["product_id"],
                "product_slug": variant["product_slug"],
                "product_name": variant["product_name"],
                "size": variant["size"] or "Free",
                "color": variant["color"] or "",
                "qty": qty,
                "stock_qty": stock_qty,
                "unit_price": unit_price,
                "regular_unit_price": regular_unit_price,
                "promotion_name": None,
                "has_promotion": False,
                "line_total": unit_price * qty,
                "image": image,
            }
        )
    items.sort(key=lambda item: item["product_name"].lower())
    return items


def _load_variant_rows(conn, variant_ids):
    if not variant_ids:
        return {}
    placeholders = ",".join("?" for _ in variant_ids)
    variant_rows = conn.execute(
        f"""
        SELECT
            v.id AS variant_id,
            v.product_id,
            v.size,
            v.color,
            v.price,
            v.stock_qty,
            v.is_active,
            p.slug AS product_slug,
            p.name AS product_name,
            p.base_price,
            p.status AS product_status,
            c.slug AS character_slug
        FROM product_variants v
        JOIN products p ON p.id = v.product_id
        LEFT JOIN characters c ON c.id = p.character_id
        WHERE v.id IN ({placeholders})
        """,
        tuple(variant_ids),
    ).fetchall()
    product_ids = sorted({row["product_id"] for row in variant_rows})
    image_map = _load_product_image_map(conn, product_ids)
    mapped = {}
    for row in variant_rows:
        item = dict(row)
        product_images = image_map.get(row["product_id"], {})
        color_images = product_images.get("colors", {})
        item["image"] = (
            color_images.get(_normalize_product_color(row["color"]))
            or product_images.get("primary")
        )
        mapped[row["variant_id"]] = item
    return mapped


def _load_product_image_map(conn, product_ids):
    if not product_ids:
        return {}
    placeholders = ",".join("?" for _ in product_ids)
    rows = conn.execute(
        f"""
        SELECT product_id, url, color, is_primary
        FROM product_images
        WHERE product_id IN ({placeholders})
        ORDER BY is_primary DESC, sort_order, id
        """,
        tuple(product_ids),
    ).fetchall()
    image_map = {}
    for row in rows:
        product_id = row["product_id"]
        if not row["url"]:
            continue
        product_images = image_map.setdefault(
            product_id,
            {"primary": None, "colors": {}},
        )
        color = _normalize_product_color(row["color"])
        if color and color not in product_images["colors"]:
            product_images["colors"][color] = row["url"]
        if row["is_primary"] and not product_images["primary"]:
            product_images["primary"] = row["url"]
        if not product_images["primary"]:
            product_images["primary"] = row["url"]
    return image_map


def _evaluate_coupon(conn, code, items, subtotal):
    normalized_code = (code or "").strip().upper()
    if not normalized_code:
        return {"valid": False, "error": "Mã giảm giá trống."}

    coupon = conn.execute(
        """
        SELECT *
        FROM coupons
        WHERE UPPER(code) = ? AND is_active = 1
        """,
        (normalized_code,),
    ).fetchone()
    if coupon is None:
        return {"valid": False, "error": "Mã giảm giá không tồn tại hoặc đã hết hạn."}

    now = datetime.utcnow()
    starts_at = _parse_iso_datetime(coupon["starts_at"])
    if starts_at and now < starts_at:
        return {"valid": False, "error": "Mã giảm giá chưa đến thời gian sử dụng."}
    ends_at = _parse_iso_datetime(coupon["ends_at"])
    if ends_at and now > ends_at:
        return {"valid": False, "error": "Mã giảm giá đã hết hạn."}
    usage_limit = coupon["usage_limit"]
    if usage_limit is not None and _parse_int(coupon["used_count"], 0) >= _parse_int(usage_limit, 0):
        return {"valid": False, "error": "Mã giảm giá đã hết lượt sử dụng."}
    min_order = _parse_int(coupon["min_order"], 0)
    if subtotal < min_order:
        return {"valid": False, "error": f"Đơn hàng tối thiểu {min_order:,} để dùng mã này."}

    applies_to = (coupon["applies_to"] or "all").strip().lower()
    if applies_to == "all":
        eligible_subtotal = subtotal
    else:
        eligible_product_ids = _eligible_product_ids(conn, coupon["id"], applies_to)
        eligible_subtotal = sum(
            item["line_total"] for item in items if item["product_id"] in eligible_product_ids
        )
    if eligible_subtotal <= 0:
        return {"valid": False, "error": "Mã giảm giá không áp dụng cho sản phẩm trong giỏ."}

    discount_type = (coupon["discount_type"] or "").strip().lower()
    value = _parse_int(coupon["value"], 0)
    if discount_type == "percent":
        discount = int(round(eligible_subtotal * value / 100))
    else:
        discount = value
    max_discount = coupon["max_discount"]
    if max_discount is not None:
        discount = min(discount, _parse_int(max_discount, 0))
    discount = max(0, min(discount, eligible_subtotal))
    if discount <= 0:
        return {"valid": False, "error": "Mã giảm giá không tạo ra ưu đãi hợp lệ."}
    return {"valid": True, "coupon": coupon, "discount": discount}


def _eligible_product_ids(conn, coupon_id, applies_to):
    if applies_to == "product":
        rows = conn.execute(
            "SELECT product_id FROM coupon_products WHERE coupon_id = ?",
            (coupon_id,),
        ).fetchall()
        return {row["product_id"] for row in rows}
    if applies_to == "category":
        rows = conn.execute(
            """
            SELECT DISTINCT product_id
            FROM product_categories
            WHERE category_id IN (
                SELECT category_id FROM coupon_categories WHERE coupon_id = ?
            )
            """,
            (coupon_id,),
        ).fetchall()
        return {row["product_id"] for row in rows}
    return set()


def _estimate_shipping(conn, discounted_subtotal, shipping_zone):
    return {
        "fee": DEFAULT_SHIPPING_FEE,
        "estimated": True,
        "label": "Free ship",
    }
