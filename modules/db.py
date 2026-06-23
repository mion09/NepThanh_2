import os
import sqlite3
import time
from collections.abc import Mapping
from datetime import datetime
from werkzeug.security import generate_password_hash

try:
    import libsql
except ImportError:
    libsql = None

from modules.config import BASE_DIR, DB_PATH, TURSO_AUTH_TOKEN, TURSO_DATABASE_URL, USE_TURSO


if USE_TURSO and libsql is None:
    raise RuntimeError(
        "Turso is configured but the 'libsql' package is not installed."
    )


INTEGRITY_ERRORS = tuple(
    error_type
    for error_type in (
        sqlite3.IntegrityError,
        getattr(libsql, "IntegrityError", None),
    )
    if error_type is not None
)

_TURSO_SYNC_INTERVAL_SECONDS = max(
    0,
    int((os.environ.get("TURSO_SYNC_INTERVAL_SECONDS") or "15").strip() or "15"),
)
_LAST_TURSO_SYNC_AT = 0.0
_SCHEMA_BOOTSTRAP_VERSION = "2026-06-22-beige-shirt-color-images"

STATIC_PRODUCT_IMAGES = (
    {
        "product_slug": "ao-co-cheo",
        "url": "images/shirt/co_cheo_black.jpg",
        "color": "black",
        "sort_order": 0,
        "is_primary": 1,
        "alt_text": "Co Cheo black shirt",
    },
    {
        "product_slug": "ao-anh-hai",
        "url": "images/shirt/anh_hai_black.png",
        "color": "black",
        "sort_order": 10,
        "is_primary": 0,
        "alt_text": "Anh Hai black shirt",
    },
    {
        "product_slug": "ao-chang-khen",
        "url": "images/shirt/chang_khen_black.jpg",
        "color": "black",
        "sort_order": 0,
        "is_primary": 1,
        "alt_text": "Chang Khen black shirt",
    },
    {
        "product_slug": "ao-chang-khen",
        "url": "images/shirt/chang_khen_white.jpg",
        "color": "white",
        "sort_order": 20,
        "is_primary": 0,
        "alt_text": "Chang Khen white shirt",
    },
    {
        "product_slug": "ao-chang-khen",
        "url": "images/shirt/chang_khen_be.jpg",
        "color": "beige",
        "sort_order": 30,
        "is_primary": 0,
        "alt_text": "Chang Khen beige shirt",
    },
    {
        "product_slug": "ao-chu-xam",
        "url": "images/shirt/chu_xam_white.png",
        "color": "white",
        "sort_order": 10,
        "is_primary": 0,
        "alt_text": "Chu Xam white shirt",
    },
    {
        "product_slug": "ao-co-cheo",
        "url": "images/shirt/co_cheo_white.jpg",
        "color": "white",
        "sort_order": 20,
        "is_primary": 0,
        "alt_text": "Co Cheo white shirt",
    },
    {
        "product_slug": "ao-co-cheo",
        "url": "images/shirt/co_cheo_be.png",
        "color": "beige",
        "sort_order": 30,
        "is_primary": 0,
        "alt_text": "Co Cheo beige shirt",
    },
    {
        "product_slug": "ao-nang-then",
        "url": "images/shirt/nang_then_black.jpg",
        "color": "black",
        "sort_order": 0,
        "is_primary": 1,
        "alt_text": "Nang Then black shirt",
    },
    {
        "product_slug": "ao-nang-then",
        "url": "images/shirt/nang_then_white.jpg",
        "color": "white",
        "sort_order": 20,
        "is_primary": 0,
        "alt_text": "Nang Then white shirt",
    },
    {
        "product_slug": "ao-nang-then",
        "url": "images/shirt/nang_then_be.jpg",
        "color": "beige",
        "sort_order": 30,
        "is_primary": 0,
        "alt_text": "Nang Then beige shirt",
    },
    {
        "product_slug": "ao-be-roi",
        "url": "images/shirt/mua_roi_white.png",
        "color": "white",
        "sort_order": 10,
        "is_primary": 0,
        "alt_text": "Be Roi white shirt",
    },
)


class ManagedConnection:
    def __init__(self, conn, sync_enabled=False):
        self._conn = conn
        self._sync_enabled = sync_enabled

    def execute(self, sql, parameters=()):
        return ManagedCursor(self._conn.execute(sql, parameters))

    def commit(self):
        self._conn.commit()
        if self._sync_enabled:
            _maybe_sync_turso(self._conn, force=True)

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class ManagedCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def lastrowid(self):
        return getattr(self._cursor, "lastrowid", None)

    @property
    def rowcount(self):
        return getattr(self._cursor, "rowcount", -1)

    @property
    def description(self):
        return getattr(self._cursor, "description", None)

    def fetchone(self):
        row = self._cursor.fetchone()
        return _normalize_row(row, self.description)

    def fetchall(self):
        return [_normalize_row(row, self.description) for row in self._cursor.fetchall()]

    def __iter__(self):
        for row in self._cursor:
            yield _normalize_row(row, self.description)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class ManagedRow(Mapping):
    def __init__(self, data, values):
        self._data = data
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def keys(self):
        return self._data.keys()

    def get(self, key, default=None):
        return self._data.get(key, default)


def _normalize_row(row, description):
    if row is None:
        return None
    if isinstance(row, ManagedRow):
        return row
    if isinstance(row, sqlite3.Row):
        data = {key: row[key] for key in row.keys()}
        values = tuple(row[idx] for idx in range(len(row)))
        return ManagedRow(data, values)
    if isinstance(row, Mapping):
        data = dict(row)
        values = tuple(data.values())
        return ManagedRow(data, values)
    if description:
        columns = [col[0] for col in description]
        values = tuple(row)
        data = {column: values[idx] for idx, column in enumerate(columns)}
        return ManagedRow(data, values)
    values = tuple(row) if isinstance(row, (list, tuple)) else (row,)
    data = {str(idx): value for idx, value in enumerate(values)}
    return ManagedRow(data, values)


def _get_db():
    if USE_TURSO:
        conn = _connect_turso()
        _maybe_sync_turso(conn)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return ManagedConnection(conn, sync_enabled=USE_TURSO)


def _connect_turso():
    try:
        return libsql.connect(
            DB_PATH,
            sync_url=TURSO_DATABASE_URL,
            auth_token=TURSO_AUTH_TOKEN,
        )
    except ValueError as exc:
        if "invalid local state" not in str(exc).lower():
            raise
        _remove_turso_replica_files(DB_PATH)
        return libsql.connect(
            DB_PATH,
            sync_url=TURSO_DATABASE_URL,
            auth_token=TURSO_AUTH_TOKEN,
        )


def _remove_turso_replica_files(path):
    for suffix in ("", "-shm", "-wal", "-info"):
        candidate = f"{path}{suffix}"
        try:
            if os.path.exists(candidate):
                os.remove(candidate)
        except OSError:
            pass


def _maybe_sync_turso(conn, force=False):
    global _LAST_TURSO_SYNC_AT
    if not USE_TURSO:
        return False
    now = time.monotonic()
    if not force and _LAST_TURSO_SYNC_AT:
        if now - _LAST_TURSO_SYNC_AT < _TURSO_SYNC_INTERVAL_SECONDS:
            return False
    conn.sync()
    _LAST_TURSO_SYNC_AT = now
    return True


def _ensure_column(conn, table, column, column_sql):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        return
    if any(row["name"] == column for row in rows):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_sql}")


def _create_base_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            full_name TEXT,
            is_verified INTEGER NOT NULL DEFAULT 0,
            role TEXT DEFAULT 'customer',
            phone TEXT,
            is_blocked INTEGER DEFAULT 0,
            customer_group TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS characters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            nickname TEXT,
            origin TEXT,
            personality TEXT,
            symbol TEXT,
            role TEXT,
            story_text TEXT,
            audio_url TEXT,
            music_sample_url TEXT,
            audio_source_url TEXT,
            seo_title TEXT,
            seo_description TEXT,
            image_url TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            long_description TEXT,
            character_id INTEGER,
            collection TEXT,
            base_price INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft',
            seo_title TEXT,
            seo_description TEXT,
            is_featured INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            FOREIGN KEY(character_id) REFERENCES characters(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            sku TEXT UNIQUE,
            size TEXT,
            color TEXT,
            price INTEGER,
            stock_qty INTEGER NOT NULL DEFAULT 0,
            weight_grams INTEGER NOT NULL DEFAULT 250,
            is_active INTEGER NOT NULL DEFAULT 1,
            low_stock_threshold INTEGER DEFAULT 5,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            alt_text TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            color TEXT,
            is_primary INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
        )
        """
    )


def init_db():
    conn = _get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    if _get_setting(conn, "_schema_bootstrap_version") == _SCHEMA_BOOTSTRAP_VERSION:
        if _ensure_admin_user(conn):
            conn.commit()
        conn.close()
        return

    _create_base_tables(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT UNIQUE NOT NULL,
            user_id INTEGER,
            status TEXT NOT NULL DEFAULT 'new',
            subtotal INTEGER NOT NULL DEFAULT 0,
            shipping_fee INTEGER NOT NULL DEFAULT 0,
            discount_amount INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            payment_status TEXT DEFAULT 'unpaid',
            recipient_name TEXT,
            email TEXT,
            phone TEXT,
            line1 TEXT,
            line2 TEXT,
            ward TEXT,
            district TEXT,
            province TEXT,
            country TEXT DEFAULT 'VN',
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            canceled_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER,
            variant_id INTEGER,
            product_name TEXT NOT NULL,
            variant_label TEXT,
            sku TEXT,
            qty INTEGER NOT NULL DEFAULT 1,
            unit_price INTEGER NOT NULL DEFAULT 0,
            total_price INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE SET NULL,
            FOREIGN KEY(variant_id) REFERENCES product_variants(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_status_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            note TEXT,
            admin_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,
            FOREIGN KEY(admin_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            description TEXT,
            parent_id INTEGER,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(parent_id) REFERENCES categories(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_categories (
            product_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            PRIMARY KEY(product_id, category_id),
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_tags (
            product_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY(product_id, tag_id),
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS banners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            image_url TEXT NOT NULL,
            link_url TEXT,
            position TEXT DEFAULT 'homepage',
            sort_order INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            body_html TEXT,
            status TEXT DEFAULT 'draft',
            seo_title TEXT,
            seo_description TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            excerpt TEXT,
            body_html TEXT,
            cover_image TEXT,
            status TEXT DEFAULT 'draft',
            published_at TEXT,
            seo_title TEXT,
            seo_description TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            description TEXT,
            image_url TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coupons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            discount_type TEXT NOT NULL,
            value INTEGER NOT NULL,
            min_order INTEGER DEFAULT 0,
            max_discount INTEGER,
            starts_at TEXT,
            ends_at TEXT,
            usage_limit INTEGER,
            used_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            applies_to TEXT DEFAULT 'all',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coupon_products (
            coupon_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            PRIMARY KEY(coupon_id, product_id),
            FOREIGN KEY(coupon_id) REFERENCES coupons(id) ON DELETE CASCADE,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coupon_categories (
            coupon_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            PRIMARY KEY(coupon_id, category_id),
            FOREIGN KEY(coupon_id) REFERENCES coupons(id) ON DELETE CASCADE,
            FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS promotions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            promo_type TEXT NOT NULL,
            discount_type TEXT NOT NULL,
            value INTEGER NOT NULL,
            category_id INTEGER,
            starts_at TEXT,
            ends_at TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inventory_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant_id INTEGER NOT NULL,
            change_qty INTEGER NOT NULL,
            reason TEXT,
            note TEXT,
            admin_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(variant_id) REFERENCES product_variants(id) ON DELETE CASCADE,
            FOREIGN KEY(admin_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            action TEXT NOT NULL,
            entity_type TEXT,
            entity_id INTEGER,
            details_json TEXT,
            ip_address TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(admin_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            admin_id INTEGER,
            note TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(admin_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS addresses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            recipient_name TEXT,
            phone TEXT,
            line1 TEXT NOT NULL,
            line2 TEXT,
            ward TEXT,
            district TEXT NOT NULL,
            province TEXT NOT NULL,
            country TEXT DEFAULT 'VN',
            is_default INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qr_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            variant_id INTEGER NOT NULL,
            character_id INTEGER NOT NULL,
            batch_code TEXT,
            serial_no TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'disabled', 'expired')),
            created_at TEXT NOT NULL,
            FOREIGN KEY(variant_id) REFERENCES product_variants(id) ON DELETE CASCADE,
            FOREIGN KEY(character_id) REFERENCES characters(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qr_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qr_tag_id INTEGER NOT NULL,
            user_id INTEGER,
            scanned_at TEXT NOT NULL,
            ip_hash TEXT,
            user_agent TEXT,
            referrer TEXT,
            utm_source TEXT,
            utm_campaign TEXT,
            FOREIGN KEY(qr_tag_id) REFERENCES qr_tags(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    _ensure_column(conn, "users", "role", "TEXT DEFAULT 'customer'")
    _ensure_column(conn, "users", "is_verified", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "users", "phone", "TEXT")
    _ensure_column(conn, "users", "is_blocked", "INTEGER DEFAULT 0")
    _ensure_column(conn, "users", "customer_group", "TEXT")
    _ensure_column(conn, "users", "notes", "TEXT")
    _ensure_column(conn, "products", "seo_title", "TEXT")
    _ensure_column(conn, "products", "seo_description", "TEXT")
    _ensure_column(conn, "products", "long_description", "TEXT")
    _ensure_column(conn, "products", "is_featured", "INTEGER DEFAULT 0")
    _ensure_column(conn, "products", "collection", "TEXT")
    _ensure_column(conn, "product_variants", "low_stock_threshold", "INTEGER DEFAULT 5")
    _ensure_column(conn, "product_images", "color", "TEXT")
    _ensure_column(conn, "product_images", "is_primary", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "characters", "nickname", "TEXT")
    _ensure_column(conn, "characters", "origin", "TEXT")
    _ensure_column(conn, "characters", "personality", "TEXT")
    _ensure_column(conn, "characters", "symbol", "TEXT")
    _ensure_column(conn, "characters", "role", "TEXT")
    _ensure_column(conn, "characters", "story_text", "TEXT")
    _ensure_column(conn, "characters", "audio_url", "TEXT")
    _ensure_column(conn, "characters", "music_sample_url", "TEXT")
    _ensure_column(conn, "characters", "audio_source_url", "TEXT")
    _ensure_column(conn, "characters", "seo_title", "TEXT")
    _ensure_column(conn, "characters", "seo_description", "TEXT")
    _ensure_column(conn, "characters", "image_url", "TEXT")
    _ensure_column(conn, "characters", "is_active", "INTEGER DEFAULT 1")
    _ensure_column(conn, "orders", "shipping_provider", "TEXT")
    _ensure_column(conn, "orders", "tracking_code", "TEXT")
    _ensure_column(conn, "qr_tags", "batch_code", "TEXT")
    _ensure_column(conn, "qr_tags", "serial_no", "TEXT")
    _ensure_column(conn, "qr_tags", "status", "TEXT DEFAULT 'active'")
    _ensure_column(conn, "qr_tags", "created_at", "TEXT")
    _ensure_column(conn, "qr_scans", "ip_hash", "TEXT")
    _ensure_column(conn, "qr_scans", "user_agent", "TEXT")
    _ensure_column(conn, "qr_scans", "referrer", "TEXT")
    _ensure_column(conn, "qr_scans", "utm_source", "TEXT")
    _ensure_column(conn, "qr_scans", "utm_campaign", "TEXT")
    conn.execute("UPDATE users SET role = 'customer' WHERE role IS NULL")
    conn.execute("UPDATE characters SET is_active = 1 WHERE is_active IS NULL")
    conn.execute("UPDATE qr_tags SET status = lower(status) WHERE status IS NOT NULL")
    conn.execute("UPDATE qr_tags SET status = 'disabled' WHERE status = 'inactive'")
    conn.execute(
        "UPDATE qr_tags SET status = 'active' WHERE status IS NULL OR status = ''"
    )
    conn.execute(
        """
        UPDATE qr_tags
        SET status = 'disabled'
        WHERE status NOT IN ('active', 'disabled', 'expired')
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qr_tags_token ON qr_tags(token)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qr_scans_tag_time ON qr_scans(qr_tag_id, scanned_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qr_scans_tag_ip_time ON qr_scans(qr_tag_id, ip_hash, scanned_at)"
    )
    conn.execute(
        """
        UPDATE product_images
        SET is_primary = 1
        WHERE id IN (
            SELECT MIN(candidate.id)
            FROM product_images candidate
            WHERE NOT EXISTS (
                SELECT 1
                FROM product_images primary_image
                WHERE primary_image.product_id = candidate.product_id
                  AND primary_image.is_primary = 1
            )
            GROUP BY candidate.product_id
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_product_images_one_primary ON product_images(product_id) WHERE is_primary = 1"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_product_images_color_order ON product_images(product_id, color, sort_order, id)"
    )

    _ensure_static_product_images(conn)
    _set_setting(conn, "_schema_bootstrap_version", _SCHEMA_BOOTSTRAP_VERSION)
    _ensure_admin_user(conn)
    conn.commit()
    conn.close()


def _static_product_image_file_exists(url):
    if not url or "://" in url:
        return True
    clean_url = url.strip().replace("\\", "/")
    if clean_url.startswith("/static/"):
        clean_url = clean_url[len("/static/"):]
    elif clean_url.startswith("static/"):
        clean_url = clean_url[len("static/"):]
    return os.path.isfile(os.path.join(BASE_DIR, "static", *clean_url.lstrip("/").split("/")))


def _missing_static_product_image(row):
    return row is not None and not _static_product_image_file_exists(row["url"])


def _find_stale_product_image(conn, product_id, color, prefer_primary=False):
    rows = conn.execute(
        """
        SELECT id, url, is_primary
        FROM product_images
        WHERE product_id = ?
          AND COALESCE(color, '') = COALESCE(?, '')
        ORDER BY is_primary DESC, sort_order, id
        """,
        (product_id, color),
    ).fetchall()
    for row in rows:
        if _missing_static_product_image(row):
            return row
    if not prefer_primary:
        return None
    primary = conn.execute(
        """
        SELECT id, url, is_primary
        FROM product_images
        WHERE product_id = ? AND is_primary = 1
        ORDER BY sort_order, id
        LIMIT 1
        """,
        (product_id,),
    ).fetchone()
    if _missing_static_product_image(primary):
        return primary
    return None


def _should_make_static_image_primary(conn, product_id, image):
    if not image["is_primary"]:
        return False
    current_primary = conn.execute(
        """
        SELECT id, url, is_primary
        FROM product_images
        WHERE product_id = ? AND is_primary = 1
        ORDER BY sort_order, id
        LIMIT 1
        """,
        (product_id,),
    ).fetchone()
    return current_primary is None or _missing_static_product_image(current_primary)


def _set_static_product_image_primary(conn, product_id, image_id):
    conn.execute("UPDATE product_images SET is_primary = 0 WHERE product_id = ?", (product_id,))
    conn.execute(
        "UPDATE product_images SET is_primary = 1 WHERE id = ? AND product_id = ?",
        (image_id, product_id),
    )


def _ensure_static_product_images(conn):
    for image in STATIC_PRODUCT_IMAGES:
        absolute_path = os.path.join(BASE_DIR, "static", *image["url"].split("/"))
        if not os.path.isfile(absolute_path):
            continue
        product = conn.execute(
            "SELECT id FROM products WHERE slug = ?",
            (image["product_slug"],),
        ).fetchone()
        if product is None:
            continue
        exists = conn.execute(
            "SELECT id FROM product_images WHERE product_id = ? AND url = ?",
            (product["id"], image["url"]),
        ).fetchone()
        should_be_primary = _should_make_static_image_primary(conn, product["id"], image)
        if exists is not None:
            if should_be_primary:
                _set_static_product_image_primary(conn, product["id"], exists["id"])
            continue
        stale_image = _find_stale_product_image(
            conn,
            product["id"],
            image["color"],
            prefer_primary=bool(image["is_primary"]),
        )
        if stale_image is not None:
            conn.execute(
                """
                UPDATE product_images
                SET url = ?, alt_text = ?, sort_order = ?, color = ?
                WHERE id = ?
                """,
                (
                    image["url"],
                    image["alt_text"],
                    image["sort_order"],
                    image["color"],
                    stale_image["id"],
                ),
            )
            if should_be_primary or stale_image["is_primary"]:
                _set_static_product_image_primary(conn, product["id"], stale_image["id"])
            continue
        is_primary = 1 if should_be_primary else 0
        conn.execute(
            """
            INSERT INTO product_images (product_id, url, alt_text, sort_order, color, is_primary)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                product["id"],
                image["url"],
                image["alt_text"],
                image["sort_order"],
                image["color"],
                is_primary,
            ),
        )


def _ensure_admin_user(conn):
    admin_email = os.environ.get("ADMIN_EMAIL")
    admin_password = os.environ.get("ADMIN_PASSWORD")
    if not admin_email or not admin_password:
        return False

    normalized_email = admin_email.strip().lower()
    row = conn.execute(
        "SELECT id, role FROM users WHERE email = ?",
        (normalized_email,),
    ).fetchone()
    now = datetime.utcnow().isoformat()
    if row is None:
        conn.execute(
            """
            INSERT INTO users (email, password_hash, full_name, is_verified, role, created_at, updated_at)
            VALUES (?, ?, ?, 1, 'admin', ?, ?)
            """,
            (
                normalized_email,
                generate_password_hash(admin_password),
                "Administrator",
                now,
                now,
            ),
        )
        return True

    if (row["role"] or "").strip().lower() != "admin":
        conn.execute(
            "UPDATE users SET role = 'admin', updated_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        return True
    return False


def _get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None or row["value"] is None:
        return default
    return row["value"]


def _set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
