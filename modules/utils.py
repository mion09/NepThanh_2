import hashlib
import io
import os
import re
import secrets
import unicodedata
import urllib.parse
from datetime import datetime
from werkzeug.utils import secure_filename

from modules.config import BASE_DIR, UPLOAD_DIR

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
BLOB_READ_WRITE_TOKEN_ENV_VARS = (
    "NEP_THANH_2_BLOB_READ_WRITE_TOKEN",
    "NEP_THANH_BLOB_READ_WRITE_TOKEN",
    "BLOB_READ_WRITE_TOKEN",
    "VERCEL_BLOB_READ_WRITE_TOKEN",
)
PRODUCT_COLOR_DETAILS = {
    "black": {"label": "Đen", "css": "#111111"},
    "white": {"label": "Trắng", "css": "#ffffff"},
    "red": {"label": "Đỏ", "css": "#b22222"},
    "blue": {"label": "Xanh dương", "css": "#2563eb"},
    "green": {"label": "Xanh lá", "css": "#15803d"},
    "yellow": {"label": "Vàng", "css": "#eab308"},
    "pink": {"label": "Hồng", "css": "#ec4899"},
    "gray": {"label": "Xám", "css": "#9ca3af"},
    "beige": {"label": "Be", "css": "#d6c2a1"},
}


def _is_external_url(value):
    if not value:
        return False
    parsed = urllib.parse.urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalize_static_path(value):
    if not value:
        return None
    value = value.strip().replace("\\", "/")
    if _is_external_url(value):
        return value
    if value.startswith("/static/"):
        value = value[len("/static/"):]
    elif value.startswith("static/"):
        value = value[len("static/"):]
    return value.lstrip("/")


def _static_path_exists(relative_path):
    if not relative_path or _is_external_url(relative_path):
        return False
    return os.path.exists(os.path.join(BASE_DIR, "static", *relative_path.split("/")))


def _find_static_asset(folders, stems, extensions=IMAGE_EXTENSIONS):
    candidates = []
    seen = set()
    normalized_extensions = tuple(ext.lower() for ext in extensions)
    for folder in folders:
        clean_folder = _normalize_static_path(folder)
        if not clean_folder:
            continue
        absolute_folder = os.path.join(BASE_DIR, "static", *clean_folder.split("/"))
        if not os.path.isdir(absolute_folder):
            continue
        try:
            names = sorted(os.listdir(absolute_folder))
        except OSError:
            continue
        for stem in stems:
            clean_stem = os.path.splitext(os.path.basename((stem or "").strip()))[0]
            if not clean_stem:
                continue
            for name in names:
                file_stem, ext = os.path.splitext(name)
                if file_stem != clean_stem or ext.lower() not in normalized_extensions:
                    continue
                relative_path = f"{clean_folder}/{name}".replace("\\", "/")
                if relative_path in seen:
                    continue
                seen.add(relative_path)
                absolute_path = os.path.join(absolute_folder, name)
                try:
                    size = os.path.getsize(absolute_path)
                except OSError:
                    size = float("inf")
                candidates.append((size, relative_path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][1]


def _slugify(value):
    value = (value or "").strip()
    if not value:
        return secrets.token_hex(4)
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value).strip("-").lower()
    return cleaned or secrets.token_hex(4)


def _normalize_product_color(value):
    raw = (value or "").strip()
    if not raw:
        return ""
    normalized = unicodedata.normalize("NFKD", raw).replace("Đ", "D").replace("đ", "d")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    key = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    aliases = {
        "den": "black",
        "trang": "white",
        "do": "red",
        "xanh-duong": "blue",
        "xanh-la": "green",
        "vang": "yellow",
        "hong": "pink",
        "xam": "gray",
    }
    return aliases.get(key, key)


def _product_color_details(value):
    key = _normalize_product_color(value)
    details = PRODUCT_COLOR_DETAILS.get(key)
    if details:
        return {"key": key, **details}
    return {
        "key": key,
        "label": (value or key or "Không phân màu").strip(),
        "css": "#d1d5db",
    }


def _is_allowed_image_filename(filename):
    return bool(filename) and os.path.splitext(filename)[1].lower() in IMAGE_EXTENSIONS


def _parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _build_order_number():
    stamp = datetime.utcnow().strftime("%Y%m%d")
    token = secrets.token_hex(2).upper()
    return f"NT{stamp}{token}"


def _save_upload(file_storage, subdir):
    if not file_storage or not file_storage.filename:
        return None
    filename = secure_filename(file_storage.filename)
    if not filename:
        return None
    unique = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(3)}_{filename}"
    if os.environ.get("VERCEL"):
        blob = _get_blob_client().put(
            f"{subdir}/{unique}",
            file_storage.read(),
            access="public",
        )
        return blob.url

    target_dir = os.path.join(UPLOAD_DIR, subdir)
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, unique)
    file_storage.save(path)
    rel_path = os.path.relpath(path, os.path.join(BASE_DIR, "static"))
    return rel_path.replace("\\", "/")


def _get_blob_read_write_token():
    for env_name in BLOB_READ_WRITE_TOKEN_ENV_VARS:
        token = (os.environ.get(env_name) or "").strip().strip('"').strip("'")
        if token:
            return token
    env_names = ", ".join(BLOB_READ_WRITE_TOKEN_ENV_VARS)
    raise RuntimeError(f"One of {env_names} is required for Vercel Blob uploads.")


def _get_blob_client():
    from vercel.blob import BlobClient

    return BlobClient(token=_get_blob_read_write_token())


def _hash_ip(ip_address):
    if not ip_address:
        return None
    return hashlib.sha256(ip_address.encode("utf-8")).hexdigest()[:24]


def _generate_qr_png(data):
    try:
        import qrcode
    except ImportError:
        return None
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _safe_next_url(value):
    if not value:
        return None
    value = value.strip()
    for _ in range(5):
        decoded = urllib.parse.unquote(value)
        if decoded == value:
            break
        value = decoded
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return None
    blocked = {
        "/login",
        "/signup",
        "/logout",
        "/auth/google",
        "/auth/google/callback",
    }
    path = parsed.path or "/"
    if path in blocked or path.startswith("/auth/"):
        return None
    if parsed.query:
        return f"{path}?{parsed.query}"
    return path


def _safe_background_url(candidate):
    if not candidate:
        return None
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return None
    path = parsed.path or "/"
    blocked = {
        "/login",
        "/signup",
        "/logout",
        "/auth/google",
        "/auth/google/callback",
    }
    if path in blocked or path.startswith("/auth/"):
        return None
    if parsed.query:
        return f"{path}?{parsed.query}"
    return path


def _background_from_referrer(referrer, host):
    if not referrer:
        return None
    try:
        parsed = urllib.parse.urlparse(referrer)
    except ValueError:
        return None
    if parsed.netloc and parsed.netloc != host:
        return None
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return _safe_background_url(path)
