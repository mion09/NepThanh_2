import glob
import os
import time

from modules.config import BASE_DIR
from modules.db import _get_db
from modules.promotions import (
    apply_promotion_to_price,
    best_promotion_for_price,
    get_product_promotion_map,
)
from modules.utils import (
    _find_static_asset,
    _is_external_url,
    _normalize_static_path,
    _static_path_exists,
)


STATIC_DIR = os.path.join(BASE_DIR, "static")
_CONTENT_CACHE_TTL_SECONDS = max(
    0,
    int((os.environ.get("CONTENT_CACHE_TTL_SECONDS") or "30").strip() or "30"),
)
_CONTENT_CACHE = {}

CHARACTER_BLOB_ASSETS = {
    "anh-hai": {
        "model": "https://orrtnvht4or15ro0.public.blob.vercel-storage.com/characters/models/anhhai-v3.glb",
    },
    "be-roi": {
        "model": "https://orrtnvht4or15ro0.public.blob.vercel-storage.com/characters/models/be-roi.glb",
    },
    "chang-khen": {
        "model": "https://orrtnvht4or15ro0.public.blob.vercel-storage.com/characters/models/changkhenv2.glb",
        "intro_video": "https://orrtnvht4or15ro0.public.blob.vercel-storage.com/characters/videos/changkhen_v2.mp4",
    },
    "co-cheo": {
        "model": "https://orrtnvht4or15ro0.public.blob.vercel-storage.com/characters/models/cocheo-v4-s2.glb",
        "intro_video": "https://orrtnvht4or15ro0.public.blob.vercel-storage.com/characters/videos/cocheo_cc3.mp4",
    },
    "nang-then": {
        "model": "https://orrtnvht4or15ro0.public.blob.vercel-storage.com/characters/models/nang-then-v2.glb",
        "intro_video": "https://orrtnvht4or15ro0.public.blob.vercel-storage.com/characters/videos/nangthen_V3.mp4",
    },
    "chu-xam": {
        "model": "https://orrtnvht4or15ro0.public.blob.vercel-storage.com/characters/models/chu-xam.glb",
    },
}


def _character_asset_type(path):
    ext = os.path.splitext((path or "").lower())[1]
    if ext in {".glb", ".gltf"}:
        return "model"
    return "image"


def _first_existing(candidates):
    for candidate in candidates:
        if candidate and (_is_external_url(candidate) or _static_path_exists(candidate)):
            return candidate
    return None


def _smallest_existing(candidates):
    matches = []
    for candidate in candidates:
        if not candidate or not _static_path_exists(candidate):
            continue
        absolute = os.path.join(STATIC_DIR, *candidate.split("/"))
        try:
            size = os.path.getsize(absolute)
        except OSError:
            size = float("inf")
        matches.append((size, candidate))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]))
    return matches[0][1]


def _glob_relative(patterns):
    for pattern in patterns:
        matches = sorted(glob.glob(os.path.join(STATIC_DIR, *pattern.split("/"))))
        if matches:
            return os.path.relpath(matches[0], STATIC_DIR).replace("\\", "/")
    return None


def _glob_smallest_relative(patterns):
    matches = []
    for pattern in patterns:
        for match in glob.glob(os.path.join(STATIC_DIR, *pattern.split("/"))):
            try:
                size = os.path.getsize(match)
            except OSError:
                size = float("inf")
            matches.append((size, match))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]))
    return os.path.relpath(matches[0][1], STATIC_DIR).replace("\\", "/")


def _slug_stems(slug):
    normalized = (slug or "").strip()
    if not normalized:
        return []
    return [normalized.replace("-", "_"), normalized]


def _candidate_stem(path):
    normalized = _normalize_static_path(path)
    if not normalized:
        return None
    return os.path.splitext(os.path.basename(normalized))[0]


def _resolve_character_asset_path(candidate):
    normalized = _normalize_static_path(candidate)
    if not normalized:
        return None
    if _is_external_url(normalized):
        return normalized
    if _static_path_exists(normalized):
        return normalized
    if "/" not in normalized:
        for prefix in (
            "images/characters/",
            "models/characters/",
            "images/",
            "models/",
        ):
            resolved = f"{prefix}{normalized}"
            if _static_path_exists(resolved):
                return resolved
    if os.path.splitext(normalized)[1].lower() not in {".glb", ".gltf", ".mp4"}:
        image_match = _find_static_asset(
            ("images/characters", "images"),
            [_candidate_stem(normalized)],
        )
        if image_match:
            return image_match
    return normalized


def _character_preview_for_stem(stem):
    base = stem.replace("-", "_")
    preview = _find_static_asset(("images/characters", "images"), [base, stem])
    if preview:
        return preview
    exact = _smallest_existing(
        [
            f"images/{base}.jpg",
            f"images/{base}.jpeg",
            f"images/{base}.png",
            f"images/{base}.webp",
            f"images/characters/{base}.jpg",
            f"images/characters/{base}.jpeg",
            f"images/characters/{base}.png",
            f"images/characters/{base}.webp",
        ]
    )
    if exact:
        return exact
    return _glob_smallest_relative(
        [
            f"images/{base}*.jpg",
            f"images/{base}*.jpeg",
            f"images/{base}*.png",
            f"images/{base}*.webp",
            f"images/characters/{base}*.jpg",
            f"images/characters/{base}*.jpeg",
            f"images/characters/{base}*.png",
            f"images/characters/{base}*.webp",
        ]
    )


def _character_preview_for_asset(asset_path, slug):
    if _is_external_url(asset_path):
        return _character_preview_for_stem(slug)
    stem = os.path.splitext(os.path.basename(asset_path or ""))[0]
    preview = _character_preview_for_stem(stem)
    if preview:
        return preview
    return _character_preview_for_stem(slug)


def _character_intro_video_for_slug(slug):
    blob_asset = CHARACTER_BLOB_ASSETS.get(slug or "", {}).get("intro_video")
    if blob_asset:
        return blob_asset
    stem = slug.replace("-", "_")
    return _first_existing(
        [
            f"videos/characters/{stem}.mp4",
            f"videos/characters/{slug}.mp4",
            f"images/characters/{stem}.mp4",
            f"images/characters/{slug}.mp4",
        ]
    )


def _character_model_for_slug(slug):
    blob_asset = CHARACTER_BLOB_ASSETS.get(slug or "", {}).get("model")
    if blob_asset:
        return blob_asset
    return _first_existing(
        [
            f"models/characters/{slug.replace('-', '_')}.glb",
            f"models/characters/{slug}.glb",
            f"models/characters/{slug.replace('-', '_')}.gltf",
            f"models/characters/{slug}.gltf",
            f"images/characters/{slug.replace('-', '_')}.glb",
            f"images/characters/{slug}.glb",
            f"images/characters/{slug.replace('-', '_')}.gltf",
            f"images/characters/{slug}.gltf",
        ]
    )


def _character_image_for_slug(slug):
    return (
        _find_static_asset(("images/characters", "images"), _slug_stems(slug))
        or "images/logo/logo.png"
    )


def _product_image_for(row, image_url):
    normalized = _normalize_static_path(image_url)
    if normalized and _static_path_exists(normalized):
        return normalized

    product_stems = _slug_stems(row["slug"])
    character_slug = row["character_slug"] if "character_slug" in row.keys() else None
    character_stems = _slug_stems(character_slug)
    candidate_stem = _candidate_stem(normalized)
    stems = []
    for stem in [candidate_stem] + character_stems + product_stems:
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


def _map_character(row):
    story_text = row["story_text"] or ""
    description = story_text or row["origin"] or ""
    bio_parts = [row["origin"], row["personality"], row["symbol"], row["role"]]
    bio = " ".join(part for part in bio_parts if part)
    audio_source = row["audio_url"] or row["music_sample_url"]
    audio_source_url = row["audio_source_url"] if "audio_source_url" in row.keys() else ""
    if not _is_external_url(audio_source_url):
        audio_source_url = ""
    requested_asset = _resolve_character_asset_path(row["image_url"]) if row["image_url"] else None
    requested_asset_exists = requested_asset and (
        _is_external_url(requested_asset) or _static_path_exists(requested_asset)
    )
    requested_asset_type = _character_asset_type(requested_asset) if requested_asset_exists else None
    model_path = requested_asset if requested_asset_type == "model" else _character_model_for_slug(row["slug"])
    slug_image = _character_image_for_slug(row["slug"])

    if requested_asset_exists and requested_asset_type == "image":
        preview_image = requested_asset
    elif model_path:
        preview_image = _character_preview_for_asset(model_path, row["slug"]) or slug_image
    elif requested_asset_exists:
        preview_image = requested_asset
    else:
        preview_image = slug_image

    intro_video = _character_intro_video_for_slug(row["slug"])
    final_visual_path = model_path or preview_image
    final_visual_type = "model" if model_path else "image"

    character = {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "nickname": row["nickname"] or "",
        "origin": row["origin"] or "",
        "personality": row["personality"] or "",
        "symbol": row["symbol"] or "",
        "role": row["role"] or "",
        "description": description,
        "story_text": story_text,
        "bio": bio,
        "audio_file": _normalize_static_path(audio_source) if audio_source else None,
        "audio_url": _normalize_static_path(audio_source) if audio_source else None,
        "audio_source_url": audio_source_url,
        "image": preview_image,
        "model_path": model_path,
        "asset_path": final_visual_path,
        "asset_type": final_visual_type,
        "final_visual_path": final_visual_path,
        "final_visual_type": final_visual_type,
        "intro_video": intro_video,
    }
    if row["seo_description"]:
        character["seo_description"] = row["seo_description"]
    if row["seo_title"]:
        character["seo_title"] = row["seo_title"]
    character["is_active"] = row["is_active"] if "is_active" in row.keys() else 1
    return character


def _map_product(row, image_url, promotions=None):
    image = _product_image_for(row, image_url)
    regular_price = row["base_price"] or 0
    promotion = best_promotion_for_price(regular_price, promotions or [])
    sale_price = apply_promotion_to_price(regular_price, promotion)
    product = {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "price": sale_price,
        "regular_price": regular_price,
        "promotion_name": promotion["name"] if promotion else None,
        "promotion_discount_type": promotion["discount_type"] if promotion else None,
        "promotion_value": promotion["value"] if promotion else None,
        "has_promotion": bool(promotion and sale_price < regular_price),
        "character_id": row["character_id"],
        "image": image,
        "short_description": row["description"] or "",
        "long_description": row["long_description"] if "long_description" in row.keys() else "",
        "status": row["status"],
        "is_featured": row["is_featured"] if "is_featured" in row.keys() else 0,
        "collection": row["collection"] if "collection" in row.keys() else None,
    }
    if row["description"]:
        product["seo_description"] = row["description"]
    if row["seo_description"]:
        product["seo_description"] = row["seo_description"]
    if row["seo_title"]:
        product["seo_title"] = row["seo_title"]
    return product


def _get_cached_content(cache_key, loader):
    now = time.monotonic()
    cached = _CONTENT_CACHE.get(cache_key)
    if cached and now < cached["expires_at"]:
        return cached["value"]
    value = loader()
    _CONTENT_CACHE[cache_key] = {
        "value": value,
        "expires_at": now + _CONTENT_CACHE_TTL_SECONDS,
    }
    return value


def invalidate_content_cache():
    _CONTENT_CACHE.clear()


def load_characters():
    def loader():
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM characters WHERE is_active = 1 ORDER BY id"
        ).fetchall()
        conn.close()
        return [_map_character(row) for row in rows]

    return _get_cached_content("characters", loader)


def load_products():
    def loader():
        conn = _get_db()
        products = conn.execute(
            """
            SELECT p.*, c.slug AS character_slug
            FROM products p
            LEFT JOIN characters c ON c.id = p.character_id
            WHERE p.status = 'active'
            ORDER BY p.id
            """
        ).fetchall()
        image_rows = conn.execute(
            "SELECT product_id, url FROM product_images ORDER BY is_primary DESC, sort_order, id"
        ).fetchall()
        promotion_map = get_product_promotion_map(
            conn, [row["id"] for row in products]
        )
        conn.close()
        image_map = {}
        for row in image_rows:
            if row["product_id"] not in image_map and row["url"]:
                image_map[row["product_id"]] = row["url"]
        return [
            _map_product(row, image_map.get(row["id"]), promotion_map.get(row["id"]))
            for row in products
        ]

    return _get_cached_content("products_active", loader)


def load_all_products():
    def loader():
        conn = _get_db()
        products = conn.execute(
            """
            SELECT p.*, c.slug AS character_slug
            FROM products p
            LEFT JOIN characters c ON c.id = p.character_id
            ORDER BY p.id
            """
        ).fetchall()
        image_rows = conn.execute(
            "SELECT product_id, url FROM product_images ORDER BY is_primary DESC, sort_order, id"
        ).fetchall()
        promotion_map = get_product_promotion_map(
            conn, [row["id"] for row in products]
        )
        conn.close()
        image_map = {}
        for row in image_rows:
            if row["product_id"] not in image_map and row["url"]:
                image_map[row["product_id"]] = row["url"]
        return [
            _map_product(row, image_map.get(row["id"]), promotion_map.get(row["id"]))
            for row in products
        ]

    return _get_cached_content("products_all", loader)
