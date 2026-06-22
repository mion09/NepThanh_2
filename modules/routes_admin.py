import io
import json
import logging
import os
from datetime import date, datetime, timedelta

from flask import abort, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import generate_password_hash

from modules.auth import (
    _admin_email_allowlist,
    _create_user,
    _get_current_user,
    _get_user_by_email,
    admin_required,
)
from modules.config import (
    BASE_DIR,
    LOW_STOCK_DEFAULT,
    ORDER_STATUSES,
    PAYMENT_STATUSES,
    PROCESSING_STATUSES,
    REVENUE_STATUSES,
    ROLE_PERMISSIONS,
)
from modules.checkout import (
    _ORDER_CANCEL_STATUSES,
    _deduct_restored_stock_for_order,
    _restore_stock_for_order,
    ensure_checkout_tables,
    get_bank_transfer_instructions,
    send_order_status_update,
)
from modules.data_access import invalidate_content_cache
from modules.db import INTEGRITY_ERRORS, _get_db, _get_setting, _set_setting
from modules.qr_service import (
    create_qr_batch,
    disable_qr_token,
    export_qr_pdf_sheet,
    export_qr_png_zip,
    get_batch_tokens,
    get_qr_stats,
    list_qr_batches,
)
from modules.utils import (
    _build_order_number,
    _generate_qr_png,
    _get_blob_client,
    _is_allowed_image_filename,
    _normalize_static_path,
    _normalize_product_color,
    _parse_int,
    _save_upload,
    _slugify,
)

LOGGER = logging.getLogger(__name__)
PRODUCT_STATUSES = {"active", "coming_soon", "hidden", "archived"}


def _normalize_product_status(value):
    status = (value or "active").strip()
    return status if status in PRODUCT_STATUSES else "active"


def _log_action(admin_id, action, entity_type=None, entity_id=None, details=None):
    conn = _get_db()
    conn.execute(
        """
        INSERT INTO audit_logs (admin_id, action, entity_type, entity_id, details_json, ip_address, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            admin_id,
            action,
            entity_type,
            entity_id,
            json.dumps(details, ensure_ascii=False) if details else None,
            request.remote_addr,
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def _load_catalog_data(conn):
    categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    tags = conn.execute("SELECT * FROM tags ORDER BY name").fetchall()
    characters = conn.execute("SELECT id, name FROM characters ORDER BY name").fetchall()
    collections = conn.execute(
        "SELECT * FROM collections WHERE is_active = 1 ORDER BY name"
    ).fetchall()
    return categories, tags, characters, collections


def _get_product_relations(conn, product_id):
    category_ids = [
        row["category_id"]
        for row in conn.execute(
            "SELECT category_id FROM product_categories WHERE product_id = ?",
            (product_id,),
        ).fetchall()
    ]
    tag_ids = [
        row["tag_id"]
        for row in conn.execute(
            "SELECT tag_id FROM product_tags WHERE product_id = ?",
            (product_id,),
        ).fetchall()
    ]
    return category_ids, tag_ids


def _ensure_primary_product_image(conn, product_id):
    primary = conn.execute(
        "SELECT id FROM product_images WHERE product_id = ? AND is_primary = 1 LIMIT 1",
        (product_id,),
    ).fetchone()
    if primary:
        return
    first_image = conn.execute(
        "SELECT id FROM product_images WHERE product_id = ? ORDER BY sort_order, id LIMIT 1",
        (product_id,),
    ).fetchone()
    if first_image:
        conn.execute(
            "UPDATE product_images SET is_primary = 1 WHERE id = ?",
            (first_image["id"],),
        )


def _set_primary_product_image(conn, product_id, image_id):
    conn.execute(
        "UPDATE product_images SET is_primary = 0 WHERE product_id = ?",
        (product_id,),
    )
    conn.execute(
        "UPDATE product_images SET is_primary = 1 WHERE id = ? AND product_id = ?",
        (image_id, product_id),
    )


def _delete_product_upload(image_url):
    normalized = _normalize_static_path(image_url)
    if normalized and normalized.startswith(("http://", "https://")):
        if ".blob.vercel-storage.com/" not in normalized:
            return
        try:
            _get_blob_client().delete(normalized)
        except Exception:
            LOGGER.exception("Failed to delete product image blob: %s", normalized)
        return
    prefix = "uploads/products/"
    if not normalized or not normalized.startswith(prefix):
        return
    uploads_dir = os.path.abspath(os.path.join(BASE_DIR, "static", "uploads", "products"))
    target = os.path.abspath(os.path.join(BASE_DIR, "static", *normalized.split("/")))
    if target.startswith(uploads_dir + os.sep) and os.path.isfile(target):
        os.remove(target)


def _save_product_relations(conn, product_id, category_ids, tag_ids):
    conn.execute("DELETE FROM product_categories WHERE product_id = ?", (product_id,))
    conn.execute("DELETE FROM product_tags WHERE product_id = ?", (product_id,))
    for category_id in category_ids:
        conn.execute(
            "INSERT INTO product_categories (product_id, category_id) VALUES (?, ?)",
            (product_id, category_id),
        )
    for tag_id in tag_ids:
        conn.execute(
            "INSERT INTO product_tags (product_id, tag_id) VALUES (?, ?)",
            (product_id, tag_id),
        )


def _validate_unique_slug(conn, table, slug, exclude_id=None):
    if exclude_id:
        row = conn.execute(
            f"SELECT id FROM {table} WHERE slug = ? AND id != ?",
            (slug, exclude_id),
        ).fetchone()
    else:
        row = conn.execute(f"SELECT id FROM {table} WHERE slug = ?", (slug,)).fetchone()
    return row is None


def register_admin_routes(app):
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        return redirect(url_for("home"))

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("user_id", None)
        return redirect(url_for("home"))

    @app.route("/admin")
    @admin_required("dashboard")
    def admin_dashboard():
        conn = _get_db()
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        month_start = today.replace(day=1)
        status_placeholders = ",".join(["?"] * len(REVENUE_STATUSES))
        revenue_today = conn.execute(
            f"SELECT COALESCE(SUM(total), 0) AS total FROM orders WHERE status IN ({status_placeholders}) AND date(created_at) = ?",
            (*REVENUE_STATUSES, today.isoformat()),
        ).fetchone()["total"]
        revenue_week = conn.execute(
            f"SELECT COALESCE(SUM(total), 0) AS total FROM orders WHERE status IN ({status_placeholders}) AND date(created_at) >= ?",
            (*REVENUE_STATUSES, week_start.isoformat()),
        ).fetchone()["total"]
        revenue_month = conn.execute(
            f"SELECT COALESCE(SUM(total), 0) AS total FROM orders WHERE status IN ({status_placeholders}) AND date(created_at) >= ?",
            (*REVENUE_STATUSES, month_start.isoformat()),
        ).fetchone()["total"]
        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM orders GROUP BY status"
        ).fetchall()
        status_map = {row["status"]: row["count"] for row in status_rows}
        new_orders = status_map.get("new", 0)
        processing_orders = sum(status_map.get(status, 0) for status in PROCESSING_STATUSES)
        completed_orders = status_map.get("completed", 0)
        cancelled_orders = status_map.get("cancelled", 0) + status_map.get("refunded", 0) + status_map.get("returned", 0)
        top_products = conn.execute(
            f"""
            SELECT product_name, SUM(qty) AS qty_sold, SUM(total_price) AS revenue
            FROM order_items
            JOIN orders ON orders.id = order_items.order_id
            WHERE orders.status IN ({status_placeholders})
            GROUP BY product_name
            ORDER BY qty_sold DESC
            LIMIT 5
            """,
            (*REVENUE_STATUSES,),
        ).fetchall()
        low_stock = conn.execute(
            """
            SELECT product_variants.*, products.name AS product_name
            FROM product_variants
            JOIN products ON products.id = product_variants.product_id
            WHERE product_variants.is_active = 1
              AND product_variants.stock_qty <= product_variants.low_stock_threshold
            ORDER BY product_variants.stock_qty ASC
            LIMIT 8
            """
        ).fetchall()
        start_day = today - timedelta(days=29)
        chart_rows = conn.execute(
            """
            SELECT date(created_at) AS day, COUNT(*) AS orders, COALESCE(SUM(total), 0) AS revenue
            FROM orders
            WHERE date(created_at) >= ?
            GROUP BY date(created_at)
            """,
            (start_day.isoformat(),),
        ).fetchall()
        chart_map = {row["day"]: row for row in chart_rows}
        chart_labels = []
        chart_orders = []
        chart_revenue = []
        for offset in range(30):
            day = start_day + timedelta(days=offset)
            key = day.isoformat()
            chart_labels.append(day.strftime("%d/%m"))
            chart_orders.append(chart_map.get(key, {"orders": 0})["orders"])
            chart_revenue.append(chart_map.get(key, {"revenue": 0})["revenue"])
        conn.close()
        return render_template(
            "admin/dashboard.html",
            title="Admin Dashboard",
            section="dashboard",
            revenue_today=revenue_today,
            revenue_week=revenue_week,
            revenue_month=revenue_month,
            new_orders=new_orders,
            processing_orders=processing_orders,
            completed_orders=completed_orders,
            cancelled_orders=cancelled_orders,
            top_products=top_products,
            low_stock=low_stock,
            chart_labels=json.dumps(chart_labels),
            chart_orders=json.dumps(chart_orders),
            chart_revenue=json.dumps(chart_revenue),
        )

    @app.route("/admin/products")
    @admin_required("products")
    def admin_products():
        status_filter = request.args.get("status", "").strip()
        query_text = request.args.get("q", "").strip()
        conn = _get_db()
        sql = """
            SELECT p.*, c.name AS character_name,
                (SELECT COUNT(*) FROM product_variants v WHERE v.product_id = p.id) AS variant_count,
                (SELECT COALESCE(SUM(stock_qty), 0) FROM product_variants v WHERE v.product_id = p.id) AS stock_qty
            FROM products p
            LEFT JOIN characters c ON c.id = p.character_id
            WHERE 1 = 1
        """
        params = []
        if status_filter:
            sql += " AND p.status = ?"
            params.append(status_filter)
        if query_text:
            sql += " AND (p.name LIKE ? OR p.slug LIKE ?)"
            like = f"%{query_text}%"
            params.extend([like, like])
        sql += " ORDER BY p.created_at DESC"
        products = conn.execute(sql, params).fetchall()
        conn.close()
        return render_template(
            "admin/products.html",
            title="Quản lý sản phẩm",
            section="products",
            products=products,
            status_filter=status_filter,
            query_text=query_text,
        )


    @app.route("/admin/products/new", methods=["GET", "POST"])
    @admin_required("products")
    def admin_product_new():
        conn = _get_db()
        categories, tags, characters, collections = _load_catalog_data(conn)
        error = None
        product = None
        category_ids = []
        tag_ids = []
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            slug = request.form.get("slug", "").strip() or _slugify(name)
            description = request.form.get("description", "").strip()
            long_description = request.form.get("long_description", "").strip()
            seo_title = request.form.get("seo_title", "").strip()
            seo_description = request.form.get("seo_description", "").strip()
            base_price = _parse_int(request.form.get("base_price"), 0)
            character_id = _parse_int(request.form.get("character_id"))
            status = _normalize_product_status(request.form.get("status", "active"))
            is_featured = 1 if request.form.get("is_featured") else 0
            collection = request.form.get("collection", "").strip() or None
            category_ids = [cid for cid in (_parse_int(cid) for cid in request.form.getlist("category_ids")) if cid]
            tag_ids = [tid for tid in (_parse_int(tid) for tid in request.form.getlist("tag_ids")) if tid]
            if not name:
                error = "Vui lòng nhập tên sản phẩm."
            elif not _validate_unique_slug(conn, "products", slug):
                error = "Slug đã tồn tại."
            else:
                now = datetime.utcnow().isoformat()
                cur = conn.execute(
                    """
                    INSERT INTO products (slug, name, description, long_description, character_id, collection, base_price, status, created_at, seo_title, seo_description, is_featured)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slug,
                        name,
                        description,
                        long_description,
                        character_id,
                        collection,
                        base_price,
                        status,
                        now,
                        seo_title,
                        seo_description,
                        is_featured,
                    ),
                )
                product_id = cur.lastrowid
                _save_product_relations(conn, product_id, category_ids, tag_ids)
                conn.commit()
                _log_action(
                    _get_current_user()["id"],
                    "create",
                    "product",
                    product_id,
                    {"name": name},
                )
                conn.close()
                return redirect(url_for("admin_product_edit", product_id=product_id))
        conn.close()
        return render_template(
            "admin/product_form.html",
            title="Thêm sản phẩm",
            section="products",
            product=product,
            categories=categories,
            tags=tags,
            characters=characters,
            collections=collections,
            category_ids=category_ids,
            tag_ids=tag_ids,
            error=error,
        )

    @app.route("/admin/products/<int:product_id>/edit", methods=["GET", "POST"])
    @admin_required("products")
    def admin_product_edit(product_id):
        conn = _get_db()
        product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if product is None:
            conn.close()
            abort(404)
        categories, tags, characters, collections = _load_catalog_data(conn)
        category_ids, tag_ids = _get_product_relations(conn, product_id)
        error = None
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            slug = request.form.get("slug", "").strip() or _slugify(name)
            description = request.form.get("description", "").strip()
            long_description = request.form.get("long_description", "").strip()
            seo_title = request.form.get("seo_title", "").strip()
            seo_description = request.form.get("seo_description", "").strip()
            base_price = _parse_int(request.form.get("base_price"), 0)
            character_id = _parse_int(request.form.get("character_id"))
            status = _normalize_product_status(request.form.get("status", "active"))
            is_featured = 1 if request.form.get("is_featured") else 0
            collection = request.form.get("collection", "").strip() or None
            category_ids = [cid for cid in (_parse_int(cid) for cid in request.form.getlist("category_ids")) if cid]
            tag_ids = [tid for tid in (_parse_int(tid) for tid in request.form.getlist("tag_ids")) if tid]
            if not name:
                error = "Vui lòng nhập tên sản phẩm."
            elif not _validate_unique_slug(conn, "products", slug, exclude_id=product_id):
                error = "Slug đã tồn tại."
            else:
                conn.execute(
                    """
                    UPDATE products
                    SET slug = ?, name = ?, description = ?, long_description = ?, character_id = ?, collection = ?, base_price = ?, status = ?, seo_title = ?, seo_description = ?, is_featured = ?
                    WHERE id = ?
                    """,
                    (
                        slug,
                        name,
                        description,
                        long_description,
                        character_id,
                        collection,
                        base_price,
                        status,
                        seo_title,
                        seo_description,
                        is_featured,
                        product_id,
                    ),
                )
                _save_product_relations(conn, product_id, category_ids, tag_ids)
                conn.commit()
                _log_action(
                    _get_current_user()["id"],
                    "update",
                    "product",
                    product_id,
                    {"name": name},
                )
                product = conn.execute(
                    "SELECT * FROM products WHERE id = ?", (product_id,)
                ).fetchone()
        variants = conn.execute(
            "SELECT * FROM product_variants WHERE product_id = ? ORDER BY id",
            (product_id,),
        ).fetchall()
        images = conn.execute(
            "SELECT * FROM product_images WHERE product_id = ? ORDER BY sort_order, id",
            (product_id,),
        ).fetchall()
        conn.close()
        return render_template(
            "admin/product_form.html",
            title="Chỉnh sửa sản phẩm",
            section="products",
            product=product,
            categories=categories,
            tags=tags,
            characters=characters,
            collections=collections,
            category_ids=category_ids,
            tag_ids=tag_ids,
            variants=variants,
            images=images,
            error=error,
        )

    @app.route("/admin/products/<int:product_id>/delete", methods=["POST"])
    @admin_required("products")
    def admin_product_delete(product_id):
        conn = _get_db()
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()
        _log_action(_get_current_user()["id"], "delete", "product", product_id)
        conn.close()
        return redirect(url_for("admin_products"))

    @app.route("/admin/products/<int:product_id>/variants/new", methods=["POST"])
    @admin_required("products")
    def admin_variant_new(product_id):
        conn = _get_db()
        product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if product is None:
            conn.close()
            abort(404)
        size = request.form.get("size", "").strip() or "Free"
        color = _normalize_product_color(request.form.get("color", "")) or "black"
        sku = request.form.get("sku", "").strip() or f"{product['slug']}-{size}-{color}".upper()
        price = request.form.get("price")
        stock_qty = _parse_int(request.form.get("stock_qty"), 0)
        weight_grams = _parse_int(request.form.get("weight_grams"), 250)
        is_active = 1 if request.form.get("is_active") else 0
        low_stock_threshold = _parse_int(request.form.get("low_stock_threshold"), LOW_STOCK_DEFAULT)
        try:
            conn.execute(
                """
                INSERT INTO product_variants (product_id, sku, size, color, price, stock_qty, weight_grams, is_active, low_stock_threshold)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    sku,
                    size,
                    color,
                    _parse_int(price) if price else None,
                    stock_qty,
                    weight_grams,
                    is_active,
                    low_stock_threshold,
                ),
            )
            conn.commit()
            _log_action(
                _get_current_user()["id"],
                "create",
                "variant",
                product_id,
                {"sku": sku},
            )
        except INTEGRITY_ERRORS:
            conn.close()
            return redirect(url_for("admin_product_edit", product_id=product_id, error="SKU đã tồn tại."))
        conn.close()
        return redirect(url_for("admin_product_edit", product_id=product_id))

    @app.route("/admin/variants/<int:variant_id>/edit", methods=["POST"])
    @admin_required("products")
    def admin_variant_edit(variant_id):
        conn = _get_db()
        variant = conn.execute(
            "SELECT * FROM product_variants WHERE id = ?", (variant_id,)
        ).fetchone()
        if variant is None:
            conn.close()
            abort(404)
        size = request.form.get("size", "").strip() or "Free"
        color = _normalize_product_color(request.form.get("color", "")) or "black"
        sku = request.form.get("sku", "").strip() or variant["sku"]
        price = request.form.get("price")
        stock_qty = _parse_int(request.form.get("stock_qty"), 0)
        weight_grams = _parse_int(request.form.get("weight_grams"), 250)
        is_active = 1 if request.form.get("is_active") else 0
        low_stock_threshold = _parse_int(request.form.get("low_stock_threshold"), LOW_STOCK_DEFAULT)
        conn.execute(
            """
            UPDATE product_variants
            SET sku = ?, size = ?, color = ?, price = ?, stock_qty = ?, weight_grams = ?, is_active = ?, low_stock_threshold = ?
            WHERE id = ?
            """,
            (
                sku,
                size,
                color,
                _parse_int(price) if price else None,
                stock_qty,
                weight_grams,
                is_active,
                low_stock_threshold,
                variant_id,
            ),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("admin_product_edit", product_id=variant["product_id"]))

    @app.route("/admin/variants/<int:variant_id>/delete", methods=["POST"])
    @admin_required("products")
    def admin_variant_delete(variant_id):
        conn = _get_db()
        variant = conn.execute(
            "SELECT product_id FROM product_variants WHERE id = ?",
            (variant_id,),
        ).fetchone()
        if variant is None:
            conn.close()
            abort(404)
        conn.execute("DELETE FROM product_variants WHERE id = ?", (variant_id,))
        conn.commit()
        conn.close()
        return redirect(url_for("admin_product_edit", product_id=variant["product_id"]))

    @app.route("/admin/products/<int:product_id>/images/new", methods=["POST"])
    @admin_required("products")
    def admin_product_image_new(product_id):
        conn = _get_db()
        product = conn.execute(
            "SELECT id FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if product is None:
            conn.close()
            abort(404)
        image_url = _normalize_static_path(request.form.get("image_url", ""))
        alt_text = request.form.get("alt_text", "").strip()
        color = _normalize_product_color(request.form.get("color", ""))
        sort_order = _parse_int(request.form.get("sort_order"), 0)
        is_primary = 1 if request.form.get("is_primary") else 0
        file_storage = request.files.get("image_file")
        if file_storage and _is_allowed_image_filename(file_storage.filename):
            saved = _save_upload(file_storage, "products")
            if saved:
                image_url = saved
        if image_url:
            cur = conn.execute(
                """
                INSERT INTO product_images (product_id, url, alt_text, sort_order, color, is_primary)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (product_id, image_url, alt_text, sort_order, color or None),
            )
            if is_primary:
                _set_primary_product_image(conn, product_id, cur.lastrowid)
            else:
                _ensure_primary_product_image(conn, product_id)
            conn.commit()
            invalidate_content_cache()
        conn.close()
        return redirect(url_for("admin_product_edit", product_id=product_id))

    @app.route("/admin/product-images/<int:image_id>/edit", methods=["POST"])
    @admin_required("products")
    def admin_product_image_edit(image_id):
        conn = _get_db()
        image = conn.execute(
            "SELECT product_id, url FROM product_images WHERE id = ?", (image_id,)
        ).fetchone()
        if image is None:
            conn.close()
            abort(404)
        url = _normalize_static_path(request.form.get("image_url", ""))
        alt_text = request.form.get("alt_text", "").strip()
        color = _normalize_product_color(request.form.get("color", ""))
        sort_order = _parse_int(request.form.get("sort_order"), 0)
        is_primary = 1 if request.form.get("is_primary") else 0
        file_storage = request.files.get("image_file")
        if file_storage and _is_allowed_image_filename(file_storage.filename):
            saved = _save_upload(file_storage, "products")
            if saved:
                _delete_product_upload(image["url"])
                url = saved
        conn.execute(
            "UPDATE product_images SET url = ?, alt_text = ?, sort_order = ?, color = ? WHERE id = ?",
            (url, alt_text, sort_order, color or None, image_id),
        )
        if is_primary:
            _set_primary_product_image(conn, image["product_id"], image_id)
        else:
            conn.execute(
                "UPDATE product_images SET is_primary = 0 WHERE id = ?",
                (image_id,),
            )
            _ensure_primary_product_image(conn, image["product_id"])
        conn.commit()
        invalidate_content_cache()
        conn.close()
        return redirect(url_for("admin_product_edit", product_id=image["product_id"]))

    @app.route("/admin/product-images/<int:image_id>/delete", methods=["POST"])
    @admin_required("products")
    def admin_product_image_delete(image_id):
        conn = _get_db()
        image = conn.execute(
            "SELECT product_id, url FROM product_images WHERE id = ?",
            (image_id,),
        ).fetchone()
        if image is None:
            conn.close()
            abort(404)
        conn.execute("DELETE FROM product_images WHERE id = ?", (image_id,))
        _ensure_primary_product_image(conn, image["product_id"])
        conn.commit()
        invalidate_content_cache()
        conn.close()
        _delete_product_upload(image["url"])
        return redirect(url_for("admin_product_edit", product_id=image["product_id"]))

    @app.route("/admin/categories", methods=["GET", "POST"])
    @admin_required("products")
    def admin_categories():
        conn = _get_db()
        error = None
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            slug = request.form.get("slug", "").strip() or _slugify(name)
            description = request.form.get("description", "").strip()
            parent_id = request.form.get("parent_id") or None
            if not name:
                error = "Vui lòng nhập tên danh mục."
            elif not _validate_unique_slug(conn, "categories", slug):
                error = "Slug đã tồn tại."
            else:
                now = datetime.utcnow().isoformat()
                conn.execute(
                    """
                    INSERT INTO categories (name, slug, description, parent_id, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (name, slug, description, parent_id, now, now),
                )
                conn.commit()
        categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
        conn.close()
        return render_template(
            "admin/categories.html",
            title="Danh mục sản phẩm",
            section="products",
            categories=categories,
            error=error,
        )

    @app.route("/admin/categories/<int:category_id>/delete", methods=["POST"])
    @admin_required("products")
    def admin_category_delete(category_id):
        conn = _get_db()
        conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        conn.commit()
        conn.close()
        return redirect(url_for("admin_categories"))

    @app.route("/admin/tags", methods=["GET", "POST"])
    @admin_required("products")
    def admin_tags():
        conn = _get_db()
        error = None
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            slug = request.form.get("slug", "").strip() or _slugify(name)
            if not name:
                error = "Vui lòng nhập tên tag."
            elif not _validate_unique_slug(conn, "tags", slug):
                error = "Slug đã tồn tại."
            else:
                conn.execute(
                    "INSERT INTO tags (name, slug, created_at) VALUES (?, ?, ?)",
                    (name, slug, datetime.utcnow().isoformat()),
                )
                conn.commit()
        tags = conn.execute("SELECT * FROM tags ORDER BY name").fetchall()
        conn.close()
        return render_template(
            "admin/tags.html",
            title="Tag sản phẩm",
            section="products",
            tags=tags,
            error=error,
        )

    @app.route("/admin/tags/<int:tag_id>/delete", methods=["POST"])
    @admin_required("products")
    def admin_tag_delete(tag_id):
        conn = _get_db()
        conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        conn.commit()
        conn.close()
        return redirect(url_for("admin_tags"))

    @app.route("/admin/collections", methods=["GET", "POST"])
    @admin_required("content")
    def admin_collections():
        conn = _get_db()
        error = None
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            slug = request.form.get("slug", "").strip() or _slugify(name)
            description = request.form.get("description", "").strip()
            image_url = _normalize_static_path(request.form.get("image_url", ""))
            if not name:
                error = "Vui lòng nhập tên bộ sưu tập."
            elif not _validate_unique_slug(conn, "collections", slug):
                error = "Slug đã tồn tại."
            else:
                now = datetime.utcnow().isoformat()
                conn.execute(
                    """
                    INSERT INTO collections (name, slug, description, image_url, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (name, slug, description, image_url, now, now),
                )
                conn.commit()
        collections = conn.execute(
            "SELECT * FROM collections ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        return render_template(
            "admin/collections.html",
            title="Bộ sưu tập",
            section="content",
            collections=collections,
            error=error,
        )

    @app.route("/admin/collections/<int:collection_id>/delete", methods=["POST"])
    @admin_required("content")
    def admin_collection_delete(collection_id):
        conn = _get_db()
        conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
        conn.commit()
        conn.close()
        return redirect(url_for("admin_collections"))

    @app.route("/admin/orders")
    @admin_required("orders")
    def admin_orders():
        status_filter = request.args.get("status", "").strip()
        query_text = request.args.get("q", "").strip()
        conn = _get_db()
        sql = """
            SELECT orders.*, users.email AS user_email
            FROM orders
            LEFT JOIN users ON users.id = orders.user_id
            WHERE 1 = 1
        """
        params = []
        if status_filter:
            sql += " AND orders.status = ?"
            params.append(status_filter)
        if query_text:
            sql += " AND (orders.order_number LIKE ? OR orders.email LIKE ? OR orders.phone LIKE ?)"
            like = f"%{query_text}%"
            params.extend([like, like, like])
        sql += " ORDER BY orders.created_at DESC"
        orders = conn.execute(sql, params).fetchall()
        conn.close()
        return render_template(
            "admin/orders.html",
            title="Quản lý đơn hàng",
            section="orders",
            orders=orders,
            status_filter=status_filter,
            query_text=query_text,
            statuses=ORDER_STATUSES,
        )

    @app.route("/admin/orders/new", methods=["GET", "POST"])
    @admin_required("orders")
    def admin_order_new():
        conn = _get_db()
        variants = conn.execute(
            """
            SELECT product_variants.*, products.name AS product_name, products.base_price AS base_price
            FROM product_variants
            JOIN products ON products.id = product_variants.product_id
            WHERE product_variants.is_active = 1
            ORDER BY products.name, product_variants.size
            """
        ).fetchall()
        users = conn.execute(
            "SELECT id, email, full_name FROM users ORDER BY created_at DESC"
        ).fetchall()
        error = None
        if request.method == "POST":
            customer_email = request.form.get("email", "").strip().lower() or None
            customer_name = request.form.get("recipient_name", "").strip()
            phone = request.form.get("phone", "").strip()
            line1 = request.form.get("line1", "").strip()
            line2 = request.form.get("line2", "").strip()
            ward = request.form.get("ward", "").strip()
            district = request.form.get("district", "").strip()
            province = request.form.get("province", "").strip()
            notes = request.form.get("notes", "").strip()
            shipping_fee = _parse_int(request.form.get("shipping_fee"), 0)
            discount_amount = _parse_int(request.form.get("discount_amount"), 0)
            user_id = _parse_int(request.form.get("user_id")) or None
            if customer_email and not user_id:
                user_row = conn.execute(
                    "SELECT id FROM users WHERE email = ?", (customer_email,)
                ).fetchone()
                if user_row:
                    user_id = user_row["id"]
            variant_ids = request.form.getlist("variant_id")
            qtys = request.form.getlist("qty")
            prices = request.form.getlist("price")
            variant_map = {str(row["id"]): row for row in variants}
            items = []
            subtotal = 0
            for idx, variant_id in enumerate(variant_ids):
                if not variant_id or variant_id not in variant_map:
                    continue
                qty = _parse_int(qtys[idx] if idx < len(qtys) else 1, 1)
                if qty <= 0:
                    continue
                variant = variant_map[variant_id]
                fallback_price = variant["price"] if variant["price"] is not None else variant["base_price"]
                price = _parse_int(
                    prices[idx] if idx < len(prices) else fallback_price or 0,
                    fallback_price or 0,
                )
                line_total = price * qty
                subtotal += line_total
                items.append(
                    {
                        "variant": variant,
                        "qty": qty,
                        "price": price,
                        "total": line_total,
                    }
                )
            if not items:
                error = "Vui lòng chọn ít nhất một sản phẩm."
            else:
                stock_errors = []
                requested_qty_by_variant = {}
                for item in items:
                    variant = item["variant"]
                    variant_id = variant["id"]
                    requested_qty_by_variant[variant_id] = (
                        requested_qty_by_variant.get(variant_id, 0) + item["qty"]
                    )
                variants_by_id = {item["variant"]["id"]: item["variant"] for item in items}
                for variant_id, requested_qty in requested_qty_by_variant.items():
                    variant = variants_by_id[variant_id]
                    stock_qty = max(_parse_int(variant["stock_qty"], 0), 0)
                    if requested_qty > stock_qty:
                        stock_errors.append(
                            f"{variant['product_name']} - {variant['size']}/{variant['color']} "
                            f"({variant['sku']}) chỉ còn {stock_qty}, đang tạo {requested_qty}."
                        )
                if stock_errors:
                    error = "Không đủ tồn kho: " + " ".join(stock_errors)
            if not error:
                order_number = _build_order_number()
                now = datetime.utcnow().isoformat()
                total = subtotal + shipping_fee - discount_amount
                cur = conn.execute(
                    """
                    INSERT INTO orders (order_number, user_id, status, subtotal, shipping_fee, discount_amount, total, payment_status, recipient_name, email, phone, line1, line2, ward, district, province, notes, created_at, updated_at)
                    VALUES (?, ?, 'new', ?, ?, ?, ?, 'unpaid', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_number,
                        user_id,
                        subtotal,
                        shipping_fee,
                        discount_amount,
                        total,
                        customer_name,
                        customer_email,
                        phone,
                        line1,
                        line2,
                        ward,
                        district,
                        province,
                        notes,
                        now,
                        now,
                    ),
                )
                order_id = cur.lastrowid
                for item in items:
                    variant = item["variant"]
                    variant_label = f"{variant['size']} / {variant['color']}"
                    conn.execute(
                        """
                        INSERT INTO order_items (order_id, product_id, variant_id, product_name, variant_label, sku, qty, unit_price, total_price)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            order_id,
                            variant["product_id"],
                            variant["id"],
                            variant["product_name"],
                            variant_label,
                            variant["sku"],
                            item["qty"],
                            item["price"],
                            item["total"],
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE product_variants SET stock_qty = stock_qty - ? WHERE id = ?
                        """,
                        (item["qty"], variant["id"]),
                    )
                    conn.execute(
                        """
                        INSERT INTO inventory_movements (variant_id, change_qty, reason, note, admin_id, created_at)
                        VALUES (?, ?, 'order', ?, ?, ?)
                        """,
                        (
                            variant["id"],
                            -item["qty"],
                            f"Order {order_number}",
                            _get_current_user()["id"],
                            now,
                        ),
                    )
                conn.execute(
                    """
                    INSERT INTO order_status_events (order_id, status, note, admin_id, created_at)
                    VALUES (?, 'new', 'Tạo đơn thủ công', ?, ?)
                    """,
                    (order_id, _get_current_user()["id"], now),
                )
                conn.commit()
                _log_action(
                    _get_current_user()["id"],
                    "create",
                    "order",
                    order_id,
                    {"order_number": order_number},
                )
                conn.close()
                return redirect(url_for("admin_order_detail", order_id=order_id))
        conn.close()
        return render_template(
            "admin/order_form.html",
            title="Tạo đơn hàng",
            section="orders",
            variants=variants,
            users=users,
            error=error,
        )

    @app.route("/admin/orders/<int:order_id>", methods=["GET", "POST"])
    @admin_required("orders")
    def admin_order_detail(order_id):
        ensure_checkout_tables()
        conn = _get_db()
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            conn.close()
            abort(404)
        error = None
        status_email_note = None
        if request.method == "POST":
            new_status = request.form.get("status")
            new_payment_status = request.form.get("payment_status")
            note = request.form.get("note", "").strip()
            shipping_provider = request.form.get("shipping_provider", "").strip() or None
            tracking_code = request.form.get("tracking_code", "").strip() or None
            changed = False
            now = datetime.utcnow().isoformat()
            admin_id = _get_current_user()["id"]

            if new_status in ORDER_STATUSES and new_status != order["status"]:
                now = datetime.utcnow().isoformat()
                completed_at = now if new_status == "completed" else order["completed_at"]
                canceled_at = now if new_status in _ORDER_CANCEL_STATUSES else order["canceled_at"]
                stock_restored = False
                if (
                    new_status in _ORDER_CANCEL_STATUSES
                    and order["status"] not in _ORDER_CANCEL_STATUSES
                ):
                    movement_note = f"Admin {new_status} {order['order_number']}"
                    if note:
                        movement_note = f"{movement_note}: {note}"
                    stock_restored = _restore_stock_for_order(
                        conn,
                        order["id"],
                        order["order_number"],
                        now,
                        reason=f"admin_{new_status}",
                        note=movement_note,
                        admin_id=admin_id,
                    )
                stock_rededucted = False
                if (
                    new_status not in _ORDER_CANCEL_STATUSES
                    and order["status"] in _ORDER_CANCEL_STATUSES
                ):
                    movement_note = f"Admin reactivate {order['order_number']}"
                    if note:
                        movement_note = f"{movement_note}: {note}"
                    try:
                        stock_rededucted = _deduct_restored_stock_for_order(
                            conn,
                            order["id"],
                            order["order_number"],
                            now,
                            note=movement_note,
                            admin_id=admin_id,
                        )
                    except ValueError as exc:
                        error = str(exc)
                if error is None:
                    conn.execute(
                        """
                        UPDATE orders SET status = ?, updated_at = ?, completed_at = ?, canceled_at = ?
                        WHERE id = ?
                        """,
                        (new_status, now, completed_at, canceled_at, order_id),
                    )
                    event_note = note
                    if stock_restored:
                        event_note = f"{event_note}\nĐã hoàn tồn kho cho đơn hàng.".strip()
                    if stock_rededucted:
                        event_note = f"{event_note}\nĐã trừ lại tồn kho cho đơn hàng.".strip()
                    conn.execute(
                        """
                        INSERT INTO order_status_events (order_id, status, note, admin_id, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (order_id, new_status, event_note, admin_id, now),
                    )
                    status_email_note = note or f"Trạng thái đơn đã đổi sang {new_status}"
                    changed = True
            if new_payment_status in PAYMENT_STATUSES and new_payment_status != order["payment_status"]:
                conn.execute(
                    """
                    UPDATE orders
                    SET payment_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_payment_status, now, order_id),
                )
                conn.execute(
                    """
                    UPDATE payment_transactions
                    SET status = ?
                    WHERE order_id = ? AND gateway = 'bank_transfer'
                    """,
                    (new_payment_status, order_id),
                )
                payment_note = note or f"Trạng thái thanh toán đã đổi sang {new_payment_status}"
                conn.execute(
                    """
                    INSERT INTO order_status_events (order_id, status, note, admin_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (order_id, new_status if new_status in ORDER_STATUSES else order["status"], payment_note, admin_id, now),
                )
                if not status_email_note:
                    status_email_note = payment_note
                changed = True
            if shipping_provider != order["shipping_provider"] or tracking_code != order["tracking_code"]:
                conn.execute(
                    """
                    UPDATE orders
                    SET shipping_provider = ?, tracking_code = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (shipping_provider, tracking_code, now, order_id),
                )
                tracking_note = "Cập nhật thông tin vận đơn"
                if tracking_code:
                    tracking_note = f"{tracking_note}: {shipping_provider or 'Đơn vị vận chuyển'} - {tracking_code}"
                tracking_event_status = (
                    new_status
                    if changed and new_status in ORDER_STATUSES
                    else order["status"]
                )
                conn.execute(
                    """
                    INSERT INTO order_status_events (order_id, status, note, admin_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (order_id, tracking_event_status, tracking_note, admin_id, now),
                )
                if not status_email_note:
                    status_email_note = tracking_note
                changed = True
            if changed:
                conn.commit()
                order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        items = conn.execute(
            "SELECT * FROM order_items WHERE order_id = ?",
            (order_id,),
        ).fetchall()
        events = conn.execute(
            """
            SELECT order_status_events.*, users.full_name AS admin_name, users.email AS admin_email
            FROM order_status_events
            LEFT JOIN users ON users.id = order_status_events.admin_id
            WHERE order_id = ?
            ORDER BY created_at DESC
            """,
            (order_id,),
        ).fetchall()
        conn.close()
        if status_email_note:
            send_order_status_update(order_id, status_note=status_email_note)
        return render_template(
            "admin/order_detail.html",
            title=f"Đơn hàng {order['order_number']}",
            section="orders",
            order=order,
            items=items,
            events=events,
            statuses=ORDER_STATUSES,
            payment_statuses=PAYMENT_STATUSES,
            bank_transfer=get_bank_transfer_instructions(order),
            error=error,
        )

    @app.route("/admin/orders/<int:order_id>/invoice")
    @admin_required("orders")
    def admin_order_invoice(order_id):
        conn = _get_db()
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        items = conn.execute(
            "SELECT * FROM order_items WHERE order_id = ?", (order_id,)
        ).fetchall()
        conn.close()
        if order is None:
            abort(404)
        return render_template(
            "admin/invoice.html",
            title=f"Hóa đơn {order['order_number']}",
            order=order,
            items=items,
        )

    @app.route("/admin/orders/<int:order_id>/packing-slip")
    @admin_required("orders")
    def admin_order_packing(order_id):
        conn = _get_db()
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        items = conn.execute(
            "SELECT * FROM order_items WHERE order_id = ?", (order_id,)
        ).fetchall()
        conn.close()
        if order is None:
            abort(404)
        return render_template(
            "admin/packing_slip.html",
            title=f"Phiếu giao hàng {order['order_number']}",
            order=order,
            items=items,
        )

    @app.route("/admin/customers")
    @admin_required("customers")
    def admin_customers():
        query_text = request.args.get("q", "").strip()
        group_filter = request.args.get("group", "").strip()
        conn = _get_db()
        status_placeholders = ",".join(["?"] * len(REVENUE_STATUSES))
        sql = f"""
            SELECT users.*, COUNT(orders.id) AS order_count,
                   COALESCE(SUM(orders.total), 0) AS total_spent,
                   MAX(orders.created_at) AS last_order_at
            FROM users
            LEFT JOIN orders ON orders.user_id = users.id AND orders.status IN ({status_placeholders})
            WHERE 1 = 1
        """
        params = list(REVENUE_STATUSES)
        if group_filter:
            sql += " AND users.customer_group = ?"
            params.append(group_filter)
        if query_text:
            sql += " AND (users.email LIKE ? OR users.full_name LIKE ?)"
            like = f"%{query_text}%"
            params.extend([like, like])
        sql += " GROUP BY users.id ORDER BY users.created_at DESC"
        customers = conn.execute(sql, params).fetchall()
        conn.close()
        return render_template(
            "admin/customers.html",
            title="Khách hàng",
            section="customers",
            customers=customers,
            query_text=query_text,
            group_filter=group_filter,
        )

    @app.route("/admin/customers/<int:user_id>", methods=["GET", "POST"])
    @admin_required("customers")
    def admin_customer_detail(user_id):
        conn = _get_db()
        customer = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if customer is None:
            conn.close()
            abort(404)
        if request.method == "POST":
            action = request.form.get("action")
            if action == "update":
                full_name = request.form.get("full_name", "").strip()
                customer_group = request.form.get("customer_group", "").strip()
                conn.execute(
                    """
                    UPDATE users SET full_name = ?, customer_group = ? WHERE id = ?
                    """,
                    (full_name or customer["full_name"], customer_group or None, user_id),
                )
                conn.commit()
            elif action == "toggle_block":
                new_state = 0 if customer["is_blocked"] else 1
                conn.execute(
                    "UPDATE users SET is_blocked = ? WHERE id = ?",
                    (new_state, user_id),
                )
                conn.commit()
            elif action == "add_note":
                note = request.form.get("note", "").strip()
                if note:
                    conn.execute(
                        """
                        INSERT INTO customer_notes (user_id, admin_id, note, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (user_id, _get_current_user()["id"], note, datetime.utcnow().isoformat()),
                    )
                    conn.commit()
            customer = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        orders = conn.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        notes = conn.execute(
            """
            SELECT customer_notes.*, users.full_name AS admin_name, users.email AS admin_email
            FROM customer_notes
            LEFT JOIN users ON users.id = customer_notes.admin_id
            WHERE customer_notes.user_id = ?
            ORDER BY customer_notes.created_at DESC
            """,
            (user_id,),
        ).fetchall()
        conn.close()
        return render_template(
            "admin/customer_detail.html",
            title="Chi tiết khách hàng",
            section="customers",
            customer=customer,
            orders=orders,
            notes=notes,
        )

    @app.route("/admin/inventory")
    @admin_required("inventory")
    def admin_inventory():
        query_text = request.args.get("q", "").strip()
        low_only = request.args.get("low") == "1"
        conn = _get_db()
        sql = """
            SELECT product_variants.*, products.name AS product_name
            FROM product_variants
            JOIN products ON products.id = product_variants.product_id
            WHERE 1 = 1
        """
        params = []
        if query_text:
            sql += " AND (products.name LIKE ? OR product_variants.sku LIKE ?)"
            like = f"%{query_text}%"
            params.extend([like, like])
        if low_only:
            sql += " AND product_variants.stock_qty <= product_variants.low_stock_threshold"
        sql += " ORDER BY products.name, product_variants.size"
        variants = conn.execute(sql, params).fetchall()
        conn.close()
        return render_template(
            "admin/inventory.html",
            title="Quản lý kho",
            section="inventory",
            variants=variants,
            query_text=query_text,
            low_only=low_only,
        )

    @app.route("/admin/inventory/move", methods=["POST"])
    @admin_required("inventory")
    def admin_inventory_move():
        variant_id = _parse_int(request.form.get("variant_id"))
        change_qty = _parse_int(request.form.get("change_qty"))
        reason = request.form.get("reason", "").strip()
        note = request.form.get("note", "").strip()
        if not variant_id or change_qty == 0:
            return redirect(url_for("admin_inventory"))
        conn = _get_db()
        conn.execute(
            "UPDATE product_variants SET stock_qty = stock_qty + ? WHERE id = ?",
            (change_qty, variant_id),
        )
        conn.execute(
            """
            INSERT INTO inventory_movements (variant_id, change_qty, reason, note, admin_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                variant_id,
                change_qty,
                reason,
                note,
                _get_current_user()["id"],
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("admin_inventory"))

    @app.route("/admin/content/banners", methods=["GET", "POST"])
    @admin_required("content")
    def admin_banners():
        conn = _get_db()
        error = None
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            image_url = _normalize_static_path(request.form.get("image_url", ""))
            link_url = request.form.get("link_url", "").strip()
            position = request.form.get("position", "homepage").strip()
            sort_order = _parse_int(request.form.get("sort_order"), 0)
            is_active = 1 if request.form.get("is_active") else 0
            if not image_url:
                error = "Vui lòng nhập URL ảnh."
            else:
                conn.execute(
                    """
                    INSERT INTO banners (title, image_url, link_url, position, sort_order, is_active, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title,
                        image_url,
                        link_url,
                        position,
                        sort_order,
                        is_active,
                        datetime.utcnow().isoformat(),
                    ),
                )
                conn.commit()
        banners = conn.execute(
            "SELECT * FROM banners ORDER BY sort_order, created_at DESC"
        ).fetchall()
        conn.close()
        return render_template(
            "admin/banners.html",
            title="Banner & Slider",
            section="content",
            banners=banners,
            error=error,
        )

    @app.route("/admin/content/banners/<int:banner_id>/delete", methods=["POST"])
    @admin_required("content")
    def admin_banner_delete(banner_id):
        conn = _get_db()
        conn.execute("DELETE FROM banners WHERE id = ?", (banner_id,))
        conn.commit()
        conn.close()
        return redirect(url_for("admin_banners"))

    @app.route("/admin/content/pages")
    @admin_required("content")
    def admin_pages():
        conn = _get_db()
        pages = conn.execute("SELECT * FROM pages ORDER BY updated_at DESC").fetchall()
        conn.close()
        return render_template(
            "admin/pages.html",
            title="Trang tĩnh",
            section="content",
            pages=pages,
        )

    @app.route("/admin/content/pages/new", methods=["GET", "POST"])
    @admin_required("content")
    def admin_page_new():
        conn = _get_db()
        error = None
        page = None
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            slug = request.form.get("slug", "").strip() or _slugify(title)
            body_html = request.form.get("body_html", "").strip()
            status = request.form.get("status", "draft")
            seo_title = request.form.get("seo_title", "").strip()
            seo_description = request.form.get("seo_description", "").strip()
            if not title:
                error = "Vui lòng nhập tiêu đề."
            elif not _validate_unique_slug(conn, "pages", slug):
                error = "Slug đã tồn tại."
            else:
                now = datetime.utcnow().isoformat()
                conn.execute(
                    """
                    INSERT INTO pages (slug, title, body_html, status, seo_title, seo_description, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (slug, title, body_html, status, seo_title, seo_description, now, now),
                )
                conn.commit()
                conn.close()
                return redirect(url_for("admin_pages"))
        conn.close()
        return render_template(
            "admin/page_form.html",
            title="Tạo trang",
            section="content",
            page=page,
            error=error,
        )

    @app.route("/admin/content/pages/<int:page_id>/edit", methods=["GET", "POST"])
    @admin_required("content")
    def admin_page_edit(page_id):
        conn = _get_db()
        page = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        if page is None:
            conn.close()
            abort(404)
        error = None
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            slug = request.form.get("slug", "").strip() or _slugify(title)
            body_html = request.form.get("body_html", "").strip()
            status = request.form.get("status", "draft")
            seo_title = request.form.get("seo_title", "").strip()
            seo_description = request.form.get("seo_description", "").strip()
            if not title:
                error = "Vui lòng nhập tiêu đề."
            elif not _validate_unique_slug(conn, "pages", slug, exclude_id=page_id):
                error = "Slug đã tồn tại."
            else:
                conn.execute(
                    """
                    UPDATE pages
                    SET slug = ?, title = ?, body_html = ?, status = ?, seo_title = ?, seo_description = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (slug, title, body_html, status, seo_title, seo_description, datetime.utcnow().isoformat(), page_id),
                )
                conn.commit()
                page = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        conn.close()
        return render_template(
            "admin/page_form.html",
            title="Chỉnh sửa trang",
            section="content",
            page=page,
            error=error,
        )

    @app.route("/admin/content/pages/<int:page_id>/delete", methods=["POST"])
    @admin_required("content")
    def admin_page_delete(page_id):
        conn = _get_db()
        conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))
        conn.commit()
        conn.close()
        return redirect(url_for("admin_pages"))

    @app.route("/admin/content/posts")
    @admin_required("content")
    def admin_posts():
        conn = _get_db()
        posts = conn.execute("SELECT * FROM posts ORDER BY updated_at DESC").fetchall()
        conn.close()
        return render_template(
            "admin/posts.html",
            title="Blog / Tin tức",
            section="content",
            posts=posts,
        )

    @app.route("/admin/content/posts/new", methods=["GET", "POST"])
    @admin_required("content")
    def admin_post_new():
        conn = _get_db()
        error = None
        post = None
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            slug = request.form.get("slug", "").strip() or _slugify(title)
            excerpt = request.form.get("excerpt", "").strip()
            body_html = request.form.get("body_html", "").strip()
            cover_image = request.form.get("cover_image", "").strip()
            status = request.form.get("status", "draft")
            seo_title = request.form.get("seo_title", "").strip()
            seo_description = request.form.get("seo_description", "").strip()
            if not title:
                error = "Vui lòng nhập tiêu đề."
            elif not _validate_unique_slug(conn, "posts", slug):
                error = "Slug đã tồn tại."
            else:
                now = datetime.utcnow().isoformat()
                published_at = now if status == "published" else None
                conn.execute(
                    """
                    INSERT INTO posts (slug, title, excerpt, body_html, cover_image, status, published_at, seo_title, seo_description, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slug,
                        title,
                        excerpt,
                        body_html,
                        cover_image,
                        status,
                        published_at,
                        seo_title,
                        seo_description,
                        now,
                        now,
                    ),
                )
                conn.commit()
                conn.close()
                return redirect(url_for("admin_posts"))
        conn.close()
        return render_template(
            "admin/post_form.html",
            title="Tạo bài viết",
            section="content",
            post=post,
            error=error,
        )

    @app.route("/admin/content/posts/<int:post_id>/edit", methods=["GET", "POST"])
    @admin_required("content")
    def admin_post_edit(post_id):
        conn = _get_db()
        post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if post is None:
            conn.close()
            abort(404)
        error = None
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            slug = request.form.get("slug", "").strip() or _slugify(title)
            excerpt = request.form.get("excerpt", "").strip()
            body_html = request.form.get("body_html", "").strip()
            cover_image = request.form.get("cover_image", "").strip()
            status = request.form.get("status", "draft")
            seo_title = request.form.get("seo_title", "").strip()
            seo_description = request.form.get("seo_description", "").strip()
            if not title:
                error = "Vui lòng nhập tiêu đề."
            elif not _validate_unique_slug(conn, "posts", slug, exclude_id=post_id):
                error = "Slug đã tồn tại."
            else:
                published_at = post["published_at"]
                if status == "published" and not published_at:
                    published_at = datetime.utcnow().isoformat()
                if status != "published":
                    published_at = None
                conn.execute(
                    """
                    UPDATE posts
                    SET slug = ?, title = ?, excerpt = ?, body_html = ?, cover_image = ?, status = ?, published_at = ?, seo_title = ?, seo_description = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        slug,
                        title,
                        excerpt,
                        body_html,
                        cover_image,
                        status,
                        published_at,
                        seo_title,
                        seo_description,
                        datetime.utcnow().isoformat(),
                        post_id,
                    ),
                )
                conn.commit()
                post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        conn.close()
        return render_template(
            "admin/post_form.html",
            title="Chỉnh sửa bài viết",
            section="content",
            post=post,
            error=error,
        )

    @app.route("/admin/content/posts/<int:post_id>/delete", methods=["POST"])
    @admin_required("content")
    def admin_post_delete(post_id):
        conn = _get_db()
        conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        conn.commit()
        conn.close()
        return redirect(url_for("admin_posts"))

    @app.route("/admin/marketing/coupons", methods=["GET", "POST"])
    @admin_required("marketing")
    def admin_coupons():
        conn = _get_db()
        error = None
        if request.method == "POST":
            code = request.form.get("code", "").strip().upper()
            discount_type = request.form.get("discount_type", "percent")
            value = _parse_int(request.form.get("value"), 0)
            min_order = _parse_int(request.form.get("min_order"), 0)
            max_discount = request.form.get("max_discount")
            starts_at = request.form.get("starts_at") or None
            ends_at = request.form.get("ends_at") or None
            usage_limit = request.form.get("usage_limit") or None
            applies_to = request.form.get("applies_to", "all")
            is_active = 1 if request.form.get("is_active") else 0
            if not code:
                error = "Vui lòng nhập mã giảm giá."
            else:
                try:
                    conn.execute(
                        """
                        INSERT INTO coupons (code, discount_type, value, min_order, max_discount, starts_at, ends_at, usage_limit, is_active, applies_to, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            code,
                            discount_type,
                            value,
                            min_order,
                            _parse_int(max_discount) if max_discount else None,
                            starts_at,
                            ends_at,
                            _parse_int(usage_limit) if usage_limit else None,
                            is_active,
                            applies_to,
                            datetime.utcnow().isoformat(),
                        ),
                    )
                    conn.commit()
                except INTEGRITY_ERRORS:
                    error = "Mã giảm giá đã tồn tại."
        coupons = conn.execute("SELECT * FROM coupons ORDER BY created_at DESC").fetchall()
        conn.close()
        return render_template(
            "admin/coupons.html",
            title="Mã giảm giá",
            section="marketing",
            coupons=coupons,
            error=error,
        )

    @app.route("/admin/marketing/coupons/<int:coupon_id>", methods=["GET", "POST"])
    @admin_required("marketing")
    def admin_coupon_detail(coupon_id):
        conn = _get_db()
        coupon = conn.execute("SELECT * FROM coupons WHERE id = ?", (coupon_id,)).fetchone()
        if coupon is None:
            conn.close()
            abort(404)

        error = None
        if request.method == "POST":
            code = request.form.get("code", "").strip().upper()
            discount_type = request.form.get("discount_type", "percent")
            value = _parse_int(request.form.get("value"), 0)
            min_order = _parse_int(request.form.get("min_order"), 0)
            max_discount = request.form.get("max_discount")
            starts_at = request.form.get("starts_at") or None
            ends_at = request.form.get("ends_at") or None
            usage_limit = request.form.get("usage_limit") or None
            applies_to = request.form.get("applies_to", "all")
            is_active = 1 if request.form.get("is_active") else 0
            duplicate = conn.execute(
                "SELECT id FROM coupons WHERE UPPER(code) = ? AND id != ?",
                (code, coupon_id),
            ).fetchone()
            if not code:
                error = "Vui lòng nhập mã giảm giá."
            elif duplicate:
                error = "Mã giảm giá đã tồn tại."
            else:
                conn.execute(
                    """
                    UPDATE coupons
                    SET code = ?, discount_type = ?, value = ?, min_order = ?, max_discount = ?,
                        starts_at = ?, ends_at = ?, usage_limit = ?, is_active = ?, applies_to = ?
                    WHERE id = ?
                    """,
                    (
                        code,
                        discount_type,
                        value,
                        min_order,
                        _parse_int(max_discount) if max_discount else None,
                        starts_at,
                        ends_at,
                        _parse_int(usage_limit) if usage_limit else None,
                        is_active,
                        applies_to,
                        coupon_id,
                    ),
                )
                conn.commit()
                return redirect(url_for("admin_coupon_detail", coupon_id=coupon_id))

        coupon = conn.execute("SELECT * FROM coupons WHERE id = ?", (coupon_id,)).fetchone()
        conn.close()
        return render_template(
            "admin/coupon_detail.html",
            title=f"Mã giảm giá {coupon['code']}",
            section="marketing",
            coupon=coupon,
            error=error,
        )

    @app.route("/admin/marketing/coupons/<int:coupon_id>/delete", methods=["POST"])
    @admin_required("marketing")
    def admin_coupon_delete(coupon_id):
        conn = _get_db()
        conn.execute("DELETE FROM coupons WHERE id = ?", (coupon_id,))
        conn.commit()
        conn.close()
        return redirect(url_for("admin_coupons"))

    @app.route("/admin/marketing/promotions", methods=["GET", "POST"])
    @admin_required("marketing")
    def admin_promotions():
        conn = _get_db()
        categories = conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
        error = None
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            promo_type = request.form.get("promo_type", "flash")
            discount_type = request.form.get("discount_type", "percent")
            value = _parse_int(request.form.get("value"), 0)
            category_id = request.form.get("category_id") or None
            starts_at = request.form.get("starts_at") or None
            ends_at = request.form.get("ends_at") or None
            is_active = 1 if request.form.get("is_active") else 0
            if not name:
                error = "Vui lòng nhập tên chương trình."
            else:
                conn.execute(
                    """
                    INSERT INTO promotions (name, promo_type, discount_type, value, category_id, starts_at, ends_at, is_active, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        promo_type,
                        discount_type,
                        value,
                        _parse_int(category_id) if category_id else None,
                        starts_at,
                        ends_at,
                        is_active,
                        datetime.utcnow().isoformat(),
                    ),
                )
                conn.commit()
                invalidate_content_cache()
        promotions = conn.execute(
            """
            SELECT promotions.*, categories.name AS category_name
            FROM promotions
            LEFT JOIN categories ON categories.id = promotions.category_id
            ORDER BY promotions.created_at DESC
            """
        ).fetchall()
        conn.close()
        return render_template(
            "admin/promotions.html",
            title="Khuyến mãi / Flash sale",
            section="marketing",
            promotions=promotions,
            categories=categories,
            error=error,
        )

    @app.route("/admin/marketing/promotions/<int:promo_id>", methods=["GET", "POST"])
    @admin_required("marketing")
    def admin_promotion_detail(promo_id):
        conn = _get_db()
        promotion = conn.execute(
            "SELECT * FROM promotions WHERE id = ?", (promo_id,)
        ).fetchone()
        if promotion is None:
            conn.close()
            abort(404)

        categories = conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
        error = None
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            promo_type = request.form.get("promo_type", "flash")
            discount_type = request.form.get("discount_type", "percent")
            value = _parse_int(request.form.get("value"), 0)
            category_id = request.form.get("category_id") or None
            starts_at = request.form.get("starts_at") or None
            ends_at = request.form.get("ends_at") or None
            is_active = 1 if request.form.get("is_active") else 0
            if not name:
                error = "Vui lòng nhập tên chương trình."
            else:
                conn.execute(
                    """
                    UPDATE promotions
                    SET name = ?, promo_type = ?, discount_type = ?, value = ?,
                        category_id = ?, starts_at = ?, ends_at = ?, is_active = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        promo_type,
                        discount_type,
                        value,
                        _parse_int(category_id) if category_id else None,
                        starts_at,
                        ends_at,
                        is_active,
                        promo_id,
                    ),
                )
                conn.commit()
                invalidate_content_cache()
                return redirect(url_for("admin_promotion_detail", promo_id=promo_id))

        promotion = conn.execute(
            """
            SELECT promotions.*, categories.name AS category_name
            FROM promotions
            LEFT JOIN categories ON categories.id = promotions.category_id
            WHERE promotions.id = ?
            """,
            (promo_id,),
        ).fetchone()
        conn.close()
        return render_template(
            "admin/promotion_detail.html",
            title=f"Khuyến mãi {promotion['name']}",
            section="marketing",
            promotion=promotion,
            categories=categories,
            error=error,
        )

    @app.route("/admin/marketing/promotions/<int:promo_id>/delete", methods=["POST"])
    @admin_required("marketing")
    def admin_promotion_delete(promo_id):
        conn = _get_db()
        conn.execute("DELETE FROM promotions WHERE id = ?", (promo_id,))
        conn.commit()
        conn.close()
        invalidate_content_cache()
        return redirect(url_for("admin_promotions"))

    def _character_form_payload():
        name = request.form.get("name", "").strip()
        return {
            "name": name,
            "slug": request.form.get("slug", "").strip() or _slugify(name),
            "nickname": request.form.get("nickname", "").strip(),
            "origin": request.form.get("origin", "").strip(),
            "personality": request.form.get("personality", "").strip(),
            "symbol": request.form.get("symbol", "").strip(),
            "role": request.form.get("role", "").strip(),
            "story_text": request.form.get("story_text", "").strip(),
            "audio_url": _normalize_static_path(request.form.get("audio_url", "")),
            "music_sample_url": _normalize_static_path(request.form.get("music_sample_url", "")),
            "audio_source_url": request.form.get("audio_source_url", "").strip(),
            "image_url": _normalize_static_path(request.form.get("image_url", "")),
            "seo_title": request.form.get("seo_title", "").strip(),
            "seo_description": request.form.get("seo_description", "").strip(),
            "is_active": 1 if request.form.get("is_active") else 0,
        }

    def _character_usage_counts(conn, character_id):
        product_count = conn.execute(
            "SELECT COUNT(*) AS count FROM products WHERE character_id = ?",
            (character_id,),
        ).fetchone()["count"]
        qr_count = conn.execute(
            "SELECT COUNT(*) AS count FROM qr_tags WHERE character_id = ?",
            (character_id,),
        ).fetchone()["count"]
        return product_count, qr_count

    @app.route("/admin/characters")
    @admin_required("characters")
    def admin_characters():
        query_text = request.args.get("q", "").strip()
        status_filter = request.args.get("status", "").strip()
        conn = _get_db()
        sql = """
            SELECT characters.*,
                   COUNT(DISTINCT products.id) AS product_count,
                   COUNT(DISTINCT qr_tags.id) AS qr_count
            FROM characters
            LEFT JOIN products ON products.character_id = characters.id
            LEFT JOIN qr_tags ON qr_tags.character_id = characters.id
            WHERE 1 = 1
        """
        params = []
        if query_text:
            like = f"%{query_text}%"
            sql += " AND (characters.name LIKE ? OR characters.slug LIKE ? OR characters.nickname LIKE ?)"
            params.extend([like, like, like])
        if status_filter == "active":
            sql += " AND characters.is_active = 1"
        elif status_filter == "hidden":
            sql += " AND characters.is_active = 0"
        sql += " GROUP BY characters.id ORDER BY characters.created_at DESC"
        characters = conn.execute(sql, params).fetchall()
        conn.close()
        return render_template(
            "admin/characters.html",
            title="Quản lý nhân vật",
            section="characters",
            characters=characters,
            query_text=query_text,
            status_filter=status_filter,
        )

    @app.route("/admin/characters/new", methods=["GET", "POST"])
    @admin_required("characters")
    def admin_character_new():
        conn = _get_db()
        error = None
        character = None
        if request.method == "POST":
            character = _character_form_payload()
            if not character["name"]:
                error = "Vui lòng nhập tên nhân vật."
            elif not character["slug"]:
                error = "Vui lòng nhập slug nhân vật."
            elif not _validate_unique_slug(conn, "characters", character["slug"]):
                error = "Slug đã tồn tại."
            else:
                now = datetime.utcnow().isoformat()
                fields = (
                    "slug", "name", "nickname", "origin", "personality", "symbol", "role",
                    "story_text", "audio_url", "music_sample_url", "audio_source_url", "seo_title",
                    "seo_description", "image_url", "is_active",
                )
                cur = conn.execute(
                    f"""
                    INSERT INTO characters ({", ".join(fields)}, created_at)
                    VALUES ({", ".join("?" for _ in fields)}, ?)
                    """,
                    tuple(character[field] for field in fields) + (now,),
                )
                character_id = cur.lastrowid
                conn.commit()
                invalidate_content_cache()
                _log_action(
                    _get_current_user()["id"],
                    "create",
                    "character",
                    character_id,
                    {"name": character["name"], "slug": character["slug"]},
                )
                conn.close()
                return redirect(url_for("admin_character_edit", character_id=character_id))
        conn.close()
        return render_template(
            "admin/character_form.html",
            title="Thêm nhân vật",
            section="characters",
            character=character,
            error=error,
            product_count=0,
            qr_count=0,
        )

    @app.route("/admin/characters/<int:character_id>/edit", methods=["GET", "POST"])
    @admin_required("characters")
    def admin_character_edit(character_id):
        conn = _get_db()
        character = conn.execute(
            "SELECT * FROM characters WHERE id = ?", (character_id,)
        ).fetchone()
        if character is None:
            conn.close()
            abort(404)
        error = None
        if request.args.get("delete_error"):
            error = "Không thể xóa nhân vật đang gắn với sản phẩm hoặc mã QR. Hãy chuyển sản phẩm/QR sang nhân vật khác trước."
        if request.method == "POST":
            form_character = _character_form_payload()
            if not form_character["name"]:
                error = "Vui lòng nhập tên nhân vật."
                character = form_character
                character["id"] = character_id
            elif not form_character["slug"]:
                error = "Vui lòng nhập slug nhân vật."
                character = form_character
                character["id"] = character_id
            elif not _validate_unique_slug(conn, "characters", form_character["slug"], exclude_id=character_id):
                error = "Slug đã tồn tại."
                character = form_character
                character["id"] = character_id
            else:
                conn.execute(
                    """
                    UPDATE characters
                    SET slug = ?, name = ?, nickname = ?, origin = ?, personality = ?,
                        symbol = ?, role = ?, story_text = ?, audio_url = ?,
                        music_sample_url = ?, audio_source_url = ?, seo_title = ?, seo_description = ?,
                        image_url = ?, is_active = ?
                    WHERE id = ?
                    """,
                    (
                        form_character["slug"],
                        form_character["name"],
                        form_character["nickname"],
                        form_character["origin"],
                        form_character["personality"],
                        form_character["symbol"],
                        form_character["role"],
                        form_character["story_text"],
                        form_character["audio_url"],
                        form_character["music_sample_url"],
                        form_character["audio_source_url"],
                        form_character["seo_title"],
                        form_character["seo_description"],
                        form_character["image_url"],
                        form_character["is_active"],
                        character_id,
                    ),
                )
                conn.commit()
                invalidate_content_cache()
                _log_action(
                    _get_current_user()["id"],
                    "update",
                    "character",
                    character_id,
                    {"name": form_character["name"], "slug": form_character["slug"]},
                )
                character = conn.execute(
                    "SELECT * FROM characters WHERE id = ?", (character_id,)
                ).fetchone()
        product_count, qr_count = _character_usage_counts(conn, character_id)
        conn.close()
        return render_template(
            "admin/character_form.html",
            title="Chỉnh sửa nhân vật",
            section="characters",
            character=character,
            error=error,
            product_count=product_count,
            qr_count=qr_count,
        )

    @app.route("/admin/characters/<int:character_id>/delete", methods=["POST"])
    @admin_required("characters")
    def admin_character_delete(character_id):
        conn = _get_db()
        character = conn.execute(
            "SELECT id, name, slug FROM characters WHERE id = ?",
            (character_id,),
        ).fetchone()
        if character is None:
            conn.close()
            abort(404)
        product_count, qr_count = _character_usage_counts(conn, character_id)
        if product_count or qr_count:
            conn.close()
            return redirect(url_for("admin_character_edit", character_id=character_id, delete_error=1))
        conn.execute("DELETE FROM characters WHERE id = ?", (character_id,))
        conn.commit()
        conn.close()
        invalidate_content_cache()
        _log_action(
            _get_current_user()["id"],
            "delete",
            "character",
            character_id,
            {"name": character["name"], "slug": character["slug"]},
        )
        return redirect(url_for("admin_characters"))

    def _load_qr_manager_options():
        conn = _get_db()
        variants = conn.execute(
            """
            SELECT product_variants.id, product_variants.sku, product_variants.size, product_variants.color,
                   products.name AS product_name
            FROM product_variants
            JOIN products ON products.id = product_variants.product_id
            WHERE product_variants.is_active = 1
            ORDER BY products.name, product_variants.sku
            """
        ).fetchall()
        characters = conn.execute(
            "SELECT id, name, slug FROM characters ORDER BY name"
        ).fetchall()
        conn.close()
        return variants, characters

    @app.route("/admin/qr")
    @admin_required("qr")
    def admin_qr():
        batch_code = request.args.get("batch_code", "").strip()
        character_id = _parse_int(request.args.get("character_id"), 0)
        date_from = (request.args.get("date_from") or "").strip()
        date_to = (request.args.get("date_to") or "").strip()
        notice = (request.args.get("notice") or "").strip()

        variants, characters = _load_qr_manager_options()
        stats = get_qr_stats(
            batch_code=batch_code or None,
            character_id=character_id or None,
            date_from=date_from or None,
            date_to=date_to or None,
        )
        batches = list_qr_batches(limit=100)
        selected_batch = batch_code
        if not selected_batch and batches:
            selected_batch = batches[0]["batch_code"] or ""
        tokens = get_batch_tokens(selected_batch, limit=1000) if selected_batch else []

        return render_template(
            "admin/qr.html",
            title="QR Manager",
            section="qr",
            notice=notice,
            variants=variants,
            characters=characters,
            batches=batches,
            tokens=tokens,
            selected_batch=selected_batch,
            filters={
                "batch_code": batch_code,
                "character_id": character_id,
                "date_from": date_from,
                "date_to": date_to,
            },
            stats=stats,
        )

    @app.route("/admin/qr/new", methods=["GET", "POST"])
    @admin_required("qr")
    def admin_qr_new():
        variants, characters = _load_qr_manager_options()
        error = None
        form_values = {
            "variant_id": "",
            "character_id": "",
            "batch_code": f"NE-{datetime.utcnow().year}-{datetime.utcnow().month:02d}",
            "quantity": "200",
        }
        if request.method == "POST":
            form_values["variant_id"] = request.form.get("variant_id", "").strip()
            form_values["character_id"] = request.form.get("character_id", "").strip()
            form_values["batch_code"] = request.form.get("batch_code", "").strip()
            form_values["quantity"] = request.form.get("quantity", "").strip()

            variant_id = _parse_int(form_values["variant_id"], 0)
            character_id = _parse_int(form_values["character_id"], 0)
            quantity = _parse_int(form_values["quantity"], 0)
            batch_code = form_values["batch_code"]

            if not variant_id or not character_id:
                error = "Vui lòng chọn biến thể và nhân vật."
            elif not batch_code:
                error = "Vui lòng nhập batch code."
            elif quantity <= 0:
                error = "Số lượng phải lớn hơn 0."
            elif quantity > 5000:
                error = "Số lượng tối đa cho mỗi batch là 5000."
            else:
                try:
                    created = create_qr_batch(
                        variant_id=variant_id,
                        character_id=character_id,
                        batch_code=batch_code,
                        quantity=quantity,
                    )
                except (RuntimeError, ValueError) as exc:
                    error = str(exc)
                else:
                    notice = f"Đã tạo {len(created)} QR token cho batch {batch_code}."
                    return redirect(url_for("admin_qr", batch_code=batch_code, notice=notice))

        return render_template(
            "admin/qr_new.html",
            title="Tạo Batch QR",
            section="qr",
            variants=variants,
            characters=characters,
            form_values=form_values,
            error=error,
        )

    @app.route("/admin/qr/<token>/disable", methods=["POST"])
    @admin_required("qr")
    def admin_qr_disable(token):
        batch_code = (request.form.get("batch_code") or "").strip()
        disable_qr_token(token)
        if batch_code:
            return redirect(url_for("admin_qr", batch_code=batch_code))
        return redirect(url_for("admin_qr"))

    @app.route("/admin/qr/export/<batch_code>")
    @admin_required("qr")
    def admin_qr_export(batch_code):
        export_format = (request.args.get("format") or "zip").strip().lower()
        if export_format == "pdf":
            pdf_buffer = export_qr_pdf_sheet(batch_code, request.url_root.rstrip("/"))
            if pdf_buffer is None:
                abort(501)
            return send_file(
                pdf_buffer,
                mimetype="application/pdf",
                download_name=f"qr-{batch_code}.pdf",
                as_attachment=True,
            )
        try:
            zip_buffer, _ = export_qr_png_zip(
                batch_code=batch_code,
                base_url=request.url_root.rstrip("/"),
            )
        except ValueError:
            abort(404)
        except RuntimeError:
            abort(500)
        safe_batch = batch_code.replace("/", "-").replace("\\", "-")
        return send_file(
            zip_buffer,
            mimetype="application/zip",
            download_name=f"qr-{safe_batch}.zip",
            as_attachment=True,
        )

    @app.route("/admin/qr-tags", methods=["GET", "POST"])
    @admin_required("qr")
    def admin_qr_tags():
        if request.method == "POST":
            return redirect(url_for("admin_qr_new"))
        return redirect(url_for("admin_qr"))

    @app.route("/admin/qr-tags/<int:qr_id>/download")
    @admin_required("qr")
    def admin_qr_download(qr_id):
        conn = _get_db()
        tag = conn.execute(
            "SELECT token, serial_no FROM qr_tags WHERE id = ?",
            (qr_id,),
        ).fetchone()
        conn.close()
        if tag is None:
            abort(404)
        qr_url = request.url_root.rstrip("/") + url_for("qr_short_redirect", token=tag["token"])
        png_bytes = _generate_qr_png(qr_url)
        if png_bytes is None:
            placeholder = os.path.join(BASE_DIR, "static", "images", "qr_placeholder.png")
            return send_file(
                placeholder,
                mimetype="image/png",
                download_name=f"qr-{tag['token']}.png",
                as_attachment=True,
            )
        filename = (tag["serial_no"] or tag["token"]).replace("/", "-").replace("\\", "-")
        return send_file(
            io.BytesIO(png_bytes),
            mimetype="image/png",
            download_name=f"{filename}.png",
            as_attachment=True,
        )

    @app.route("/admin/qr-tags/<int:qr_id>/delete", methods=["POST"])
    @admin_required("qr")
    def admin_qr_delete(qr_id):
        conn = _get_db()
        tag = conn.execute(
            "SELECT token, batch_code FROM qr_tags WHERE id = ?",
            (qr_id,),
        ).fetchone()
        conn.close()
        if tag:
            disable_qr_token(tag["token"])
            if tag["batch_code"]:
                return redirect(url_for("admin_qr", batch_code=tag["batch_code"]))
        return redirect(url_for("admin_qr"))

    @app.route("/admin/reports")
    @admin_required("reports")
    def admin_reports():
        conn = _get_db()
        status_placeholders = ",".join(["?"] * len(REVENUE_STATUSES))
        daily_rows = conn.execute(
            f"""
            SELECT date(created_at) AS day, COUNT(*) AS orders, COALESCE(SUM(total), 0) AS revenue
            FROM orders
            WHERE status IN ({status_placeholders})
            GROUP BY date(created_at)
            ORDER BY day DESC
            LIMIT 30
            """,
            (*REVENUE_STATUSES,),
        ).fetchall()
        monthly_rows = conn.execute(
            f"""
            SELECT strftime('%Y-%m', created_at) AS month, COUNT(*) AS orders, COALESCE(SUM(total), 0) AS revenue
            FROM orders
            WHERE status IN ({status_placeholders})
            GROUP BY strftime('%Y-%m', created_at)
            ORDER BY month DESC
            LIMIT 12
            """,
            (*REVENUE_STATUSES,),
        ).fetchall()
        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM orders GROUP BY status"
        ).fetchall()
        top_products = conn.execute(
            f"""
            SELECT product_name, SUM(qty) AS qty_sold, SUM(total_price) AS revenue
            FROM order_items
            JOIN orders ON orders.id = order_items.order_id
            WHERE orders.status IN ({status_placeholders})
            GROUP BY product_name
            ORDER BY qty_sold DESC
            LIMIT 10
            """,
            (*REVENUE_STATUSES,),
        ).fetchall()
        slow_inventory = conn.execute(
            f"""
            SELECT product_variants.*, products.name AS product_name,
                   MAX(orders.created_at) AS last_sold
            FROM product_variants
            JOIN products ON products.id = product_variants.product_id
            LEFT JOIN order_items ON order_items.variant_id = product_variants.id
            LEFT JOIN orders ON orders.id = order_items.order_id AND orders.status IN ({status_placeholders})
            GROUP BY product_variants.id
            ORDER BY last_sold IS NOT NULL, last_sold ASC
            LIMIT 10
            """,
            (*REVENUE_STATUSES,),
        ).fetchall()
        repeat_customers = conn.execute(
            f"""
            SELECT users.id, users.email, users.full_name,
                   COUNT(orders.id) AS order_count,
                   COALESCE(SUM(orders.total), 0) AS total_spent
            FROM users
            JOIN orders ON orders.user_id = users.id AND orders.status IN ({status_placeholders})
            GROUP BY users.id
            HAVING COUNT(orders.id) >= 2
            ORDER BY total_spent DESC
            LIMIT 10
            """,
            (*REVENUE_STATUSES,),
        ).fetchall()
        qr_by_day = conn.execute(
            """
            SELECT date(scanned_at) AS day, COUNT(*) AS scans
            FROM qr_scans
            GROUP BY date(scanned_at)
            ORDER BY day DESC
            LIMIT 14
            """
        ).fetchall()
        qr_by_character = conn.execute(
            """
            SELECT characters.name AS character_name, COUNT(qr_scans.id) AS scans
            FROM qr_scans
            JOIN qr_tags ON qr_tags.id = qr_scans.qr_tag_id
            JOIN characters ON characters.id = qr_tags.character_id
            GROUP BY characters.id
            ORDER BY scans DESC
            LIMIT 10
            """
        ).fetchall()
        conn.close()
        return render_template(
            "admin/reports.html",
            title="Báo cáo",
            section="reports",
            daily_rows=daily_rows,
            monthly_rows=monthly_rows,
            status_rows=status_rows,
            top_products=top_products,
            slow_inventory=slow_inventory,
            repeat_customers=repeat_customers,
            qr_by_day=qr_by_day,
            qr_by_character=qr_by_character,
        )

    @app.route("/admin/settings", methods=["GET", "POST"])
    @admin_required("settings")
    def admin_settings():
        conn = _get_db()
        if request.method == "POST":
            _set_setting(conn, "store_name", request.form.get("store_name", "").strip())
            _set_setting(conn, "store_email", request.form.get("store_email", "").strip())
            _set_setting(conn, "shipping_fee", request.form.get("shipping_fee", "").strip())
            _set_setting(conn, "free_shipping_threshold", request.form.get("free_shipping_threshold", "").strip())
            _set_setting(conn, "payment_method", request.form.get("payment_method", "").strip())
            _set_setting(conn, "sms_provider", request.form.get("sms_provider", "").strip())
            _set_setting(conn, "email_provider", request.form.get("email_provider", "").strip())
            conn.commit()
        settings = {
            "store_name": _get_setting(conn, "store_name", ""),
            "store_email": _get_setting(conn, "store_email", ""),
            "shipping_fee": _get_setting(conn, "shipping_fee", "0"),
            "free_shipping_threshold": _get_setting(conn, "free_shipping_threshold", "0"),
            "payment_method": _get_setting(conn, "payment_method", ""),
            "sms_provider": _get_setting(conn, "sms_provider", ""),
            "email_provider": _get_setting(conn, "email_provider", ""),
        }
        conn.close()
        return render_template(
            "admin/settings.html",
            title="Cài đặt",
            section="settings",
            settings=settings,
        )

    @app.route("/admin/users", methods=["GET", "POST"])
    @admin_required("users")
    def admin_users():
        conn = _get_db()
        error = None
        if request.method == "POST":
            action = request.form.get("action")
            if action == "create":
                email = request.form.get("email", "").strip().lower()
                password = request.form.get("password", "")
                full_name = request.form.get("full_name", "").strip()
                role = request.form.get("role", "staff")
                if not email or not password:
                    error = "Vui lòng nhập email và mật khẩu."
                elif _get_user_by_email(email):
                    error = "Email đã tồn tại."
                else:
                    _create_user(
                        email,
                        generate_password_hash(password),
                        full_name=full_name,
                        is_verified=1,
                        role=role,
                    )
            elif action == "update_role":
                user_id = _parse_int(request.form.get("user_id"))
                role = request.form.get("role", "staff")
                conn.execute(
                    "UPDATE users SET role = ? WHERE id = ?",
                    (role, user_id),
                )
                conn.commit()
            elif action == "toggle_block":
                user_id = _parse_int(request.form.get("user_id"))
                user_row = conn.execute(
                    "SELECT is_blocked FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                if user_row:
                    new_state = 0 if user_row["is_blocked"] else 1
                    conn.execute(
                        "UPDATE users SET is_blocked = ? WHERE id = ?",
                        (new_state, user_id),
                    )
                    conn.commit()
        roles = sorted(ROLE_PERMISSIONS.keys())
        role_placeholders = ",".join(["?"] * len(roles))
        admin_emails = list(_admin_email_allowlist())
        if admin_emails:
            email_placeholders = ",".join(["?"] * len(admin_emails))
            sql = (
                f"SELECT id, email, full_name, role, is_blocked, created_at FROM users "
                f"WHERE role IN ({role_placeholders}) OR email IN ({email_placeholders}) "
                "ORDER BY created_at DESC"
            )
            params = roles + admin_emails
        else:
            sql = (
                f"SELECT id, email, full_name, role, is_blocked, created_at FROM users "
                f"WHERE role IN ({role_placeholders}) ORDER BY created_at DESC"
            )
            params = roles
        users = conn.execute(sql, params).fetchall()
        conn.close()
        return render_template(
            "admin/users.html",
            title="Tài khoản admin/staff",
            section="users",
            users=users,
            roles=roles,
            error=error,
        )

    @app.route("/admin/audit")
    @admin_required("settings")
    def admin_audit():
        conn = _get_db()
        logs = conn.execute(
            """
            SELECT audit_logs.*, users.email AS admin_email
            FROM audit_logs
            LEFT JOIN users ON users.id = audit_logs.admin_id
            ORDER BY audit_logs.created_at DESC
            LIMIT 100
            """
        ).fetchall()
        conn.close()
        return render_template(
            "admin/audit.html",
            title="Nhật ký hoạt động",
            section="settings",
            logs=logs,
        )
