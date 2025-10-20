
# app.py — con Web Push, notificaciones (Europe/Madrid a medianoche) y seguimiento de precio
from flask import Flask, render_template, request, redirect, session, send_file, jsonify, flash, Response, make_response, abort, url_for
import psycopg2, psycopg2.extras
from psycopg2.pool import SimpleConnectionPool
from datetime import datetime, date, timedelta, timezone
import random, os, io, json, time, queue, threading, hashlib, base64, binascii
from werkzeug.utils import secure_filename
from contextlib import closing
from base64 import b64encode
from werkzeug.security import generate_password_hash, check_password_hash
import requests
import re  # <-- Seguimiento de precio
from urllib.parse import urljoin, urlparse, quote_plus
import unicodedata


# Web Push
from pywebpush import webpush, WebPushException

try:
    import pytz  # opcional (fallback)
except Exception:
    pytz = None

# (opcional) mejor parser HTML para precio
try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

app = Flask(__name__)
# Límite de subida (ajustable por env MAX_UPLOAD_MB). Evita BYTEA enormes que disparan RAM.
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_UPLOAD_MB', '2')) * 1024 * 1024
app.secret_key = os.environ.get('SECRET_KEY', 'tu_clave_secreta_aqui')

@app.before_request
def _fast_head_for_healthchecks():
    # Responde en seco a HEAD para no entrar en la vista pesada ni abrir DB
    if request.method == "HEAD" and request.path == "/":
        return ("", 204)

# ======= Opciones de app =======
app.config['TEMPLATES_AUTO_RELOAD'] = False
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False



# ======= Compresión (si está) =======
try:
    from flask_compress import Compress
    app.config['COMPRESS_MIMETYPES'] = ['text/html', 'text/css', 'application/json', 'application/javascript']
    app.config['COMPRESS_LEVEL'] = 6
    app.config['COMPRESS_MIN_SIZE'] = 1024
    Compress(app)
except Exception:
    pass

# ⬇️ AÑADE ESTO AQUÍ (justo después de la config)
import os, tempfile, pathlib
try:
    from jinja2 import FileSystemBytecodeCache

    JINJA_CACHE_DIR = os.environ.get(
        "JINJA_CACHE_DIR",
        os.path.join(tempfile.gettempdir(), "jinja_cache")  # normalmente /tmp/jinja_cache en Render
    )
    pathlib.Path(JINJA_CACHE_DIR).mkdir(parents=True, exist_ok=True)

    app.jinja_env.bytecode_cache = FileSystemBytecodeCache(
        directory=JINJA_CACHE_DIR,
        pattern='mochitos-%s.cache'
    )
except Exception as e:
    app.logger.warning("Jinja bytecode cache desactivado: %s", e)

# ========= Discord logs (asíncrono) =========
DISCORD_WEBHOOK = os.environ.get('DISCORD_WEBHOOK', '')

def client_ip():
    return (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()

def _is_hashed(v: str) -> bool:
    return isinstance(v, str) and (v.startswith('pbkdf2:') or v.startswith('scrypt:'))

_DISCORD_Q = queue.Queue(maxsize=500)

def _discord_worker():
    while True:
        url, payload = _DISCORD_Q.get()
        try:
            requests.post(url, json=payload, timeout=2)
        except Exception as e:
            print(f"[discord] error: {e}")
        finally:
            _DISCORD_Q.task_done()

if DISCORD_WEBHOOK:
    t = threading.Thread(target=_discord_worker, daemon=True)
    t.start()

def send_discord(event: str, payload: dict | None = None):
    if not DISCORD_WEBHOOK:
        return
    try:
        display_user = None
        if 'username' in session and session['username'] in ('mochito', 'mochita'):
            display_user = session['username']
        embed = {
            "title": event,
            "color": 0xE84393,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "fields": []
        }
        if display_user:
            embed["fields"].append({"name": "Usuario", "value": display_user, "inline": True})
        try:
            ruta = f"{request.method} {request.path}"
        except Exception:
            ruta = "(sin request)"
        embed["fields"] += [
            {"name": "Ruta", "value": ruta, "inline": True},
            {"name": "IP", "value": client_ip() or "?", "inline": True}
        ]
        if payload:
            raw = json.dumps(payload, ensure_ascii=False)
            for i, ch in enumerate([raw[i:i+1000] for i in range(0, len(raw), 1000)][:3]):
                embed["fields"].append({"name": "Datos" + (f" ({i+1})" if i else ""), "value": f"```json\n{ch}\n```", "inline": False})
        _DISCORD_Q.put_nowait((DISCORD_WEBHOOK, {"username": "Mochitos Logs", "embeds": [embed]}))
    except Exception as e:
        print(f"[discord] prep error: {e}")

# ========= Config Postgres + POOL =========
DATABASE_URL = os.environ.get('DATABASE_URL', '')

def _normalize_database_url(url: str) -> str:
    if not url:
        return url
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url

DATABASE_URL = _normalize_database_url(DATABASE_URL)

from psycopg2 import OperationalError, InterfaceError, DatabaseError

PG_POOL = None


# ===== Presencia: util para tocar/crear fila inmediatamente =====
def touch_presence(user: str, device: str = 'page-view'):
    ensure_presence_table()
    now = europe_madrid_now()
    try:
        ua = (request.headers.get('User-Agent') or '')[:300]
    except Exception:
        ua = ''
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO user_presence (username, last_seen, device, user_agent)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (username) DO UPDATE
                SET last_seen = EXCLUDED.last_seen,
                    device    = EXCLUDED.device,
                    user_agent= EXCLUDED.user_agent;
            """, (user, now, device, ua))
            conn.commit()
    finally:
        conn.close()



def _init_pool():
    global PG_POOL
    if PG_POOL:
        return
    if not DATABASE_URL:
        raise RuntimeError('Falta DATABASE_URL para PostgreSQL')

    # Máximo 3 conexiones por defecto (suficiente en 512 MB). Sube con DB_MAX_CONN si lo necesitas.
    maxconn = max(3, int(os.environ.get('DB_MAX_CONN', '3')))
    PG_POOL = SimpleConnectionPool(
        1, maxconn,
        dsn=DATABASE_URL,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
        connect_timeout=8
    )


class PooledConn:
    """Wrapper que inicializa la sesión y se autorecupera si la conexión está rota."""
    def __init__(self, pool, conn):
        self._pool = pool
        self._conn = conn
        self._inited = False

    def _reconnect(self):
        try:
            self._pool.putconn(self._conn, close=True)
        except Exception:
            pass
        self._conn = self._pool.getconn()
        self._inited = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def cursor(self, *a, **k):
        if "cursor_factory" not in k:
            k["cursor_factory"] = psycopg2.extras.DictCursor

        if not self._inited:
            try:
                with self._conn.cursor() as c:
                    c.execute("SET application_name = 'mochitos';")
                    c.execute("SET idle_in_transaction_session_timeout = '5s';")
                    c.execute("SET statement_timeout = '5s';")
                self._inited = True
            except (OperationalError, InterfaceError):
                self._reconnect()
                with self._conn.cursor() as c:
                    c.execute("SET application_name = 'mochitos';")
                    c.execute("SET idle_in_transaction_session_timeout = '5s';")
                    c.execute("SET statement_timeout = '5s';")
                self._inited = True

        try:
            return self._conn.cursor(*a, **k)
        except (OperationalError, InterfaceError):
            self._reconnect()
            return self._conn.cursor(*a, **k)

    def close(self):
        try:
            self._pool.putconn(self._conn)
        except Exception:
            pass

def get_db_connection():
    """Saca una conexión del pool y hace ping."""
    _init_pool()
    conn = PG_POOL.getconn()
    wrapped = PooledConn(PG_POOL, conn)
    try:
        with wrapped.cursor() as c:
            c.execute("SELECT 1;")
            _ = c.fetchone()
    except (OperationalError, InterfaceError, DatabaseError):
        wrapped._reconnect()
        with wrapped.cursor() as c:
            c.execute("SELECT 1;")
            _ = c.fetchone()
    return wrapped

# ========= Zona horaria Europe/Madrid =========
def _eu_last_sunday(year: int, month: int) -> date:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    d = next_month - timedelta(days=1)
    while d.weekday() != 6:
        d -= timedelta(days=1)
    return d

def _europe_madrid_now_fallback() -> datetime:
    utc_now = datetime.now(timezone.utc)
    y = utc_now.year
    start_dst_utc = datetime(y, 3, _eu_last_sunday(y, 3).day, 1, 0, 0, tzinfo=timezone.utc)
    end_dst_utc   = datetime(y, 10, _eu_last_sunday(y, 10).day, 1, 0, 0, tzinfo=timezone.utc)
    offset_hours = 2 if start_dst_utc <= utc_now < end_dst_utc else 1
    return (utc_now + timedelta(hours=offset_hours)).replace(tzinfo=None)

def europe_madrid_now() -> datetime:
    utc_now = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return utc_now.astimezone(ZoneInfo("Europe/Madrid"))
    except Exception:
        pass
    if pytz:
        try:
            tz = pytz.timezone("Europe/Madrid")
            return tz.fromutc(utc_now.replace(tzinfo=pytz.utc))
        except Exception:
            pass
    return _europe_madrid_now_fallback()

def today_madrid() -> date:
    return europe_madrid_now().date()

def seconds_until_next_midnight_madrid() -> float:
    now = europe_madrid_now()
    nxt = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return (nxt - now).total_seconds()


def is_due_or_overdue(now_madrid: datetime, hhmm: str, grace_minutes: int = 720) -> bool:
    """Devuelve True si ya toca o si vamos tarde hasta grace_minutes (por si el server estuvo dormido)."""
    try:
        hh, mm = map(int, hhmm.split(":"))
    except Exception:
        return False
    scheduled = now_madrid.replace(hour=hh, minute=mm, second=0, microsecond=0)
    delta = (now_madrid - scheduled).total_seconds()
    return (delta >= 0) and (delta <= grace_minutes * 60)


def now_madrid_str() -> str:
    return europe_madrid_now().strftime("%Y-%m-%d %H:%M:%S")

EDVX_BASE = "https://estrenosdivx.net"
EDVX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": EDVX_BASE + "/",
}

def _get(url, timeout=12):
    r = requests.get(url, headers=EDVX_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r

def _abs(url):
    try:
        return url if bool(urlparse(url).netloc) else urljoin(EDVX_BASE, url)
    except Exception:
        return url

def _norm_text(s: str) -> str:
    if not s: return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.lower().strip().split())

def _score_title(title: str, q: str) -> int:
    t = _norm_text(title); qn = _norm_text(q)
    if not t or not qn: return 0
    score = 0
    if t.startswith(qn): score += 120
    elif qn in t:       score += 80
    score += min(40, 10 * len(set(t.split()) & set(qn.split())))
    return score

def _parse_results(html):
    results, seen = [], set()
    if BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        candidates = soup.select(
            "h2.entry-title a, .entry-title a, article h2 a, "
            ".post-title a, .result-item h2 a, .titulo a, "
            "a[href*='/pelicula/'], a[href*='/series/'], a[href*='/estreno/']"
        )
        for a in candidates:
            href = (a.get("href") or "").strip()
            title = (a.get_text(" ", strip=True) or "").strip()
            if not href or not title: continue
            url = _abs(href)
            netloc = urlparse(url).netloc or ""
            if netloc and "estrenosdivx.net" not in netloc: continue
            if any(x in url for x in ("/?s=", "/page/", "/category/", "/buscar/")) and "pelicula" not in url and "series" not in url:
                continue
            if url in seen: continue

            thumb = None
            img_tag = a.find_previous("img") or a.find_next("img")
            if img_tag and img_tag.get("src"): thumb = img_tag.get("src")

            m = re.search(r"\((19|20)\d{2}\)", title)
            year = m.group(0)[1:-1] if m else None

            results.append({
                "title": title,
                "url": _abs(url),
                "image": _abs(thumb) if thumb else None,
                "year": year,
                "source": "estrenosdivx"
            })
            seen.add(url)
        return results

    # Fallback sin bs4
    for m in re.finditer(r'<a[^>]+href="(?P<h>[^"]+)"[^>]*>(?P<t>.*?)</a>', html, flags=re.I|re.S):
        href = m.group("h") or ""
        traw = re.sub(r"<[^>]+>", " ", m.group("t") or "")
        title = " ".join((traw or "").split()).strip()
        if not href or not title: continue
        url = _abs(href)
        nu = urlparse(url)
        if nu.netloc and "estrenosdivx.net" not in nu.netloc: continue
        if all(k not in url for k in ("/pelicula/", "/series/", "/estreno/", "/ver-")): continue
        if url in seen: continue
        m2 = re.search(r"\((19|20)\d{2}\)", title)
        year = m2.group(0)[1:-1] if m2 else None
        results.append({"title": title, "url": url, "image": None, "year": year, "source": "estrenosdivx"})
        seen.add(url)
    return results

def _search_edvx(query):
    # 1) ?s=
    url_wp = f"{EDVX_BASE}/?s={quote_plus(query)}"
    try:
        r = _get(url_wp)
        res = _parse_results(r.text)
        if res: return res, url_wp
    except Exception:
        pass
    # 2) /buscar/
    url_buscar = f"{EDVX_BASE}/buscar/{quote_plus(query)}/"
    try:
        r = _get(url_buscar)
        res = _parse_results(r.text)
        if res: return res, url_buscar
    except Exception:
        pass
    return [], None

def _search_ddg_fallback(query, max_items=12):
    url = "https://duckduckgo.com/html/?q=" + requests.utils.quote(f"site:estrenosdivx.net {query}") + "&kl=es-es"
    out = []
    try:
        r = _get(url)
        if BeautifulSoup:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a.result__a, a.result__url"):
                href = (a.get("href") or "").strip()
                title = (a.get_text(" ", strip=True) or "").strip()
                if not href or not title: continue
                if "estrenosdivx.net" not in (urlparse(href).netloc or ""): continue
                out.append({"title": title, "url": href, "image": None, "year": None, "source": "duckduckgo"})
                if len(out) >= max_items: break
        else:
            for m in re.finditer(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, flags=re.I|re.S):
                href = m.group(1).strip()
                title = re.sub(r"<[^>]+>", " ", m.group(2)).strip()
                if "estrenosdivx.net" not in (urlparse(href).netloc or ""): continue
                out.append({"title": title, "url": href, "image": None, "year": None, "source": "duckduckgo"})
                if len(out) >= max_items: break
    except Exception:
        pass
    return out, url

def search_estrenos(query: str, max_items: int = 24):
    res, src = _search_edvx(query)
    if not res:
        ddg, src2 = _search_ddg_fallback(query, max_items=max_items)
        res, src = ddg, src2

    by_url, by_title = {}, set()
    for item in res:
        url = item.get("url"); title = item.get("title") or ""
        if not url or not title: continue
        tn = _norm_text(title)
        if url in by_url or tn in by_title: continue
        item["_score"] = _score_title(title, query)
        by_url[url] = item; by_title.add(tn)

    ranked = sorted(by_url.values(), key=lambda x: x.get("_score", 0), reverse=True)
    for it in ranked: it.pop("_score", None)
    return ranked[:max_items], src



# ========= SSE =========
_subscribers_lock = threading.Lock()
_subscribers: set[queue.Queue] = set()

def broadcast(event_name: str, data: dict):
    with _subscribers_lock:
        for qcli in list(_subscribers):
            try:
                qcli.put_nowait({"event": event_name, "data": data})
            except Exception:
                pass

@app.route("/events")
def sse_events():
    client_q: queue.Queue = queue.Queue(maxsize=200)
    with _subscribers_lock:
        _subscribers.add(client_q)

    def gen():
        try:
            yield ":\n\n"
            while True:
                try:
                    ev = client_q.get(timeout=15)
                    payload = json.dumps(ev['data'], separators=(',', ':'))
                    yield f"event: {ev['event']}\ndata: {payload}\n\n"
                except queue.Empty:
                    yield f": k {int(time.time())}\n\n"
        finally:
            with _subscribers_lock:
                _subscribers.discard(client_q)

    return Response(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no"
    })

# ========= Constantes =========
QUESTIONS = [
    # Amorosas / Emocionales
    "¿Qué fue lo que más te atrajo de mí al principio?",
    "¿Qué parte de nuestra relación te hace sentir más feliz?",
    "¿Qué canción te recuerda a nosotros?",
    "¿Qué harías si solo tuviéramos un día más juntos?",
    "¿Qué detalle pequeño que hago te enamora más?",
    "¿Cómo describirías nuestro amor en tres palabras?",
    "¿Qué es lo que más amas de nuestras conversaciones?",
    "¿Qué sueñas para nuestro futuro juntos?",
    "¿Qué te hace sentir más amado/a por mí?",
    "¿Qué te gustaría que nunca cambiara entre nosotros?",
    "¿Qué promesa me harías hoy sin pensarlo dos veces?",
    # Divertidas
    "Si fuéramos un dúo cómico, ¿cómo nos llamaríamos?",
    "¿Qué harías si despertaras y fueras yo por un día?",
    "¿Cuál es el apodo más ridículo que se te ocurre para mí?",
    "¿Qué canción cantarías desnudo/a en la ducha como si fuera un show?",
    "¿Qué superpoder inútil te gustaría tener?",
    "¿Qué animal representa mejor nuestra relación y por qué?",
    "¿Cuál es el momento más tonto que hemos vivido juntos?",
    "¿Qué harías si estuviéramos atrapados en un supermercado por 24 horas?",
    "¿Qué serie seríamos si nuestra vida fuera una comedia?",
    "¿Con qué personaje de dibujos animados me comparas?",
    # Picantes 🔥
    "¿Qué parte de mi cuerpo te gusta más tocar?",
    "¿Dónde te gustaría que te besara ahora mismo?",
    "¿Has fantaseado conmigo hoy?",
    "¿Cuál fue la última vez que soñaste algo caliente conmigo?",
    "¿En qué lugar prohibido te gustaría hacerlo conmigo?",
    "¿Qué prenda mía te gustaría quitarme con los dientes?",
    "¿Qué harías si estuviéramos solos en un ascensor por 30 minutos?",
    "¿Cuál es tu fantasía secreta conmigo que aún no me has contado?",
    "¿Qué juguete usarías conmigo esta noche?",
    "¿Te gustaría que te atara o prefieres atarme a mí?",
    # Creativas
    "Si tuviéramos una casa del árbol, ¿cómo sería por dentro?",
    "Si hiciéramos una película sobre nosotros, ¿cómo se llamaría?",
    "¿Cómo sería nuestro planeta si fuéramos los únicos habitantes?",
    "Si pudieras diseñar una cita perfecta desde cero, ¿cómo sería?",
    "Si nos perdiéramos en el tiempo, ¿en qué época te gustaría vivir conmigo?",
    "Si nuestra historia de amor fuera un libro, ¿cómo sería el final?",
    "Si pudieras regalarme una experiencia mágica, ¿ cuál sería?",
    "¿Qué mundo ficticio te gustaría explorar conmigo?",
    # Reflexivas
    "¿Qué aprendiste sobre ti mismo/a desde que estamos juntos?",
    "¿Qué miedos tienes sobre el futuro y cómo puedo ayudarte con ellos?",
    "¿Cómo te gustaría crecer como pareja conmigo?",
    "¿Qué errores cometiste en el pasado que no quieres repetir conmigo?",
    "¿Qué significa para ti una relación sana?",
    "¿Cuál es el mayor sueño que quieres cumplir y cómo puedo ayudarte?",
    "¿Qué necesitas escuchar más seguido de mí?",
    "¿Qué momento de tu infancia quisieras revivir conmigo al lado?",
    # Random
    "¿Cuál es el olor que más te recuerda a mí?",
    "¿Qué comida describes como ‘sexy’?",
    "¿Qué harías si fueras invisible por un día y solo yo te pudiera ver?",
    "¿Qué parte de mi rutina diaria te parece más adorable?",
    "¿Si pudieras clonar una parte de mí, cuál sería?",
    "¿Qué emoji usarías para describir nuestra relación?",
    "¿Si solo pudieras besarme o abrazarme por un mes, qué eliges?",
]
RELATION_START = date(2025, 8, 2)
INTIM_PIN = os.environ.get('INTIM_PIN', '6969')

# ========= Mini-cache =========
import time as _time
_cache_store = {}
def _cache_key(fn_name): return f"_cache::{fn_name}"
def ttl_cache(seconds=10):
    def deco(fn):
        key = _cache_key(fn.__name__)
        def wrapped(*a, **k):
            now = _time.time()
            item = _cache_store.get(key)
            if item and (now - item[0] < seconds):
                return item[1]
            val = fn(*a, **k)
            _cache_store[key] = (now, val)
            return val
        wrapped.__wrapped__ = fn
        wrapped._cache_key = key
        return wrapped
    return deco
def cache_invalidate(*fn_names):
    for name in fn_names:
        _cache_store.pop(_cache_key(name), None)

# ========= DB init (añadimos app_state y push_subscriptions si no existen) =========
def init_db():
    with closing(get_db_connection()) as conn, conn.cursor() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY, username TEXT UNIQUE, password TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS schedule_times (
            id SERIAL PRIMARY KEY, username TEXT NOT NULL, time TEXT NOT NULL,
            UNIQUE (username, time))''')
        c.execute('''CREATE TABLE IF NOT EXISTS daily_questions (
            id SERIAL PRIMARY KEY, question TEXT, date TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS answers (
            id SERIAL PRIMARY KEY, question_id INTEGER, username TEXT, answer TEXT)''')
        c.execute("ALTER TABLE answers ADD COLUMN IF NOT EXISTS created_at TEXT")
        c.execute("ALTER TABLE answers ADD COLUMN IF NOT EXISTS updated_at TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS answers_unique ON answers (question_id, username)")
        c.execute('''CREATE TABLE IF NOT EXISTS meeting (id SERIAL PRIMARY KEY, meeting_date TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS banner (
            id SERIAL PRIMARY KEY, image_data BYTEA, filename TEXT, mime_type TEXT, uploaded_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS travels (
            id SERIAL PRIMARY KEY, destination TEXT NOT NULL, description TEXT, travel_date TEXT,
            is_visited BOOLEAN DEFAULT FALSE, created_by TEXT, created_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS travel_photos (
            id SERIAL PRIMARY KEY, travel_id INTEGER, image_url TEXT NOT NULL,
            uploaded_by TEXT, uploaded_at TEXT, FOREIGN KEY(travel_id) REFERENCES travels(id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS wishlist (
            id SERIAL PRIMARY KEY, product_name TEXT NOT NULL, product_link TEXT, notes TEXT,
            created_by TEXT, created_at TEXT, is_purchased BOOLEAN DEFAULT FALSE)''')
        c.execute("""ALTER TABLE wishlist
                     ADD COLUMN IF NOT EXISTS priority TEXT
                     CHECK (priority IN ('alta','media','baja')) DEFAULT 'media'""")
        c.execute("""ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS is_gift BOOLEAN DEFAULT FALSE""")
        c.execute("""ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS size TEXT""")
        c.execute('''CREATE TABLE IF NOT EXISTS schedules (
            id SERIAL PRIMARY KEY, username TEXT NOT NULL, day TEXT NOT NULL,
            time TEXT NOT NULL, activity TEXT, color TEXT, UNIQUE(username, day, time))''')
        c.execute('''CREATE TABLE IF NOT EXISTS locations (
            id SERIAL PRIMARY KEY, username TEXT UNIQUE, location_name TEXT,
            latitude REAL, longitude REAL, updated_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS profile_pictures (
            id SERIAL PRIMARY KEY, username TEXT UNIQUE, image_data BYTEA,
            filename TEXT, mime_type TEXT, uploaded_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS intimacy_events (
            id SERIAL PRIMARY KEY, username TEXT NOT NULL, ts TEXT NOT NULL, place TEXT, notes TEXT)''')
        # App state
        c.execute('''CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY, value TEXT)''')


                # === ADMIN: notificaciones programadas ===
        c.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_notifications (
            id SERIAL PRIMARY KEY,
            created_by TEXT NOT NULL,
            target TEXT NOT NULL CHECK (target IN ('mochito','mochita','both')),
            title TEXT NOT NULL,
            body TEXT,
            url TEXT,
            tag TEXT,
            icon TEXT,
            badge TEXT,
            when_at TEXT NOT NULL,  -- Europe/Madrid 'YYYY-MM-DD HH:MM:SS'
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','cancelled')),
            created_at TEXT NOT NULL,
            sent_at TEXT
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_sched_status_when ON scheduled_notifications (status, when_at)")

            # --- Películas / Series ---
        c.execute("""
            CREATE TABLE IF NOT EXISTS media_items (
                id SERIAL PRIMARY KEY,
                title           TEXT        NOT NULL,
                cover_url       TEXT,
                link_url        TEXT,
                on_netflix      BOOLEAN     DEFAULT FALSE,
                on_prime        BOOLEAN     DEFAULT FALSE,
                priority        TEXT        CHECK (priority IN ('alta','media','baja')) DEFAULT 'media',
                comment         TEXT,                -- comentario en "Por ver"
                created_by      TEXT,
                created_at      TEXT,
                is_watched      BOOLEAN     DEFAULT FALSE,
                watched_at      TEXT,
                rating          INTEGER     CHECK (rating BETWEEN 1 AND 5),
                watched_comment TEXT                 -- comentario en "Vistas"
            );
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_media_watched_prio ON media_items (is_watched, priority, created_at DESC);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_media_watched_at   ON media_items (is_watched, watched_at DESC);")
        # Guardar reviews por usuario y media calcular medias
        c.execute("""ALTER TABLE media_items ADD COLUMN IF NOT EXISTS reviews JSONB DEFAULT '{}'::jsonb""")
        c.execute("""ALTER TABLE media_items ADD COLUMN IF NOT EXISTS avg_rating REAL""")



                # === Ciclo (menstrual / estado de ánimo) ===
        c.execute("""
        CREATE TABLE IF NOT EXISTS cycle_entries (
            id          SERIAL PRIMARY KEY,
            username    TEXT        NOT NULL,                -- propietaria de los datos (normalmente 'mochita')
            day         TEXT        NOT NULL,                -- 'YYYY-MM-DD' (fecha local Madrid)
            mood        TEXT        CHECK (mood IN (
                           'feliz','normal','triste','estresada','sensible','cansada','irritable','con_energia'
                         )),
            flow        TEXT        CHECK (flow IN ('nada','poco','medio','mucho')),
            pain        INTEGER     CHECK (pain BETWEEN 0 AND 10),
            notes       TEXT,
            created_by  TEXT,
            created_at  TEXT,
            UNIQUE (username, day)
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_cycle_user_day ON cycle_entries (username, day DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cycle_flow ON cycle_entries (flow)")



        # === Preguntas (banco editable por admin) ===
        c.execute("""
        CREATE TABLE IF NOT EXISTS question_bank (
        id SERIAL PRIMARY KEY,
        text TEXT UNIQUE NOT NULL,
        used BOOLEAN NOT NULL DEFAULT FALSE,
        used_at TEXT,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TEXT NOT NULL
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_qbank_used_active ON question_bank (used, active)")
        c.execute("ALTER TABLE daily_questions ADD COLUMN IF NOT EXISTS bank_id INTEGER")

        # Seed inicial desde QUESTIONS si el banco está vacío
        c.execute("SELECT COUNT(*) FROM question_bank")
        if (c.fetchone()[0] or 0) == 0:
            now = now_madrid_str()
            for q in QUESTIONS:
                try:
                    c.execute("INSERT INTO question_bank (text, created_at) VALUES (%s,%s)", (q, now))
                except Exception:
                    pass
            conn.commit()

        # Push subscriptions
        c.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            endpoint TEXT UNIQUE NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TEXT)''')
        # Seed básico
        try:
            c.execute("INSERT INTO users (username, password) VALUES (%s, %s) ON CONFLICT (username) DO NOTHING", ('mochito','1234'))
            c.execute("INSERT INTO users (username, password) VALUES (%s, %s) ON CONFLICT (username) DO NOTHING", ('mochita','1234'))
            now = now_madrid_str()
            c.execute("""INSERT INTO locations (username, location_name, latitude, longitude, updated_at)
                         VALUES (%s,%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING""",
                      ('mochito','Algemesí, Valencia', 39.1925, -0.4353, now))
            c.execute("""INSERT INTO locations (username, location_name, latitude, longitude, updated_at)
                         VALUES (%s,%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING""",
                      ('mochita','Córdoba', 37.8882, -4.7794, now))
            conn.commit()
        except Exception as e:
            print("Seed error:", e); conn.rollback()
        # Índices
        c.execute("CREATE INDEX IF NOT EXISTS idx_answers_q_user ON answers (question_id, username)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_travels_vdate ON travels (is_visited, travel_date DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_wishlist_state_prio ON wishlist (is_purchased, priority, created_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_intim_user_ts ON intimacy_events (username, ts DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions (username)")

        # ======= Seguimiento de PRECIO: migraciones =======
        c.execute("""ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS track_price BOOLEAN DEFAULT FALSE""")
        c.execute("""ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS last_price_cents INTEGER""")
        c.execute("""ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS currency TEXT""")
        c.execute("""ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS last_checked TEXT""")
        c.execute("""ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS alert_drop_percent REAL""")
        c.execute("""ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS alert_below_cents INTEGER""")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_wishlist_track ON wishlist (track_price, is_purchased)""")

        conn.commit()

init_db()

# ========= Helpers =========
# ========= Helpers =========
def _parse_dt(txt: str):
    if not txt:
        return None
    try:
        dt = datetime.strptime(txt.strip(), "%Y-%m-%d %H:%M:%S")  # string local sin tz
    except Exception:
        return None
    # Adjunta la zona Europe/Madrid para que sea "aware"
    try:
        from zoneinfo import ZoneInfo
        return dt.replace(tzinfo=ZoneInfo("Europe/Madrid"))
    except Exception:
        if pytz:
            try:
                return pytz.timezone("Europe/Madrid").localize(dt)
            except Exception:
                pass
    return dt  # último recurso (naive) — pero ya no restamos naive con aware en tu código


def _parse_date(dtxt: str):
    try: return datetime.strptime(dtxt.strip(), "%Y-%m-%d").date()
    except Exception: return None

ALLOWED_MOODS = ('feliz','normal','triste','estresada','sensible','cansada','irritable','con_energia')
ALLOWED_FLOWS = ('nada','poco','medio','mucho')

def get_cycle_entries(user: str, d_from: date, d_to: date):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT username, day, mood, flow, pain, notes
                FROM cycle_entries
                WHERE username=%s AND day BETWEEN %s AND %s
                ORDER BY day ASC
            """, (user, d_from.isoformat(), d_to.isoformat()))
            rows = c.fetchall()
            return [dict(r) for r in rows]
    finally:
        conn.close()

def predict_cycle(user: str, lookback_days: int = 120):
    """Predicción simple:
       - Detecta inicios de regla como días con flow != 'nada' cuya víspera no tiene sangrado.
       - Calcula longitud media entre los dos últimos inicios; si no, 28.
       - Predice próximo inicio = último_inicio + longitud.
       - Ovulación ≈ 14 días antes del próximo inicio; fértil = ovul-4 .. ovul+1
    """
    today = today_madrid()
    start = today - timedelta(days=lookback_days)
    entries = get_cycle_entries(user, start, today)
    bleed_days = set()
    for e in entries:
        if (e.get('flow') or 'nada') != 'nada':
            d = _parse_date(e['day'])
            if d: bleed_days.add(d)
    if not bleed_days:
        return {"have_data": False, "cycle_length": 28}

    # inicios = días con sangrado y día anterior SIN sangrado
    inicios = []
    for d in sorted(bleed_days):
        prev = d - timedelta(days=1)
        if prev not in bleed_days:
            inicios.append(d)
    if not inicios:
        inicios = sorted(bleed_days)

    last_start = inicios[-1]
    cycle_len = 28
    if len(inicios) >= 2:
        cycle_len = (inicios[-1] - inicios[-2]).days or 28
        if cycle_len < 21 or cycle_len > 40:
            cycle_len = 28

    next_start = last_start + timedelta(days=cycle_len)
    ovulation = next_start - timedelta(days=14)
    fertile_a = ovulation - timedelta(days=4)
    fertile_b = ovulation + timedelta(days=1)
    period_end = next_start + timedelta(days=4)

    return {
        "have_data": True,
        "cycle_length": int(cycle_len),
        "last_start": last_start.isoformat(),
        "next_start": next_start.isoformat(),
        "period_end": period_end.isoformat(),
        "ovulation": ovulation.isoformat(),
        "fertile_start": fertile_a.isoformat(),
        "fertile_end": fertile_b.isoformat(),
    }

# ======= Ciclo: helpers para /regla (basados en cycle_entries) =======
FLOW_ORDER = {'nada': 0, 'poco': 1, 'medio': 2, 'mucho': 3}

def _daterange(d1: date, d2: date):
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)

def get_periods_for_user(user: str = 'mochita', lookback_days: int = 365):
    today = today_madrid()
    start = today - timedelta(days=lookback_days)
    end   = today
    entries = get_cycle_entries(user, start, end)

    bleed_days = {}
    for e in entries:
        f = (e.get('flow') or 'nada').strip()
        if f != 'nada':
            d = _parse_date(e['day'])
            if d:
                bleed_days[d] = {
                    "flow": f,
                    "pain": e.get('pain'),
                    "notes": (e.get('notes') or '').strip()
                }

    if not bleed_days:
        return []

    # Agrupa días consecutivos como un periodo
    periods = []
    sorted_days = sorted(bleed_days.keys())
    block = [sorted_days[0]]
    for d in sorted_days[1:]:
        if d == block[-1] + timedelta(days=1):
            block.append(d)
        else:
            periods.append(block)
            block = [d]
    periods.append(block)

    out = []
    for block in periods:
        start_d = block[0]
        end_d   = block[-1]
        length  = len(block)
        max_flow = max((FLOW_ORDER.get(bleed_days[d]["flow"], 0) for d in block), default=0)
        flow_label = next(k for k, v in FLOW_ORDER.items() if v == max_flow)
        pains = [int(bleed_days[d]["pain"]) for d in block if isinstance(bleed_days[d]["pain"], int)]
        avg_pain = round(sum(pains)/len(pains), 1) if pains else None
        notes = "\n".join(sorted({bleed_days[d]["notes"] for d in block if (bleed_days[d]["notes"] or "").strip()}))
        out.append({
            "id": f"{start_d.isoformat()}_{end_d.isoformat()}",
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "length": length,
            "flow": flow_label,
            "pain": avg_pain,
            "notes": notes or ""
        })
    return out

def compute_cycle_stats(periods: list[dict], default_cycle_len: int = 28):
    today = today_madrid()
    starts = sorted([_parse_date(p['start']) for p in periods if _parse_date(p['start'])])
    if not starts:
        next_start = today + timedelta(days=default_cycle_len)
        ovul = next_start - timedelta(days=14)
        return {
            "have_data": False,
            "avg_cycle_len": default_cycle_len,
            "avg_period_len": None,
            "last_start": None,
            "next_period_date": next_start.isoformat(),
            "ovulation_day": ovul.isoformat(),
            "fertile_start": (ovul - timedelta(days=4)).isoformat(),
            "fertile_end": (ovul + timedelta(days=1)).isoformat(),
            "day_of_cycle": None,
            "days_to_next": (next_start - today).days
        }

    gaps = []
    for i in range(1, len(starts)):
        gaps.append((starts[i] - starts[i-1]).days)
    gaps = [g for g in gaps if 21 <= g <= 40]
    avg_cycle = round(sum(gaps)/len(gaps)) if gaps else default_cycle_len

    last_start = starts[-1]
    next_start = last_start + timedelta(days=avg_cycle)
    ovul = next_start - timedelta(days=14)
    fertile_a = ovul - timedelta(days=4)
    fertile_b = ovul + timedelta(days=1)
    lens = [int(p.get('length') or 0) for p in periods if p.get('length')]
    avg_period_len = round(sum(lens)/len(lens)) if lens else None
    doc = (today - last_start).days + 1 if today >= last_start else None

    return {
        "have_data": True,
        "avg_cycle_len": int(avg_cycle),
        "avg_period_len": (int(avg_period_len) if avg_period_len else None),
        "last_start": last_start.isoformat(),
        "next_period_date": next_start.isoformat(),
        "ovulation_day": ovul.isoformat(),
        "fertile_start": fertile_a.isoformat(),
        "fertile_end": fertile_b.isoformat(),
        "day_of_cycle": (int(doc) if doc and doc > 0 else None),
        "days_to_next": (next_start - today).days
    }

def build_calendar_data(year: int, month: int, periods: list[dict], stats: dict):
    import calendar
    first = date(year, month, 1)
    last  = date(year, month, calendar.monthrange(year, month)[1])
    from datetime import date as _date, datetime as _datetime
    t = today_madrid()
    today = t.date() if isinstance(t, _datetime) else (t if isinstance(t, _date) else _date.today())


    period_days = set()
    for p in periods:
        s = _parse_date(p["start"])
        e = _parse_date(p["end"]) or s
        for d in _daterange(s, e):
            period_days.add(d)

    fertile_days = set()
    if stats and stats.get("fertile_start") and stats.get("fertile_end"):
        fa = _parse_date(stats["fertile_start"])
        fb = _parse_date(stats["fertile_end"])
        if fa and fb and fa <= fb:
            for d in _daterange(fa, fb):
                fertile_days.add(d)

    ovu_day = _parse_date(stats.get("ovulation_day") or "") if stats else None

    days = []
    for d in _daterange(first, last):
        days.append({
            "iso": d.isoformat(),
            "day": d.day,
            "is_today": (d == today),
            "is_period": (d in period_days),
            "is_fertile": (d in fertile_days),
            "is_ovulation": (ovu_day == d)
        })

    return {
        "year": year,
        "month": month,
        "first_weekday": first.weekday(),  # 0=Lunes
        "days": days
    }

def save_period(start: str, end: str | None, flow: str | None, pain: str | None, notes: str | None, user: str = 'mochita'):
    s = _parse_date(start)
    e = _parse_date(end) if end else s
    if not s or not e or e < s:
        return
    flow = (flow or 'medio').strip()
    pval = None
    try:
        if pain is not None and str(pain).isdigit():
            p = int(pain)
            if 0 <= p <= 10:
                pval = p
    except Exception:
        pval = None
    now_txt = now_madrid_str()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            for d in _daterange(s, e):
                c.execute("""
                    INSERT INTO cycle_entries (username, day, mood, flow, pain, notes, created_by, created_at)
                    VALUES (%s,%s,NULL,%s,%s,%s,%s,%s)
                    ON CONFLICT (username, day) DO UPDATE
                    SET flow=EXCLUDED.flow,
                        pain=EXCLUDED.pain,
                        notes=EXCLUDED.notes,
                        created_by=EXCLUDED.created_by,
                        created_at=EXCLUDED.created_at
                """, (user, d.isoformat(), flow, pval, (notes or '').strip() or None, session.get('username','?'), now_txt))
            conn.commit()
    finally:
        conn.close()

def save_symptoms(payload: dict, user: str = 'mochita'):
    day  = (payload.get("date") or "").strip()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day or ""):
        return
    mood = (payload.get("mood") or "").strip() or None
    extras = {
        "energy": (payload.get("energy") or "").strip(),
        "bbt": (payload.get("bbt") or "").strip(),
        "had_sex": bool(payload.get("had_sex")),
        "protected": bool(payload.get("protected")),
        "symptoms": payload.get("symptoms") or [],
        "notes": (payload.get("notes") or "").strip()
    }
    extras_txt = ("; ".join(
        [f"Energía: {extras['energy']}" if extras['energy'] else "",
         f"BBT: {extras['bbt']}" if extras['bbt'] else "",
         "Sexo: sí" if extras['had_sex'] else "",
         "Protegido" if extras['protected'] else "",
         ("Sx: " + ", ".join(extras['symptoms'])) if extras['symptoms'] else "",
         extras['notes']]
    )).strip("; ").strip()
    now_txt = now_madrid_str()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT flow, pain, notes FROM cycle_entries WHERE username=%s AND day=%s", (user, day))
            prev = c.fetchone()
            prev_notes = (prev['notes'] if prev else "") or ""
            new_notes = "\n".join([t for t in [prev_notes.strip(), extras_txt] if t]).strip() or None
            c.execute("""
                INSERT INTO cycle_entries (username, day, mood, flow, pain, notes, created_by, created_at)
                VALUES (%s,%s,%s,NULL,NULL,%s,%s,%s)
                ON CONFLICT (username, day) DO UPDATE
                SET mood=EXCLUDED.mood,
                    notes=EXCLUDED.notes,
                    created_by=EXCLUDED.created_by,
                    created_at=EXCLUDED.created_at
            """, (user, day, mood, new_notes, session.get('username','?'), now_txt))
            conn.commit()
    finally:
        conn.close()

def delete_period_by_id(pid: str, user: str = 'mochita'):
    if not pid:
        return
    conn = get_db_connection()
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}', pid):
            s_txt, e_txt = pid.split('_', 1)
            s = _parse_date(s_txt); e = _parse_date(e_txt)
            if s and e and e >= s:
                with conn.cursor() as c:
                    c.execute("""
                        DELETE FROM cycle_entries
                         WHERE username=%s AND day BETWEEN %s AND %s AND COALESCE(flow,'nada') <> 'nada'
                    """, (user, s.isoformat(), e.isoformat()))
                    conn.commit()
        elif pid.isdigit():
            with conn.cursor() as c:
                c.execute("DELETE FROM cycle_entries WHERE id=%s AND username=%s", (int(pid), user))
                conn.commit()
    finally:
        conn.close()

def _reviews_dict(v):
    if isinstance(v, dict): return v
    if isinstance(v, str) and v.strip():
        try: return json.loads(v)
        except Exception: return {}
    return {}

def _compute_avg_from_reviews(rv: dict) -> float:
    vals = []
    for u, obj in (rv or {}).items():
        try:
            r = int((obj or {}).get('rating') or 0)
            if 1 <= r <= 5:
                vals.append(r)
        except Exception:
            pass
    return round(sum(vals)/len(vals), 1) if vals else 0.0

def _enrich_media_row(row, user):
    """Devuelve un dict con rating/comment de 'Tú' y del otro ya resueltos."""
    d = dict(row)
    rv = _reviews_dict(d.get('reviews') or {})
    other = other_of(user)
    def g(u, key): return (rv.get(u) or {}).get(key)
    d['rating_me']     = int(g(user,  'rating') or 0)
    d['rating_other']  = int(g(other, 'rating') or 0)
    d['comment_me']    = g(user,  'comment') or ''
    d['comment_other'] = g(other, 'comment') or ''
    d['avg_rating']    = d.get('avg_rating') or 0
    return d


# --- Lógica dependiente de fecha en Madrid ---
def get_intim_stats():
    today = today_madrid()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT ts FROM intimacy_events WHERE username IN ('mochito','mochita')")
            rows = c.fetchall()
    finally:
        conn.close()
    dts = []
    for r in rows:
        dt = _parse_dt(r[0])
        if dt:
            dts.append(dt)
    if not dts:
        return {'today_count': 0, 'month_total': 0, 'year_total': 0, 'days_since_last': None, 'last_dt': None, 'streak_days': 0}
    today_count = sum(1 for dt in dts if dt.date() == today)
    month_total = sum(1 for dt in dts if dt.year == today.year and dt.month == today.month)
    year_total = sum(1 for dt in dts if dt.year == today.year)
    last_dt = max(dts); days_since_last = (today - last_dt.date()).days
    if today_count == 0:
        streak_days = 0
    else:
        dates_with = {dt.date() for dt in dts}
        streak_days = 0; cur = today
        while cur in dates_with:
            streak_days += 1; cur = cur - timedelta(days=1)
    return {'today_count': today_count, 'month_total': month_total, 'year_total': year_total,
            'days_since_last': days_since_last, 'last_dt': last_dt, 'streak_days': streak_days}

def get_intim_events(limit: int = 200):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""SELECT id, username, ts, place, notes
                         FROM intimacy_events
                         WHERE username IN ('mochito','mochita')
                         ORDER BY ts DESC LIMIT %s""", (limit,))
            rows = c.fetchall()
            return [{'id': r[0], 'username': r[1], 'ts': r[2], 'place': r[3] or '', 'notes': r[4] or ''} for r in rows]
    finally:
        conn.close()

def require_admin():
    if 'username' not in session or session['username'] != 'mochito':
        abort(403)

def qbank_pick_random(conn) -> dict | None:
    with conn.cursor() as c:
        c.execute("""
            SELECT id, text
            FROM question_bank
            WHERE active=TRUE AND used=FALSE
            ORDER BY RANDOM()
            LIMIT 1
        """)
        row = c.fetchone()
        return dict(row) if row else None

def qbank_mark_used(conn, qid: int, used: bool):
    with conn.cursor() as c:
        if used:
            c.execute("UPDATE question_bank SET used=TRUE, used_at=%s WHERE id=%s", (now_madrid_str(), qid))
        else:
            c.execute("UPDATE question_bank SET used=FALSE, used_at=NULL WHERE id=%s", (qid,))
    conn.commit()


def parse_datetime_local_to_madrid(dt_local: str) -> str | None:
    """Recibe 'YYYY-MM-DDTHH:MM' de <input type='datetime-local'> y lo guarda como texto local 'YYYY-MM-DD HH:MM:SS' Europe/Madrid."""
    if not dt_local:
        return None
    try:
        naive = datetime.strptime(dt_local.strip(), "%Y-%m-%dT%H:%M")
        # lo consideramos hora local Madrid (sin tz) y guardamos como texto "local"
        return naive.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def get_today_question(today: date | None = None):
    if today is None:
        today = today_madrid()
    today_str = today.isoformat()
    conn = get_db_connection()
    try:
        # ¿Hay ya pregunta para hoy?
        with conn.cursor() as c:
            c.execute("""
                SELECT id, question, bank_id
                FROM daily_questions
                WHERE TRIM(date)=%s
                ORDER BY id DESC
                LIMIT 1
            """, (today_str,))
            row = c.fetchone()
            if row:
                return row['id'], row['question']

        # No existe aún -> elige del banco (SIN usar)
        picked = qbank_pick_random(conn)
        if not picked:
            # 🚫 Ya no reciclamos. Si no quedan, avisamos.
            return (None, "Ya no quedan preguntas sin usar. Añade más en Admin → Preguntas.")

        with conn.cursor() as c:
            c.execute("""
                INSERT INTO daily_questions (question, date, bank_id)
                VALUES (%s,%s,%s)
                RETURNING id
            """, (picked['text'], today_str, picked['id']))
            qid = c.fetchone()[0]
            conn.commit()

        # Marca esa del banco como usada (desde el momento en que está activa para HOY)
        qbank_mark_used(conn, picked['id'], True)

        return qid, picked['text']
    finally:
        conn.close()



def reroll_today_question() -> bool:
    """Cambia la pregunta de HOY sin romper la racha.
       - La que estaba vuelve al bombo (used=False).
       - La nueva queda marcada used=True.
    """
    today = today_madrid()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT id, bank_id
                FROM daily_questions
                WHERE date=%s
                ORDER BY id DESC
                LIMIT 1
            """, (today.isoformat(),))
            row = c.fetchone()
            if not row:
                return False
            dq_id, old_bank_id = row['id'], row['bank_id']

        # Liberamos la anterior (si había)
        if old_bank_id:
            qbank_mark_used(conn, old_bank_id, False)

        newq = qbank_pick_random(conn)
        if not newq:
            # Si no hay nueva, restauramos el estado de la vieja
            if old_bank_id:
                qbank_mark_used(conn, old_bank_id, True)
            return False

        with conn.cursor() as c:
            c.execute("DELETE FROM answers WHERE question_id=%s", (dq_id,))
            c.execute("UPDATE daily_questions SET question=%s, bank_id=%s WHERE id=%s",
                      (newq['text'], newq['id'], dq_id))
            conn.commit()

        qbank_mark_used(conn, newq['id'], True)

        try:
            broadcast("dq_change", {"id": int(dq_id)})
        except Exception:
            pass
        return True
    finally:
        conn.close()



def days_together():
    return max((today_madrid() - RELATION_START).days, 0)


def days_until_meeting():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT meeting_date FROM meeting ORDER BY id DESC LIMIT 1")
            row = c.fetchone()
            if not row:
                return None
            meeting_date = datetime.strptime(row[0], "%Y-%m-%d").date()
            # IMPORTANTE: NO recortar a 0; queremos valores negativos el día siguiente
            return (meeting_date - today_madrid()).days
    finally:
        conn.close()


@ttl_cache(seconds=30)
def get_banner():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT image_data, mime_type FROM banner ORDER BY id DESC LIMIT 1")
            row = c.fetchone()
            if row:
                return f"data:{row[1]};base64,{b64encode(row[0]).decode('utf-8')}"
            return None
    finally:
        conn.close()

@ttl_cache(seconds=30)
def get_profile_pictures():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT username, image_data, mime_type FROM profile_pictures")
            pics = {}
            for u, img, mt in c.fetchall():
                pics[u] = f"data:{mt};base64,{b64encode(img).decode('utf-8')}"
            return pics
    finally:
        conn.close()

@ttl_cache(seconds=8)
def compute_streaks():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT dq.id, dq.date, COUNT(DISTINCT a.username) AS cnt
                FROM daily_questions dq
                LEFT JOIN answers a 
                  ON a.question_id = dq.id
                 AND a.username IN ('mochito','mochita')
                GROUP BY dq.id, dq.date
                ORDER BY dq.date ASC
            """)
            rows = c.fetchall()
    finally:
        conn.close()
    if not rows:
        return 0, 0
    def parse_d(dtxt): return datetime.strptime(dtxt, "%Y-%m-%d").date()
    compl = [parse_d(r[1]) for r in rows if r[2] and int(r[2]) >= 2]
    if not compl:
        return 0, 0
    compl = sorted(compl)
    best = run = 1
    for i in range(1, len(compl)):
        if compl[i] == compl[i-1] + timedelta(days=1):
            run += 1
        else:
            run = 1
        best = max(best, run)
    today = today_madrid()
    latest = None
    for d in sorted(set(compl), reverse=True):
        if d <= today:
            latest = d; break
    if latest is None:
        return 0, best
    cur = 1; d = latest - timedelta(days=1)
    sset = set(compl)
    while d in sset:
        cur += 1; d -= timedelta(days=1)
    return cur, best

# ========= Web Push helpers =========
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
VAPID_PUBLIC_KEY_HEX = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com").strip()

def hex_to_base64url(hexstr: str) -> str:
    try:
        b = binascii.unhexlify(hexstr)
        s = base64.urlsafe_b64encode(b).decode().rstrip("=")
        return s
    except Exception:
        return ""

def get_vapid_public_base64url() -> str:
    return hex_to_base64url(VAPID_PUBLIC_KEY_HEX)

def get_subscriptions_for(user: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE username=%s""",(user,))
            return [{"id": r[0], "endpoint": r[1], "keys": {"p256dh": r[2], "auth": r[3]}} for r in c.fetchall()]
    finally:
        conn.close()

def _delete_subscription_by_endpoint(endpoint: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM push_subscriptions WHERE endpoint=%s", (endpoint,))
            conn.commit()
    finally:
        conn.close()

def send_push_raw(sub: dict, payload: dict):
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY_HEX:
        return False
    try:
        webpush(
            subscription_info={
                "endpoint": sub["endpoint"],
                "keys": {
                    "p256dh": sub["keys"]["p256dh"],
                    "auth": sub["keys"]["auth"],
                }
            },
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_SUBJECT},
            timeout=5
        )
        return True
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):
            try:
                _delete_subscription_by_endpoint(sub["endpoint"])
            except Exception:
                pass
        print(f"[push] error: {e}")
        return False
    except Exception as e:
        print(f"[push] error gen: {e}")
        return False

def send_push_to(user: str,
                 title: str,
                 body: str | None = None,
                 data: dict | None = None,
                 url: str | None = None,
                 tag: str | None = None,
                 icon: str | None = None,
                 badge: str | None = None):
    subs = get_subscriptions_for(user)
    payload = {"title": title}
    if body is not None and body != "": payload["body"] = body
    if url:   payload["url"]   = url
    if tag:   payload["tag"]   = tag
    if icon:  payload["icon"]  = icon
    if badge: payload["badge"] = badge
    if data:  payload["data"]  = data
    ok = False
    for sub in subs:
        ok = send_push_raw(sub, payload) or ok
    return ok

def send_push_both(title: str, body: str | None = None, **kw):
    send_push_to('mochito', title, body, **kw)
    send_push_to('mochita', title, body, **kw)

# ---- Plantillas de notificaciones (ES) + planificador de avisos de encuentro ----
def push_wishlist_notice(creator: str, is_gift: bool):
    other = 'mochita' if creator == 'mochito' else 'mochito'
    if is_gift:
        title = "🎁 Nuevo regalo"
        body  = "Tu pareja ha añadido un regalo (oculto) a la lista."
        tag   = "wishlist-gift"
    else:
        title = "🛍️ Nuevo deseo"
        body  = "Se ha añadido un artículo a la lista de deseos."
        tag   = "wishlist-item"
    send_push_to(other, title, body, url="/#wishlist", tag=tag)



def push_media_added(creator: str, mid: int, title: str,
                     on_netflix: bool = False, on_prime: bool = False,
                     link_url: str | None = None):
    """🔔 Notifica al otro cuando se añade un título a 'Por ver'."""
    other = other_of(creator)
    plataformas = []
    if on_netflix: plataformas.append("Netflix")
    if on_prime:   plataformas.append("Prime Video")
    suf = f" ({' · '.join(plataformas)})" if plataformas else ""
    send_push_to(
        other,
        title="🎬 Nueva para ver",
        body=f"Tu pareja ha añadido “{title}”{suf}.",
        url="/#pelis",
        tag=f"media-add-{mid}"
    )

def push_media_watched(watcher: str, mid: int, title: str, rating: int | None = None):
    """🔔 Notifica al otro cuando se marca como vista (incluye nota si hay)."""
    other = other_of(watcher)
    stars = f" · {rating}/5 ⭐" if (isinstance(rating, int) and 1 <= rating <= 5) else ""
    send_push_to(
        other,
        title="👀 Marcada como vista",
        body=f"Tu pareja ha marcado “{title}” como vista{stars}.",
        url="/#pelis",
        tag=f"media-watched-{mid}"
    )


def push_answer_notice(responder: str):
    other = 'mochita' if responder == 'mochito' else 'mochito'
    who   = "tu pareja" if responder in ("mochito", "mochita") else responder
    send_push_to(other,
                 title="Pregunta del día",
                 body=f"{who} ha respondido la pregunta de hoy. ¡Mírala! 💬",
                 url="/#pregunta",
                 tag="dq-answer")

def push_daily_new_question():
    send_push_both(title="Nueva pregunta del día",
                   body="Ya puedes responder la pregunta de hoy. ¡Mantén la racha! 🔥",
                   url="/#pregunta",
                   tag="dq-new")

def push_last_hours(user: str):
    send_push_to(user,
                 title="Últimas horas para contestar",
                 body="Te quedan pocas horas para responder la pregunta de hoy.",
                 url="/#pregunta",
                 tag="dq-last-hours")

def push_meeting_countdown(dleft: int):
    title = f"¡Queda {dleft} día para veros!" if dleft == 1 else f"¡Quedan {dleft} días para veros!"
    send_push_both(title=title,
                   body="Que ganaaasssss!! ❤️‍🔥",
                   url="/#contador",
                   tag="meeting-countdown")



# --- Nuevas notificaciones románticas ---
def push_relationship_day(day_count: int):
    """💖 Notifica días especiales de relación"""
    title = f"💖 Hoy cumplís {day_count} días juntos"
    body = "Gracias por estar cada dia a mi lado."
    tag = f"relationship-{day_count}"
    send_push_both(title=title, body=body, url="/#contador", tag=tag)
    send_discord("Relationship milestone", {"days": day_count})


def push_relationship_milestones():
    """Comprueba si hoy se cumple algún hito (75,100,150,200,250,300,365...)"""
    days = days_together()
    milestones = [75, 100, 150, 200, 250, 300, 365]
    if days in milestones:
        # Evita enviar varias veces el mismo día
        key = f"milestone_{days}"
        if not state_get(key, ""):
            push_relationship_day(days)
            state_set(key, "1")


def push_relationship_daily():
    """Envía cada día: 'Hoy cumplís X días juntos' (salta si hoy es hito)."""
    days = days_together()
    if days <= 0:
        return  # por si RELATION_START estuviera en el futuro

    milestones = {75, 100, 150, 200, 250, 300, 365}
    if days in milestones:
        return  # el hito ya lo anuncia push_relationship_milestones()

    # Evita duplicados el mismo día
    key = f"rel_daily::{today_madrid().isoformat()}"
    if not state_get(key, ""):
        send_push_both(
            title=f"💖 Hoy cumplís {days} días juntos",
            body="Un día más, y cada vez mejor 🥰",
            url="/#contador",
            tag=f"relationship-day-{days}"
        )
        state_set(key, "1")


def push_gift_purchased(row):
    """🎁 Notifica cuando un regalo oculto (is_gift=True) pasa a comprado"""
    created_by = row.get("created_by")
    other = other_of(created_by)
    pname = row.get("product_name", "Regalo")
    send_push_to(other,
                 title="🎁 Tu regalo está listo",
                 body="Tu pareja ha marcado como comprado un regalo 💝",
                 url="/#wishlist",
                 tag=f"gift-purchased-{row['id']}")
    send_discord("Gift purchased", {"id": row["id"], "by": created_by, "name": pname})


# ========= App state helpers =========
def state_get(key: str, default: str | None = None) -> str | None:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT value FROM app_state WHERE key=%s", (key,))
            row = c.fetchone()
            return row[0] if row else default
    finally:
        conn.close()

def state_set(key: str, value: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""INSERT INTO app_state (key,value) VALUES (%s,%s)
                         ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""", (key, value))
            conn.commit()
    finally:
        conn.close()

def _meet_times_key(day: date): return f"meet_times::{day.isoformat()}"
def _meet_sent_key(day: date, hhmm: str): return f"meet_sent::{day.isoformat()}::{hhmm}"

def ensure_meet_times(day: date):
    key = _meet_times_key(day)
    raw = state_get(key, "")
    try:
        times = json.loads(raw) if raw else None
    except Exception:
        times = None
    if not times:
        n = random.choice([2, 3])
        chosen = set()
        while len(chosen) < n:
            h = random.randint(9, 23)
            m = random.randint(0, 59)
            chosen.add(f"{h:02d}:{m:02d}")
        times = sorted(chosen)
        state_set(key, json.dumps(times))
    return times



def _rel_daily_time_key(day: date) -> str:
    return f"rel_daily_time::{day.isoformat()}"

def _rel_daily_sent_key(day: date, hhmm: str) -> str:
    return f"rel_daily_sent::{day.isoformat()}::{hhmm}"

def ensure_rel_daily_time(day: date, now_md: datetime) -> str | None:
    """
    Guarda y devuelve una hora HH:MM para el aviso diario de relación,
    elegida UNA VEZ por día dentro de 09:00–23:59. Si ya pasó la ventana, marca como omitido.
    Si el servidor arranca después de las 09:00, elige entre 'ahora' y 23:59.
    """
    key = _rel_daily_time_key(day)
    raw = (state_get(key, "") or "").strip()
    if raw == "-":
        return None
    if re.fullmatch(r"\d{2}:\d{2}", raw or ""):
        return raw

    # Ventana [09:00, 23:59]
    start_min = 9*60
    end_min   = 23*60 + 59

    # punto de partida: si es hoy, no programar en el pasado
    if now_md.date() == day:
        now_min = now_md.hour*60 + now_md.minute
        low = max(start_min, now_min)
    else:
        low = start_min

    if low > end_min:
        # ya fuera de ventana → omitir el día
        state_set(key, "-")
        return None

    pick = random.randint(low, end_min)
    hh, mm = divmod(pick, 60)
    hhmm = f"{hh:02d}:{mm:02d}"
    state_set(key, hhmm)
    return hhmm

def maybe_push_relationship_daily_windowed(now_md: datetime):
    """
    Envia la notificación diaria 'Hoy cumplís X días…' una vez entre 09:00–23:59.
    Evita días con hitos (75/100/150/200/250/300/365) porque esos los anuncia otro flujo.
    """
    dcount = days_together()
    if dcount <= 0:
        return

    milestones = {75, 100, 150, 200, 250, 300, 365}
    if dcount in milestones:
        return  # el hito se anuncia por push_relationship_milestones()

    day = now_md.date()
    hhmm = ensure_rel_daily_time(day, now_md)
    if not hhmm:
        return  # omitido hoy o fuera de ventana

    sent_key = _rel_daily_sent_key(day, hhmm)
    if state_get(sent_key, ""):
        return  # ya enviada hoy

    if is_due_or_overdue(now_md, hhmm, 720):
        # Enviar ahora
        send_push_both(
            title=f"💖 Hoy cumplís {dcount} días juntos",
            body="Un día más, y cada vez mejor 🥰",
            url="/#contador",
            tag=f"relationship-day-{dcount}"
        )
        state_set(sent_key, "1")


def due_now(now_madrid: datetime, hhmm: str) -> bool:
    try:
        hh, mm = map(int, hhmm.split(":"))
    except Exception:
        return False
    scheduled = now_madrid.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return abs((now_madrid - scheduled).total_seconds()) <= 90

# ========= Seguimiento de PRECIO: helpers =========
PRICE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

def _to_cents_from_str(s: str) -> int | None:
    if not s: return None
    s = s.strip()
    s_plain = re.sub(r"[^\d,.\-]", "", s)
    if not s_plain: return None
    if "," in s_plain and "." in s_plain:
        last = max(s_plain.rfind(","), s_plain.rfind("."))
        int_part = re.sub(r"[.,]", "", s_plain[:last])
        frac = s_plain[last+1:last+3].ljust(2,"0")
        num = f"{int_part}.{frac}"
    elif "," in s_plain:
        parts = s_plain.split(",")
        if len(parts[-1]) in (1,2):
            num = f"{''.join(parts[:-1])}.{parts[-1].ljust(2,'0')}"
        else:
            num = s_plain.replace(",", "")
    else:
        num = s_plain
    try:
        return int(round(float(num) * 100))
    except Exception:
        return None

def _find_in_jsonld(html: str):
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, flags=re.S|re.I):
        blob = m.group(1)
        pm = re.search(r'"price"\s*:\s*"?(?P<p>[\d.,]+)"?', blob)
        cm = re.search(r'"priceCurrency"\s*:\s*"?(?P<c>[A-Z]{3})"?', blob)
        if pm:
            cents = _to_cents_from_str(pm.group("p"))
            curr = (cm.group("c") if cm else None) or ("EUR" if "€" in html else None)
            if cents: return cents, curr
    return None, None

def _meta_price(soup):
    for selector in [
        ('meta', {'property': 'product:price:amount'}),
        ('meta', {'property': 'og:price:amount'}),
        ('meta', {'name': 'twitter:data1'}),
        ('meta', {'itemprop': 'price'}),
    ]:
        tag = soup.find(*selector)
        val = (tag.get('content') if tag else None)
        if val:
            cents = _to_cents_from_str(val)
            if cents: return cents
    tag = soup.find(attrs=lambda at: at and any('price' in k for k in at.keys()))
    if tag:
        for k,v in tag.attrs.items():
            if 'price' in k and v:
                cents = _to_cents_from_str(str(v))
                if cents: return cents
    return None


# ======== Presencia (helpers) ========
_presence_ready = False
_presence_lock = threading.Lock()

def ensure_presence_table():
    global _presence_ready
    if _presence_ready:
        return
    with _presence_lock:
        if _presence_ready:
            return
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                c.execute("""
                CREATE TABLE IF NOT EXISTS user_presence (
                    username    text PRIMARY KEY,
                    last_seen   timestamptz NOT NULL,
                    device      text,
                    user_agent  text
                );
                """)
                conn.commit()
            _presence_ready = True
        finally:
            conn.close()

def other_of(u: str) -> str:
    if u == 'mochito': return 'mochita'
    if u == 'mochita': return 'mochito'
    return 'mochita'  # fallback



def fetch_price(url:str)->tuple[int|None,str|None,str|None]:
    try:
        # Stream con tope de bytes: evita tragarse HTML de 10-20 MB
        with requests.get(
            url,
            timeout=(3.05, 6),
            headers={'User-Agent': PRICE_UA},
            stream=True
        ) as resp:
            resp.raise_for_status()
            max_bytes = int(os.environ.get('PRICE_MAX_BYTES', '1200000'))  # ~1.2MB
            buf = io.BytesIO()
            for chunk in resp.iter_content(chunk_size=16384):
                if not chunk:
                    break
                buf.write(chunk)
                if buf.tell() >= max_bytes:
                    break
            html = buf.getvalue().decode(resp.encoding or 'utf-8', errors='ignore')

        # Título sin BeautifulSoup si no hace falta
        mtitle = re.search(r'<title>(.*?)</title>', html, flags=re.I|re.S)
        title = (mtitle.group(1).strip() if mtitle else None)

        # Prioriza JSON-LD (rápido)
        c_jsonld, curr = _find_in_jsonld(html)

        cents_dom = None
        soup = None
        # Solo parsea con BS4 si el HTML no es gigante
        if BeautifulSoup and len(html) <= 400_000:
            soup = BeautifulSoup(html, 'html.parser')
            cents_dom = _meta_price(soup)

        cents_rx = None
        for pat in [r'€\s*\d[\d\.\,]*', r'\d[\d\.\,]*\s*€', r'\b\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})\b']:
            m = re.search(pat, html)
            if m:
                cents_rx = _to_cents_from_str(m.group(0))
                break

        price = c_jsonld or cents_dom or cents_rx
        if price:
            if not curr:
                curr = 'EUR' if '€' in html else None
            return price, curr, title
        return None, None, title
    except Exception as e:
        print('[price] fetch error:', e)
        return None, None, None


def fmt_eur(cents: int | None) -> str:
    if cents is None: return "—"
    euros = cents/100.0
    return f"{euros:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def _maybe_shrink_image(image_data: bytes, max_px: int = 1600, max_kb: int = 500):
    """
    Devuelve (bytes_optimizados, mime_override_o_None). Intenta convertir a WebP y limitar tamaño.
    Si no hay PIL, corta a max_kb como red de seguridad (no cambia funcionalidad visible).
    """
    try:
        from PIL import Image  # pip install Pillow (opcional)
        im = Image.open(io.BytesIO(image_data)).convert('RGB')
        w, h = im.size
        if max(w, h) > max_px:
            ratio = max_px / float(max(w, h))
            im = im.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, format='WEBP', quality=82, method=6)
        out_bytes = out.getvalue()
        if len(out_bytes) <= len(image_data):
            return out_bytes, 'image/webp'
    except Exception:
        pass

    if len(image_data) > max_kb * 1024:
        # Último recurso: recorte duro para evitar BYTEA gigantes (mantiene “una imagen”, no cambia rutas)
        return image_data[:max_kb * 1024], None
    return image_data, None


def notify_price_drop(row, old_cents: int, new_cents: int):
    """Notifica bajada de precio.
       - Si es regalo (is_gift=True): SOLO al creador.
       - Si no es regalo: a ambos.
    """
    # row es DictRow -> acceder por clave
    wid         = row['id']
    pname       = row['product_name']
    link        = row.get('product_link')
    created_by  = row.get('created_by')
    is_gift     = bool(row.get('is_gift'))

    drop = old_cents - new_cents
    pct = (drop / old_cents) * 100 if old_cents else 0.0
    title = "⬇️ ¡Bajada de precio!"
    body  = f"{pname}\nDe {fmt_eur(old_cents)} a {fmt_eur(new_cents)} (−{pct:.1f}%)."
    tag   = f"price-drop-{wid}"

    try:
        if is_gift and created_by in ("mochito", "mochita"):
            # Regalo → notificar SOLO al creador
            send_push_to(created_by, title=title, body=body, url=link, tag=tag)
        else:
            # No es regalo → ambos
            send_push_both(title=title, body=body, url=link, tag=tag)
    except Exception as e:
        print("[push price]", e)

    send_discord("Price drop", {
        "wid": int(wid),
        "name": pname,
        "old": old_cents,
        "new": new_cents,
        "pct": round(pct, 1),
        "is_gift": is_gift,
        "created_by": created_by
    })


def form_or_json(*keys, default=None):
    j = request.get_json(silent=True) or {}
    for k in keys:
        if k in request.form: return request.form.get(k)
        if k in j:           return j.get(k)
    return default


def sweep_price_checks(max_items: int = 6, min_age_minutes: int = 180):
    """Consulta algunos items con tracking y actualiza si baja el precio."""
    now_txt = now_madrid_str()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
              SELECT id, product_name, product_link, created_by,
                     is_gift,
                     track_price, last_price_cents, currency, last_checked,
                     COALESCE(alert_drop_percent, 0) AS alert_pct,
                     alert_below_cents
              FROM wishlist
              WHERE track_price = TRUE AND is_purchased = FALSE
                    AND product_link IS NOT NULL AND TRIM(product_link) <> ''
              ORDER BY COALESCE(last_checked, '1970-01-01') ASC
              LIMIT %s
            """, (max_items,))
            rows = c.fetchall()

        for row in rows:
            wid = row['id']
            last_checked = _parse_dt(row['last_checked'] or "")
            if last_checked:
                from datetime import timedelta as _td
                if (europe_madrid_now() - last_checked) < _td(minutes=min_age_minutes):
                    continue

            price_cents, curr, _title = fetch_price(row['product_link'])
            if not price_cents:
                with conn.cursor() as c2:
                    c2.execute("UPDATE wishlist SET last_checked=%s WHERE id=%s", (now_txt, wid))
                    conn.commit()
                continue

            old = row['last_price_cents']
            notify = False
            if old is None:
                pass
            else:
                if price_cents < old:
                    drop_pct = (old - price_cents) * 100 / old if old else 0
                    if row['alert_below_cents'] is not None and price_cents <= int(row['alert_below_cents']):
                        notify = True
                    elif row['alert_pct'] and drop_pct < float(row['alert_pct']):
                        notify = False
                    else:
                        notify = True

            with conn.cursor() as c3:
                c3.execute("""
                  UPDATE wishlist
                  SET last_price_cents=%s,
                      currency=%s,
                      last_checked=%s
                  WHERE id=%s
                """, (price_cents, curr or row['currency'] or "EUR", now_txt, wid))
                conn.commit()

            if notify and old is not None:
                notify_price_drop(row, old, price_cents)

    finally:
        conn.close()



def run_scheduled_jobs(now=None):
    """Ejecuta UNA iteración del scheduler (idéntico a lo que haces dentro del while del background_loop)."""
    now = now or europe_madrid_now()
    today = now.date()

    # 1) Pregunta del día + push (una vez al día)
    last_dq_push = state_get("last_dq_push_date", "")
    if str(today) != last_dq_push:
        qid, _ = get_today_question(today)   # fecha local
        if qid:
            push_daily_new_question()
            state_set("last_dq_push_date", str(today))
            cache_invalidate('compute_streaks')
        else:
            send_discord("Daily question skipped (empty bank)", {"date": str(today)})

    # 2) Aviso diario "Hoy cumplís X días..." (ventana 09:00–23:59, tolerante a atrasos via is_due_or_overdue)
    try:
        maybe_push_relationship_daily_windowed(now)
    except Exception as e:
        print("[tick rel_daily_window]", e)

    # 3) Hitos 75/100/150/200/250/300/365
    try:
        push_relationship_milestones()
    except Exception as e:
        print("[tick milestone]", e)

    # 4) Notificaciones programadas (admin)
    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                c.execute("""
                    SELECT id, target, title, body, url, tag, icon, badge, when_at
                    FROM scheduled_notifications
                    WHERE status='pending'
                    ORDER BY when_at ASC
                    LIMIT 25
                """)
                rows = c.fetchall()

            now_local = europe_madrid_now()
            for r in rows:
                when_dt = _parse_dt(r['when_at'] or "")
                if when_dt and when_dt <= now_local:
                    target = r['target']
                    if target == 'both':
                        send_push_both(title=r['title'], body=r['body'] or None,
                                       url=r['url'], tag=r['tag'], icon=r['icon'], badge=r['badge'])
                    else:
                        send_push_to(target, title=r['title'], body=r['body'] or None,
                                     url=r['url'], tag=r['tag'], icon=r['icon'], badge=r['badge'])
                    with conn.cursor() as c2:
                        c2.execute("""
                           UPDATE scheduled_notifications
                           SET status='sent', sent_at=%s
                           WHERE id=%s
                        """, (now_madrid_str(), r['id']))
                        conn.commit()
                    send_discord("Admin: push sent (scheduled)", {"id": int(r['id']), "title": r['title']})
        finally:
            conn.close()
    except Exception as e:
        print("[tick scheduled push]", e)

    # 5) Countdown para meeting (1–3 días)
    d = days_until_meeting()
    if d is not None and d in (1, 2, 3):
        times = ensure_meet_times(today)
        for hhmm in times:
            sent_key = _meet_sent_key(today, hhmm)
            if not state_get(sent_key, "") and is_due_or_overdue(now, hhmm, 720):
                push_meeting_countdown(d)
                state_set(sent_key, "1")
    else:
        state_set(_meet_times_key(today), json.dumps([]))

    # 6) Últimas 3h para responder la DQ
    try:
        qid, _ = get_today_question(today)
    except Exception:
        qid = None
    if qid:
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                c.execute("SELECT username FROM answers WHERE question_id=%s", (qid,))
                have = {r[0] for r in c.fetchall()}
        finally:
            conn.close()
        secs_left = seconds_until_next_midnight_madrid()
        if secs_left <= 3 * 3600:
            for u in ('mochito', 'mochita'):
                if u not in have:
                    key = f"late_reminder_{today.isoformat()}_{u}"
                    if not state_get(key, ""):
                        push_last_hours(u)
                        state_set(key, "1")

    # 7) Barrido de precios cada ~3 horas
    try:
        last_sweep = state_get("last_price_sweep", "")
        do = True
        if last_sweep:
            dt = _parse_dt(last_sweep)
            if dt:
                from datetime import timedelta as _td
                do = (europe_madrid_now() - dt) >= _td(hours=3)
        if do:
            sweep_price_checks(max_items=6, min_age_minutes=180)
            state_set("last_price_sweep", now_madrid_str())
    except Exception as e:
        print("[tick price sweep]", e)


def background_loop():
    """Bucle ligero que delega en run_scheduled_jobs()."""
    print('[bg] scheduler iniciado (thin)')
    # Si quieres el mismo ritmo que antes (~45s), dejo 45 por defecto
    interval = int(os.environ.get('SCHEDULER_INTERVAL', '45'))
    while True:
        try:
            run_scheduled_jobs()  # aquí ya están: DQ diaria, aviso romántico, hitos, scheduled, meeting, último aviso, precios...
        except Exception as e:
            print(f"[bg] error: {e}")
        time.sleep(interval)


# ========= Rutas =========
@app.route('/', methods=['GET', 'POST'])
def index():
    # LOGIN
    if 'username' not in session:
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '').strip()
            conn = get_db_connection()
            try:
                with conn.cursor() as c:
                    c.execute("SELECT password FROM users WHERE username=%s", (username,))
                    row = c.fetchone()
                    if not row:
                        send_discord("Login FAIL", {"username_intent": username, "reason": "user_not_found"})
                        return render_template('index.html', error="Usuario o contraseña incorrecta", profile_pictures={})
                    stored = row[0]
                    if _is_hashed(stored):
                        ok = check_password_hash(stored, password); mode = "hashed"
                    else:
                        ok = (stored == password); mode = "plaintext"
                    if ok:
                        session['username'] = username
                        send_discord("Login OK", {"username": username, "mode": mode})
                        return redirect('/')
                    else:
                        send_discord("Login FAIL", {"username_intent": username, "reason": "bad_password", "mode": mode})
                        return render_template('index.html', error="Usuario o contraseña incorrecta", profile_pictures={})
            finally:
                conn.close()
        return render_template('index.html', error=None, profile_pictures={})

    # LOGUEADO
    user = session.get('username')  # <- mover fuera del if anterior
    # 👉 Marca presencia al cargar la página (crea/actualiza la fila)
    try:
        touch_presence(user, device='page-view')
    except Exception as e:
        app.logger.warning("touch_presence failed: %s", e)

    question_id, question_text = get_today_question()  # usa fecha de Madrid por defecto
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            # ------------ POST acciones ------------
            # 1) Foto perfil
            if request.method == 'POST' and 'update_profile' in request.form and 'profile_picture' in request.files:
                file = request.files['profile_picture']
                if file and file.filename:
                    raw = file.read()
                    img_bytes, mime_over = _maybe_shrink_image(raw)  # ⬅️ aquí se optimiza
                    filename = secure_filename(file.filename)
                    mime_type = mime_over or file.mimetype
                    c.execute("""
                        INSERT INTO profile_pictures (username, image_data, filename, mime_type, uploaded_at)
                        VALUES (%s,%s,%s,%s,%s)
                        ON CONFLICT (username) DO UPDATE
                        SET image_data=EXCLUDED.image_data, filename=EXCLUDED.filename,
                            mime_type=EXCLUDED.mime_type, uploaded_at=EXCLUDED.uploaded_at
                    """, (user, img_bytes, filename, mime_type, now_madrid_str()))
                    conn.commit()
                    flash('Foto de perfil actualizada ✅','success')
                    send_discord('Profile picture updated', {'user': user, 'filename': filename})
                    cache_invalidate('get_profile_pictures')
                    broadcast('profile_update', {'user': user})

                return redirect('/')



            # 2) Cambio de contraseña
            if request.method == 'POST' and 'change_password' in request.form:
                current_password = request.form.get('current_password', '').strip()
                new_password = request.form.get('new_password', '').strip()
                confirm_password = request.form.get('confirm_password', '').strip()
                if not current_password or not new_password or not confirm_password:
                    flash("Completa todos los campos de contraseña.", "error"); return redirect('/')
                if new_password != confirm_password:
                    flash("La nueva contraseña y la confirmación no coinciden.", "error"); return redirect('/')
                if len(new_password) < 4:
                    flash("La nueva contraseña debe tener al menos 4 caracteres.", "error"); return redirect('/')
                c.execute("SELECT password FROM users WHERE username=%s", (user,))
                row = c.fetchone()
                if not row:
                    flash("Usuario no encontrado.", "error"); return redirect('/')
                stored = row[0]
                valid_current = check_password_hash(stored, current_password) if _is_hashed(stored) else (stored == current_password)
                if not valid_current:
                    flash("La contraseña actual no es correcta.", "error")
                    send_discord("Change password FAIL", {"user": user, "reason": "wrong_current"}); return redirect('/')
                new_hash = generate_password_hash(new_password)
                c.execute("UPDATE users SET password=%s WHERE username=%s", (new_hash, user))
                conn.commit(); flash("Contraseña cambiado correctamente 🎉", "success")
                send_discord("Change password OK", {"user": user}); return redirect('/')

            # 3) Responder pregunta
            if request.method == 'POST' and 'answer' in request.form:
                answer = request.form['answer'].strip()
                if question_id is not None and answer:
                    now_txt = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"  # UTC
                    c.execute("SELECT id, answer, created_at, updated_at FROM answers WHERE question_id=%s AND username=%s",
                              (question_id, user))
                    prev = c.fetchone()
                    if not prev:
                        c.execute("""INSERT INTO answers (question_id, username, answer, created_at, updated_at)
                                     VALUES (%s,%s,%s,%s,%s) ON CONFLICT (question_id, username) DO NOTHING""",
                                  (question_id, user, answer, now_txt, now_txt))
                        conn.commit(); send_discord("Answer submitted", {"user": user, "question_id": question_id})
                    else:
                        prev_id, prev_text, prev_created, prev_updated = prev
                        if answer != (prev_text or ""):
                            if prev_created is None:
                                c.execute("""UPDATE answers SET answer=%s, updated_at=%s, created_at=%s WHERE id=%s""",
                                          (answer, now_txt, prev_updated or now_txt, prev_id))
                            else:
                                c.execute("""UPDATE answers SET answer=%s, updated_at=%s WHERE id=%s""",
                                          (answer, now_txt, prev_id))
                            conn.commit(); send_discord("Answer edited", {"user": user, "question_id": question_id})
                    cache_invalidate('compute_streaks')
                    broadcast("dq_answer", {"user": user})
                    try:
                        push_answer_notice(user)
                    except Exception as e:
                        print("[push answer] ", e)
                return redirect('/')

            # 3-bis) Cambiar la pregunta de HOY (no borra fila => preserva racha)
            if request.method == 'POST' and 'dq_reroll' in request.form:
                ok = reroll_today_question()
                if ok:
                    cache_invalidate('compute_streaks')
                    flash("Pregunta cambiada para hoy ✅", "success")
                    send_discord("DQ reroll", {"user": user})
                else:
                    flash("No quedan preguntas disponibles para cambiar 😅", "error")
                return redirect('/')


            # 4) Meeting date
            if request.method == 'POST' and 'meeting_date' in request.form:
                meeting_date = request.form['meeting_date']
                c.execute("INSERT INTO meeting (meeting_date) VALUES (%s)", (meeting_date,))
                conn.commit(); flash("Fecha actualizada 📅", "success")
                send_discord("Meeting date updated", {"user": user, "date": meeting_date})
                broadcast("meeting_update", {"date": meeting_date}); return redirect('/')

            # 5) Banner
            if request.method == 'POST' and 'banner' in request.files:
                file = request.files['banner']
                if file and file.filename:
                    raw = file.read()
                    img_bytes, mime_over = _maybe_shrink_image(raw, max_px=1800, max_kb=700)
                    filename = secure_filename(file.filename)
                    mime_type = mime_over or file.mimetype
                    c.execute(
                        "INSERT INTO banner (image_data, filename, mime_type, uploaded_at) VALUES (%s,%s,%s,%s)",
                        (img_bytes, filename, mime_type, now_madrid_str())
                    )
                    conn.commit()
                    flash('Banner actualizado 🖼️', 'success')
                    send_discord('Banner updated', {'user': user, 'filename': filename})
                    cache_invalidate('get_banner')
                    broadcast('banner_update', {'by': user})

                return redirect('/')

            # 6) Nuevo viaje
            if request.method == 'POST' and 'travel_destination' in request.form:
                destination = request.form['travel_destination'].strip()
                description = request.form.get('travel_description', '').strip()
                travel_date = request.form.get('travel_date', '')
                is_visited = 'travel_visited' in request.form
                if destination:
                    c.execute("""INSERT INTO travels (destination, description, travel_date, is_visited, created_by, created_at)
                                 VALUES (%s,%s,%s,%s,%s,%s)""",
                              (destination, description, travel_date, is_visited, user, now_madrid_str()))
                    conn.commit(); flash("Viaje añadido ✈️", "success")
                    send_discord("Travel added", {"user": user, "dest": destination, "visited": is_visited})
                    broadcast("travel_update", {"type": "add"})
                return redirect('/')

            # 7) Foto de viaje (URL)
            if request.method == 'POST' and 'travel_photo_url' in request.form:
                travel_id = request.form.get('travel_id')
                image_url = request.form['travel_photo_url'].strip()
                if image_url and travel_id:
                    c.execute("""INSERT INTO travel_photos (travel_id, image_url, uploaded_by, uploaded_at)
                                 VALUES (%s,%s,%s,%s)""",
                              (travel_id, image_url, user, now_madrid_str()))
                    conn.commit(); flash("Foto añadida 📸", "success")
                    send_discord("Travel photo added", {"user": user, "travel_id": travel_id})
                    broadcast("travel_update", {"type": "photo_add", "id": int(travel_id)})
                return redirect('/')
            

# 8) Wishlist add (acepta form o JSON y nombres antiguos/nuevos)
            if request.method == 'POST' and 'edit_wishlist_item' not in request.path:
                    data = request.get_json(silent=True) or request.form

                    def g(*keys, default=""):
                        for k in keys:
                            v = data.get(k)
                            if v is not None:
                                return v.strip() if isinstance(v, str) else v
                        return default

                    product_name = g('product_name', 'productName')
                    product_link = g('product_link', 'url', 'link')
                    notes        = g('wishlist_notes', 'notes', 'description')
                    size         = g('size', 'talla')
                    priority     = (g('priority') or 'media').lower()
                    is_gift      = str(g('is_gift','isGift','gift')).lower() in ('1','true','on','yes','si','sí')

                    track_price  = str(g('track_price','trackPrice')).lower() in ('1','true','on','yes','si','sí')
                    alert_drop_percent = g('alert_drop_percent','alertDropPercent')
                    alert_below_price  = g('alert_below_price','alertBelowPrice')

                    if priority not in ('alta','media','baja'):
                        priority = 'media'

                    def euros_to_cents(v):
                        v = (v or '').strip()
                        if not v: return None
                        return _to_cents_from_str(v.replace('€',''))

                    alert_below_cents = euros_to_cents(alert_below_price)
                    alert_pct = float(alert_drop_percent) if alert_drop_percent else None

                    # 👉 Solo actuar si realmente es un alta de wishlist
                    if product_name:
                        c.execute("""
                            INSERT INTO wishlist
                                (product_name, product_link, notes, size, created_by, created_at,
                                is_purchased, priority, is_gift,
                                track_price, alert_drop_percent, alert_below_cents)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """, (product_name, product_link, notes, size, user, now_madrid_str(),
                            False, priority, is_gift, track_price, alert_pct, alert_below_cents))
                        conn.commit()

                        flash("Producto añadido 🛍️", "success")
                        send_discord("Wishlist added (compat)", {
                            "user": user, "name": product_name, "priority": priority,
                            "is_gift": is_gift, "size": size, "track_price": track_price
                        })
                        broadcast("wishlist_update", {"type": "add"})
                        try:
                            push_wishlist_notice(user, is_gift)
                        except Exception as e:
                            print("[push wishlist] ", e)

                        # Responder acorde al tipo de petición
                        if request.is_json or request.headers.get('Accept','').startswith('application/json'):
                            return jsonify({"ok": True})
                        return redirect('/')
                    # Si NO hay product_name, seguimos con el resto de handlers sin devolver nada.

                



            # INTIMIDAD
            if request.method == 'POST' and 'intim_unlock_pin' in request.form:
                pin_try = request.form.get('intim_pin', '').strip()
                if pin_try == INTIM_PIN:
                    session['intim_unlocked'] = True; flash("Módulo Intimidad desbloqueado ✅", "success")
                    send_discord("Intimidad unlock OK", {"user": user})
                else:
                    session.pop('intim_unlocked', None); flash("PIN incorrecto ❌", "error")
                    send_discord("Intimidad unlock FAIL", {"user": user})
                return redirect('/')

            if request.method == 'POST' and 'intim_lock' in request.form:
                session.pop('intim_unlocked', None); flash("Módulo Intimidad ocultado 🔒", "info"); return redirect('/')

            if request.method == 'POST' and 'intim_register' in request.form:
                if not session.get('intim_unlocked'):
                    flash("Debes desbloquear con PIN para registrar.", "error"); return redirect('/')
                place = (request.form.get('intim_place') or '').strip()
                notes = (request.form.get('intim_notes') or '').strip()
                now_txt = now_madrid_str()
                with conn.cursor() as c2:
                    c2.execute("""INSERT INTO intimacy_events (username, ts, place, notes) VALUES (%s,%s,%s,%s)""",
                               (user, now_txt, place or None, notes or None))
                    conn.commit()
                flash("Momento registrado ❤️", "success")
                send_discord("Intimidad registered", {"user": user, "place": place, "notes_len": len(notes)})
                broadcast("intim_update", {"type": "add"})
                return redirect('/')

            # ------------ Consultas para render ------------
            # Respuestas del día
            c.execute("SELECT username, answer, created_at, updated_at FROM answers WHERE question_id=%s", (question_id,))
            ans_rows = c.fetchall()

            # Viajes + fotos
            c.execute("""
                SELECT id, destination, description, travel_date, is_visited, created_by
                FROM travels
                ORDER BY is_visited, travel_date DESC
            """)
            travels = c.fetchall()

            c.execute("SELECT travel_id, id, image_url, uploaded_by FROM travel_photos ORDER BY id DESC")
            all_ph = c.fetchall()

            # Wishlist (blindaje regalos)
            c.execute("""
                SELECT
                    id,
                    product_name,
                    product_link,
                    notes,
                    created_by,
                    created_at,
                    is_purchased,
                    COALESCE(priority,'media') AS priority,
                    COALESCE(is_gift,false)   AS is_gift,
                    size,
                    COALESCE(track_price,false) AS track_price,
                    last_price_cents,
                    currency,
                    last_checked,
                    alert_drop_percent,
                    alert_below_cents
                FROM wishlist
                ORDER BY
                    is_purchased ASC,
                    CASE COALESCE(priority,'media')
                        WHEN 'alta' THEN 0
                        WHEN 'media' THEN 1
                        ELSE 2
                    END,
                    created_at DESC
            """)
            wl_rows = c.fetchall()  # <-- IMPORTANTE: justo después del SELECT de wishlist

          # --- Media: POR VER ---
            c.execute("""
                SELECT
                id, title, cover_url, link_url, on_netflix, on_prime,
                comment, priority,
                created_by, created_at,
                reviews,            -- NUEVO (JSONB)
                avg_rating          -- NUEVO (media)
                FROM media_items
                WHERE is_watched = FALSE
                ORDER BY
                CASE priority WHEN 'alta' THEN 0 WHEN 'media' THEN 1 ELSE 2 END,
                COALESCE(created_at, '1970-01-01') DESC,
                id DESC
            """)
            media_to_watch = c.fetchall()

            # --- Media: VISTAS ---
            c.execute("""
                SELECT
                id, title, cover_url, link_url,
                reviews,            -- NUEVO (JSONB)
                avg_rating,         -- NUEVO (media)
                watched_at,
                created_by
                FROM media_items
                WHERE is_watched = TRUE
                ORDER BY COALESCE(watched_at, '1970-01-01') DESC, id DESC
            """)
            media_watched = c.fetchall()


            # Helpers para leer reviews y exponer campos cómodos a la plantilla
        def _parse_reviews_value(raw):
            # raw puede venir como dict (jsonb) o str (json)
            if isinstance(raw, dict):
                return dict(raw)
            if isinstance(raw, str) and raw.strip():
                try:
                    return json.loads(raw)
                except Exception:
                    return {}
            return {}

        def _norm_rate(v):
            try:
                i = int(v)
                return i if 1 <= i <= 5 else None
            except Exception:
                return None

        def enrich_media(rows, me_user):
            other_user = 'mochita' if me_user == 'mochito' else 'mochito'
            out = []
            for r in rows:
                d = dict(r)  # DictRow -> dict
                rv = _parse_reviews_value(d.get('reviews'))

                my_r = rv.get(me_user) or {}
                ot_r = rv.get(other_user) or {}

                d['my_rating']     = _norm_rate(my_r.get('rating'))
                d['my_comment']    = (my_r.get('comment') or '').strip()
                d['other_rating']  = _norm_rate(ot_r.get('rating'))
                d['other_comment'] = (ot_r.get('comment') or '').strip()

                # Compatibilidad con plantillas antiguas:
                if d.get('rating') is None and d.get('avg_rating') is not None:
                    d['rating'] = d['avg_rating']              # usa la media como "rating"
                if d.get('watched_comment') is None and d.get('my_comment'):
                    d['watched_comment'] = d['my_comment']     # usa tu comentario como "watched_comment"

                out.append(d)
            return out

        # Enriquecer las dos listas con campos cómodos para la plantilla
        media_to_watch = enrich_media(media_to_watch, user)
        media_watched  = enrich_media(media_watched, user)


        # --- Procesamiento Python *fuera* del cursor ---
        # Respuestas
        answers = [(r[0], r[1]) for r in ans_rows]
        answers_created_at = {r[0]: r[2] for r in ans_rows}
        answers_updated_at = {r[0]: r[3] for r in ans_rows}
        answers_edited = {r[0]: (r[2] is not None and r[3] is not None and r[2] != r[3]) for r in ans_rows}

        other_user = 'mochita' if user == 'mochito' else 'mochito'
        dict_ans = {u: a for (u, a) in answers}
        user_answer, other_answer = dict_ans.get(user), dict_ans.get(other_user)
        show_answers = (user_answer is not None) and (other_answer is not None)

        # Fotos de viajes agrupadas
        travel_photos_dict = {}
        for tr_id, pid, url, up in all_ph:
            travel_photos_dict.setdefault(tr_id, []).append({'id': pid, 'url': url, 'uploaded_by': up})

        # Blindaje wishlist (regalos ocultos al no-creador)
        safe_items = []
        for (wid, product_name, product_link, notes, created_by, created_at,
             is_purchased, priority, is_gift, size,
             track_price, last_price_cents, currency, last_checked, alert_pct, alert_below_cents) in wl_rows:

            priority = priority or 'media'
            is_gift = bool(is_gift)
            track_price = bool(track_price)

            base_tuple_visible = (
                wid, product_name, product_link, notes,
                created_by, created_at, is_purchased,
                priority, is_gift, size
            )

            extra_price = (track_price, last_price_cents, currency, last_checked, alert_pct, alert_below_cents)

            if is_gift and created_by != user:
                safe_items.append((
                    wid,
                    "🎁 Regalo oculto",
                    None,
                    None,
                    created_by,
                    created_at,
                    is_purchased,
                    priority,
                    True,
                    None,
                    False, None, None, None, None, None  # oculta precio
                ))
            else:
                safe_items.append(base_tuple_visible + extra_price)

        wishlist_items = safe_items

        banner_file = get_banner()
        profile_pictures = get_profile_pictures()

    finally:
        conn.close()

    current_streak, best_streak = compute_streaks()
    intim_unlocked = bool(session.get('intim_unlocked'))
    intim_stats = get_intim_stats()
    intim_events = get_intim_events(200) if intim_unlocked else []


     # --- Ciclo para interfaz ---
    cycle_user = 'mochita'  # por defecto lo guardamos para ella; cambia si quieres
    today = today_madrid()
    month_start = today.replace(day=1)
    # mostramos 3 meses: actual y los 2 siguientes para planificar
    from datetime import timedelta as _td
    # último día del tercer mes
    def _last_day_of_month(y, m):
        import calendar
        return date(y, m, calendar.monthrange(y, m)[1])
    m3_y = month_start.year + (month_start.month + 2 - 1)//12
    m3_m = (month_start.month + 2 - 1) % 12 + 1
    month_end = _last_day_of_month(m3_y, m3_m)

    cycle_entries = get_cycle_entries(cycle_user, month_start, month_end)
    cycle_pred = predict_cycle(cycle_user)

    moods_for_select = list(ALLOWED_MOODS)
    flows_for_select = list(ALLOWED_FLOWS)

    return render_template('index.html',
                           question=question_text,
                           show_answers=show_answers,
                           answers=answers,
                           answers_edited=answers_edited,
                           answers_created_at=answers_created_at,
                           answers_updated_at=answers_updated_at,
                           user_answer=user_answer,
                           other_user=other_user,
                           other_answer=other_answer,
                           days_together=days_together(),
                           days_until_meeting=days_until_meeting(),
                           travels=travels,
                           travel_photos_dict=travel_photos_dict,
                           wishlist_items=wishlist_items,
                           username=user,
                           banner_file=banner_file,
                           profile_pictures=profile_pictures,
                           error=None,
                           current_streak=current_streak,
                           best_streak=best_streak,
                           intim_unlocked=intim_unlocked,
                           intim_stats=intim_stats,
                           intim_events=intim_events,
                           media_to_watch=media_to_watch,
                           media_watched=media_watched,
                           wishlist=wishlist_items,
                           cycle_user=cycle_user,
                           cycle_entries=cycle_entries,
                           cycle_pred=cycle_pred,
                           moods_for_select=moods_for_select,
                           flows_for_select=flows_for_select
                           )

# ======= Rutas REST extra (con broadcast) =======
@app.route('/delete_travel', methods=['POST'])
def delete_travel():
    if 'username' not in session:
        return redirect('/')
    try:
        travel_id = request.form['travel_id']
        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute("DELETE FROM travel_photos WHERE travel_id=%s", (travel_id,))
            c.execute("DELETE FROM travels WHERE id=%s", (travel_id,))
            conn.commit()
        flash("Viaje eliminado 🗑️", "success")
        broadcast("travel_update", {"type": "delete", "id": int(travel_id)})
        return redirect('/')
    except Exception as e:
        print(f"Error en delete_travel: {e}"); flash("No se pudo eliminar el viaje.", "error"); return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()

@app.route('/delete_travel_photo', methods=['POST'])
def delete_travel_photo():
    if 'username' not in session:
        return redirect('/')
    try:
        photo_id = request.form['photo_id']
        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute("DELETE FROM travel_photos WHERE id=%s", (photo_id,))
            conn.commit()
        flash("Foto eliminada 🗑️", "success")
        broadcast("travel_update", {"type": "photo_delete", "id": int(photo_id)})
        return redirect('/')
    except Exception as e:
        print(f"Error en delete_travel_photo: {e}"); flash("No se pudo eliminar la foto.", "error"); return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()



            

@app.route('/toggle_travel_status', methods=['POST'])
def toggle_travel_status():
    if 'username' not in session:
        return redirect('/')
    try:
        travel_id = request.form['travel_id']
        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute("SELECT is_visited FROM travels WHERE id=%s", (travel_id,))
            current_status = c.fetchone()[0]
            new_status = not current_status
            c.execute("UPDATE travels SET is_visited=%s WHERE id=%s", (new_status, travel_id))
            conn.commit()
        flash("Estado del viaje actualizado ✅", "success")
        broadcast("travel_update", {"type": "toggle", "id": int(travel_id), "is_visited": bool(new_status)})
        return redirect('/')
    except Exception as e:
        print(f"Error en toggle_travel_status: {e}"); flash("No se pudo actualizar el estado del viaje.", "error"); return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()

@app.route('/delete_wishlist_item', methods=['POST'])
def delete_wishlist_item():
    if 'username' not in session:
        return redirect('/')
    try:
        item_id = request.form['item_id']
        user = session['username']
        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute("DELETE FROM wishlist WHERE id=%s", (item_id,))
            conn.commit()
        flash("Elemento eliminado de la lista 🗑️", "success")
        send_discord("Wishlist delete", {"user": user, "item_id": item_id})
        broadcast("wishlist_update", {"type": "delete", "id": int(item_id)})
        return redirect('/')
    except Exception as e:
        print(f"Error en delete_wishlist_item: {e}")
        flash("No se pudo eliminar el elemento.", "error")
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()

@app.route('/toggle_wishlist_item', methods=['POST'])
def toggle_wishlist_item():
    if 'username' not in session:
        return redirect('/')
    try:
        item_id = request.form['item_id']
        user = session['username']
        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute("SELECT id, product_name, is_purchased, is_gift, created_by FROM wishlist WHERE id=%s", (item_id,))
            row = c.fetchone()
            if not row:
                flash("Elemento no encontrado.", "error")
                return redirect('/')

            wid, pname, current_state, is_gift, created_by = row
            new_state = not bool(current_state)

            c.execute("UPDATE wishlist SET is_purchased=%s WHERE id=%s", (new_state, wid))
            conn.commit()

            # 🎁 Si es regalo oculto y se marca como comprado → notifica
            if is_gift and new_state:
                try:
                    push_gift_purchased({
                        "id": wid,
                        "product_name": pname,
                        "created_by": created_by
                    })
                except Exception as e:
                    print("[push gift_purchased]", e)

        flash("Estado del elemento actualizado ✅", "success")
        send_discord("Wishlist toggle", {"user": user, "item_id": item_id, "is_purchased": bool(new_state)})
        broadcast("wishlist_update", {"type": "toggle", "id": int(item_id), "is_purchased": bool(new_state)})
        return redirect('/')
    except Exception as e:
        print(f"Error en toggle_wishlist_item: {e}")
        flash("No se pudo actualizar el estado del elemento.", "error")
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()

@app.route('/edit_wishlist_item', methods=['POST'])
def edit_wishlist_item():
    if 'username' not in session:
        return redirect('/')
    try:
        item_id      = request.form.get('item_id')
        product_name = (request.form.get('product_name') or '').strip()
        product_link = (request.form.get('product_link') or '').strip()
        notes        = (request.form.get('wishlist_notes') or '').strip()
        size         = (request.form.get('size') or '').strip()
        priority     = (request.form.get('priority') or 'media').strip()
        is_gift      = bool(request.form.get('is_gift'))

        # Tracking
        alert_drop_percent = (request.form.get('alert_drop_percent') or '').strip()
        alert_below_price  = (request.form.get('alert_below_price')  or '').strip()
        track_price        = bool(request.form.get('track_price'))

        if priority not in ('alta', 'media', 'baja'):
            priority = 'media'

        def euros_to_cents(v):
            v = (v or '').strip()
            if not v: return None
            return _to_cents_from_str(v.replace('€',''))

        alert_below_cents = euros_to_cents(alert_below_price)
        alert_pct = float(alert_drop_percent) if alert_drop_percent else None

        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute("""
                UPDATE wishlist SET
                    product_name=%s,
                    product_link=%s,
                    notes=%s,
                    size=%s,
                    priority=%s,
                    is_gift=%s,
                    track_price=%s,
                    alert_drop_percent=%s,
                    alert_below_cents=%s
                 WHERE id=%s""",
              (product_name, product_link, notes, size, priority, is_gift,
               track_price, alert_pct, alert_below_cents, item_id))
            conn.commit()
        flash("Elemento actualizado ✏️", "success")
        send_discord("Wishlist edit", {
            "user": session['username'], "item_id": item_id,
            "priority": priority, "is_gift": is_gift,
            "track_price": track_price, "alert_pct": alert_pct, "alert_below": alert_below_cents
        })
        broadcast("wishlist_update", {"type": "edit", "id": int(item_id)})
        return redirect('/')
    except Exception as e:
        print(f"Error en edit_wishlist_item: {e}")
        flash("No se pudo editar el elemento.", "error")
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()

# ======= Ubicación del usuario =======
@app.route('/update_location', methods=['POST'])
def update_location():
    if 'username' not in session:
        return redirect('/')
    try:
        user = session['username']
        location_name = (request.form.get('location_name') or '').strip()
        lat = request.form.get('latitude')
        lon = request.form.get('longitude')
        lat = float(lat) if lat not in (None, '') else None
        lon = float(lon) if lon not in (None, '') else None
        now_txt = now_madrid_str()

        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO locations (username, location_name, latitude, longitude, updated_at)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (username) DO UPDATE
                SET location_name=EXCLUDED.location_name,
                    latitude=EXCLUDED.latitude,
                    longitude=EXCLUDED.longitude,
                    updated_at=EXCLUDED.updated_at
            """, (user, location_name, lat, lon, now_txt))
            conn.commit()
        flash("Ubicación actualizada 📍", "success")
        send_discord("Location updated", {"user": user, "name": location_name, "lat": lat, "lon": lon})
        broadcast("location_update", {"user": user})
        return redirect('/')
    except Exception as e:
        print(f"Error en update_location: {e}")
        flash("No se pudo actualizar la ubicación.", "error")
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()

# ======= Logout =======
@app.route('/logout')
def logout():
    try:
        user = session.get('username')
        session.clear()
        send_discord("Logout", {"user": user})
    except Exception:
        pass
    return redirect('/')

# ======= Web Push: claves y suscripciones =======
@app.get("/push/vapid-public")
def push_vapid_public():
    key_b64url = get_vapid_public_base64url()
    if not key_b64url:
        return jsonify({"error": "vapid_public_missing"}), 500
    return jsonify({"vapidPublicKey": key_b64url})

@app.post("/push/subscribe")
def push_subscribe():
    if "username" not in session:
        return jsonify({"error": "unauthenticated"}), 401
    user = session["username"]
    sub = request.get_json(silent=True) or {}
    endpoint = sub.get("endpoint")
    p256dh = (sub.get("keys") or {}).get("p256dh")
    auth   = (sub.get("keys") or {}).get("auth")
    if not (endpoint and p256dh and auth):
        return jsonify({"error": "invalid_subscription"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO push_subscriptions (username, endpoint, p256dh, auth, created_at)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (endpoint) DO UPDATE
                SET username=EXCLUDED.username,
                    p256dh=EXCLUDED.p256dh,
                    auth=EXCLUDED.auth,
                    created_at=EXCLUDED.created_at
            """, (user, endpoint, p256dh, auth, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})

@app.route('/push/unsubscribe', methods=['POST'])
def push_unsubscribe():
    if 'username' not in session:
        return jsonify({"ok": False, "error": "not_logged"}), 401
    try:
        payload = request.get_json(force=True, silent=False)
        endpoint = (payload.get('endpoint') or '').strip()
        if not endpoint:
            return jsonify({"ok": False, "error": "bad_payload"}), 400
        _delete_subscription_by_endpoint(endpoint)
        send_discord("Push unsubscribed", {"user": session['username']})
        return jsonify({"ok": True})
    except Exception as e:
        print(f"[push_unsubscribe] {e}")
        return jsonify({"ok": False, "error": "server_error"}), 500

@app.route('/push/test', methods=['POST', 'GET'])
def push_test():
    if 'username' not in session:
        return jsonify({"ok": False, "error": "not_logged"}), 401
    try:
        who = request.values.get('who', 'both')  # 'mochito' | 'mochita' | 'both'
        title = request.values.get('title', 'Prueba de notificación 🔔')
        body  = request.values.get('body',  'Esto es un test desde el servidor.')

        if who == 'both':
            send_push_both(title, body)
        elif who in ('mochito','mochita'):
            send_push_to(who, title, body)
        else:
            return jsonify({"ok": False, "error": "bad_who"}), 400

        return jsonify({"ok": True})
    except Exception as e:
        print(f"[push_test] {e}")
        return jsonify({"ok": False, "error": "server_error"}), 500

@app.get('/push/list')
def push_list():
    if 'username' not in session:
        return jsonify({"ok": False, "error": "not_logged"}), 401
    try:
        subs = get_subscriptions_for(session['username'])
        return jsonify({"ok": True, "count": len(subs), "subs": subs})
    except Exception as e:
        print(f"[push_list] {e}")
        return jsonify({"ok": False, "error": "server_error"}), 500

# ======= PRECIO: endpoint manual de prueba =======
@app.post('/price_check_now')
def price_check_now():
    if 'username' not in session:
        return jsonify({"ok":False,"error":"unauthenticated"}), 401
    try:
        wid = request.form.get('id')
        if not wid: return jsonify({"ok":False,"error":"missing id"}), 400
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                c.execute("""SELECT id, product_name, product_link, created_by,
                                    is_gift,
                                    track_price, last_price_cents, currency,
                                    alert_drop_percent, alert_below_cents
                             FROM wishlist WHERE id=%s""", (wid,))
                row = c.fetchone()
            if not row or not row['product_link']: return jsonify({"ok":False,"error":"not_found_or_no_link"}), 404
            price, curr, _ = fetch_price(row['product_link'])
            if not price:
                return jsonify({"ok":False,"error":"no_price"}), 200
            old = row['last_price_cents']
            with conn.cursor() as c2:
                c2.execute("""UPDATE wishlist SET last_price_cents=%s, currency=COALESCE(%s,currency), last_checked=%s WHERE id=%s""",
                           (price, curr, now_madrid_str(), wid))
                conn.commit()
            if old is not None and price < old:
                notify_price_drop(row, old, price)
            return jsonify({"ok":True,"old":old,"new":price})
        finally:
            conn.close()
    except Exception as e:
        print("[price_check_now]", e)
        return jsonify({"ok":False,"error":"server_error"}), 500

# ======= Errores simpáticos =======
@app.errorhandler(404)
def not_found(_):
    return render_template('404.html'), 404

@app.errorhandler(500)
def server_error(_):
    return render_template('500.html'), 500

# ======= Horarios (página) =======
@app.route('/horario', methods=['GET'])
def schedule_page():
    if 'username' not in session:
        return redirect('/')
    return render_template('schedule.html')

# Alias opcional
@app.route('/schedule', methods=['GET'])
def schedule_alias():
    return redirect('/horario')

# ======= Horarios (API) =======
@app.get('/api/schedules')
def api_get_schedules():
    if 'username' not in session:
        return jsonify({"error": "unauthenticated"}), 401

    schedules = {"mochito": {}, "mochita": {}}
    custom_times_sets = {"mochito": set(), "mochita": set()}

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""SELECT username, day, time, activity, color FROM public.schedules""")
            for row in c.fetchall():
                username = row['username']
                day = row['day']
                time_ = row['time']
                activity = row['activity'] or ''
                color = row['color'] or '#888888'
                if username not in schedules:
                    continue
                day_map = schedules[username].setdefault(day, {})
                day_map[time_] = {"activity": activity, "color": color}

            c.execute("""SELECT username, time FROM public.schedule_times""")
            for row in c.fetchall():
                u = row['username']
                t = row['time']
                if u in custom_times_sets:
                    custom_times_sets[u].add(t)
    finally:
        conn.close()

    def sort_hhmm(lst):
        def key(t):
            try:
                h, m = t.split(':')
                return (int(h), int(m))
            except Exception:
                return (0, 0)
        return sorted(lst, key=key)

    custom_times = {
        "mochito": sort_hhmm(list(custom_times_sets["mochito"])),
        "mochita": sort_hhmm(list(custom_times_sets["mochita"]))
    }

    return jsonify({"schedules": schedules, "customTimes": custom_times})

@app.post('/api/schedules/save')
def api_save_schedules():
    if 'username' not in session:
        return jsonify({"ok": False, "error": "unauthenticated"}), 401

    payload = request.get_json(silent=True) or {}
    schedules = payload.get("schedules") or {}
    custom_times = payload.get("customTimes") or {}

    if not isinstance(schedules, dict) or not isinstance(custom_times, dict):
        return jsonify({"ok": False, "error": "bad_payload"}), 400

    users = ("mochito", "mochita")
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            for user in users:
                c.execute("DELETE FROM public.schedules WHERE username=%s", (user,))
                us = schedules.get(user) or {}
                for day, time_map in (us.items()):
                    time_map = time_map or {}
                    for t, obj in time_map.items():
                        activity = (obj or {}).get("activity", "")
                        color = (obj or {}).get("color", "#888888")
                        c.execute("""
                            INSERT INTO public.schedules (username, day, time, activity, color)
                            VALUES (%s,%s,%s,%s,%s)
                        """, (user, day, t, activity, color))

                c.execute("DELETE FROM public.schedule_times WHERE username=%s", (user,))
                for t in (custom_times.get(user) or []):
                    c.execute("""
                        INSERT INTO public.schedule_times (username, time)
                        VALUES (%s,%s)
                    """, (user, t))
        conn.commit()
        send_discord("Schedules saved", {"by": session.get('username', '?')})
        broadcast("schedule_update", {"by": session.get('username', '?')})
        return jsonify({"ok": True})
    except Exception as e:
        print("[api_save_schedules] error:", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "server_error"}), 500
    finally:
        conn.close()

# --- Service Worker en la raíz ---
@app.route("/sw.js")
def sw():
    import os as _os
    try:
        resp = send_file(_os.path.join(app.static_folder, "sw.js"))
    except Exception:
        resp = send_file("static/sw.js")
    resp = make_response(resp)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.mimetype = "application/javascript"
    return resp

# ======= Página debug de push =======
@app.get("/push/debug")
def push_debug_page():
    html = '''
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Activar notificaciones — Guía paso a paso</title>
<style>
:root{ --brand:#e84393; --ink:#202124; --muted:#5f6368; --ok:#10b981; --warn:#f59e0b; --err:#ef4444; --bg:#fafafa; --card:#fff; --line:#ececec; }
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--ink);font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.55}
.container{max-width:960px;margin:24px auto;padding:0 16px}
h1{font-size:1.6rem;margin:12px 0 6px}
.subtitle{color:var(--muted);margin:0 0 14px}
.badge{display:inline-block;padding:4px 10px;border-radius:999px;background:#f3f4f6;color:#111;font-size:.8rem;margin-right:8px}
.badge.ok{background:#ecfdf5;color:#065f46}
.badge.warn{background:#fffbeb;color:#92400e}
.badge.err{background:#fef2f2;color:#991b1b}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:14px 0;box-shadow:0 2px 10px rgba(0,0,0,.03)}
.card h2{font-size:1.1rem;margin:0 0 8px}
.grid{display:grid;gap:12px}
@media(min-width:800px){ .grid{grid-template-columns:1.2fr .8fr} }
.step{display:flex;gap:12px;align-items:flex-start}
.step .num{flex:0 0 32px;height:32px;border-radius:50%;background:var(--brand);color:#fff;display:grid;place-items:center;font-weight:700}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
button{padding:10px 14px;border:1px solid var(--line);border-radius:10px;background:#fff;cursor:pointer}
button.primary{background:var(--brand);color:#fff;border-color:var(--brand)}
button.ghost{background:#fff}
button:disabled{opacity:.6;cursor:not-allowed}
.tip{font-size:.92rem;color:var(--muted);margin-top:6px}
.hr{height:1px;background:#ececec;margin:14px 0}
.state{display:grid;gap:10px}
.state .row{display:flex;align-items:center;gap:10px}
.state .dot{width:10px;height:10px;border-radius:50%;background:#ddd}
.state .dot.ok{background:var(--ok)}
.state .dot.warn{background:var(--warn)}
.state .dot.err{background:var(--err)}
.kbd{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#f3f4f6;border:1px solid #e5e7eb;border-bottom-width:2px;border-radius:6px;padding:2px 6px}
.progress{height:10px;background:#eee;border-radius:999px;overflow:hidden}
.progress > span{display:block;height:100%;background:linear-gradient(90deg,var(--brand),#ff7ab6);width:0%;transition:width .3s}
#log{white-space:pre-wrap;background:#111;color:#eee;padding:12px;border-radius:12px;margin-top:12px;min-height:140px}
.helper{font-size:.95rem}
.helper li{margin:.25rem 0}
.header-actions{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 0}
a.link{color:var(--brand);text-decoration:none;border-bottom:1px dotted var(--brand)}
a.link:hover{text-decoration:underline}
a.link{color:#e84393}
.small{font-size:.88rem;color:var(--muted)}
</style>
</head>
<body>
<div class="container">
<h1>🔔 Activar notificaciones</h1>
<p class="subtitle">Sigue estos pasos. Te marcamos en verde lo que ya está OK. Al final puedes enviarte una prueba.</p>

<div class="card">
  <div class="progress"><span id="progressBar" style="width:0%"></span></div>
  <div class="state" style="margin-top:10px">
    <div class="row"><span id="chk-https" class="dot"></span> <b>HTTPS</b> (obligatorio)</div>
    <div class="row"><span id="chk-standalone" class="dot"></span> <b>Modo app</b> (iOS: desde “Pantalla de inicio”)</div>
    <div class="row"><span id="chk-sw" class="dot"></span> <b>Service Worker</b> registrado</div>
    <div class="row"><span id="chk-perm" class="dot"></span> <b>Permiso de notificaciones</b></div>
    <div class="row"><span id="chk-sub" class="dot"></span> <b>Dispositivo suscrito</b></div>
  </div>
</div>

<div class="grid">
  <!-- PASOS -->
  <div>
    <div class="card">
      <div class="step">
        <div class="num">1</div>
        <div>
          <h2>Instala como app (iPhone/iPad)</h2>
          <div class="tip" id="iosTip">
            En iOS, las <b>push</b> solo funcionan si abres la web desde el icono en <b>Pantalla de inicio</b>.
          </div>
          <ul class="helper" id="iosHowTo" style="display:none">
            <li>Abre en Safari ➜ pulsa <span class="kbd">Compartir</span> ➜ <span class="kbd">Añadir a pantalla de inicio</span>.</li>
            <li>Abre la app desde el icono y vuelve a esta pantalla.</li>
          </ul>
          <div class="actions">
            <button id="btn-check-standalone" class="ghost">Comprobar estado</button>
            <button id="btn-home" class="ghost">Ir al inicio</button>
          </div>
          <div class="tip small" id="envInfo"></div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="step">
        <div class="num">2</div>
        <div>
          <h2>Registrar Service Worker</h2>
          <p class="helper">Es quien recibe las push en segundo plano.</p>
          <div class="actions">
            <button id="btn-reg" class="primary">Registrar SW</button>
            <button id="btn-local-sw" class="ghost">Probar notificación (SW)</button>
          </div>
          <div class="tip">Si falla: comprueba que <code>/sw.js</code> existe y no está cacheado.</div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="step">
        <div class="num">3</div>
        <div>
          <h2>Dar permiso y suscribirse</h2>
          <p class="helper">Primero concede permiso al navegador y luego registramos tu endpoint en el servidor.</p>
          <div class="actions">
            <button id="btn-perm" class="primary">Conceder permiso</button>
            <button id="btn-local" class="ghost">Probar notificación (Página)</button>
            <button id="btn-sub" class="primary">Suscribirme</button>
          </div>
          <div class="tip">Si aparece <b>denied</b>, ve a Ajustes del sistema ▶ Notificaciones y habilítalas para esta app.</div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="step">
        <div class="num">4</div>
        <div>
          <h2>Probar desde el servidor</h2>
          <p class="helper">Con todo en verde, envíate una notificación real desde el backend.</p>
          <div class="actions">
            <button id="btn-sendme" class="primary">Enviar prueba</button>
            <button id="btn-list" class="ghost">Listar suscripciones</button>
          </div>
          <div class="tip">Si no llega, revisa que la clave VAPID esté bien y que la suscripción exista en DB.</div>
        </div>
      </div>
    </div>
  </div>

  <!-- LATERAL -->
  <div>
    <div class="card">
      <h2>Consejos rápidos</h2>
      <ul class="helper">
        <li><b>iOS</b>: usa la app desde <i>Pantalla de inicio</i>, no desde Safari.</li>
        <li>Comprueba que <b>HTTPS</b> está activo (candado en la barra).</li>
        <li>Permiso debe quedar en <span class="kbd">granted</span>.</li>
        <li>Tras suscribirte, en DB debe guardarse tu <span class="kbd">endpoint</span>.</li>
        <li>La prueba del servidor usa <span class="kbd">/push/test</span> para tu usuario logueado.</li>
      </ul>
      <div class="hr"></div>
      <div class="actions">
        <button id="btn-copy" class="ghost">Copiar registro</button>
        <a href="/" class="link">Volver al inicio</a>
      </div>
    </div>

    <div class="card">
      <h2>Registro</h2>
      <div id="log">Cargando…</div>
    </div>
  </div>
</div>
</div>

<script>
function log(){ const el=document.getElementById('log'); el.textContent += Array.from(arguments).join(' ') + "\\n"; el.scrollTop = el.scrollHeight; }
function b64ToBytes(s){ const pad='='.repeat((4 - s.length % 4) % 4); const b64=(s+pad).replace(/-/g,'+').replace(/_/g,'/'); const raw=atob(b64); const arr=new Uint8Array(raw.length); for(let i=0;i<raw.length;i++)arr[i]=raw.charCodeAt(i); return arr; }
function setDot(id, state){ const el=document.getElementById(id); el.className='dot'; if(state==='ok') el.classList.add('ok'); else if(state==='warn') el.classList.add('warn'); else if(state==='err') el.classList.add('err'); }
function pctDone(parts){ const sum = parts.reduce((a,b)=>a+(b?1:0),0); return Math.round((sum/parts.length)*100); }
function setProgress(p){ document.getElementById('progressBar').style.width = p + '%'; }

const UA = navigator.userAgent || '';
const isIOS = /iPad|iPhone|iPod/.test(UA);
const isSafari = /^((?!chrome|android).)*safari/i.test(UA);
const isStandalone = window.matchMedia('(display-mode: standalone)').matches || (window.navigator.standalone === true);
const isHTTPS = location.protocol === 'https:';
const envText = [
  isIOS ? 'iOS' : (/Android/i.test(UA) ? 'Android' : 'Desktop'),
  isSafari ? 'Safari' : ( /Chrome/i.test(UA) ? 'Chrome' : 'Navegador'),
  isStandalone ? 'Standalone' : 'Navegador',
  isHTTPS ? 'HTTPS' : 'HTTP'
].join(' · ');
document.getElementById('envInfo').textContent = envText;

if(isIOS){
  document.getElementById('iosHowTo').style.display = isStandalone ? 'none' : 'block';
}

setDot('chk-https', isHTTPS ? 'ok' : 'err');
setDot('chk-standalone', (isIOS ? (isStandalone ? 'ok' : 'warn') : 'ok'));

(async function autoRegisterSW(){
  try{
    if('serviceWorker' in navigator){
      const reg = await navigator.serviceWorker.register('/sw.js');
      log('SW registrado (auto):', reg.scope || '(sin scope)');
      setDot('chk-sw','ok');
    }else{
      log('Este navegador no soporta Service Worker');
      setDot('chk-sw','err');
    }
  }catch(e){
    log('Error registrando SW:', e.message||e);
    setDot('chk-sw','err');
  }
  refreshProgress();
})();

function refreshPermission(){
  if(!('Notification' in window)) { setDot('chk-perm','err'); return; }
  const st = Notification.permission; // default | granted | denied
  if(st === 'granted') setDot('chk-perm','ok');
  else if(st === 'denied') setDot('chk-perm','err');
  else setDot('chk-perm','warn');
}

async function hasSubscription(){
  try{
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    return !!sub;
  }catch{ return false; }
}
async function refreshSubscriptionDot(){
  setDot('chk-sub', (await hasSubscription()) ? 'ok' : 'warn');
}

function refreshProgress(){
  const dots = ['chk-https','chk-standalone','chk-sw','chk-perm','chk-sub'];
  const ok = dots.map(id => document.getElementById(id).classList.contains('ok'));
  setProgress(pctDone(ok));
}

document.getElementById('btn-home').onclick = ()=>{ location.href='/'; };

document.getElementById('btn-check-standalone').onclick = ()=>{
  const again = window.matchMedia('(display-mode: standalone)').matches || (window.navigator.standalone === true);
  setDot('chk-standalone', (isIOS ? (again ? 'ok' : 'warn') : 'ok'));
  if(isIOS && !again) log('Aún no estás en modo app. Añade a pantalla de inicio y vuelve a abrir desde el icono.');
  refreshProgress();
};

document.getElementById('btn-reg').onclick = async ()=>{
  if(!('serviceWorker' in navigator)){ log('SW no soportado'); setDot('chk-sw','err'); return; }
  try{
    const reg = await navigator.serviceWorker.register('/sw.js');
    log('SW registrado:', reg.scope || '(sin scope)');
    setDot('chk-sw','ok');
  }catch(e){ log('Error registrando SW:', e.message||e); setDot('chk-sw','err'); }
  refreshProgress();
};

document.getElementById('btn-perm').onclick = async ()=>{
  if(!('Notification' in window)){ log('Notifications no soportadas'); setDot('chk-perm','err'); return; }
  try{
    const perm = await Notification.requestPermission();
    log('Permiso =>', perm);
  }catch(e){ log('requestPermission error:', e.message||e); }
  refreshPermission(); refreshProgress();
};

document.getElementById('btn-local').onclick = async ()=>{
  try{
    if (!('Notification' in window)) { log('Notifications no soportadas'); return; }
    if (Notification.permission !== 'granted'){ log('Permiso no concedido'); return; }
    new Notification('Notificación local', { body: 'Mostrada desde la página (Window)' });
    log('OK: notificación local (Window)');
  }catch(e){ log('Local notif error:', e.message||e); }
};

document.getElementById('btn-local-sw').onclick = async ()=>{
  try{
    const reg = await navigator.serviceWorker.ready;
    if (!reg.showNotification){ log('showNotification no disponible en este SW'); return; }
    await reg.showNotification('Notificación local (SW)', { body: 'Mostrada por el Service Worker' });
    log('OK: notificación local (SW)');
  }catch(e){ log('SW notif error:', e.message||e); }
};

async function doSubscribe(){
  try{
    const r = await fetch('/push/vapid-public');
    const j = await r.json().catch(()=>({}));
    if (!j.vapidPublicKey){ log("vapidPublicKey vacío / error en backend"); return false; }
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub){
      sub = await reg.pushManager.subscribe({ userVisibleOnly:true, applicationServerKey: b64ToBytes(j.vapidPublicKey) });
    }
    const rr = await fetch('/push/subscribe', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(sub) });
    const jj = await rr.json().catch(()=>({}));
    log("Suscripción =>", JSON.stringify(jj));
    return !!jj.ok;
  }catch(e){
    log('subscribe error:', e.message||e);
    return false;
  }
}

document.getElementById('btn-sub').onclick = async ()=>{
  const ok = await doSubscribe();
  await refreshSubscriptionDot(); refreshProgress();
  if(ok) log('✅ Suscrito correctamente');
  else log('❌ No se pudo suscribir (revisa claves VAPID y permisos)');
};

document.getElementById('btn-sendme').onclick = async ()=>{
  try{
    const r = await fetch('/push/test', { method:'POST' });
    const j = await r.json().catch(()=>({}));
    log('push/test =>', JSON.stringify(j));
  }catch(e){ log('push/test error:', e.message||e); }
};

document.getElementById('btn-list').onclick = async ()=>{
  try{
    const r = await fetch('/push/list');
    const j = await r.json().catch(()=>({}));
    log('push/list =>', JSON.stringify(j));
  }catch(e){ log('push/list error (¿ruta no implementada?):', e.message||e); }
};

document.getElementById('btn-copy').onclick = async ()=>{
  try{
    const txt = document.getElementById('log').textContent;
    await navigator.clipboard.writeText(txt);
    log('📋 Registro copiado al portapapeles');
  }catch(e){ log('No se pudo copiar:', e.message||e); }
};

(function init(){
  const envText = document.getElementById('envInfo').textContent;
  const standaloneMsg = isIOS ? (isStandalone ? 'En modo app ✅' : 'Abierto en Safari ❗️') : 'Modo app no requerido';
  log('Entorno =>', envText);
  log('Standalone =>', standaloneMsg);
  log('Notification.permission =>', (window.Notification && Notification.permission) ? Notification.permission : 'no soportado');
  log('serviceWorker =>', 'serviceWorker' in navigator);
  log('PushManager =>', !!window.PushManager);
  refreshPermission();
  refreshSubscriptionDot();
  refreshProgress();
})();
</script>
</body>
</html>
'''
    return html


# ======= Debug zona horaria =======
@app.get("/_debug/tz")
def _debug_tz():
    n = europe_madrid_now()
    return jsonify({
        "utc": datetime.now(timezone.utc).isoformat(),
        "madrid": n.isoformat(),
        "date_madrid": n.date().isoformat(),
        "secs_to_midnight_madrid": int(seconds_until_next_midnight_madrid())
    })


# ======== Presencia (rutas) ========
@app.post("/presence/ping")
def presence_ping():
    if 'username' not in session:
        return jsonify(ok=False, error="unauthorized"), 401
    ensure_presence_table()
    u   = session['username']
    now = europe_madrid_now()
    ua  = (request.headers.get('User-Agent') or '')[:300]
    data = request.get_json(silent=True) or {}
    device = (data.get('device') or '')[:80]

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO user_presence (username, last_seen, device, user_agent)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (username) DO UPDATE
                SET last_seen = EXCLUDED.last_seen,
                    device    = EXCLUDED.device,
                    user_agent= EXCLUDED.user_agent;
            """, (u, now, device, ua))
            conn.commit()
    finally:
        conn.close()

    try:
        broadcast("presence", {"kind":"presence","user":u,"ts":now.isoformat()})
    except Exception:
        pass

    return jsonify(ok=True)

@app.get("/presence/other")
def presence_other():
    if 'username' not in session:
        return jsonify(ok=False, error="unauthorized"), 401
    ensure_presence_table()
    me    = session['username']
    other = request.args.get('user') or ('mochita' if me=='mochito' else 'mochito')

    last_seen = None
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT last_seen FROM user_presence WHERE username=%s;", (other,))
            row = c.fetchone()
            if row: last_seen = row['last_seen']
    finally:
        conn.close()

    now = europe_madrid_now()
    window = int(os.environ.get("PRESENCE_ONLINE_WINDOW", "70"))
    online = False
    ago_seconds = None
    if last_seen:
        ago_seconds = max(0, int((now - last_seen).total_seconds()))
        online = ago_seconds <= window

    return jsonify(ok=True, user=other, online=online,
                   last_seen=(last_seen.isoformat() if last_seen else None),
                   ago_seconds=ago_seconds)


@app.get("/presence/_debug_dump")
def presence_debug_dump():
    ensure_presence_table()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT username, last_seen FROM user_presence ORDER BY username;")
            rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()
    return jsonify(rows)


# === ADMIN: página y acciones ===

@app.get("/admin")
def admin_home():
    require_admin()
    # Listar próximas programadas (pendientes)
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT id, target, title, body, url, tag, icon, badge, when_at, status, created_at
                FROM scheduled_notifications
                WHERE status='pending'
                ORDER BY when_at ASC
                LIMIT 50
            """)
            pending = c.fetchall()
            c.execute("""
                SELECT id, target, title, when_at, sent_at
                FROM scheduled_notifications
                WHERE status='sent'
                ORDER BY sent_at DESC
                LIMIT 10
            """)
            recent = c.fetchall()
    finally:
        conn.close()
    return render_template("admin.html",
                           username=session['username'],
                           pending=pending,
                           recent=recent,
                           vapid_public_key=get_vapid_public_base64url())

@app.post("/admin/reset_pw")
def admin_reset_pw():
    require_admin()
    user = request.form.get("user")
    new_password = (request.form.get("new_password") or "").strip()
    if user not in ("mochito", "mochita") or len(new_password) < 4:
        flash("Datos inválidos (usuario o contraseña demasiado corta).", "error")
        return redirect("/admin")
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE users SET password=%s WHERE username=%s",
                      (generate_password_hash(new_password), user))
            conn.commit()
        flash(f"Contraseña de {user} cambiada ✅", "success")
        send_discord("Admin: password reset", {"by":"mochito", "user":user})
    finally:
        conn.close()
    return redirect("/admin")

@app.post("/admin/push/send_now")
def admin_push_send_now():
    require_admin()
    target = request.form.get("target", "both")
    title  = (request.form.get("title") or "").strip()
    body   = (request.form.get("body") or "").strip()
    url    = (request.form.get("url") or "").strip() or None
    tag    = (request.form.get("tag") or "").strip() or None
    icon   = (request.form.get("icon") or "").strip() or None
    badge  = (request.form.get("badge") or "").strip() or None

    if not title:
        flash("Título requerido.", "error")
        return redirect("/admin")

    if target == "both":
        send_push_both(title=title, body=body, url=url, tag=tag, icon=icon, badge=badge)
    elif target in ("mochito","mochita"):
        send_push_to(target, title=title, body=body, url=url, tag=tag, icon=icon, badge=badge)
    else:
        flash("Destino inválido.", "error"); return redirect("/admin")

    flash("Notificación enviada ✅", "success")
    send_discord("Admin: push now", {"target":target, "title":title})
    return redirect("/admin")

@app.post("/admin/push/schedule")
def admin_push_schedule():
    require_admin()
    target = request.form.get("target", "both")
    title  = (request.form.get("title") or "").strip()
    body   = (request.form.get("body") or "").strip()
    when_local = request.form.get("when_local")  # YYYY-MM-DDTHH:MM
    url    = (request.form.get("url") or "").strip() or None
    tag    = (request.form.get("tag") or "").strip() or None
    icon   = (request.form.get("icon") or "").strip() or None
    badge  = (request.form.get("badge") or "").strip() or None

    when_txt = parse_datetime_local_to_madrid(when_local)
    if not (title and when_txt and target in ("mochito","mochita","both")):
        flash("Faltan datos (título/fecha o destino incorrecto).", "error")
        return redirect("/admin")

    now_txt = now_madrid_str()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO scheduled_notifications
                (created_by, target, title, body, url, tag, icon, badge, when_at, status, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)
                RETURNING id
            """, ('mochito', target, title, body, url, tag, icon, badge, when_txt, now_txt))
            _new_id = c.fetchone()[0]
            conn.commit()
        flash("Notificación programada 📆", "success")
        send_discord("Admin: push scheduled", {"target":target, "title":title, "when_at":when_txt})
    finally:
        conn.close()
    return redirect("/admin")

@app.post("/admin/push/cancel")
def admin_push_cancel():
    require_admin()
    sid = request.form.get("id")
    if not sid:
        flash("Falta ID.", "error")
        return redirect("/admin")
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE scheduled_notifications SET status='cancelled' WHERE id=%s AND status='pending'", (sid,))
            affected = c.rowcount
            conn.commit()
        if affected:
            flash("Programada cancelada 🛑", "info")
            send_discord("Admin: push cancelled", {"id": int(sid)})
        else:
            flash("No se pudo cancelar (¿ya enviada o inexistente?).", "error")
    finally:
        conn.close()
    return redirect("/admin")


ADMIN_RESET_TOKEN = os.environ.get("ADMIN_RESET_TOKEN", "").strip()

@app.get("/__reset_pw")
def __reset_pw():
    # Seguridad: si no hay token configurado devolvemos 404 para ocultar
    if not ADMIN_RESET_TOKEN:
        return ("Not found", 404)
    t = request.args.get("token", "")
    u = request.args.get("u", "")
    pw = request.args.get("pw", "")
    if t != ADMIN_RESET_TOKEN:
        abort(403)
    if u not in ("mochito","mochita") or len(pw) < 4:
        return jsonify(ok=False, error="bad_params"), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE users SET password=%s WHERE username=%s",
                      (generate_password_hash(pw), u))
            conn.commit()
        send_discord("Admin: reset via token", {"user": u})
        return jsonify(ok=True)
    finally:
        conn.close()

# === ADMIN: banco de preguntas ===

@app.get("/admin/questions")
def admin_questions():
    require_admin()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
              SELECT id, text, used, used_at, active, created_at
              FROM question_bank
              ORDER BY active DESC, used, id DESC
            """)
            rows = c.fetchall()
            c.execute("SELECT COUNT(*) FROM question_bank")
            total = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM question_bank WHERE used=FALSE AND active=TRUE")
            pend = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM question_bank WHERE used=TRUE")
            used = c.fetchone()[0]
    finally:
        conn.close()
    return render_template("admin_questions.html",
                           username=session['username'],
                           rows=rows,
                           stats={"total": total, "pend": pend, "used": used})

@app.post("/admin/questions")
def admin_questions_add():
    require_admin()
    text = (request.form.get("text") or "").strip()
    if text:
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO question_bank (text, created_at)
                    VALUES (%s,%s)
                    ON CONFLICT (text) DO NOTHING
                """, (text, now_madrid_str()))
                conn.commit()
        finally:
            conn.close()
        flash("Pregunta añadida ✅", "success")
    return redirect("/admin/questions")

@app.post("/admin/questions/import")
def admin_questions_import():
    require_admin()
    bulk = (request.form.get("bulk") or "")
    lines = [l.strip() for l in bulk.splitlines() if l.strip()]
    if lines:
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                now = now_madrid_str()
                for t in lines:
                    try:
                        c.execute("INSERT INTO question_bank (text, created_at) VALUES (%s,%s)", (t, now))
                    except Exception:
                        pass
                conn.commit()
        finally:
            conn.close()
        flash(f"Importadas {len(lines)} preguntas ✅", "success")
    return redirect("/admin/questions")

@app.post("/admin/questions/<int:qid>/edit")
def admin_questions_edit(qid):
    require_admin()
    text = (request.form.get("text") or "").strip()
    if text:
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                c.execute("UPDATE question_bank SET text=%s WHERE id=%s", (text, qid))
                conn.commit()
        finally:
            conn.close()
        flash("Pregunta actualizada ✏️", "success")
    return redirect("/admin/questions")

@app.post("/admin/questions/<int:qid>/delete")
def admin_questions_delete(qid):
    require_admin()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE question_bank SET active=FALSE WHERE id=%s", (qid,))
            conn.commit()
    finally:
        conn.close()
    flash("Pregunta desactivada 🗑️", "info")
    return redirect("/admin/questions")

@app.post("/admin/questions/<int:qid>/toggle_used")
def admin_questions_toggle_used(qid):
    require_admin()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT used FROM question_bank WHERE id=%s", (qid,))
            row = c.fetchone()
            if row:
                new_used = not bool(row['used'])
                if new_used:
                    c.execute("UPDATE question_bank SET used=TRUE, used_at=%s WHERE id=%s", (now_madrid_str(), qid))
                else:
                    c.execute("UPDATE question_bank SET used=FALSE, used_at=NULL WHERE id=%s", (qid,))
                conn.commit()
    finally:
        conn.close()
    return redirect("/admin/questions")

@app.post("/admin/questions/reset_used")
def admin_questions_reset_used():
    require_admin()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE question_bank SET used=FALSE, used_at=NULL WHERE active=TRUE")
            conn.commit()
    finally:
        conn.close()
    flash("Marcados 'usada' reseteados ✅", "success")
    return redirect("/admin/questions")

@app.post("/admin/questions/rotate_now")
def admin_questions_rotate_now():
    require_admin()
    ok = reroll_today_question()
    flash("Pregunta rotada ahora ✅" if ok else "No hay preguntas disponibles para rotar 😅",
          "success" if ok else "error")
    return redirect("/admin/questions")


@app.post("/admin/questions/delete_all")
def admin_questions_delete_all():
    require_admin()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM question_bank")
            conn.commit()
    finally:
        conn.close()
    flash("Se han borrado TODAS las preguntas del banco 🗑️", "info")
    return redirect("/admin/questions")


# === Tick invocable por HTTP (compatible con GET/POST) ===
@app.route("/__cron/tick", methods=["GET","POST"])
def __cron_tick():
    token = (request.headers.get("X-CRON-TOKEN") or request.args.get("token","")).strip()
    need  = os.environ.get("CRON_TOKEN","").strip()
    if not need or token != need:
        return jsonify(ok=False, error="forbidden"), 403
    try:
        # Ejecuta UNA pasada de las tareas (debes tener run_scheduled_jobs ya definido)
        run_scheduled_jobs()
        return jsonify(ok=True, ran_at=now_madrid_str())
    except Exception as e:
        print("[cron_tick]", e)
        return jsonify(ok=False, error="server_error"), 500



@app.cli.command("tick")
def cli_tick():
    """Ejecuta una pasada del scheduler (para Cron Jobs)."""
    run_scheduled_jobs()
    print("tick OK", now_madrid_str())



@app.post('/media/add')
def media_add():
    if 'username' not in session:
        return redirect('/')
    user = session['username']

    title      = (request.form.get('title') or '').strip()
    cover_url  = (request.form.get('cover_url') or '').strip()
    link_url   = (request.form.get('link_url') or '').strip()
    on_netflix = bool(request.form.get('on_netflix'))
    on_prime   = bool(request.form.get('on_prime'))
    priority   = (request.form.get('priority') or 'media').strip()
    comment    = (request.form.get('comment') or '').strip()

    if priority not in ('alta', 'media', 'baja'):
        priority = 'media'

    if not title:
        flash('Falta el título.', 'error')
        return redirect('/#pelis')

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO media_items
                    (title, cover_url, link_url, on_netflix, on_prime, priority, comment,
                    created_by, created_at, is_watched)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)
                RETURNING id
            """, (title, cover_url, link_url, on_netflix, on_prime, priority, comment,
                user, now_madrid_str()))
            mid = c.fetchone()[0]
            conn.commit()

            # 🔔 Push al otro
            try:
                push_media_added(
                    creator=user,
                    mid=int(mid),
                    title=title,
                    on_netflix=bool(on_netflix),
                    on_prime=bool(on_prime),
                    link_url=link_url or None
                )
            except Exception as e:
                print("[push media_added]", e)

        try: send_discord('Media add', {'by': user, 'title': title, 'priority': priority})
        except Exception: pass
        flash('Añadido a "Por ver" 🎬', 'success')
    except Exception as e:
        print('[media_add] ', e)
        try: conn.rollback()
        except Exception: pass
        flash('No se pudo añadir.', 'error')
    finally:
        conn.close()
    return redirect('/#pelis')


@app.post('/media/watch')
def media_mark_watched():
    if 'username' not in session:
        return redirect('/')
    user = session['username']

    mid = (request.form.get('id') or '').strip()
    rating_raw = (request.form.get('rating') or '').strip()
    watched_comment = (request.form.get('watched_comment') or '').strip()

    # rating ∈ {1..5} o None
    rating = None
    if rating_raw.isdigit():
        r = int(rating_raw)
        if 1 <= r <= 5:
            rating = r

    if not mid:
        flash('Falta ID.', 'error')
        return redirect('/#pelis')

    conn = get_db_connection()
    try:
        did_push = False
        title_row = ''

        with conn.cursor() as c:
            # Leemos estado previo para notificación y transición
            c.execute("SELECT is_watched, title, reviews FROM media_items WHERE id=%s", (mid,))
            row = c.fetchone()
            if not row:
                flash('Elemento no encontrado.', 'error')
                return redirect('/#pelis')

            prev_watched = bool(row['is_watched'] if isinstance(row, dict) else row[0])
            title_row    = (row['title'] if isinstance(row, dict) else row[1]) or ''
            raw_reviews  =  row['reviews'] if isinstance(row, dict) else row[2]

            # Normaliza reviews (dict/json/empty)
            try:
                rv = dict(raw_reviews) if isinstance(raw_reviews, dict) else (
                    json.loads(raw_reviews) if (isinstance(raw_reviews, str) and raw_reviews.strip()) else {}
                )
            except Exception:
                rv = {}

            prev = rv.get(user) or {}
            rv[user] = {
                "rating":  rating if rating is not None else prev.get("rating"),
                "comment": (watched_comment or prev.get("comment") or "")
            }

            # Recalcular media
            vals = []
            for obj in rv.values():
                try:
                    rr = int((obj or {}).get('rating') or 0)
                    if 1 <= rr <= 5:
                        vals.append(rr)
                except Exception:
                    pass
            avg = round(sum(vals)/len(vals), 1) if vals else None

            # Persistir como vista (y dejar legacy a NULL)
            c.execute("""
                UPDATE media_items
                   SET is_watched     = TRUE,
                       watched_at      = %s,
                       reviews         = %s::jsonb,
                       avg_rating      = %s,
                       rating          = NULL,
                       watched_comment = NULL
                 WHERE id=%s
            """, (now_madrid_str(), json.dumps(rv, ensure_ascii=False), avg, mid))

            if not prev_watched:
                did_push = True

        conn.commit()

        # Notificar fuera de la transacción
        if did_push:
            try:
                push_media_watched(watcher=user, mid=int(mid), title=title_row, rating=rating)
            except Exception as e:
                print("[push media_watched]", e)

        try:
            send_discord('Media watched', {'by': user, 'id': int(mid), 'rating': rating})
        except Exception:
            pass
        flash('Marcado como visto ✅', 'success')

    except Exception as e:
        print('[media_mark_watched] ', e)
        try: conn.rollback()
        except Exception: pass
        flash('No se pudo marcar como visto.', 'error')
    finally:
        conn.close()

    return redirect('/#pelis')


@app.post('/media/rate')
def media_rate_update():
    if 'username' not in session:
        return redirect('/')
    mid = request.form.get('id')
    rating_raw = (request.form.get('rating') or '').strip()
    watched_comment = (request.form.get('watched_comment') or '').strip()

    if not mid:
        flash('Falta ID.', 'error')
        return redirect('/#pelis')

    rating = None
    if rating_raw.isdigit():
        r = int(rating_raw)
        if 1 <= r <= 5:
            rating = r

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                UPDATE media_items
                SET rating = %s,
                    watched_comment = NULLIF(%s,'')
                WHERE id = %s AND is_watched = TRUE
            """, (rating, watched_comment, mid))
            conn.commit()
        flash('Valoración guardada ⭐', 'success')
    except Exception as e:
        print('[media_rate_update] ', e)
        try: conn.rollback()
        except Exception: pass
        flash('No se pudo guardar la valoración.', 'error')
    finally:
        conn.close()

    return redirect('/#pelis')


@app.post('/media/unwatch')
def media_unwatch():
    if 'username' not in session:
        return redirect('/')
    mid = request.form.get('id')
    if not mid:
        flash('Falta ID.', 'error')
        return redirect('/#pelis')

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                UPDATE media_items
                SET is_watched = FALSE,
                    watched_at = NULL,
                    rating = NULL,
                    watched_comment = NULL
                WHERE id = %s
            """, (mid,))
            conn.commit()
        flash('Devuelto a "Por ver".', 'info')
    except Exception as e:
        print('[media_unwatch] ', e)
        try: conn.rollback()
        except Exception: pass
        flash('No se pudo desmarcar.', 'error')
    finally:
        conn.close()

    return redirect('/#pelis')


@app.post('/media/delete')
def media_delete():
    if 'username' not in session:
        return redirect('/')
    mid = request.form.get('id')
    if not mid:
        flash('Falta ID.', 'error')
        return redirect('/#pelis')

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM media_items WHERE id = %s", (mid,))
            conn.commit()
        flash('Eliminado 🗑️', 'success')
    except Exception as e:
        print('[media_delete] ', e)
        try: conn.rollback()
        except Exception: pass
        flash('No se pudo eliminar.', 'error')
    finally:
        conn.close()

    return redirect('/#pelis')


@app.route('/media/update', methods=['GET','POST'])
def media_update():
    # GET directo → vuelve a Pelis
    if request.method == 'GET':
        return redirect('/#pelis')

    if 'username' not in session:
        return redirect('/')

    u = session['username']
    mid = (request.form.get('id') or '').strip()
    if not mid:
        flash('Falta ID.', 'error')
        return redirect('/#pelis')

    # Metadatos (Por ver)
    title      = (request.form.get('title') or '').strip()
    cover_url  = (request.form.get('cover_url') or '').strip()
    link_url   = (request.form.get('link_url') or '').strip()
    on_netflix = bool(request.form.get('on_netflix'))
    on_prime   = bool(request.form.get('on_prime'))
    priority   = (request.form.get('priority') or 'media').strip()
    comment    = (request.form.get('comment') or '').strip()

    # Opciones de visto / rating
    mark_watched     = bool(request.form.get('mark_watched') or request.form.get('watch'))
    unwatch          = bool(request.form.get('unwatch'))
    rating_raw       = (request.form.get('rating') or '').strip()
    watched_comment  = (request.form.get('watched_comment') or '').strip()

    rating = None
    if rating_raw.isdigit():
        rr = int(rating_raw)
        if 1 <= rr <= 5:
            rating = rr

    if priority not in ('alta','media','baja'):
        priority = 'media'

    conn = get_db_connection()
    try:
        did_push = False
        title_row = ''

        with conn.cursor() as c:
            # 0) Leer estado previo (para detectar transición y tener el título)
            c.execute("SELECT is_watched, title, reviews FROM media_items WHERE id=%s", (mid,))
            base = c.fetchone()
            if not base:
                flash('Elemento no encontrado.', 'error')
                return redirect('/#pelis')

            prev_watched = bool(base['is_watched'] if isinstance(base, dict) else base[0])
            title_row    = (base['title']      if isinstance(base, dict) else base[1]) or ''
            raw_reviews0 =  base['reviews']    if isinstance(base, dict) else base[2]

            # 1) Actualiza metadatos si vienen en el form
            c.execute("""
                UPDATE media_items SET
                    title      = COALESCE(NULLIF(%s,''), title),
                    cover_url  = COALESCE(NULLIF(%s,''), cover_url),
                    link_url   = COALESCE(NULLIF(%s,''), link_url),
                    on_netflix = %s,
                    on_prime   = %s,
                    priority   = %s,
                    comment    = %s
                WHERE id=%s
            """, (title, cover_url, link_url, on_netflix, on_prime, priority, comment, mid))

            # 2) Marcar como NO vista (si se pidió)
            if unwatch:
                c.execute("""
                    UPDATE media_items
                       SET is_watched = FALSE,
                           watched_at = NULL,
                           rating = NULL,
                           watched_comment = NULL
                     WHERE id=%s
                """, (mid,))

            # 3) Marcar vista / guardar review en JSONB (+avg) si procede
            elif mark_watched or rating is not None or watched_comment:
                # Partimos de las reviews que ya había
                try:
                    rv = (dict(raw_reviews0) if isinstance(raw_reviews0, dict)
                          else (json.loads(raw_reviews0) if (isinstance(raw_reviews0, str) and raw_reviews0.strip()) else {}))
                except Exception:
                    rv = {}

                prev = rv.get(u) or {}
                rv[u] = {
                    "rating":  rating if rating is not None else prev.get("rating"),
                    "comment": (watched_comment or prev.get("comment") or "")
                }

                vals = []
                for obj in rv.values():
                    try:
                        r = int((obj or {}).get('rating') or 0)
                        if 1 <= r <= 5:
                            vals.append(r)
                    except Exception:
                        pass
                avg = round(sum(vals)/len(vals), 1) if vals else None

                c.execute("""
                    UPDATE media_items
                       SET is_watched    = TRUE,
                           watched_at     = %s,
                           reviews        = %s::jsonb,
                           avg_rating     = %s,
                           -- legacy a NULL para no confundir
                           rating         = NULL,
                           watched_comment= NULL
                     WHERE id=%s
                """, (now_madrid_str(), json.dumps(rv, ensure_ascii=False), avg, mid))

                # Si antes no estaba vista y ahora sí, luego notificamos
                if not prev_watched:
                    did_push = True

        conn.commit()

        # 4) Notificar (ya fuera de la transacción)
        if did_push:
            try:
                push_media_watched(watcher=u, mid=int(mid), title=title_row, rating=rating)
            except Exception as e:
                print("[push media_watched/update]", e)

        flash('Actualizado 🎬', 'success')

    except Exception as e:
        print('[media_update] ', e)
        try:
            conn.rollback()
        except Exception:
            pass
        flash('No se pudo actualizar.', 'error')
    finally:
        conn.close()

    return redirect('/#pelis')

@app.get('/api/cycle')
def api_cycle_get():
    if 'username' not in session:
        return jsonify({"error": "unauthenticated"}), 401
    # Por defecto mostramos 3 meses centrados en el actual
    who = (request.args.get('user') or 'mochita').strip() or 'mochita'
    ym  = request.args.get('from')  # 'YYYY-MM'
    months = int(request.args.get('months') or 3)
    today = today_madrid()
    if ym and re.fullmatch(r'\d{4}-\d{2}', ym):
        y, m = map(int, ym.split('-'))
        first = date(y, m, 1)
    else:
        first = today.replace(day=1)
    # rango de meses
    from datetime import timedelta as _td
    # primer día del bloque (mes inicial)
    start = first
    # último día del bloque
    y = first.year + (first.month - 1 + months - 1) // 12
    m = (first.month - 1 + months - 1) % 12 + 1
    if m == 12:
        end = date(y, 12, 31)
    else:
        end = (date(y, m+1, 1) - _td(days=1))
    data = get_cycle_entries(who, start, end)
    pred = predict_cycle(who)
    return jsonify({"ok": True, "user": who, "from": start.isoformat(), "to": end.isoformat(), "entries": data, "prediction": pred})

@app.post('/cycle/save')
def cycle_save():
    if 'username' not in session:
        return redirect('/')

    created_by = session['username']

    # 🔒 Solo mochita puede editar/guardar
    if created_by != 'mochita':
        try:
            send_discord("Cycle save FORBIDDEN", {"by": created_by})
        except Exception:
            pass
        if request.is_json or request.headers.get('Accept','').startswith('application/json'):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        flash("Solo mochita puede editar el ciclo.", "error")
        return redirect('/#ciclo')

    # Admite form o JSON
    payload = request.get_json(silent=True) or request.form

    # Fuerza a guardar SIEMPRE para mochita (ignora 'for_user' entrante)
    for_user = 'mochita'

    day  = (payload.get('day') or '').strip()
    mood = (payload.get('mood') or '').strip() or None
    flow = (payload.get('flow') or '').strip() or None
    pain_raw = (payload.get('pain') or '').strip()
    notes = (payload.get('notes') or '').strip() or None

    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day or ''):
        if request.is_json or request.headers.get('Accept','').startswith('application/json'):
            return jsonify({"ok": False, "error": "bad_day"}), 400
        flash("Fecha inválida.", "error")
        return redirect('/#ciclo')

    if mood and mood not in ALLOWED_MOODS:
        mood = None
    if flow and flow not in ALLOWED_FLOWS:
        flow = None

    pain = None
    if pain_raw.isdigit():
        pr = int(pain_raw)
        if 0 <= pr <= 10:
            pain = pr

    now_txt = now_madrid_str()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO cycle_entries (username, day, mood, flow, pain, notes, created_by, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (username, day) DO UPDATE
                SET mood=EXCLUDED.mood,
                    flow=EXCLUDED.flow,
                    pain=EXCLUDED.pain,
                    notes=EXCLUDED.notes,
                    created_by=EXCLUDED.created_by,
                    created_at=EXCLUDED.created_at
            """, (for_user, day, mood, flow, pain, notes, created_by, now_txt))
            conn.commit()
        try:
            send_discord("Cycle save", {"by": created_by, "for": for_user, "day": day, "flow": flow, "mood": mood, "pain": pain})
            broadcast("cycle_update", {"user": for_user, "day": day})
        except Exception:
            pass
    finally:
        conn.close()

    if request.is_json or request.headers.get('Accept','').startswith('application/json'):
        return jsonify({"ok": True})
    flash("Día guardado ✅", "success")
    return redirect('/#ciclo')


@app.post('/cycle/delete')
def cycle_delete():
    if 'username' not in session:
        return redirect('/')

    created_by = session['username']

    # 🔒 Solo mochita puede borrar
    if created_by != 'mochita':
        try:
            send_discord("Cycle delete FORBIDDEN", {"by": created_by})
        except Exception:
            pass
        if request.is_json or request.headers.get('Accept','').startswith('application/json'):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        flash("Solo mochita puede editar el ciclo.", "error")
        return redirect('/#ciclo')

    # Siempre operamos sobre mochita
    for_user = 'mochita'
    day = (request.form.get('day') or request.values.get('day') or '').strip()

    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day or ''):
        if request.is_json or request.headers.get('Accept','').startswith('application/json'):
            return jsonify({"ok": False, "error": "bad_day"}), 400
        flash("Fecha inválida.", "error")
        return redirect('/#ciclo')

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM cycle_entries WHERE username=%s AND day=%s", (for_user, day))
            conn.commit()
        try:
            send_discord("Cycle delete", {"by": created_by, "for": for_user, "day": day})
            broadcast("cycle_update", {"user": for_user, "day": day, "deleted": True})
        except Exception:
            pass
    finally:
        conn.close()

    if request.is_json or request.headers.get('Accept','').startswith('application/json'):
        return jsonify({"ok": True})
    flash("Día eliminado 🗑️", "info")
    return redirect('/#ciclo')


# ======= Página /regla =======
@app.route("/regla")
def regla_page():
    periods = get_periods_for_user()  # -> [{id, start, end, length, flow, pain, notes}, ...]
    cycle_stats = compute_cycle_stats(periods)  # -> dict con métricas y predicciones

    # Prepara calendario del mes actual
    year, month = date.today().year, date.today().month
    cal = build_calendar_data(year, month, periods, cycle_stats)

    return render_template(
        "regla.html",
        periods=periods,
        cycle_stats=cycle_stats,
        calendar=cal,
        symptoms_choices=['Cólicos','Dolor lumbar','Hinchazón','Sensibilidad pechos','Dolor de cabeza','Acné','Antojos','Insomnio','Náuseas','Diarrea/Estreñimiento'],
        today=date.today().isoformat()
    )

@app.post("/regla/add_period")
def add_period():
    start = request.form.get("start_date")
    end   = request.form.get("end_date") or None
    flow  = request.form.get("flow") or "medio"
    pain  = request.form.get("pain") or None
    notes = request.form.get("notes") or ""
    save_period(start, end, flow, pain, notes)
    flash("Periodo guardado", "success")
    return redirect(url_for("regla_page"))

@app.post("/regla/add_symptom")
def add_symptoms():
    symptoms = request.form.getlist("symptoms")
    payload = {
        "date": request.form.get("sym_date"),
        "mood": request.form.get("mood"),
        "energy": request.form.get("energy"),
        "bbt": request.form.get("bbt"),
        "had_sex": bool(request.form.get("had_sex")),
        "protected": bool(request.form.get("protected")),
        "symptoms": symptoms,
        "notes": request.form.get("sym_notes") or ""
    }
    save_symptoms(payload)
    flash("Síntomas guardados", "success")
    return redirect(url_for("regla_page"))

@app.post("/regla/delete_period")
def delete_period():
    pid = request.form.get("id")
    delete_period_by_id(pid)
    flash("Registro eliminado", "success")
    return redirect(url_for("regla_page"))



# ======= Ciclo: batch-save y borrados masivos + tema =======

@app.post('/cycle/batch_save')
def cycle_batch_save():
    if 'username' not in session:
        return jsonify({"ok": False, "error": "unauthenticated"}), 401

    payload = request.get_json(silent=True) or {}
    for_user = (payload.get('for_user') or 'mochita').strip() or 'mochita'
    items = payload.get('items') or []
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "error": "empty"}), 400

    now_txt = now_madrid_str()
    created_by = session['username']
    saved = 0
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            for it in items:
                day  = (it.get('day') or '').strip()
                mood = (it.get('mood') or None)
                flow = (it.get('flow') or None)
                # Validaciones
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day or ''):
                    continue
                if mood and mood not in ALLOWED_MOODS:
                    mood = None
                if flow and flow not in ALLOWED_FLOWS:
                    flow = None
                c.execute("""
                    INSERT INTO cycle_entries (username, day, mood, flow, pain, notes, created_by, created_at)
                    VALUES (%s,%s,%s,%s,NULL,NULL,%s,%s)
                    ON CONFLICT (username, day) DO UPDATE
                    SET mood=EXCLUDED.mood,
                        flow=EXCLUDED.flow,
                        created_by=EXCLUDED.created_by,
                        created_at=EXCLUDED.created_at
                """, (for_user, day, mood, flow, created_by, now_txt))
                saved += 1
        conn.commit()
        try:
            send_discord("Cycle batch_save", {"by": created_by, "for": for_user, "count": saved})
            broadcast("cycle_update", {"user": for_user, "bulk": True})
        except Exception:
            pass
    finally:
        conn.close()
    return jsonify({"ok": True, "saved": saved})


@app.post('/cycle/bulk_delete')
def cycle_bulk_delete():
    if 'username' not in session:
        return jsonify({"ok": False, "error": "unauthenticated"}), 401

    payload   = request.get_json(silent=True) or {}
    for_user  = (payload.get('for_user') or 'mochita').strip() or 'mochita'
    scope     = (payload.get('scope') or 'month').strip()  # 'month' | 'range' | 'all'
    ym        = (payload.get('ym') or '').strip()          # 'YYYY-MM' cuando scope='month'
    start_txt = (payload.get('start') or '').strip()
    end_txt   = (payload.get('end') or '').strip()

    from datetime import date as _date, timedelta as _td
    def last_day_of_month(y, m):
        import calendar
        return _date(y, m, calendar.monthrange(y, m)[1])

    start = end = None
    if scope == 'all':
        # borra todo para ese usuario
        start = _date(1900,1,1); end = _date(9999,12,31)
    elif scope == 'month' and re.fullmatch(r'\d{4}-\d{2}', ym or ''):
        y, m = map(int, ym.split('-'))
        start = _date(y, m, 1); end = last_day_of_month(y, m)
    elif scope == 'range' and re.fullmatch(r'\d{4}-\d{2}-\d{2}', start_txt or '') and re.fullmatch(r'\d{4}-\d{2}-\d{2}', end_txt or ''):
        start = datetime.strptime(start_txt, "%Y-%m-%d").date()
        end   = datetime.strptime(end_txt,   "%Y-%m-%d").date()
        if end < start:
            start, end = end, start
    else:
        return jsonify({"ok": False, "error": "bad_scope"}), 400

    deleted = 0
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                DELETE FROM cycle_entries
                 WHERE username=%s AND day BETWEEN %s AND %s
            """, (for_user, start.isoformat(), end.isoformat()))
            deleted = c.rowcount or 0
            conn.commit()
        try:
            send_discord("Cycle bulk_delete", {"by": session['username'], "for": for_user,
                                               "from": start.isoformat(), "to": end.isoformat(), "deleted": deleted})
            broadcast("cycle_update", {"user": for_user, "bulk_deleted": True,
                                       "from": start.isoformat(), "to": end.isoformat()})
        except Exception:
            pass
    finally:
        conn.close()
    return jsonify({"ok": True, "deleted": int(deleted)})


@app.get('/api/cycle/theme')
def api_cycle_theme():
    if 'username' not in session:
        return jsonify({"error": "unauthenticated"}), 401
    for_user = (request.args.get('for_user') or 'mochita').strip() or 'mochita'
    key = f"cycle_theme::{for_user}"
    raw = state_get(key, "")
    try:
        saved = json.loads(raw) if raw else {}
    except Exception:
        saved = {}
    # Valores por defecto (coinciden con tus CSS variables)
    default = {
        "primary": "#e84393", "secondary": "#fd79a8", "accent": "#a29bfe",
        "flow_none": "#cbd5e1", "flow_low": "#fecdd3", "flow_mid": "#fb7185", "flow_high": "#dc2626",
        "range_bg": "rgba(232,67,147,.10)"
    }
    theme = {**default, **(saved or {})}
    return jsonify({"ok": True, "theme": theme, "for_user": for_user})


@app.post('/api/cycle/theme')
def api_cycle_theme_save():
    if 'username' not in session:
        return jsonify({"error": "unauthenticated"}), 401
    payload = request.get_json(silent=True) or {}
    for_user = (payload.get('for_user') or 'mochita').strip() or 'mochita'
    theme    = payload.get('theme') or {}
    if not isinstance(theme, dict):
        return jsonify({"ok": False, "error": "bad_theme"}), 400
    # Filtro simple de claves permitidas
    allowed = {"primary","secondary","accent","flow_none","flow_low","flow_mid","flow_high","range_bg"}
    clean = {k: v for k, v in theme.items() if k in allowed and isinstance(v, str) and v.strip()}
    key = f"cycle_theme::{for_user}"
    state_set(key, json.dumps(clean, ensure_ascii=False))
    try:
        send_discord("Cycle theme saved", {"by": session['username'], "for": for_user})
        broadcast("cycle_theme", {"user": for_user})
    except Exception:
        pass
    return jsonify({"ok": True})



@app.post('/api/location')
def api_location():
    if 'username' not in session:
        abort(401)
    data = request.get_json(force=True) or {}
    lat = float(data.get('lat', 0))
    lng = float(data.get('lng', 0))
    acc = float(data.get('acc', 0))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({'ok': False, 'error': 'coords'}), 400

    user = session['username']
    now_txt = now_madrid_str()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO locations (username, location_name, latitude, longitude, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (username) DO UPDATE
                SET latitude = EXCLUDED.latitude,
                    longitude = EXCLUDED.longitude,
                    updated_at = EXCLUDED.updated_at
            """, (user, None, lat, lng, now_txt))
            conn.commit()
    finally:
        conn.close()
    try:
        broadcast('location_update', {'user': user})
    except Exception:
        pass
    return jsonify({'ok': True})

@app.get('/api/other_location')
def api_other_location():
    if 'username' not in session:
        abort(401)
    other = request.args.get('user') or ('mochita' if session['username'] == 'mochito' else 'mochito')
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT latitude, longitude, updated_at
                FROM locations
                WHERE username=%s
            """, (other,))
            row = c.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({})
    # row puede ser DictRow o tupla, usa claves seguras:
    lat = row['latitude'] if isinstance(row, dict) else row[0]
    lng = row['longitude'] if isinstance(row, dict) else row[1]
    ts  = row['updated_at'] if isinstance(row, dict) else row[2]
    return jsonify({'lat': lat, 'lng': lng, 'updated_at': (ts.isoformat() if ts else None)})



@app.get("/api/edvx_search")
def api_edvx_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "missing_query"}), 400

    try:
        results, used = _search_edvx(q)
        source = used or EDVX_BASE
        if not results:
            # Fallback a DDG si el sitio devuelve su página de “Ups…”
            results, used = _search_ddg_fallback(q)
            source = used

        return jsonify({
            "ok": True,
            "query": q,
            "count": len(results),
            "source": source,
            "results": results
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get('/api/estrenos/search')
def api_estrenos_search():
    if 'username' not in session:
        return jsonify({"ok": False, "error": "unauthenticated"}), 401
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({"ok": False, "error": "missing q"}), 400
    results, used_url = search_estrenos(q)
    # Log ligerito para ver qué camino tomó
    try:
        send_discord("Estrenos search", {"q": q, "count": len(results), "source": used_url})
    except Exception:
        pass
    return jsonify({"ok": True, "count": len(results), "source": used_url, "results": results})



# Arrancar el scheduler sólo si RUN_SCHEDULER=1
if os.environ.get("RUN_SCHEDULER", "1") == "1":
    threading.Thread(target=background_loop, daemon=True).start()


# ======= WSGI / Run =======
if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=bool(os.environ.get('FLASK_DEBUG')))


