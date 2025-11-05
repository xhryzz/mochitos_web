# middleware_perf.py
"""
Pequeño módulo para acelerar Flask en entornos 512 MB (Render Free):
- Compresión Gzip/Brotli (Flask-Compress)
- Servir estáticos ultra-rápido con WhiteNoise + cache agresivo
- Caché de bytecode Jinja2 en disco
- Proveedor JSON con orjson (si está disponible)
- Cabeceras Cache-Control seguras para HTML/JSON

Uso:
from middleware_perf import setup_performance
app = Flask(__name__)
setup_performance(app)
"""
from __future__ import annotations

import os
from typing import Any
from datetime import timedelta

try:
    import orjson  # type: ignore
    from flask.json.provider import DefaultJSONProvider
    class ORJSONProvider(DefaultJSONProvider):
        def dumps(self, obj: Any, **kwargs: Any) -> str:
            # orjson produce bytes; devolvemos str utf-8
            option = orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_NON_STR_KEYS
            return orjson.dumps(obj, option=option).decode("utf-8")
        def loads(self, s: str | bytes, **kwargs: Any) -> Any:
            return orjson.loads(s)
    _HAS_ORJSON = True
except Exception:
    _HAS_ORJSON = False
    ORJSONProvider = None  # type: ignore

try:
    from flask_compress import Compress  # type: ignore
except Exception:
    Compress = None  # type: ignore

try:
    from whitenoise import WhiteNoise  # type: ignore
except Exception:
    WhiteNoise = None  # type: ignore

try:
    from jinja2 import FileSystemBytecodeCache  # type: ignore
except Exception:
    FileSystemBytecodeCache = None  # type: ignore


def _immutable_name(path: str) -> bool:
    # Considera inmutables ficheros con ".min." o hash tipo ".123abc."
    base = os.path.basename(path)
    if ".min." in base:
        return True
    return any(part for part in base.split(".") if len(part) >= 6 and part.isalnum())


def setup_performance(app):
    """Configura rendimiento para Flask."""
    # 1) JSON ultrarrápido si está orjson
    if _HAS_ORJSON and ORJSONProvider is not None:
        try:
            app.json_provider_class = ORJSONProvider  # type: ignore
            app.json = app.json_provider_class(app)   # type: ignore
        except Exception:
            pass

    # 2) Ajustes de Flask
    app.config.setdefault("TEMPLATES_AUTO_RELOAD", False)
    app.config.setdefault("SEND_FILE_MAX_AGE_DEFAULT", int(timedelta(days=365).total_seconds()))
    app.config.setdefault("JSONIFY_PRETTYPRINT_REGULAR", False)

    # 3) Caché de bytecode Jinja (reduce CPU de plantillas)
    if FileSystemBytecodeCache is not None:
        cache_dir = os.environ.get("JINJA_CACHE_DIR", ".jinja_cache")
        try:
            os.makedirs(cache_dir, exist_ok=True)
            app.jinja_env.bytecode_cache = FileSystemBytecodeCache(cache_dir=cache_dir, pattern="%s.cache")
        except Exception:
            pass

    # 4) Compresión (Brotli > Gzip) si está disponible
    if Compress is not None:
        try:
            comp = Compress()
            comp.init_app(app)
            # Tipos comunes (añadir json, svg, xml…)
            app.config.setdefault("COMPRESS_MIMETYPES", [
                "text/html", "text/css", "text/xml",
                "text/plain", "application/xml",
                "application/json", "application/javascript",
                "application/x-javascript", "image/svg+xml",
                "application/manifest+json"
            ])
            app.config.setdefault("COMPRESS_LEVEL", 6)
            app.config.setdefault("COMPRESS_BR_LEVEL", 5)
            app.config.setdefault("COMPRESS_MIN_SIZE", 512)
        except Exception:
            pass

    # 5) WhiteNoise para estáticos con caché agresivo
    if WhiteNoise is not None and getattr(app, "wsgi_app", None):
        try:
            static_root = app.static_folder or "static"
            static_prefix = (app.static_url_path or "/static").lstrip("/") + "/"
            wn = WhiteNoise(
                app.wsgi_app,
                root=static_root,
                prefix=static_prefix,
                autorefresh=False,     # en Render no hay disco persistente; no recarga
                max_age=31536000,      # 1 año
                immutable_file_test=_immutable_name,
            )
            # Sirve también /favicon.ico si existe en static_root
            if os.path.exists(os.path.join(static_root, "favicon.ico")):
                wn.add_files(static_root, prefix="")
            app.wsgi_app = wn
        except Exception:
            pass

    # 6) Cabeceras por respuesta (seguras)
    @app.after_request
    def _set_caching_headers(resp):
        try:
            resp.headers.setdefault("Vary", "Accept-Encoding")
            ct = (resp.headers.get("Content-Type") or "").lower()
            # HTML: cache cortita si no hay Set-Cookie (no contenido de sesión)
            if "text/html" in ct and "Set-Cookie" not in resp.headers:
                resp.headers.setdefault("Cache-Control", "public, max-age=60")
            # JSON: deja que el cliente lo revalide rápido
            elif "application/json" in ct:
                resp.headers.setdefault("Cache-Control", "public, max-age=15")
        except Exception:
            pass
        return resp
