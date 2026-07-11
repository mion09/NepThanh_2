import json
import hashlib
import threading
from datetime import datetime

from modules.bank_transfer import (
    bank_transfer_content,
    bank_transfer_enabled,
    build_bank_transfer_instructions,
)
from modules.cart import clear_cart, get_cart_snapshot
from modules.customer_account import ensure_account_tables, get_address_by_id, get_default_address
from modules.db import INTEGRITY_ERRORS, _get_db
from modules.notifications import (
    send_new_order_alert_to_owner,
    send_order_confirmation_email,
    send_order_status_email,
)
from modules.payments_vnpay import (
    build_vnpay_payment_url,
    is_vnpay_success,
    parse_vnpay_amount,
    verify_vnpay_response,
    vnpay_enabled,
)
from modules.shipping import select_shipping_quote
from modules.utils import _build_order_number, _parse_int

_CHECKOUT_TABLES_READY = False
_ORDER_CANCEL_STATUSES = {"cancelled", "refunded", "returned"}
_STOCK_RESTORE_REASONS = {
    "payment_failed",
    "admin_cancelled",
    "admin_refunded",
    "admin_returned",
}
_STOCK_REDEDUCT_REASONS = {"admin_reactivated"}
_STOCK_TRACKING_REASONS = _STOCK_RESTORE_REASONS | _STOCK_REDEDUCT_REASONS


def ensure_checkout_tables():
    global _CHECKOUT_TABLES_READY
    if _CHECKOUT_TABLES_READY:
        return
    ensure_account_tables()
    conn = _get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            gateway TEXT NOT NULL,
            status TEXT NOT NULL,
            source TEXT,
            txn_ref TEXT,
            transaction_no TEXT,
            amount INTEGER DEFAULT 0,
            payload_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS checkout_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_key TEXT NOT NULL UNIQUE,
            user_id INTEGER,
            cart_fingerprint TEXT,
            status TEXT NOT NULL DEFAULT 'processing',
            order_id INTEGER,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_payment_transactions_order_id ON payment_transactions(order_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_checkout_submissions_order_id ON checkout_submissions(order_id)"
    )
    conn.commit()
    conn.close()
    _CHECKOUT_TABLES_READY = True


def get_checkout_prefill(user):
    ensure_account_tables()
    if not user:
        return {
            "full_name": "",
            "email": "",
            "phone": "",
            "line1": "",
            "line2": "",
            "ward": "",
            "district": "",
            "province": "",
            "country": "VN",
            "address_id": "",
        }
    conn = _get_db()
    row = conn.execute(
        "SELECT full_name, email, phone FROM users WHERE id = ?",
        (user["id"],),
    ).fetchone()
    conn.close()
    if row is None:
        base = {"full_name": "", "email": "", "phone": ""}
    else:
        base = {
            "full_name": row["full_name"] or "",
            "email": row["email"] or "",
            "phone": row["phone"] or "",
        }
    address = get_default_address(user["id"])
    if address is None:
        base.update(
            {
                "line1": "",
                "line2": "",
                "ward": "",
                "district": "",
                "province": "",
                "country": "VN",
                "address_id": "",
            }
        )
        return base
    base.update(
        {
            "full_name": address["recipient_name"] or base.get("full_name", ""),
            "phone": address["phone"] or base.get("phone", ""),
            "line1": address["line1"] or "",
            "line2": address["line2"] or "",
            "ward": address["ward"] or "",
            "district": address["district"] or "",
            "province": address["province"] or "",
            "country": address["country"] or "VN",
            "address_id": str(address["id"]),
        }
    )
    return base


def _address_or_form_value(address, key, form_data):
    submitted = (form_data.get(key) or "").strip()
    if submitted:
        return submitted
    if address is None:
        return submitted
    value = address[key]
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return value


def _cart_fingerprint(cart):
    payload = {
        "items": [
            {
                "variant_id": item.get("variant_id"),
                "qty": item.get("qty"),
                "unit_price": item.get("unit_price"),
                "line_total": item.get("line_total"),
            }
            for item in cart.get("items", [])
        ],
        "subtotal": cart.get("subtotal"),
        "discount_amount": cart.get("discount_amount"),
        "coupon_code": cart.get("coupon_code"),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _existing_order_result(conn, order_id, payment_method):
    if not order_id:
        return None
    order_row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order_row is None:
        return None
    item_rows = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ? ORDER BY id",
        (order_id,),
    ).fetchall()
    return {
        "ok": True,
        "order_id": order_row["id"],
        "order_number": order_row["order_number"],
        "total": order_row["total"],
        "payment_method": payment_method,
        "payment_url": None,
        "order": _row_to_dict(order_row),
        "items": [_row_to_dict(row) for row in item_rows],
        "duplicate": True,
    }


def _claim_checkout_submission(conn, submission_key, user_id, cart, payment_method):
    submission_key = (submission_key or "").strip()
    if not submission_key:
        return None
    now = datetime.utcnow().isoformat()
    fingerprint = _cart_fingerprint(cart)
    try:
        conn.execute(
            """
            INSERT INTO checkout_submissions (
                submission_key, user_id, cart_fingerprint, status, created_at, updated_at
            )
            VALUES (?, ?, ?, 'processing', ?, ?)
            """,
            (submission_key, user_id, fingerprint, now, now),
        )
        return None
    except INTEGRITY_ERRORS:
        row = conn.execute(
            """
            SELECT order_id, status
            FROM checkout_submissions
            WHERE submission_key = ?
            """,
            (submission_key,),
        ).fetchone()
        existing_result = _existing_order_result(
            conn,
            row["order_id"] if row else None,
            payment_method,
        )
        if existing_result:
            return existing_result
        return {
            "ok": False,
            "error": "Đơn hàng đang được xử lý. Vui lòng không bấm đặt hàng nhiều lần.",
        }


def _complete_checkout_submission(conn, submission_key, order_id, now):
    submission_key = (submission_key or "").strip()
    if not submission_key:
        return
    conn.execute(
        """
        UPDATE checkout_submissions
        SET status = 'completed', order_id = ?, error_message = NULL, updated_at = ?
        WHERE submission_key = ?
        """,
        (order_id, now, submission_key),
    )


def _mark_checkout_submission_failed(submission_key, error_message):
    submission_key = (submission_key or "").strip()
    if not submission_key:
        return
    conn = _get_db()
    try:
        conn.execute(
            """
            UPDATE checkout_submissions
            SET status = 'failed', error_message = ?, updated_at = ?
            WHERE submission_key = ? AND status = 'processing'
            """,
            (str(error_message)[:500], datetime.utcnow().isoformat(), submission_key),
        )
        conn.commit()
    finally:
        conn.close()


def _create_order_from_cart_snapshot(
    user,
    cart,
    order_data,
    remote_addr="",
    vnpay_return_url=None,
    clear_cart_after=False,
    submission_key=None,
):
    ensure_checkout_tables()
    user_id = user["id"] if user else None
    payment_method = (order_data.get("payment_method") or "cod").strip().lower()
    shipping_method = (order_data.get("shipping_method") or "").strip()
    coupon_code = cart.get("coupon_code")
    recipient_name = (order_data.get("recipient_name") or "").strip()
    email = (order_data.get("email") or "").strip().lower()
    phone = (order_data.get("phone") or "").strip()
    line1 = (order_data.get("line1") or "").strip()
    line2 = (order_data.get("line2") or "").strip()
    ward = (order_data.get("ward") or "").strip()
    district = (order_data.get("district") or "").strip()
    province = (order_data.get("province") or "").strip()
    notes = (order_data.get("notes") or "").strip()
    ghn_province_id = (order_data.get("ghn_province_id") or "").strip()
    ghn_district_id = (order_data.get("ghn_district_id") or "").strip()
    ghn_ward_code = (order_data.get("ghn_ward_code") or "").strip()
    address = {
        "line1": line1,
        "ward": ward,
        "district": district,
        "province": province,
        "ghn_province_id": ghn_province_id,
        "ghn_district_id": ghn_district_id,
        "ghn_ward_code": ghn_ward_code,
    }

    conn = _get_db()
    try:
        shipping_quote, shipping_error = select_shipping_quote(
            conn,
            cart,
            address,
            shipping_method,
            payment_method=payment_method,
        )
        if shipping_error:
            return {"ok": False, "error": shipping_error}
        shipping_fee = shipping_quote["fee"]
        order_total = max(cart["subtotal"] - cart["discount_amount"] + shipping_fee, 0)

        item_map = _variant_catalog(conn, [item["variant_id"] for item in cart["items"]])
        for item in cart["items"]:
            variant = item_map.get(item["variant_id"])
            if variant is None:
                return {"ok": False, "error": "Một số sản phẩm đã không còn tồn tại."}
            stock_qty = _parse_int(variant["stock_qty"], 0)
            if stock_qty < item["qty"]:
                return {
                    "ok": False,
                    "error": f"Size {variant['size']} của {item['product_name']} chỉ còn {stock_qty}.",
                }

        duplicate_result = _claim_checkout_submission(
            conn,
            submission_key,
            user_id,
            cart,
            payment_method,
        )
        if duplicate_result:
            return duplicate_result

        order_number = _build_order_number()
        now = datetime.utcnow().isoformat()
        full_notes = notes
        if coupon_code:
            coupon_text = f"Coupon: {coupon_code}"
            full_notes = f"{full_notes}\n{coupon_text}".strip() if full_notes else coupon_text
        shipping_note = f"Vận chuyển: {shipping_quote['carrier_label']} - {shipping_quote['service_label']}"
        full_notes = f"{full_notes}\n{shipping_note}".strip() if full_notes else shipping_note
        if payment_method == "bank_transfer":
            transfer_note = f"Thanh toán chuyển khoản: {bank_transfer_content(order_number)}"
            full_notes = f"{full_notes}\n{transfer_note}".strip() if full_notes else transfer_note

        cur = conn.execute(
            """
            INSERT INTO orders (
                order_number, user_id, status, subtotal, shipping_fee, discount_amount, total,
                payment_status, recipient_name, email, phone, line1, line2, ward, district, province,
                shipping_provider, notes, created_at, updated_at
            )
            VALUES (?, ?, 'new', ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_number,
                user_id,
                cart["subtotal"],
                shipping_fee,
                cart["discount_amount"],
                order_total,
                recipient_name,
                email,
                phone,
                line1,
                line2,
                ward,
                district,
                province,
                shipping_quote["carrier_label"],
                full_notes,
                now,
                now,
            ),
        )
        order_id = cur.lastrowid

        for item in cart["items"]:
            variant = item_map[item["variant_id"]]
            variant_label = f"{variant['size']} / {variant['color']}"
            conn.execute(
                """
                INSERT INTO order_items (order_id, product_id, variant_id, product_name, variant_label, sku, qty, unit_price, total_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    item["product_id"],
                    item["variant_id"],
                    item["product_name"],
                    variant_label,
                    variant["sku"],
                    item["qty"],
                    item["unit_price"],
                    item["line_total"],
                ),
            )
            conn.execute(
                "UPDATE product_variants SET stock_qty = stock_qty - ? WHERE id = ?",
                (item["qty"], item["variant_id"]),
            )
            conn.execute(
                """
                INSERT INTO inventory_movements (variant_id, change_qty, reason, note, admin_id, created_at)
                VALUES (?, ?, 'order', ?, NULL, ?)
                """,
                (
                    item["variant_id"],
                    -item["qty"],
                    f"Checkout {order_number}",
                    now,
                ),
            )

        conn.execute(
            """
            INSERT INTO order_status_events (order_id, status, note, admin_id, created_at)
            VALUES (?, 'new', 'Khách đặt hàng qua checkout', NULL, ?)
            """,
            (order_id, now),
        )

        if coupon_code:
            conn.execute(
                "UPDATE coupons SET used_count = used_count + 1 WHERE UPPER(code) = ?",
                (coupon_code.upper(),),
            )

        if user_id:
            conn.execute(
                """
                UPDATE users
                SET full_name = ?, phone = ?, updated_at = ?
                WHERE id = ?
                """,
                (recipient_name, phone, now, user_id),
            )

        payment_url = None
        if payment_method == "vnpay" and order_total > 0:
            payment_url = build_vnpay_payment_url(
                order_number=order_number,
                amount_vnd=order_total,
                return_url=vnpay_return_url,
                ip_addr=remote_addr,
            )
            _log_payment(
                conn,
                order_id=order_id,
                status="pending",
                source="checkout",
                txn_ref=order_number,
                amount=order_total,
                payload={"phase": "redirect_created"},
            )
        elif payment_method == "bank_transfer" and order_total > 0:
            _log_payment(
                conn,
                order_id=order_id,
                gateway="bank_transfer",
                status="pending",
                source="checkout",
                txn_ref=order_number,
                amount=order_total,
                payload={
                    "transfer_content": bank_transfer_content(order_number),
                    "phase": "instructions_created",
                },
            )
        elif order_total <= 0:
            conn.execute(
                """
                UPDATE orders
                SET payment_status = 'paid', status = 'confirmed', updated_at = ?
                WHERE id = ?
                """,
                (now, order_id),
            )
            conn.execute(
                """
                INSERT INTO order_status_events (order_id, status, note, admin_id, created_at)
                VALUES (?, 'confirmed', 'Đơn hàng 0 đồng, xác nhận thanh toán tự động', NULL, ?)
                """,
                (order_id, now),
            )

        _complete_checkout_submission(conn, submission_key, order_id, now)
        conn.commit()
        order_row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        item_rows = conn.execute(
            "SELECT * FROM order_items WHERE order_id = ? ORDER BY id",
            (order_id,),
        ).fetchall()
    except Exception as exc:
        try:
            conn.rollback()
        finally:
            conn.close()
        _mark_checkout_submission_failed(submission_key, exc)
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if clear_cart_after:
        clear_cart(user)
    order_dict = _row_to_dict(order_row)
    items_list = [_row_to_dict(row) for row in item_rows]
    # Gửi email xác nhận cho khách
    send_order_confirmation_email(order_dict, items_list)
    # Gửi email thông báo đơn mới cho chủ shop (chạy bất đồng bộ)
    threading.Thread(
        target=send_new_order_alert_to_owner,
        args=(order_dict, items_list),
        daemon=True,
    ).start()

    return {
        "ok": True,
        "order_id": order_row["id"],
        "order_number": order_row["order_number"],
        "total": order_row["total"],
        "payment_method": payment_method,
        "payment_url": payment_url,
        "order": order_dict,
        "items": items_list,
    }


def place_order_from_cart(user, form_data, remote_addr, vnpay_return_url):
    ensure_checkout_tables()
    cart = get_cart_snapshot(user)
    if not cart["items"]:
        return {"ok": False, "error": "Giỏ hàng trống, vui lòng thêm sản phẩm trước khi thanh toán."}

    user_id = user["id"] if user else None
    selected_address = None
    address_id = (form_data.get("address_id") or "").strip()
    if user_id and address_id.isdigit():
        selected_address = get_address_by_id(user_id, int(address_id))

    recipient_name = _address_or_form_value(selected_address, "recipient_name", form_data)
    email = (form_data.get("email") or "").strip().lower()
    phone = _address_or_form_value(selected_address, "phone", form_data)
    line1 = _address_or_form_value(selected_address, "line1", form_data)
    line2 = _address_or_form_value(selected_address, "line2", form_data)
    ward = _address_or_form_value(selected_address, "ward", form_data)
    district = _address_or_form_value(selected_address, "district", form_data)
    province = _address_or_form_value(selected_address, "province", form_data)
    ghn_province_id = (form_data.get("ghn_province_id") or "").strip()
    ghn_district_id = (form_data.get("ghn_district_id") or "").strip()
    ghn_ward_code = (form_data.get("ghn_ward_code") or "").strip()
    notes = (form_data.get("notes") or "").strip()
    payment_method = (form_data.get("payment_method") or "cod").strip().lower()
    shipping_method = (form_data.get("shipping_method") or "").strip()
    submission_key = (form_data.get("checkout_submission_key") or "").strip()

    if payment_method == "vnpay":
        return {"ok": False, "error": "VNPay đang tạm thời tắt. Vui lòng chọn COD hoặc chuyển khoản ngân hàng."}
    if payment_method not in {"cod", "bank_transfer"}:
        return {"ok": False, "error": "Phương thức thanh toán không hợp lệ."}
    if payment_method == "bank_transfer" and not bank_transfer_enabled():
        return {"ok": False, "error": "Chuyển khoản ngân hàng chưa được cấu hình."}
    if not recipient_name:
        return {"ok": False, "error": "Vui lòng nhập tên người nhận."}
    if email and "@" not in email:
        return {"ok": False, "error": "Vui lòng nhập email hợp lệ để nhận thông báo đơn hàng."}
    if not phone:
        return {"ok": False, "error": "Vui lòng nhập số điện thoại người nhận."}
    if not line1 or not district or not province:
        return {"ok": False, "error": "Vui lòng nhập đầy đủ địa chỉ giao hàng."}
    cart = get_cart_snapshot(user)
    return _create_order_from_cart_snapshot(
        user,
        cart,
        {
            "recipient_name": recipient_name,
            "email": email,
            "phone": phone,
            "line1": line1,
            "line2": line2,
            "ward": ward,
            "district": district,
            "province": province,
            "ghn_province_id": ghn_province_id,
            "ghn_district_id": ghn_district_id,
            "ghn_ward_code": ghn_ward_code,
            "notes": notes,
            "payment_method": payment_method,
            "shipping_method": shipping_method,
        },
        remote_addr=remote_addr,
        vnpay_return_url=vnpay_return_url,
        clear_cart_after=True,
        submission_key=submission_key,
    )


def place_order_from_bot_draft(draft_data, session_id, user=None):
    ensure_checkout_tables()
    variant_id = _parse_int(draft_data.get("variant_id"), 0)
    if variant_id <= 0:
        return {"ok": False, "error": "Missing product variant for bot order."}

    recipient_name = (draft_data.get("customer_name") or "").strip()
    phone = (draft_data.get("phone") or "").strip()
    address_text = (draft_data.get("address") or "").strip()
    if not recipient_name or not phone or not address_text:
        return {"ok": False, "error": "Missing recipient, phone, or address for bot order."}

    conn = _get_db()
    try:
        variant = _variant_catalog(conn, [variant_id]).get(variant_id)
    finally:
        conn.close()
    if variant is None:
        return {"ok": False, "error": "Product variant is unavailable."}

    unit_price = _parse_int(variant["unit_price"], _parse_int(draft_data.get("price"), 0))
    cart = {
        "items": [
            {
                "variant_id": variant_id,
                "product_id": variant["product_id"],
                "product_name": variant["product_name"] or draft_data.get("product_name") or "Nep Thanh product",
                "qty": 1,
                "unit_price": unit_price,
                "line_total": unit_price,
            }
        ],
        "subtotal": unit_price,
        "discount_amount": 0,
        "coupon_code": None,
    }
    user_email = (user.get("email") if user else "") or ""
    return _create_order_from_cart_snapshot(
        user,
        cart,
        {
            "recipient_name": recipient_name,
            "email": user_email,
            "phone": phone,
            "line1": address_text,
            "line2": "",
            "ward": "",
            "district": "",
            "province": "",
            "notes": f"AI Bot order (session: {session_id})",
            "payment_method": "cod",
            "shipping_method": "",
        },
        clear_cart_after=False,
    )


def get_order_details(order_number):
    conn = _get_db()
    order = conn.execute(
        "SELECT * FROM orders WHERE order_number = ?",
        (order_number,),
    ).fetchone()
    if order is None:
        conn.close()
        return None, []
    items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ? ORDER BY id",
        (order["id"],),
    ).fetchall()
    conn.close()
    return order, items


def order_has_bank_transfer(order_id):
    ensure_checkout_tables()
    conn = _get_db()
    row = conn.execute(
        """
        SELECT id
        FROM payment_transactions
        WHERE order_id = ? AND gateway = 'bank_transfer'
        ORDER BY id DESC
        LIMIT 1
        """,
        (order_id,),
    ).fetchone()
    conn.close()
    return row is not None


def get_bank_transfer_instructions(order):
    if order is None or not order_has_bank_transfer(order["id"]):
        return None
    return build_bank_transfer_instructions(order)


def send_order_status_update(order_id, status_note=None):
    conn = _get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        conn.close()
        return False
    items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ? ORDER BY id",
        (order_id,),
    ).fetchall()
    conn.close()
    return send_order_status_email(
        _row_to_dict(order),
        [_row_to_dict(row) for row in items],
        status_note=status_note,
    )


def handle_vnpay_callback(params, source="return"):
    ensure_checkout_tables()
    if not verify_vnpay_response(params):
        return {
            "ok": False,
            "success": False,
            "code": "97",
            "message": "Invalid signature",
            "order_number": (params.get("vnp_TxnRef") or "").strip(),
        }

    order_number = (params.get("vnp_TxnRef") or "").strip()
    if not order_number:
        return {"ok": False, "success": False, "code": "01", "message": "Order not found", "order_number": ""}

    conn = _get_db()
    order = conn.execute(
        "SELECT * FROM orders WHERE order_number = ?",
        (order_number,),
    ).fetchone()
    if order is None:
        conn.close()
        return {"ok": False, "success": False, "code": "01", "message": "Order not found", "order_number": order_number}

    amount = parse_vnpay_amount(params)
    txn_no = (params.get("vnp_TransactionNo") or "").strip()
    now = datetime.utcnow().isoformat()
    success = is_vnpay_success(params)
    status_note = None
    status_changed = False

    try:
        _log_payment(
            conn,
            order_id=order["id"],
            status="success" if success else "failed",
            source=source,
            txn_ref=order_number,
            transaction_no=txn_no,
            amount=amount,
            payload=params,
        )

        if success:
            if order["payment_status"] != "paid":
                new_status = "confirmed" if order["status"] == "new" else order["status"]
                conn.execute(
                    """
                    UPDATE orders
                    SET payment_status = 'paid', status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_status, now, order["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO order_status_events (order_id, status, note, admin_id, created_at)
                    VALUES (?, ?, ?, NULL, ?)
                    """,
                    (order["id"], new_status, "Thanh toán VNPay thành công", now),
                )
                status_note = "Thanh toán VNPay thành công"
                status_changed = True
        else:
            if order["payment_status"] != "paid" and order["status"] not in _ORDER_CANCEL_STATUSES:
                _restore_stock_for_order(conn, order["id"], order["order_number"], now)
                conn.execute(
                    """
                    UPDATE orders
                    SET payment_status = 'failed', status = 'cancelled', canceled_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, order["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO order_status_events (order_id, status, note, admin_id, created_at)
                    VALUES (?, 'cancelled', 'Thanh toán VNPay thất bại', NULL, ?)
                    """,
                    (order["id"], now),
                )
                status_note = "Thanh toán VNPay thất bại"
                status_changed = True
            elif order["payment_status"] not in {"paid", "failed"}:
                conn.execute(
                    "UPDATE orders SET payment_status = 'failed', updated_at = ? WHERE id = ?",
                    (now, order["id"]),
                )
                status_note = "Thanh toán VNPay thất bại"
                status_changed = True

        conn.commit()
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order["id"],)).fetchone()
        item_rows = conn.execute(
            "SELECT * FROM order_items WHERE order_id = ? ORDER BY id",
            (order["id"],),
        ).fetchall()
    finally:
        conn.close()

    if status_changed:
        send_order_status_email(
            _row_to_dict(order),
            [_row_to_dict(row) for row in item_rows],
            status_note=status_note,
        )
    final_success = order["payment_status"] == "paid"

    return {
        "ok": True,
        "success": final_success,
        "code": "00",
        "message": "Confirm Success",
        "order_number": order_number,
    }


def _variant_catalog(conn, variant_ids):
    if not variant_ids:
        return {}
    placeholders = ",".join("?" for _ in variant_ids)
    rows = conn.execute(
        f"""
        SELECT
            v.id,
            v.product_id,
            v.sku,
            v.size,
            v.color,
            COALESCE(v.price, p.base_price) AS unit_price,
            v.stock_qty,
            v.is_active,
            p.name AS product_name,
            p.status AS product_status
        FROM product_variants v
        JOIN products p ON p.id = v.product_id
        WHERE v.id IN ({placeholders})
        """,
        tuple(variant_ids),
    ).fetchall()
    data = {}
    for row in rows:
        if row["product_status"] != "active" or not row["is_active"]:
            continue
        data[row["id"]] = row
    return data


def _stock_already_restored_for_order(conn, order_number):
    reasons = sorted(_STOCK_TRACKING_REASONS)
    placeholders = ",".join("?" for _ in reasons)
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(change_qty), 0) AS restored_qty
        FROM inventory_movements
        WHERE reason IN ({placeholders})
          AND note LIKE ?
        """,
        (*reasons, f"%{order_number}%"),
    ).fetchone()
    return row is not None and int(row["restored_qty"] or 0) > 0


def _restore_stock_for_order(
    conn,
    order_id,
    order_number,
    now,
    reason="payment_failed",
    note=None,
    admin_id=None,
):
    if _stock_already_restored_for_order(conn, order_number):
        return False

    items = conn.execute(
        """
        SELECT variant_id, qty
        FROM order_items
        WHERE order_id = ? AND variant_id IS NOT NULL
        """,
        (order_id,),
    ).fetchall()
    if not items:
        return False

    movement_note = note or f"Rollback {order_number}"
    for item in items:
        conn.execute(
            "UPDATE product_variants SET stock_qty = stock_qty + ? WHERE id = ?",
            (item["qty"], item["variant_id"]),
        )
        conn.execute(
            """
            INSERT INTO inventory_movements (variant_id, change_qty, reason, note, admin_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                item["variant_id"],
                item["qty"],
                reason,
                movement_note,
                admin_id,
                now,
            ),
        )
    return True


def _deduct_restored_stock_for_order(
    conn,
    order_id,
    order_number,
    now,
    reason="admin_reactivated",
    note=None,
    admin_id=None,
):
    if not _stock_already_restored_for_order(conn, order_number):
        return False

    items = conn.execute(
        """
        SELECT order_items.variant_id, order_items.qty, product_variants.stock_qty
        FROM order_items
        JOIN product_variants ON product_variants.id = order_items.variant_id
        WHERE order_items.order_id = ? AND order_items.variant_id IS NOT NULL
        """,
        (order_id,),
    ).fetchall()
    if not items:
        return False

    for item in items:
        if int(item["stock_qty"] or 0) < int(item["qty"] or 0):
            raise ValueError(
                f"Không đủ tồn kho để mở lại đơn {order_number}."
            )

    movement_note = note or f"Reactivate {order_number}"
    for item in items:
        conn.execute(
            "UPDATE product_variants SET stock_qty = stock_qty - ? WHERE id = ?",
            (item["qty"], item["variant_id"]),
        )
        conn.execute(
            """
            INSERT INTO inventory_movements (variant_id, change_qty, reason, note, admin_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                item["variant_id"],
                -item["qty"],
                reason,
                movement_note,
                admin_id,
                now,
            ),
        )
    return True


def _log_payment(
    conn,
    order_id,
    status,
    source,
    gateway="vnpay",
    txn_ref=None,
    transaction_no=None,
    amount=0,
    payload=None,
):
    conn.execute(
        """
        INSERT INTO payment_transactions (
            order_id, gateway, status, source, txn_ref, transaction_no, amount, payload_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order_id,
            gateway,
            status,
            source,
            txn_ref,
            transaction_no,
            amount,
            json.dumps(payload, ensure_ascii=False) if payload else None,
            datetime.utcnow().isoformat(),
        ),
    )


def _row_to_dict(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}
