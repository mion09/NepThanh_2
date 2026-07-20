"""
Flask application for the Nếp Thanh – Dòng chảy thanh âm Việt project.
"""

import os
from datetime import datetime

from flask import Flask, render_template, request, session, url_for
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix
load_dotenv(override=True)

from modules.auth import _get_current_user, _is_admin_user, _google_enabled
from modules.cart import get_cart_item_count
from modules.config import SECRET_KEY
from modules.db import init_db
from modules.routes_admin import register_admin_routes
from modules.routes_chatbot import register_chatbot_routes
from modules.routes_public import register_public_routes
from modules.utils import _is_external_url, _normalize_static_path



app = Flask(__name__)
app.secret_key = SECRET_KEY
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config["PREFERRED_URL_SCHEME"] = "https"
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 86400

# Ensure a usable schema exists for both local runs and Vercel cold starts.
init_db()


@app.context_processor
def inject_globals():
    current_user = _get_current_user()
    ga4_events = session.pop("_ga4_events", [])
    if not isinstance(ga4_events, list):
        ga4_events = []
    return {
        "site_name": "Nếp Thanh – Dòng chảy thanh âm Việt",
        "current_year": datetime.now().year,
        "current_user": current_user,
        "is_admin": _is_admin_user(current_user),
        "google_enabled": _google_enabled(),
        "google_analytics_id": os.environ.get("GOOGLE_ANALYTICS_ID", "G-H3C8YJ190C").strip(),
        "ga4_events": ga4_events,
        "cart_count": get_cart_item_count(current_user),
    }


@app.template_global()
def asset_url(path):
    if not path:
        return ""
    normalized = _normalize_static_path(path)
    if _is_external_url(normalized):
        return normalized
    return url_for("static", filename=normalized)


register_public_routes(app)
register_admin_routes(app)
register_chatbot_routes(app)


@app.after_request
def apply_performance_headers(response):
    path = request.path.lower()
    if path.startswith("/static/"):
        if path.endswith((".glb", ".gltf", ".mp4", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".ico")):
            response.headers["Cache-Control"] = "public, max-age=604800, stale-while-revalidate=86400"
        elif path.endswith((".css", ".js")):
            response.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=86400"
    return response


@app.errorhandler(404)
def page_not_found(error):
    return (
        render_template(
            "404.html",
            title="Không tìm thấy trang",
            description="Trang bạn tìm kiếm không tồn tại. Vui lòng quay lại trang chủ.",
        ),
        404,
    )
    


if __name__ == "__main__":
    app.run(debug=True)
