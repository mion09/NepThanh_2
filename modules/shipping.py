import json
import os
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from modules.db import _get_setting
from modules.utils import _parse_int

DEFAULT_ITEM_WEIGHT_GRAMS = 300
DEFAULT_ITEM_LENGTH_CM = 30
DEFAULT_ITEM_WIDTH_CM = 25
DEFAULT_ITEM_HEIGHT_CM = 3
DEFAULT_FALLBACK_FEE = 30000
FLAT_PROVINCE_SHIPPING_FEE = 30000

WAREHOUSE_PROVINCE = "Hà Nội"
WAREHOUSE_DISTRICT = os.environ.get("SHIPPING_ORIGIN_DISTRICT", "Quận Hai Bà Trưng")
WAREHOUSE_WARD = os.environ.get("SHIPPING_ORIGIN_WARD", "")
WAREHOUSE_ADDRESS = os.environ.get("SHIPPING_ORIGIN_ADDRESS", "Kho Hà Nội")


def quote_shipping_options(conn, cart, address, selected_method=None, payment_method="cod"):
    # Tạm thời tắt báo giá API hãng, gồm "GHN - tiêu chuẩn".
    # Khi cần bật lại, khôi phục các dòng gọi _quote_ghn/_quote_ghtk/_quote_viettelpost bên dưới.
    return [_fixed_shipping_quote(cart, address)]


def select_shipping_quote(conn, cart, address, method_id, payment_method="cod"):
    method_id = (method_id or "").strip()
    quotes = quote_shipping_options(conn, cart, address, method_id, payment_method)
    if not method_id and quotes:
        return quotes[0], None
    for quote in quotes:
        if quote["id"] == method_id:
            return quote, None
    if quotes:
        return quotes[0], None
    return None, "Phương thức vận chuyển không còn khả dụng cho địa chỉ này."


def calculate_shop_shipping_fee(items=None, address=None):
    total_qty = sum(max(_parse_int(item.get("qty"), 0), 0) for item in (items or []))
    if total_qty <= 0:
        return 0
    return calculate_address_shipping_fee(address)


def calculate_address_shipping_fee(address=None):
    if is_thach_that_address(address):
        return 0
    return FLAT_PROVINCE_SHIPPING_FEE


def is_thach_that_address(address=None):
    if isinstance(address, dict):
        district_text = _normalize_text(address.get("district"))
        if district_text and "thach that" in district_text:
            return True
        address_text = _normalize_text(
            " ".join(
                str(address.get(key) or "")
                for key in ("line1", "line2", "ward", "district", "province")
            )
        )
    else:
        address_text = _normalize_text(address)
    return "thach that" in address_text


def _fixed_shipping_quote(cart=None, address=None):
    items = (cart or {}).get("items", [])
    fee = calculate_shop_shipping_fee(items, address)
    if fee:
        service_label = "Đồng giá tỉnh"
        message = "Phí ship đồng giá 30.000 đ cho đơn ngoài Thạch Thất."
    else:
        service_label = "Free ship"
        message = "Free ship cho địa chỉ Thạch Thất."
    return {
        "id": "shop:fixed",
        "carrier": "shop",
        "carrier_label": "Shop",
        "service": "fixed",
        "service_label": service_label,
        "fee": fee,
        "estimated": True,
        "source": "fixed",
        "message": message,
        "free_area": "Thạch Thất",
    }


def build_package(items):
    total_qty = sum(max(_parse_int(item.get("qty"), 0), 0) for item in items)
    total_qty = max(total_qty, 1)
    weight = sum(
        max(_parse_int(item.get("qty"), 0), 0)
        * max(_parse_int(item.get("weight_grams"), DEFAULT_ITEM_WEIGHT_GRAMS), DEFAULT_ITEM_WEIGHT_GRAMS)
        for item in items
    )
    return {
        "weight": max(weight, DEFAULT_ITEM_WEIGHT_GRAMS),
        "length": DEFAULT_ITEM_LENGTH_CM,
        "width": DEFAULT_ITEM_WIDTH_CM,
        "height": max(DEFAULT_ITEM_HEIGHT_CM, min(50, DEFAULT_ITEM_HEIGHT_CM * total_qty)),
        "items": [
            {
                "name": item.get("product_name") or "Áo phông",
                "quantity": max(_parse_int(item.get("qty"), 1), 1),
                "weight": DEFAULT_ITEM_WEIGHT_GRAMS,
                "length": DEFAULT_ITEM_LENGTH_CM,
                "width": DEFAULT_ITEM_WIDTH_CM,
                "height": DEFAULT_ITEM_HEIGHT_CM,
            }
            for item in items
        ],
    }


def normalize_address(address):
    return {
        "line1": (address.get("line1") or "").strip(),
        "ward": (address.get("ward") or "").strip(),
        "district": (address.get("district") or "").strip(),
        "province": (address.get("province") or "").strip(),
        "ghn_province_id": _parse_int(address.get("ghn_province_id"), 0),
        "ghn_district_id": _parse_int(address.get("ghn_district_id"), 0),
        "ghn_ward_code": (address.get("ghn_ward_code") or "").strip(),
    }


def list_shipping_provinces():
    token = (os.environ.get("GHN_TOKEN") or "").strip()
    if not token:
        return []
    rows = _json_request(
        f"{_ghn_base_url()}/shiip/public-api/master-data/province",
        headers={"Token": token},
    ).get("data") or []
    provinces = [
        {
            "id": row.get("ProvinceID") or row.get("ProvinceId"),
            "name": row.get("ProvinceName") or row.get("Name"),
        }
        for row in rows
        if row.get("ProvinceID") or row.get("ProvinceId")
    ]
    return sorted(provinces, key=lambda item: _normalize_text(item["name"]))


def list_shipping_districts(province_id):
    token = (os.environ.get("GHN_TOKEN") or "").strip()
    province_id = _parse_int(province_id, 0)
    if not token or province_id <= 0:
        return []
    rows = _json_request(
        f"{_ghn_base_url()}/shiip/public-api/master-data/district?province_id={province_id}",
        headers={"Token": token},
    ).get("data") or []
    districts = [
        {
            "id": row.get("DistrictID") or row.get("DistrictId"),
            "name": row.get("DistrictName") or row.get("Name"),
        }
        for row in rows
        if row.get("DistrictID") or row.get("DistrictId")
    ]
    return sorted(districts, key=lambda item: _normalize_text(item["name"]))


def list_shipping_wards(district_id):
    token = (os.environ.get("GHN_TOKEN") or "").strip()
    district_id = _parse_int(district_id, 0)
    if not token or district_id <= 0:
        return []
    rows = _json_request(
        f"{_ghn_base_url()}/shiip/public-api/master-data/ward?district_id={district_id}",
        headers={"Token": token},
    ).get("data") or []
    wards = [
        {
            "code": str(row.get("WardCode") or row.get("Code") or ""),
            "name": row.get("WardName") or row.get("Name"),
        }
        for row in rows
        if row.get("WardCode") or row.get("Code")
    ]
    return sorted(wards, key=lambda item: _normalize_text(item["name"]))


def _quote_ghn(package, address, order_value, payment_method):
    token = (os.environ.get("GHN_TOKEN") or "").strip()
    shop_id = (os.environ.get("GHN_SHOP_ID") or "").strip()
    if not token or not shop_id:
        return []
    resolved = _ghn_resolve_address(token, address)
    if not resolved:
        return []
    base = _ghn_base_url()
    payload = {
        "service_type_id": _parse_int(os.environ.get("GHN_SERVICE_TYPE_ID"), 2),
        "from_district_id": _parse_int(os.environ.get("GHN_FROM_DISTRICT_ID"), 0) or None,
        "from_ward_code": (os.environ.get("GHN_FROM_WARD_CODE") or "").strip() or None,
        "to_district_id": resolved["district_id"],
        "to_ward_code": resolved["ward_code"],
        "length": package["length"],
        "width": package["width"],
        "height": package["height"],
        "weight": package["weight"],
        "insurance_value": min(order_value, 5000000),
        "cod_value": order_value if payment_method == "cod" else 0,
        "coupon": None,
        "items": package["items"],
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    response = _json_request(
        f"{base}/shiip/public-api/v2/shipping-order/fee",
        method="POST",
        headers={"Token": token, "ShopId": shop_id},
        payload=payload,
    )
    data = response.get("data") if isinstance(response, dict) else None
    if not data:
        return []
    fee = _parse_int(data.get("total"), 0)
    if fee <= 0:
        return []
    return [
        {
            "id": "ghn:standard",
            "carrier": "ghn",
            "carrier_label": "GHN",
            "service": "standard",
            "service_label": "tiêu chuẩn",
            "fee": fee,
            "estimated": True,
            "source": "api",
            "message": "Phí tính trực tiếp từ GHN.",
            "raw": data,
        }
    ]


def _quote_ghtk(package, address, order_value):
    token = (os.environ.get("GHTK_TOKEN") or "").strip()
    if not token:
        return []
    params = {
        "pick_province": os.environ.get("GHTK_PICK_PROVINCE", WAREHOUSE_PROVINCE),
        "pick_district": os.environ.get("GHTK_PICK_DISTRICT", WAREHOUSE_DISTRICT),
        "pick_ward": os.environ.get("GHTK_PICK_WARD", WAREHOUSE_WARD),
        "pick_address": os.environ.get("GHTK_PICK_ADDRESS", WAREHOUSE_ADDRESS),
        "province": address["province"],
        "district": address["district"],
        "ward": address["ward"],
        "address": address["line1"],
        "weight": package["weight"],
        "value": order_value,
        "transport": os.environ.get("GHTK_TRANSPORT", "road"),
    }
    params = {key: value for key, value in params.items() if value not in ("", None)}
    url = "https://services.giaohangtietkiem.vn/services/shipment/fee"
    headers = {"Token": token}
    partner_code = (os.environ.get("GHTK_PARTNER_CODE") or "").strip()
    if partner_code:
        headers["X-Client-Source"] = partner_code
    response = _json_request(f"{url}?{urllib.parse.urlencode(params)}", headers=headers)
    fee_data = response.get("fee") if isinstance(response, dict) else None
    if not response.get("success") or not fee_data or not fee_data.get("delivery", True):
        return []
    fee = _parse_int(fee_data.get("fee"), 0) + _parse_int(fee_data.get("insurance_fee"), 0)
    if fee <= 0:
        return []
    return [
        {
            "id": "ghtk:road",
            "carrier": "ghtk",
            "carrier_label": "GHTK",
            "service": fee_data.get("name") or "road",
            "service_label": "GHTK - đường bộ",
            "fee": fee,
            "estimated": True,
            "source": "api",
            "message": "Phí tính trực tiếp từ GHTK.",
            "raw": fee_data,
        }
    ]


def _quote_viettelpost(package, address, order_value, payment_method):
    endpoint = (os.environ.get("VIETTELPOST_FEE_ENDPOINT") or "").strip()
    token = (os.environ.get("VIETTELPOST_TOKEN") or "").strip()
    if not endpoint or not token:
        return []
    payload = {
        "from_province": os.environ.get("VIETTELPOST_PICK_PROVINCE", WAREHOUSE_PROVINCE),
        "from_district": os.environ.get("VIETTELPOST_PICK_DISTRICT", WAREHOUSE_DISTRICT),
        "to_province": address["province"],
        "to_district": address["district"],
        "to_ward": address["ward"],
        "weight": package["weight"],
        "value": order_value,
        "cod": order_value if payment_method == "cod" else 0,
    }
    response = _json_request(
        endpoint,
        method="POST",
        headers={"Token": token, "Authorization": f"Bearer {token}"},
        payload=payload,
    )
    fee = _parse_int(response.get("fee") or response.get("total") or response.get("price"), 0)
    if fee <= 0:
        return []
    return [
        {
            "id": "viettelpost:standard",
            "carrier": "viettelpost",
            "carrier_label": "Viettel Post",
            "service": "standard",
            "service_label": "Viettel Post",
            "fee": fee,
            "estimated": True,
            "source": "api",
            "message": "Phí tính trực tiếp từ Viettel Post.",
            "raw": response,
        }
    ]


def _fallback_quotes(conn, address, package):
    base_fee = _parse_int(_get_setting(conn, "shipping_fee", str(DEFAULT_FALLBACK_FEE)), DEFAULT_FALLBACK_FEE)
    if is_thach_that_address(address):
        fee = 0
        service = "Thạch Thất"
        days = "1-2 ngày"
    else:
        fee = base_fee
        service = "Đồng giá"
        days = "2-4 ngày"
    return [
        {
            "id": "shop:fallback",
            "carrier": "shop",
            "carrier_label": "Shop",
            "service": "fallback",
            "service_label": f"{service} ({days})",
            "fee": max(fee, 0),
            "estimated": True,
            "source": "fallback",
            "message": "Phí dự phòng từ kho Hà Nội khi chưa có API hãng hoặc API không trả giá.",
        }
    ]


def _ghn_resolve_address(token, address):
    address = normalize_address(address)
    if address["ghn_district_id"] and address["ghn_ward_code"]:
        return {
            "district_id": address["ghn_district_id"],
            "ward_code": address["ghn_ward_code"],
        }
    district_id = _parse_int(os.environ.get("GHN_TO_DISTRICT_ID"), 0)
    ward_code = (os.environ.get("GHN_TO_WARD_CODE") or "").strip()
    if district_id and ward_code:
        return {"district_id": district_id, "ward_code": ward_code}
    base = _ghn_base_url()
    province_name = _normalize_text(address["province"])
    district_name = _normalize_text(address["district"])
    ward_name = _normalize_text(address["ward"])
    provinces = _json_request(f"{base}/shiip/public-api/master-data/province", headers={"Token": token}).get("data") or []
    province = _best_match(provinces, province_name, ["ProvinceName", "Name"])
    if not province:
        return None
    province_id = province.get("ProvinceID") or province.get("ProvinceId")
    districts = _json_request(
        f"{base}/shiip/public-api/master-data/district?province_id={province_id}",
        headers={"Token": token},
    ).get("data") or []
    district = _best_match(districts, district_name, ["DistrictName", "Name"])
    if not district:
        return None
    district_id = district.get("DistrictID") or district.get("DistrictId")
    wards = _json_request(
        f"{base}/shiip/public-api/master-data/ward?district_id={district_id}",
        headers={"Token": token},
    ).get("data") or []
    ward = _best_match(wards, ward_name, ["WardName", "Name"])
    if not ward:
        return None
    return {"district_id": district_id, "ward_code": str(ward.get("WardCode") or ward.get("Code"))}


def _ghn_base_url():
    if (os.environ.get("GHN_ENV") or "production").strip().lower() in {"test", "dev", "sandbox"}:
        return "https://dev-online-gateway.ghn.vn"
    return "https://online-gateway.ghn.vn"


def _json_request(url, method="GET", headers=None, payload=None):
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            body = response.read().decode("utf-8")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return {}
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        return {}


def _best_match(rows, wanted, keys):
    if not wanted:
        return None
    for row in rows:
        for key in keys:
            candidate = _normalize_text(row.get(key))
            if candidate == wanted or wanted in candidate or candidate in wanted:
                return row
    return None


def _normalize_text(value):
    text = unicodedata.normalize("NFD", str(value or "").strip().lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    for prefix in ["tinh ", "thanh pho ", "tp ", "quan ", "huyen ", "phuong ", "xa ", "thi xa "]:
        text = text.replace(prefix, "")
    return " ".join(text.split())
