

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
# ========= Discord logs (DESACTIVADO) =========
DISCORD_WEBHOOK = os.environ.get('DISCORD_WEBHOOK', '')

def client_ip():
    return (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()

def _is_hashed(v: str) -> bool:
    return isinstance(v, str) and (v.startswith('pbkdf2:') or v.startswith('scrypt:'))

def send_discord(event: str, payload: dict | None = None):
    """Logs a Discord desactivados.
    Deja la firma igual para no tocar el resto del código."""
    return

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

    # Aumentar el número máximo de conexiones
    maxconn_env = int(os.environ.get('DB_MAX_CONN', '10'))  # Aumentado a 10
    maxconn = max(1, min(maxconn_env, 15))  # Máximo 15

    PG_POOL = SimpleConnectionPool(
        1, maxconn,
        dsn=DATABASE_URL,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
        connect_timeout=10  # Aumentado timeout
    )

class PooledConn:
    def __init__(self, pool, conn):
        self._pool = pool
        self._conn = conn
        self._inited = False
        self._closed = False

    def _reconnect(self):
        try:
            self._pool.putconn(self._conn, close=True)
        except Exception:
            pass
        self._conn = self._pool.getconn()
        self._inited = False
        self._closed = False

    def __getattr__(self, name):
        if self._closed:
            raise Exception("Connection is closed")
        return getattr(self._conn, name)

    def cursor(self, *a, **k):
        if self._closed:
            raise Exception("Connection is closed")
            
        if "cursor_factory" not in k:
            k["cursor_factory"] = psycopg2.extras.DictCursor

        if not self._inited:
            try:
                with self._conn.cursor() as c:
                    c.execute("SET application_name = 'mochitos';")
                    c.execute("SET idle_in_transaction_session_timeout = '30s';")  # Aumentado
                    c.execute("SET statement_timeout = '30s';")  # Aumentado
                self._inited = True
            except (OperationalError, InterfaceError):
                self._reconnect()
                with self._conn.cursor() as c:
                    c.execute("SET application_name = 'mochitos';")
                    c.execute("SET idle_in_transaction_session_timeout = '30s';")
                    c.execute("SET statement_timeout = '30s';")
                self._inited = True

        try:
            return self._conn.cursor(*a, **k)
        except (OperationalError, InterfaceError):
            self._reconnect()
            return self._conn.cursor(*a, **k)

    def close(self):
        if not self._closed:
            try:
                self._pool.putconn(self._conn)
                self._closed = True
            except Exception:
                try:
                    self._pool.putconn(self._conn, close=True)
                except Exception:
                    pass
                self._closed = True

def get_db_connection():
    """Saca una conexión del pool y hace ping."""
    _init_pool()
    conn = None
    wrapped = None
    try:
        conn = PG_POOL.getconn()
        wrapped = PooledConn(PG_POOL, conn)
        with wrapped.cursor() as c:
            c.execute("SELECT 1;")
            _ = c.fetchone()
        return wrapped
    except (OperationalError, InterfaceError, DatabaseError):
        if wrapped:
            try:
                wrapped._reconnect()
                with wrapped.cursor() as c:
                    c.execute("SELECT 1;")
                    _ = c.fetchone()
                return wrapped
            except Exception:
                if conn:
                    PG_POOL.putconn(conn, close=True)
                raise
        else:
            if conn:
                PG_POOL.putconn(conn, close=True)
            raise
    except Exception:
        if conn:
            PG_POOL.putconn(conn, close=True)
        raise

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

def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
    # Cola pequeña para no acumular demasiados eventos en memoria
    client_q: queue.Queue = queue.Queue(maxsize=50)

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

# ========= Ruleta diaria (config) =========
# Index 0..5, mismo orden que en el HTML de la ruleta:
# +0, +5, +10, +20, +50, +100
DAILY_WHEEL_SEGMENTS = [
    {"label": "+0",   "delta": 0,   "weight": 28},  # 28% - más frecuente pero sin pasarse
    {"label": "+5",   "delta": 5,   "weight": 23},  # 23% - muy común
    {"label": "+10",  "delta": 10,  "weight": 20},  # 20% - común
    {"label": "+20",  "delta": 20,  "weight": 14},  # 14% - un poco más complicado
    {"label": "+50",  "delta": 50,  "weight": 9},   # 9%  - difícil
    {"label": "+100", "delta": 100, "weight": 6},   # 6%  - muy difícil
]


def choose_daily_wheel_segment():
    """
    Elige un segmento de DAILY_WHEEL_SEGMENTS según el campo 'weight'.
    Devuelve (index, segment_dict).
    """
    segments = DAILY_WHEEL_SEGMENTS
    weights = [s.get("weight", 1) for s in segments]
    total = sum(weights) or 1
    r = random.uniform(0, total)
    acc = 0.0
    chosen_idx = 0
    chosen_seg = segments[0]
    for idx, (w, seg) in enumerate(zip(weights, segments)):
        acc += w
        if r <= acc:
            chosen_idx = idx
            chosen_seg = seg
            break
    return chosen_idx, chosen_seg


# ========= Ruleta diaria: horario aleatorio 09:00–22:00 =========

def _wheel_reset_time_key(day: date) -> str:
    return f"wheel_reset_time::{day.isoformat()}"


def ensure_wheel_reset_time(day: date, now_madrid: datetime | None = None) -> str | None:
    """
    Devuelve la hora HH:MM (Europe/Madrid) a la que se desbloquea la ruleta ese día.
    - Una vez elegida, se guarda en app_state.
    - Entre las 09:00 y las 22:00.
    - Si el server arranca tarde, elige entre 'ahora' y las 22:00.
    - Si ya es demasiado tarde, devuelve None y guarda '-'.
    """
    now_madrid = now_madrid or europe_madrid_now()
    key = _wheel_reset_time_key(day)
    raw = (state_get(key, "") or "").strip()

    if raw == "-":
        return None
    if re.fullmatch(r"\d{2}:\d{2}", raw):
        return raw

    start_min = 9 * 60   # 09:00
    end_min   = 22 * 60  # 22:00

    if now_madrid.date() == day:
        now_min = now_madrid.hour * 60 + now_madrid.minute
        low = max(start_min, now_min)
    else:
        low = start_min

    if low > end_min:
        state_set(key, "-")
        return None

    m = random.randint(low, end_min)
    hh = m // 60
    mm = m % 60
    hhmm = f"{hh:02d}:{mm:02d}"
    state_set(key, hhmm)
    return hhmm


def build_wheel_time_payload(now_madrid: datetime | None = None) -> dict:
    """
    Devuelve info de tiempos para la ruleta:
      - today: fecha 'YYYY-MM-DD' usada en las claves de estado
      - reset_time: HH:MM en la que se desbloquea ese día
      - can_spin_now: bool (por hora)
      - seconds_to_unlock: segundos hasta que se desbloquee hoy (o 0 si ya se desbloqueó)
      - next_reset_iso: ISO de la próxima hora de reset (hoy o mañana)
      - seconds_to_next_reset: segundos hasta ese próximo reset
    """
    now_madrid = now_madrid or europe_madrid_now()
    today = now_madrid.date()
    today_iso = today.isoformat()

    reset_hhmm = ensure_wheel_reset_time(today, now_madrid)
    reset_dt_today: datetime | None = None
    if reset_hhmm:
        try:
            hh, mm = map(int, reset_hhmm.split(":"))
            reset_dt_today = now_madrid.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except Exception:
            reset_dt_today = None

    can_spin_now = False
    seconds_to_unlock: int | None = None
    next_reset_dt: datetime | None = None

    if reset_dt_today:
        if now_madrid < reset_dt_today:
            can_spin_now = False
            seconds_to_unlock = max(0, int((reset_dt_today - now_madrid).total_seconds()))
            next_reset_dt = reset_dt_today
        else:
            can_spin_now = True
            seconds_to_unlock = 0
            tomorrow = today + timedelta(days=1)
            next_reset_hhmm = ensure_wheel_reset_time(tomorrow, now_madrid + timedelta(days=1))
            if next_reset_hhmm:
                try:
                    hh2, mm2 = map(int, next_reset_hhmm.split(":"))
                    next_reset_dt = reset_dt_today.replace(
                        year=tomorrow.year,
                        month=tomorrow.month,
                        day=tomorrow.day,
                        hour=hh2,
                        minute=mm2,
                        second=0,
                        microsecond=0,
                    )
                except Exception:
                    next_reset_dt = None

    seconds_to_next_reset: int | None = None
    next_reset_iso: str | None = None
    if next_reset_dt:
        seconds_to_next_reset = max(0, int((next_reset_dt - now_madrid).total_seconds()))
        next_reset_iso = next_reset_dt.isoformat()

    return {
        "today": today_iso,
        "reset_time": reset_hhmm,
        "can_spin_now": can_spin_now,
        "seconds_to_unlock": seconds_to_unlock,
        "next_reset_iso": next_reset_iso,
        "seconds_to_next_reset": seconds_to_next_reset,
    }




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

                # === Daily Question: reacciones y chat ===
        c.execute("""
            CREATE TABLE IF NOT EXISTS dq_reactions (
                id SERIAL PRIMARY KEY,
                question_id INTEGER NOT NULL,
                from_user   TEXT    NOT NULL,
                to_user     TEXT    NOT NULL,
                reaction    TEXT    NOT NULL CHECK (char_length(reaction) <= 16),
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL,
                UNIQUE (question_id, from_user, to_user)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_dq_react_q ON dq_reactions (question_id)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS dq_chat (
                id SERIAL PRIMARY KEY,
                question_id INTEGER NOT NULL,
                username   TEXT    NOT NULL,
                msg        TEXT    NOT NULL,
                created_at TEXT    NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_dq_chat_q ON dq_chat (question_id)")


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

        # Índices para pelis/series -> aceleran muchísimo el / (home)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_iswatched_prio_created
            ON media_items (is_watched, priority, created_at DESC, id DESC)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_iswatched_watchedat
            ON media_items (is_watched, watched_at DESC, id DESC)
        """)


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
  # <-- importante

# ========= Helpers =========
# ========= Helpers =========

# ========= Gamificación: esquema, puntos y medallas =========
_gami_ready = False
_gami_lock = threading.Lock()

# def _ensure_gamification_schema():
#     """Crea columnas/tablas si faltan (seguro de repetir)."""
#     global _gami_ready
#     if _gami_ready:
#         return
#     with _gami_lock:
#         if _gami_ready:
#             return
#         conn = get_db_connection()
#         try:
#             with conn.cursor() as c:
#                 # Columna de puntos en users
#                 c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS points INTEGER DEFAULT 0")

#                 # Catálogo de medallas
#                 c.execute("""
#                 CREATE TABLE IF NOT EXISTS achievements (
#                     id SERIAL PRIMARY KEY,
#                     code TEXT UNIQUE NOT NULL,
#                     title TEXT NOT NULL,
#                     description TEXT,
#                     icon TEXT,
#                     goal INTEGER DEFAULT 0
#                 )
#                 """)

#                 # Medallas obtenidas por usuario
#                 c.execute("""
#                 CREATE TABLE IF NOT EXISTS user_achievements (
#                     id SERIAL PRIMARY KEY,
#                     user_id INTEGER NOT NULL,
#                     achievement_id INTEGER NOT NULL,
#                     earned_at TEXT,
#                     UNIQUE (user_id, achievement_id)
#                 )
#                 """)

#                 # Tienda y compras
#                 c.execute("""
#                 CREATE TABLE IF NOT EXISTS shop_items (
#                     id SERIAL PRIMARY KEY,
#                     name TEXT UNIQUE NOT NULL,
#                     cost INTEGER NOT NULL,
#                     description TEXT,
#                     icon TEXT
#                 )
#                 """)
#                 c.execute("""
#                 CREATE TABLE IF NOT EXISTS purchases (
#                     id SERIAL PRIMARY KEY,
#                     user_id INTEGER NOT NULL,
#                     item_id INTEGER NOT NULL,
#                     quantity INTEGER DEFAULT 1,
#                     note TEXT,
#                     purchased_at TEXT
#                 )
#                 """)
#                 c.execute("""
#                 CREATE TABLE IF NOT EXISTS points_history (
#                     id        SERIAL PRIMARY KEY,
#                     user_id   INTEGER NOT NULL REFERENCES users(id),
#                     delta     INTEGER NOT NULL,   -- +10, -5, etc
#                     source    TEXT    NOT NULL,   -- 'daily', 'daily_first', 'admin', 'achievement', 'shop', ...
#                     note      TEXT,
#                     created_at TEXT   NOT NULL
#                 )
#             """)
#                 # En init_db(), después de crear la tabla purchases, añade:
#                 c.execute('''CREATE TABLE IF NOT EXISTS purchases (
#                     id SERIAL PRIMARY KEY,
#                     user_id INTEGER NOT NULL,
#                     item_id INTEGER NOT NULL,
#                     quantity INTEGER DEFAULT 1,
#                     note TEXT,
#                     purchased_at TEXT,
#                     FOREIGN KEY (item_id) REFERENCES shop_items(id) ON DELETE CASCADE
#                 )''')
#             conn.commit()
#         finally:
#             conn.close()
#         _seed_gamification()
#         _gami_ready = True



def _grant_achievement_to(user: str, achievement_id: int, points_on_award=None):
    """
    Concede (si falta) la medalla a 'user', suma puntos y manda una noti con el logro.
    - achievement_id: id de la medalla (tabla achievements)
    - points_on_award:
        * si es None -> se usa achievements.points_on_award
        * si viene con número -> se usa ese número
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            # Info del logro (título + puntos por defecto)
            c.execute(
                "SELECT title, points_on_award FROM achievements WHERE id=%s",
                (achievement_id,)
            )
            ach_info = c.fetchone()
            ach_title = ach_info["title"] if ach_info else "Logro"
            db_points = ach_info["points_on_award"] if ach_info else 0

            # Usuario
            c.execute("SELECT id FROM users WHERE username=%s", (user,))
            uid = (c.fetchone() or [None])[0]
            if not uid:
                return

            # ¿Ya tenía la medalla?
            c.execute("""
                SELECT 1 FROM user_achievements
                WHERE user_id=%s AND achievement_id=%s
            """, (uid, achievement_id))
            if c.fetchone():
                return  # ya concedida

            # Puntos finales a sumar
            if points_on_award is None:
                pts = int(db_points or 0)
            else:
                pts = int(points_on_award or 0)

            if pts != 0:
                c.execute(
                    "UPDATE users SET points = COALESCE(points,0) + %s WHERE id=%s",
                    (pts, uid)
                )
                # ✅ Historial de puntos por logro
                c.execute("""
                    INSERT INTO points_history (user_id, delta, source, note, created_at)
                    VALUES (%s,%s,%s,%s,%s)
                """, (uid, pts, "achievement", ach_title, now_madrid_str()))

            # Registrar la medalla
            c.execute("""
                INSERT INTO user_achievements (user_id, achievement_id, earned_at)
                VALUES (%s,%s,%s)
                ON CONFLICT (user_id, achievement_id) DO NOTHING
            """, (uid, achievement_id, now_madrid_str()))

            # 🔔 NOTIFICACIÓN PUSH POR LOGRO Y PUNTOS
            try:
                if pts > 0:
                    body = f"{ach_title} - +{pts} puntos"
                else:
                    body = ach_title

                send_push_to(
                    user,
                    title="🏆 ¡Nuevo logro desbloqueado!",
                    body=body
                )
            except Exception as e:
                print("[push achievement]", e)

        conn.commit()
    finally:
        conn.close()



def check_relationship_milestones():
    """Concede automáticamente achievements con trigger_kind='rel_days' el día exacto."""
    dcount = days_together()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
              SELECT id, points_on_award, grant_both
              FROM achievements
              WHERE active = TRUE
                AND trigger_kind = 'rel_days'
                AND trigger_value = %s
            """, (int(dcount),))
            rows = c.fetchall()

        for r in rows:
            aid = int(r['id'])
            pts = int(r['points_on_award'] or 0)
            users = ('mochito','mochita') if bool(r['grant_both']) else ('mochito',)  # por defecto al admin
            for u in users:
                _grant_achievement_to(u, aid, pts)

            try:
                send_discord("Achievement auto-granted", {"aid": aid, "points": pts, "days": dcount, "both": bool(r['grant_both'])})
            except Exception:
                pass
    finally:
        conn.close()


def _user_current_streak(u: str) -> int:
    """Racha actual (días seguidos hasta el último día respondido ≤ hoy)."""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT dq.date
                FROM daily_questions dq
                JOIN answers a ON a.question_id = dq.id AND a.username = %s
                WHERE TRIM(COALESCE(a.answer,'')) <> ''
                ORDER BY dq.date ASC
            """, (u,))
            rows = c.fetchall()
    finally:
        conn.close()
    if not rows:
        return 0
    # convierte 'YYYY-MM-DD' -> date
    days = []
    for r in rows:
        dtxt = r[0] if isinstance(r, dict) else r[0]
        try:
            d = datetime.strptime(dtxt.strip(), "%Y-%m-%d").date()
            days.append(d)
        except Exception:
            pass
    if not days:
        return 0
    sset = set(days)
    # último día respondido que no sea futuro
    today = today_madrid()
    last = max([d for d in sset if d <= today], default=None)
    if not last:
        return 0
    streak = 1
    cur = last - timedelta(days=1)
    while cur in sset:
        streak += 1
        cur -= timedelta(days=1)
    return streak

def _first_answer_count(u: str) -> int:
    """Cuántas veces fuiste la primera respuesta del día (histórico)."""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT COUNT(*) FROM answers a
                WHERE a.username=%s
                  AND TRIM(COALESCE(a.answer,'')) <> ''
                  AND a.id = (
                      SELECT a2.id
                      FROM answers a2
                      WHERE a2.question_id = a.question_id
                        AND TRIM(COALESCE(a2.answer,'')) <> ''
                      ORDER BY COALESCE(a2.created_at,'9999-12-31T00:00:00Z'), a2.id
                      LIMIT 1
                  )
            """, (u,))
            return int((c.fetchone() or [0])[0] or 0)
    finally:
        conn.close()

def _maybe_award_achievements(u: str):
    """Revisa condiciones y concede medallas pendientes."""
    try:
        # conteos
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                c.execute("""
                    SELECT COUNT(*)
                    FROM answers a
                    JOIN daily_questions dq ON dq.id=a.question_id
                    WHERE a.username=%s AND TRIM(COALESCE(a.answer,'')) <> ''
                """, (u,))
                answers_total = int((c.fetchone() or [0])[0] or 0)
        finally:
            conn.close()

        firsts = _first_answer_count(u)
        streak  = _user_current_streak(u)

        # total de días publicados
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                c.execute("SELECT COUNT(*) FROM daily_questions")
                total_days = int((c.fetchone() or [0])[0] or 0)
        finally:
            conn.close()

        checks = [
            ("answers_30",   answers_total >= 30),
            ("answers_100",  answers_total >= 100),
            ("first_answer_1", firsts >= 1),
            ("first_answer_10", firsts >= 10),
            ("streak_7",    streak >= 7),
            ("streak_30",   streak >= 30),
            ("streak_50",   streak >= 50),
        ]
        if total_days >= 100:
            checks.append(("days_100", True))

        # Inserta las medallas que falten para el usuario
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                c.execute("SELECT id FROM users WHERE username=%s", (u,))
                user_id = (c.fetchone() or [None])[0]
                if not user_id:
                    return
                for code, ok in checks:
                    if not ok:
                        continue
                    c.execute("SELECT id FROM achievements WHERE code=%s", (code,))
                    row = c.fetchone()
                    if not row:
                        continue
                    ach_id = row[0]
                    c.execute("""
                        INSERT INTO user_achievements (user_id, achievement_id, earned_at)
                        VALUES (%s,%s,%s)
                        ON CONFLICT (user_id,achievement_id) DO NOTHING
                    """, (user_id, ach_id, now_madrid_str()))
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        app.logger.warning("maybe_award_achievements: %s", e)

# En la función award_points_for_answer, modificar para añadir notificación específica de puntos
def award_points_for_answer(question_id: int, user: str):
    """
    Suma puntos cuando un usuario publica su respuesta del día por primera vez:
      +10 pts por responder
      +10 pts extra si es la PRIMERA respuesta del día (global)
    Luego revisa medallas.
    IMPORTANTE: esta función se debe llamar SOLO cuando se inserta una respuesta nueva.
    """
    base = 10
    bonus = 0

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            # ¿Es la primera respuesta del día? (contamos respuestas NO vacías de otros usuarios)
            c.execute("""
                SELECT COUNT(*) FROM answers
                WHERE question_id=%s 
                  AND TRIM(COALESCE(answer,'')) <> ''
                  AND username != %s
            """, (question_id, user))
            other_answers = int((c.fetchone() or [0])[0] or 0)

            if other_answers == 0:
                bonus = 10  # primerx en contestar

            delta = base + bonus

            # Actualizar puntos
            c.execute("""
                UPDATE users 
                SET points = COALESCE(points,0) + %s 
                WHERE username=%s
            """, (delta, user))

            # ✅ Registrar en historial de puntos
            c.execute("SELECT id FROM users WHERE username=%s", (user,))
            row_uid = c.fetchone()
            if row_uid:
                uid = row_uid[0]
                source = "daily_first" if bonus > 0 else "daily"
                note = "Pregunta del día"
                if bonus > 0:
                    note += " (primero en contestar)"
                c.execute("""
                    INSERT INTO points_history (user_id, delta, source, note, created_at)
                    VALUES (%s,%s,%s,%s,%s)
                """, (uid, delta, source, note, now_madrid_str()))

            conn.commit()

            # 🔔 NOTIFICACIÓN PUSH POR PUNTOS GANADOS
            try:
                if bonus > 0:
                    send_push_to(
                        user,
                        title="¡Puntos ganados! 🎉",
                        body=f"Has ganado {delta} puntos (+{base} por responder, +{bonus} por ser el primero)"
                    )
                else:
                    send_push_to(
                        user,
                        title="¡Puntos ganados! 🎉",
                        body=f"Has ganado {delta} puntos por responder la pregunta"
                    )
            except Exception as e:
                print("[push points award] ", e)

    except Exception as e:
        print(f"[award_points_for_answer] Error: {e}")
        conn.rollback()
    finally:
        conn.close()

    # Revisa medallas
    _maybe_award_achievements(user)



def push_answer_edited_notice(editor: str):
    other = 'mochita' if editor == 'mochito' else 'mochito'
    who   = "tu pareja" if editor in ("mochito", "mochita") else editor
    send_push_to(
        other,
        title="Respuesta editada ✏️",
        body=f"{who} ha editado su respuesta de hoy. 💬",
        url="/#pregunta",
        tag="dq-answer-edit"
    )


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
# --- Lógica dependiente de fecha en Madrid ---
@ttl_cache(seconds=15)
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
        return {
            'today_count': 0,
            'month_total': 0,
            'year_total': 0,
            'days_since_last': None,
            'last_dt': None,
            'streak_days': 0
        }
    today_count = sum(1 for dt in dts if dt.date() == today)
    month_total = sum(1 for dt in dts if dt.year == today.year and dt.month == today.month)
    year_total = sum(1 for dt in dts if dt.year == today.year)
    last_dt = max(dts)
    days_since_last = (today - last_dt.date()).days
    if today_count == 0:
        streak_days = 0
    else:
        dates_with = {dt.date() for dt in dts}
        streak_days = 0
        cur = today
        while cur in dates_with:
            streak_days += 1
            cur = cur - timedelta(days=1)
    return {
        'today_count': today_count,
        'month_total': month_total,
        'year_total': year_total,
        'days_since_last': days_since_last,
        'last_dt': last_dt,
        'streak_days': streak_days
    }

@ttl_cache(seconds=15)
def get_intim_events(limit: int = 200):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT id, username, ts, place, notes
                FROM intimacy_events
                WHERE username IN ('mochito','mochita')
                ORDER BY ts DESC
                LIMIT %s
            """, (limit,))
            rows = c.fetchall()
            return [
                {
                    'id': r[0],
                    'username': r[1],
                    'ts': r[2],
                    'place': r[3] or '',
                    'notes': r[4] or ''
                }
                for r in rows
            ]
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


# Sustituye la función entera por esta
from datetime import date as _Date, datetime as _DateTime

def get_today_question(today: _Date | _DateTime | None = None):
    # Normaliza a date (YYYY-MM-DD)
    if today is None:
        today = europe_madrid_now().date()
    elif isinstance(today, _DateTime):
        today = today.date()
    # hoy como 'YYYY-MM-DD'
    today_str = today.isoformat()

    conn = get_db_connection()
    try:
        # ¿Ya existe para hoy?
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

        # No existe → saca una del banco
        picked = qbank_pick_random(conn)
        if not picked:
            return (None, "Ya no quedan preguntas sin usar. Añade más en Admin → Preguntas.")

        with conn.cursor() as c:
            c.execute("""
                INSERT INTO daily_questions (question, date, bank_id)
                VALUES (%s,%s,%s)
                RETURNING id
            """, (picked['text'], today_str, picked['id']))
            qid = c.fetchone()[0]
            conn.commit()

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



def push_wheel_available():
    """Se llama cuando se desbloquea la ruleta del día."""
    send_push_both(
        title="🎡 Ruleta diaria disponible",
        body="Ya podéis girar la ruleta de hoy y ganar puntos.",
        url="/",
        tag="wheel-open"
    )


def push_wheel_last_hours(user: str):
    """Recordatorio si todavía no ha girado y se acaba el día."""
    send_push_to(
        user,
        title="⏰ Últimas horas para la ruleta",
        body="Aún no has girado la ruleta de hoy. ¡No lo dejes pasar! 💖",
        url="/",
        tag="wheel-last-hours"
    )



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

        # 3-bis) Hitos de relación configurables → medallas + puntos
    try:
        check_relationship_milestones()
    except Exception as e:
        print("[tick rel_milestones]", e)


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

        # 7) Ruleta diaria:
    #    - Aviso cuando se desbloquea (según hora aleatoria 09:00–22:00).
    #    - Recordatorio en las últimas 2h del día si alguien no ha girado todavía.
    try:
        reset_hhmm = ensure_wheel_reset_time(today, now)
        if reset_hhmm:
            sent_key = f"wheel_open::{today.isoformat()}::{reset_hhmm}"
            if not state_get(sent_key, "") and is_due_or_overdue(now, reset_hhmm, grace_minutes=5):
                push_wheel_available()
                state_set(sent_key, "1")

        secs_left_day = seconds_until_next_midnight_madrid()
        if secs_left_day <= 2 * 3600:  # últimas 2 horas del día
            for u in ("mochito", "mochita"):
                spin_key = f"wheel_spin::{u}::{today.isoformat()}"
                if state_get(spin_key, ""):
                    continue  # ya ha girado hoy
                last_key = f"wheel_last_reminder::{today.isoformat()}::{u}"
                if not state_get(last_key, ""):
                    push_wheel_last_hours(u)
                    state_set(last_key, "1")
    except Exception as e:
        print("[tick wheel]", e)




    


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
                answer = (request.form.get('answer') or '').strip()
                if question_id is not None and answer:
                    now_txt = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

                    is_new = False      # se usará para saber si es primera vez
                    was_edited = False  # se usará para saber si es edición

                    conn = get_db_connection()
                    try:
                        with conn.cursor() as c:
                            c.execute("""
                                SELECT id, answer, created_at, updated_at
                                FROM answers
                                WHERE question_id=%s AND username=%s
                            """, (question_id, user))
                            prev = c.fetchone()

                            if not prev:
                                # NUEVA respuesta - insertar
                                c.execute("""
                                    INSERT INTO answers (question_id, username, answer, created_at, updated_at)
                                    VALUES (%s,%s,%s,%s,%s)
                                    ON CONFLICT (question_id, username) DO NOTHING
                                    RETURNING id
                                """, (question_id, user, answer, now_txt, now_txt))
                                new_row = c.fetchone()
                                conn.commit()
                                
                                if new_row:
                                    is_new = True
                                    send_discord("Answer submitted", {"user": user, "question_id": question_id})
                                    # ✅ OTORGAR PUNTOS por primera respuesta
                                    try:
                                        award_points_for_answer(question_id, user)
                                    except Exception as _e:
                                        app.logger.warning("award_points_for_answer failed: %s", _e)
                                        
                            else:
                                prev_id, prev_text, prev_created, prev_updated = prev
                                if answer != (prev_text or ""):
                                    # Respuesta editada - actualizar SIN sumar puntos
                                    was_edited = True
                                    if prev_created is None:
                                        c.execute("""
                                            UPDATE answers
                                            SET answer=%s, updated_at=%s, created_at=%s
                                            WHERE id=%s
                                        """, (answer, now_txt, prev_updated or now_txt, prev_id))
                                    else:
                                        c.execute("""
                                            UPDATE answers
                                            SET answer=%s, updated_at=%s
                                            WHERE id=%s
                                        """, (answer, now_txt, prev_id))
                                    conn.commit()
                                    send_discord("Answer edited", {"user": user, "question_id": question_id})

                        # --- Después de tocar BD, refrescamos cosas y mandamos eventos ---
                        cache_invalidate('compute_streaks')

                        # SSE para el front: indicamos si es nueva o editada
                        if is_new:
                            broadcast("dq_answer", {"user": user, "kind": "new"})
                            try:
                                push_answer_notice(user)
                            except Exception as e:
                                print("[push answer new] ", e)
                        elif was_edited:
                            broadcast("dq_answer", {"user": user, "kind": "edit"})
                            try:
                                push_answer_edited_notice(user)
                            except Exception as e:
                                print("[push answer edited] ", e)
                        # si no cambia el texto, no hacemos nada (ni puntos ni notis)

                    except Exception as e:
                        print(f"[answer submission] Error: {e}")
                        conn.rollback()
                    finally:
                        conn.close()
                        
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
                    conn.commit()
                    check_travel_achievements(user)

                    flash("Viaje añadido ✈️", "success")
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
            # Reacciones y chat de la Pregunta del Día actual
            dq_reactions_map = {}
            c.execute("SELECT to_user, from_user, reaction FROM dq_reactions WHERE question_id=%s", (question_id,))
            for r in c.fetchall():
                dq_reactions_map[r['to_user']] = {"from_user": r['from_user'], "reaction": r['reaction']}

            c.execute("SELECT username, msg, created_at FROM dq_chat WHERE question_id=%s ORDER BY id ASC", (question_id,))
            dq_chat_messages = [dict(row) for row in c.fetchall()]

                        # PUNTOS DEL USUARIO (añade esto)
            c.execute("SELECT COALESCE(points, 0) FROM users WHERE username=%s", (user,))
            user_points = int((c.fetchone() or [0])[0] or 0)


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
            # Solo mostramos las 300 más recientes / prioritarias en el home
            c.execute("""
                SELECT
                    id, title, cover_url, link_url, on_netflix, on_prime,
                    comment, priority,
                    created_by, created_at,
                    reviews,            -- JSONB
                    avg_rating          -- media
                FROM media_items
                WHERE is_watched = FALSE
                ORDER BY
                    CASE priority WHEN 'alta' THEN 0 WHEN 'media' THEN 1 ELSE 2 END,
                    COALESCE(created_at, '1970-01-01') DESC,
                    id DESC
                LIMIT 300
            """)
            media_to_watch = c.fetchall()

            # --- Media: VISTAS ---
            # Igual: solo las 300 últimas vistas (para que el home no explote)
            c.execute("""
                SELECT
                    id, title, cover_url, link_url,
                    reviews,            -- JSONB
                    avg_rating,         -- media
                    watched_at,
                    created_by
                FROM media_items
                WHERE is_watched = TRUE
                ORDER BY COALESCE(watched_at, '1970-01-01') DESC, id DESC
                LIMIT 300
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

        # --- Puntos de ambos para el chip ---
        with conn.cursor() as c2:
            c2.execute("""
                SELECT username, COALESCE(points,0)
                FROM users
                WHERE username IN (%s, %s)
            """, (user, other_user))
            rows = c2.fetchall()
        pts_map = {r[0]: int(r[1]) for r in rows}
        user_points   = pts_map.get(user, 0)
        other_points  = pts_map.get(other_user, 0)

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
                           flows_for_select=flows_for_select,
                           question_id=question_id,
                           dq_reactions=dq_reactions_map,
                           dq_chat_messages=dq_chat_messages,
                           user_points=user_points,   # ⬅️ añade esto
                           other_points=other_points,
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
            check_media_achievements(user)

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



@app.post("/admin/questions/<int:qid>/assign_tomorrow")
def admin_questions_assign_tomorrow(qid):
    require_admin()

    # Mañana en Europe/Madrid (tipo date)
    tmr_date = today_madrid() + timedelta(days=1)

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            # 1) Existe y activa
            c.execute("SELECT id, text, used, active FROM question_bank WHERE id=%s", (qid,))
            qb = c.fetchone()
            if not qb or not qb["active"]:
                flash("Pregunta no encontrada o inactiva.", "error")
                return redirect("/admin/questions")

            if bool(qb["used"]):
                flash("Esa pregunta ya figura como 'usada'. Desmárcala para asignarla a mañana.", "error")
                return redirect("/admin/questions")

            # 2) ¿Ya hay reservada para mañana?
            c.execute("""
                SELECT id, bank_id
                FROM daily_questions
                WHERE date=%s
                ORDER BY id DESC
                LIMIT 1
            """, (tmr_date.isoformat(),))
            row = c.fetchone()

            if row:
                dq_id = row["id"]
                prev_bank_id = row["bank_id"]
                if prev_bank_id and prev_bank_id != qid:
                    qbank_mark_used(conn, prev_bank_id, False)  # devolver al bombo
                c.execute("""
                    UPDATE daily_questions
                       SET question=%s, bank_id=%s
                     WHERE id=%s
                """, (qb["text"], qid, dq_id))
            else:
                c.execute("""
                    INSERT INTO daily_questions (question, date, bank_id)
                    VALUES (%s,%s,%s)
                """, (qb["text"], tmr_date.isoformat(), qid))

            conn.commit()

            # 3) Marcar ésta como usada (reservada)
            qbank_mark_used(conn, qid, True)

        flash(f"Asignada para mañana la #{qid} ✅", "success")
        try:
            send_discord("DQ set for tomorrow", {
                "by": session.get("username"),
                "qid": int(qid),
                "date": tmr_date.isoformat()
            })
        except Exception:
            pass
        return redirect("/admin/questions")
    finally:
        conn.close()



@app.post("/dq/react")
def dq_react():
    if 'username' not in session:
        return redirect('/')
    user = session['username']

    qid_raw = (request.form.get('question_id') or '').strip()
    try:
        qid = int(qid_raw)
    except Exception:
        qid, _ = get_today_question()  # fallback al de hoy

    target_user = (request.form.get('target_user') or '').strip()
    reaction    = (request.form.get('reaction') or '').strip()

    if not reaction:
        flash("Falta la reacción.", "error"); return redirect('/#pregunta')
    if target_user not in ('mochito', 'mochita') or target_user == user:
        flash("Destino inválido.", "error"); return redirect('/#pregunta')

    now_txt = now_madrid_str()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO dq_reactions (question_id, from_user, to_user, reaction, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (question_id, from_user, to_user)
                DO UPDATE SET reaction=EXCLUDED.reaction, updated_at=EXCLUDED.updated_at
            """, (qid, user, target_user, reaction[:16], now_txt, now_txt))
            conn.commit()
        flash("Reacción guardada ❤️", "success")
        send_discord("DQ reaction", {"by": user, "to": target_user, "reaction": reaction})
        try:
            broadcast("dq_reaction", {"from": user, "to": target_user, "reaction": reaction, "q": qid})
        except Exception:
            pass
    except Exception as e:
        print("[dq_react]", e)
        try: conn.rollback()
        except Exception: pass
        flash("No se pudo guardar la reacción.", "error")
    finally:
        conn.close()
    return redirect('/#pregunta')


@app.post("/dq/chat/send")
def dq_chat_send():
    if 'username' not in session:
        return redirect('/')
    user = session['username']

    qid_raw = (request.form.get('question_id') or '').strip()
    try:
        qid = int(qid_raw)
    except Exception:
        qid, _ = get_today_question()  # fallback al de hoy

    msg = (request.form.get('msg') or '').strip()
    if not msg:
        flash("Mensaje vacío.", "error"); return redirect('/#pregunta')
    if len(msg) > 500:
        msg = msg[:500]

    now_txt = now_madrid_str()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO dq_chat (question_id, username, msg, created_at)
                VALUES (%s,%s,%s,%s)
            """, (qid, user, msg, now_txt))
            conn.commit()
        send_discord("DQ chat", {"by": user, "q": qid, "len": len(msg)})
        try:
            broadcast("dq_chat", {"user": user, "msg": msg, "q": qid, "ts": now_txt})
        except Exception:
            pass
    except Exception as e:
        print("[dq_chat_send]", e)
        try: conn.rollback()
        except Exception: pass
        flash("No se pudo enviar el mensaje.", "error")
    finally:
        conn.close()

    return redirect('/#pregunta')





# Arrancar el scheduler sólo si RUN_SCHEDULER=1
if os.environ.get("RUN_SCHEDULER", "1") == "1":
    threading.Thread(target=background_loop, daemon=True).start()


# ======= WSGI / Run =======
if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=bool(os.environ.get('FLASK_DEBUG')))




# ========= Gamificación: puntos, medallas y tienda =========

_gami_ready = False
_gami_lock = threading.Lock()

def _ensure_gamification_schema():
    """
    Crea/migra todo lo de gamificación SOLO UNA VEZ por proceso.
    El resto de llamadas salen inmediatamente.
    """
    global _gami_ready
    if _gami_ready:
        return

    with _gami_lock:
        if _gami_ready:
            return

        with closing(get_db_connection()) as conn, conn.cursor() as c:
            # Columna de puntos en users
            c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS points INTEGER DEFAULT 0")

            # Catálogo de medallas
            c.execute("""
                CREATE TABLE IF NOT EXISTS achievements (
                    id SERIAL PRIMARY KEY,
                    code TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    icon TEXT,
                    goal INTEGER DEFAULT 0
                )
            """)

            # Medallas obtenidas por usuario
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_achievements (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    achievement_id INTEGER NOT NULL,
                    earned_at TEXT,
                    UNIQUE (user_id, achievement_id)
                )
            """)

            # Tienda
            c.execute("""
                CREATE TABLE IF NOT EXISTS shop_items (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    cost INTEGER NOT NULL,
                    description TEXT,
                    icon TEXT
                )
            """)

            # Compras (una sola definición, con FK)
            c.execute("""
                CREATE TABLE IF NOT EXISTS purchases (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    quantity INTEGER DEFAULT 1,
                    note TEXT,
                    purchased_at TEXT,
                    FOREIGN KEY (item_id) REFERENCES shop_items(id) ON DELETE CASCADE
                )
            """)

            # Historial de puntos
            c.execute("""
                CREATE TABLE IF NOT EXISTS points_history (
                    id        SERIAL PRIMARY KEY,
                    user_id   INTEGER NOT NULL REFERENCES users(id),
                    delta     INTEGER NOT NULL,
                    source    TEXT    NOT NULL,
                    note      TEXT,
                    created_at TEXT   NOT NULL
                )
            """)

            # Extiende achievements para triggers y puntos (idempotente)
            c.execute("""
                ALTER TABLE achievements
                ADD COLUMN IF NOT EXISTS trigger_kind TEXT
                    CHECK (trigger_kind IN ('rel_days','manual','none')) DEFAULT 'none'
            """)
            c.execute("""
                ALTER TABLE achievements
                ADD COLUMN IF NOT EXISTS trigger_value INTEGER
            """)
            c.execute("""
                ALTER TABLE achievements
                ADD COLUMN IF NOT EXISTS points_on_award INTEGER DEFAULT 0
            """)
            c.execute("""
                ALTER TABLE achievements
                ADD COLUMN IF NOT EXISTS grant_both BOOLEAN DEFAULT TRUE
            """)
            c.execute("""
                ALTER TABLE achievements
                ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE
            """)

            # (Estos últimos 5 son redundantes, pero inofensivos; si quieres puedes quitarlos)
            c.execute("ALTER TABLE achievements ADD COLUMN IF NOT EXISTS trigger_kind TEXT DEFAULT 'none'")
            c.execute("ALTER TABLE achievements ADD COLUMN IF NOT EXISTS trigger_value INTEGER")
            c.execute("ALTER TABLE achievements ADD COLUMN IF NOT EXISTS points_on_award INTEGER DEFAULT 0")
            c.execute("ALTER TABLE achievements ADD COLUMN IF NOT EXISTS grant_both BOOLEAN DEFAULT TRUE")
            c.execute("ALTER TABLE achievements ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE")

            conn.commit()

        _gami_ready = True



def check_relationship_milestones():
    """
    Revisa achievements activos con trigger_kind='rel_days' y concede si
    days_together() >= trigger_value y el usuario aún no la tiene.
    Si grant_both=True => a mochito y mochita.
    """
    d = days_together()
    if d <= 0:
        return
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT id, code, title, trigger_value, points_on_award, grant_both
                FROM achievements
                WHERE active=TRUE AND trigger_kind='rel_days' AND trigger_value IS NOT NULL
                ORDER BY trigger_value ASC
            """)
            achs = c.fetchall()

        users_to_check = ['mochito', 'mochita']
        for r in achs:
            ach_id  = r['id']
            tgt_val = int(r['trigger_value'] or 0)
            pts     = int(r['points_on_award'] or 0)
            both    = bool(r['grant_both'])

            if d < tgt_val:
                continue

            targets = users_to_check if both else users_to_check  # hoy por hoy ambos; si quisieras uno, cámbialo
            for u in targets:
                # ¿Ya la tiene?
                conn2 = get_db_connection()
                try:
                    with conn2.cursor() as c2:
                        c2.execute("""
                            SELECT EXISTS(
                              SELECT 1
                              FROM user_achievements ua
                              JOIN users us ON us.id=ua.user_id
                              WHERE ua.achievement_id=%s AND us.username=%s
                            )
                        """, (ach_id, u))
                        have = bool((c2.fetchone() or [False])[0])
                    if not have:
                        _grant_achievement_to(u, ach_id, pts)
                        try:
                            send_push_to(u, title="🏆 ¡Nuevo hito!", body=f"Has ganado: {r['title']} (+{pts} pts)" if pts else f"Has ganado: {r['title']}")
                        except Exception:
                            pass
                finally:
                    conn2.close()
    finally:
        conn.close()


def _seed_gamification():
    with closing(get_db_connection()) as conn, conn.cursor() as c:
        # Logros base
        achievements = [
            ("first_answer_1", "¡Primera del día!", "Responder la primera a una pregunta del día", "⚡", 1),
            ("first_answer_10", "Velocista x10", "Ser la primera en 10 preguntas del día", "⚡", 10),
            ("streak_7", "Racha 7", "Responder 7 días seguidos", "🔥", 7),
            ("streak_30", "Racha 30", "Responder 30 días seguidos", "🔥", 30),
            ("streak_50", "Racha 50", "Responder 50 días seguidos", "🔥", 50),
            ("answers_30", "Constante 30", "Acumular 30 respuestas", "🧩", 30),
            ("answers_100", "Constante 100", "Acumular 100 respuestas", "🧩", 100),
            ("days_100", "💯 100 días", "Haber publicado 100 preguntas en total", "💯", 100)
        ]
        
        # Logros de días juntos (100-365, cada 5 días)
        for day in range(100, 366, 5):
            achievements.append((
                f"rel_day_{day}",
                f"❤️ {day} días juntos",
                f"Lleváis {day} días de relación",
                "💖",
                day
            ))
        
        # Logros de contenido multimedia
        media_achievements = [
            (1, "🎬 Primera película/serie", "Añadir tu primera película o serie", "📺"),
            (5, "🎬 Cinéfilo principiante", "Añadir 5 películas o series", "📺"),
            (10, "🎬 Amante del cine", "Añadir 10 películas o series", "🎭"),
            (20, "🎬 Crítico cinematográfico", "Añadir 20 películas o series", "🏆"),
            (50, "🎬 Gurú del entretenimiento", "Añadir 50 películas o series", "👑")
        ]
        
        for count, title, desc, icon in media_achievements:
            achievements.append((
                f"media_add_{count}",
                title,
                desc,
                icon,
                count
            ))
        
        # Logros de viajes
        travel_achievements = [
            (1, "✈️ Primer destino", "Añadir vuestro primer viaje", "🧳"),
            (3, "✈️ Viajeros frecuentes", "Añadir 3 viajes", "🧳"),
            (5, "✈️ Trotamundos", "Añadir 5 viajes", "🌎"),
            (10, "✈️ Exploradores", "Añadir 10 viajes", "🗺️"),
            (15, "✈️ Nómadas digitales", "Añadir 15 viajes", "🌟")
        ]
        
        for count, title, desc, icon in travel_achievements:
            achievements.append((
                f"travel_add_{count}",
                title,
                desc,
                icon,
                count
            ))
        
        # Logros de canjes de premios
        redeem_achievements = [
            (1, "🛍️ Primer canje", "Canjear tu primer premio", "🎁"),
            (3, "🛍️ Comprador habitual", "Canjear 3 premios", "🛒"),
            (5, "🛍️ Amante de las recompensas", "Canjear 5 premios", "💎"),
            (10, "🛍️ Coleccionista", "Canjear 10 premios", "🏅"),
            (15, "🛍️ Rey/Reina de la tienda", "Canjear 15 premios", "👑")
        ]
        
        for count, title, desc, icon in redeem_achievements:
            achievements.append((
                f"redeem_{count}",
                title,
                desc,
                icon,
                count
            ))
        
        # Logros especiales adicionales
        special_achievements = [
            ("perfect_week", "🔥 Semana perfecta", "Responder todas las preguntas de una semana", "⭐", 7),
            ("monthly_champion", "🏆 Campeón del mes", "Ser el primero en responder más veces en un mes", "🏅", 1),
            ("early_bird", "🐦 Madrugador", "Ser el primero en responder 5 días seguidos antes del mediodía", "🌅", 5),
            ("night_owl", "🦉 Noctámbulo", "Responder después de las 10 PM 5 veces", "🌙", 5),
            ("creative_writer", "✍️ Escritor creativo", "Escribir respuestas de más de 100 caracteres 10 veces", "📝", 10),
            ("quick_thinker", "⚡ Pensador rápido", "Responder en menos de 2 minutos después de publicada la pregunta", "⏱️", 3)
        ]
        
        achievements.extend(special_achievements)
        
        # Insertar todos los logros
        for code, title, desc, icon, goal in achievements:
            c.execute(
                "INSERT INTO achievements (code, title, description, icon, goal) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (code) DO UPDATE SET title=EXCLUDED.title, description=EXCLUDED.description, icon=EXCLUDED.icon, goal=EXCLUDED.goal",
                (code, title, desc, icon, goal)
            )
        
        # ÍTEMS VACÍOS - Sin premios por defecto
        items = []
        
        for name, cost, desc, icon in items:
            c.execute(
                "INSERT INTO shop_items (name, cost, description, icon) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (name) DO UPDATE SET cost=EXCLUDED.cost, description=EXCLUDED.description, icon=EXCLUDED.icon",
                (name, cost, desc, icon)
            )
        conn.commit()


def check_media_achievements(username):
    """Verifica logros relacionados con añadir películas/series"""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT COUNT(*) FROM media_items WHERE created_by=%s", (username,))
            media_count = c.fetchone()[0] or 0
            
            # Verificar cada nivel de logro
            levels = [1, 5, 10, 20, 50]
            for level in levels:
                if media_count >= level:
                    _grant_achievement_by_code(username, f"media_add_{level}")
    finally:
        conn.close()

def check_travel_achievements(username):
    """Verifica logros relacionados con añadir viajes"""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT COUNT(*) FROM travels WHERE created_by=%s", (username,))
            travel_count = c.fetchone()[0] or 0
            
            # Verificar cada nivel de logro
            levels = [1, 3, 5, 10, 15]
            for level in levels:
                if travel_count >= level:
                    _grant_achievement_by_code(username, f"travel_add_{level}")
    finally:
        conn.close()

def check_redeem_achievements(username):
    """Verifica logros relacionados con canjear premios"""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT COUNT(*) FROM purchases p 
                JOIN users u ON u.id = p.user_id 
                WHERE u.username=%s
            """, (username,))
            redeem_count = c.fetchone()[0] or 0
            
            # Verificar cada nivel de logro
            levels = [1, 3, 5, 10, 15]
            for level in levels:
                if redeem_count >= level:
                    _grant_achievement_by_code(username, f"redeem_{level}")
    finally:
        conn.close()

def _grant_achievement_by_code(username, achievement_code):
    """Función helper para conceder logros por código"""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT id FROM achievements WHERE code=%s", (achievement_code,))
            row = c.fetchone()
            if row:
                achievement_id = row[0]
                _grant_achievement_to(username, achievement_id)
    finally:
        conn.close()

# --- Gamificación: bootstrap (seguro/idempotente, Flask 3.x) ---
try:
    _ensure_gamification_schema()
except Exception as e:
    app.logger.warning("gamification bootstrap: %s", e)



def _get_user_id(conn, username):
    with conn.cursor() as c:
        c.execute("SELECT id FROM users WHERE username=%s", (username,))
        row = c.fetchone()
        return row[0] if row else None

def _calc_streak(conn, username):
    with conn.cursor() as c:
        c.execute("""
            SELECT dq.date
            FROM daily_questions dq
            JOIN answers a ON a.question_id = dq.id
            WHERE a.username=%s
            ORDER BY dq.date DESC
        """, (username,))
        rows = [r[0] for r in c.fetchall()]
    if not rows:
        return 0
    unique_days = []
    seen = set()
    for d in rows:
        if d not in seen:
            unique_days.append(d)
            seen.add(d)
    try:
        today = date.today().isoformat()
    except Exception:
        return 0
    cur = date.fromisoformat(today)
    streak = 0
    for d in unique_days:
        try:
            dd = date.fromisoformat(d)
        except Exception:
            continue
        if dd == cur:
            streak += 1
            cur = cur - timedelta(days=1)
        else:
            break
    return streak




@app.route("/api/medallas/summary")
def api_medallas_summary():
    user = session.get("username")
    with closing(get_db_connection()) as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
        points = 0
        if user:
            c.execute("SELECT COALESCE(points,0) FROM users WHERE username=%s", (user,))
            row = c.fetchone()
            points = row[0] if row else 0
        c.execute("SELECT COUNT(*) FROM daily_questions")
        total_days = c.fetchone()[0] or 0
        streak = _calc_streak(conn, user) if user else 0
        first_count = 0
        if user:
            c.execute("""
                WITH firsts AS (
                    SELECT a.question_id, MIN(a.created_at) AS first_time
                    FROM answers a
                    GROUP BY a.question_id
                )
                SELECT COUNT(*) FROM answers a
                JOIN firsts f ON f.question_id=a.question_id AND a.created_at=f.first_time
                WHERE a.username=%s
            """, (user,))
            first_count = c.fetchone()[0] or 0
        c.execute("""
            SELECT a.code, a.title, a.description, a.icon,
                   CASE WHEN ua.id IS NULL THEN false ELSE true END AS earned,
                   a.goal
            FROM achievements a
            LEFT JOIN user_achievements ua 
              ON ua.achievement_id=a.id
             AND ua.user_id = (SELECT id FROM users WHERE username=%s LIMIT 1)
            ORDER BY a.id
        """, (user or '',))
        achs = [dict(r) for r in c.fetchall()]
        c.execute("""
            SELECT username, COALESCE(points,0) AS points
            FROM users
            ORDER BY points DESC, username ASC
            LIMIT 10
        """)
        ranking = [dict(r) for r in c.fetchall()]
        c.execute("SELECT id, name, cost, description, icon FROM shop_items ORDER BY cost ASC")
        shop = [dict(r) for r in c.fetchall()]

        # --- Logros conseguidos por usuario (solo vosotros dos) ---
        c.execute("""
            SELECT u.username,
                   a.title,
                   a.icon,
                   ua.earned_at
            FROM user_achievements ua
            JOIN users u ON u.id = ua.user_id
            JOIN achievements a ON a.id = ua.achievement_id
            WHERE u.username IN ('mochito','mochita')
            ORDER BY ua.earned_at DESC, a.id
        """)
        earned_by_user = {}
        for row in c.fetchall():
            uname = row["username"]
            earned_by_user.setdefault(uname, []).append({
                "title": row["title"],
                "icon": row["icon"],
                "earned_at": row["earned_at"],
            })

        # --- Historial de puntos ---
        c.execute("""
            SELECT u.username,
                   ph.delta,
                   ph.source,
                   ph.note,
                   ph.created_at
            FROM points_history ph
            JOIN users u ON u.id = ph.user_id
            WHERE u.username IN ('mochito','mochita')
            ORDER BY ph.created_at DESC, ph.id DESC
            LIMIT 80
        """)
        points_history = [
            {
                "username": row["username"],
                "delta": int(row["delta"]),
                "source": row["source"],
                "note": row["note"],
                "created_at": row["created_at"],
            }
            for row in c.fetchall()
        ]

    return jsonify({
        "user": user,
        "points": points,
        "total_days": total_days,
        "streak": streak,
        "first_count": first_count,
        "achievements": achs,
        "ranking": ranking,
        "shop": shop,
        "earned_by_user": earned_by_user,
        "points_history": points_history,
    })

from flask import request, redirect, flash, render_template, session
# Si usas psycopg2:
try:
    from psycopg2.errors import ForeignKeyViolation as PG_FK_VIOLATION
except Exception:
    PG_FK_VIOLATION = tuple()
# Si usas PyMySQL:
try:
    from pymysql.err import IntegrityError as MY_INTEGRITY_ERROR
except Exception:
    MY_INTEGRITY_ERROR = tuple()



@app.route('/tienda', methods=['GET', 'POST'])
def shop():
    if 'username' not in session:
        return redirect('/')

    user = session['username']
    is_admin = (user == 'mochito')

    try:
        _ensure_gamification_schema()
    except Exception as e:
        app.logger.warning("gamification schema ensure failed: %s", e)

    if request.method == 'POST':
        op = (request.form.get('_op') or '').strip()

        # === TOGGLE ADMIN VIEW (solo mochito) ===
        if op == 'toggle_admin':
            if not is_admin:
                flash("Solo mochito puede usar la vista admin.", "error")
                return redirect('/tienda')
            current = bool(session.get('shop_admin_view', True))
            session['shop_admin_view'] = not current
            flash(("Vista admin activada ✅" if not current else "Vista admin desactivada 👀"), "success")
            try:
                send_discord("Shop admin view toggled", {"by": user, "enabled": not current})
            except Exception:
                pass
            return redirect('/tienda')

        # === ADMIN: sumar/restar puntos a un usuario ===
        if op == 'grant_points':
            if not is_admin:
                flash("Solo el admin puede modificar puntos.", "error")
                return redirect('/tienda')

            to_user = (request.form.get('to_user') or '').strip()
            action = (request.form.get('action') or 'add').strip()
            delta = (request.form.get('points') or '').strip()
            note = (request.form.get('note') or '').strip()

            if to_user not in ('mochito', 'mochita'):
                flash("Usuario destino inválido.", "error")
                return redirect('/tienda')

            try:
                d = int(delta)
                if d <= 0:
                    raise ValueError()
            except Exception:
                flash("Cantidad inválida. Usa un entero positivo.", "error")
                return redirect('/tienda')

            # Aplicar signo según la acción
            if action == 'subtract':
                d = -d

            conn = get_db_connection()
            try:
                with conn.cursor() as c:
                    # ID del usuario destino
                    c.execute("SELECT id FROM users WHERE username=%s", (to_user,))
                    row_u = c.fetchone()
                    if not row_u:
                        flash("Usuario destino no existe.", "error")
                        return redirect('/tienda')
                    uid = row_u[0]

                    # Actualizar puntos
                    c.execute(
                        "UPDATE users SET points = COALESCE(points,0) + %s WHERE username=%s",
                        (d, to_user)
                    )

                    # ✅ Historial de puntos (admin)
                    c.execute("""
                        INSERT INTO points_history (user_id, delta, source, note, created_at)
                        VALUES (%s,%s,%s,%s,%s)
                    """, (
                        uid,
                        d,
                        "admin",
                        (note or "Ajuste manual de puntos"),
                        now_madrid_str()
                    ))

                    conn.commit()

                # 🔔 NOTIFICACIÓN PUSH POR MODIFICACIÓN MANUAL DE PUNTOS
                try:
                    if d > 0:
                        send_push_to(
                            to_user,
                            title="⭐ ¡Puntos añadidos!",
                            body=f"Se te han añadido {d} puntos. {note or ''}"
                        )
                    else:
                        send_push_to(
                            to_user,
                            title="📉 Puntos restados",
                            body=f"Se te han restado {abs(d)} puntos. {note or ''}"
                        )
                except Exception as e:
                    print("[push points modification] ", e)

                action_text = "añadieron" if action == 'add' else "restaron"
                flash(f"Se {action_text} {abs(d)} pts a {to_user} ✅", "success")
                try:
                    send_discord("Points modified", {"by": user, "to": to_user, "points": d, "note": note})
                except Exception:
                    pass
            finally:
                conn.close()
            return redirect('/tienda')

        # === Resto de acciones (redeem / CRUD items) ===
        conn = get_db_connection()
        try:
            # También en la función de canje en la tienda, añadir notificación
            if op == 'redeem':
                item_id = request.form.get('item_id')
                if not item_id:
                    flash("Falta item.", "error")
                    return redirect('/tienda')

                with conn.cursor() as c:
                    c.execute("SELECT id, name, cost FROM shop_items WHERE id=%s", (item_id,))
                    row = c.fetchone()
                    if not row:
                        flash("Premio no encontrado.", "error")
                        return redirect('/tienda')

                    it_id, it_name, it_cost = row['id'], row['name'], int(row['cost'])

                    c.execute("SELECT id, COALESCE(points,0) AS pts FROM users WHERE username=%s FOR UPDATE", (user,))
                    urow = c.fetchone()
                    if not urow:
                        flash("Usuario no encontrado.", "error")
                        return redirect('/tienda')

                    uid, points = int(urow['id']), int(urow['pts'])
                    if points < it_cost:
                        flash("No tienes puntos suficientes 😅", "error")
                        return redirect('/tienda')

                    c.execute("UPDATE users SET points = points - %s WHERE id=%s", (it_cost, uid))
                    c.execute("""
                        INSERT INTO purchases (user_id, item_id, quantity, note, purchased_at)
                        VALUES (%s,%s,1,%s,%s)
                    """, (uid, it_id, f"Canje de {it_name}", now_madrid_str()))

                    # ✅ Historial de puntos (tienda)
                    c.execute("""
                        INSERT INTO points_history (user_id, delta, source, note, created_at)
                        VALUES (%s,%s,%s,%s,%s)
                    """, (
                        uid,
                        -it_cost,
                        "shop",
                        f"Canje de {it_name}",
                        now_madrid_str()
                    ))

                    conn.commit()

                    # 🔔 NOTIFICACIÓN PUSH POR CANJE (AL USUARIO QUE CANJEA)
                    try:
                        send_push_to(
                            user,
                            title="🎁 ¡Premio canjeado!",
                            body=f"Has canjeado '{it_name}' por {it_cost} puntos"
                        )
                    except Exception as e:
                        print("[push redeem] ", e)

                    # 🔔 NOTIFICACIÓN PUSH AL OTRO USUARIO
                    other_user = 'mochita' if user == 'mochito' else 'mochito'
                    try:
                        send_push_to(
                            other_user,
                            title="🎁 ¡Se ha canjeado un premio!",
                            body=f"{user} ha canjeado '{it_name}' por {it_cost} puntos",
                            url="/tienda",
                            tag=f"shop-redeem-{it_id}"
                        )
                    except Exception as e:
                        print("[push redeem other] ", e)

                flash(f"¡Canjeado! 🎉 Disfruta tu premio: {it_name}", "success")
                try:
                    send_discord("Shop redeem", {"by": user, "item_id": int(item_id), "item_name": it_name, "cost": it_cost})
                except Exception:
                    pass
                return redirect('/tienda')

            # Solo admin: crear/editar/borrar
            if not is_admin:
                flash("Solo el admin puede modificar la tienda.", "error")
                return redirect('/tienda')

            if op == 'create_item':
                name = (request.form.get('name') or '').strip()
                cost = (request.form.get('cost') or '').strip()
                desc = (request.form.get('description') or '').strip()
                icon = (request.form.get('icon') or '').strip() or '🎁'
                try:
                    icost = int(cost)
                    if icost < 1:
                        raise ValueError()
                except Exception:
                    flash("Coste inválido.", "error")
                    return redirect('/tienda')
                with conn.cursor() as c:
                    c.execute("""INSERT INTO shop_items (name, cost, description, icon)
                                 VALUES (%s,%s,%s,%s)""", (name, icost, desc, icon))
                    conn.commit()
                    check_redeem_achievements(user)

                flash("Premio añadido ✅", "success")
                try:
                    send_discord("Shop item created", {"by": user, "name": name, "cost": icost})
                except Exception:
                    pass
                return redirect('/tienda')

            if op == 'update_item':
                item_id = request.form.get('item_id')
                name = (request.form.get('name') or '').strip()
                cost = (request.form.get('cost') or '').strip()
                desc = (request.form.get('description') or '').strip()
                icon = (request.form.get('icon') or '').strip() or '🎁'
                try:
                    icost = int(cost)
                    iid = int(item_id)
                    if icost < 1:
                        raise ValueError()
                except Exception:
                    flash("Coste/ID inválido.", "error")
                    return redirect('/tienda')

                with conn.cursor() as c:
                    c.execute("""UPDATE shop_items
                                   SET name=%s, cost=%s, description=%s, icon=%s
                                 WHERE id=%s""", (name, icost, desc, icon, iid))
                    if c.rowcount == 0:
                        conn.rollback()
                        flash("Premio no encontrado.", "error")
                        return redirect('/tienda')
                    conn.commit()
                flash("Premio actualizado ✏️", "success")
                try:
                    send_discord("Shop item updated", {"by": user, "id": int(item_id)})
                except Exception:
                    pass
                return redirect('/tienda')

            if op == 'delete_item':
                item_id = request.form.get('item_id')
                try:
                    iid = int(item_id)
                except Exception:
                    flash("ID inválido.", "error")
                    return redirect('/tienda')
                
                conn = get_db_connection()
                try:
                    with conn.cursor() as c:
                        # Primero verificar si hay compras asociadas
                        c.execute("SELECT COUNT(*) FROM purchases WHERE item_id=%s", (iid,))
                        purchase_count = c.fetchone()[0] or 0
                        
                        if purchase_count > 0:
                            flash("No se puede borrar: ya existen canjes asociados a este premio. Primero borra los canjes.", "error")
                            return redirect('/tienda')
                        
                        # Si no hay compras, proceder con el borrado
                        c.execute("DELETE FROM shop_items WHERE id=%s", (iid,))
                        if c.rowcount == 0:
                            flash("Premio no encontrado.", "error")
                            return redirect('/tienda')
                        
                        conn.commit()
                        flash("Premio borrado 🗑️", "info")
                        try:
                            send_discord("Shop item deleted", {"by": user, "id": iid})
                        except Exception:
                            pass
                            
                except Exception as e:
                    try: 
                        conn.rollback()
                    except Exception: 
                        pass
                    app.logger.exception("[/tienda] delete_item error")
                    flash("Error en la tienda.", "error")
                finally:
                    conn.close()
                
                return redirect('/tienda')

            flash("Acción desconocida.", "error")
            return redirect('/tienda')

        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            app.logger.exception("[/tienda] error")
            flash("Error en la tienda.", "error")
            return redirect('/tienda')
        finally:
            conn.close()

    # === GET === 
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            # Puntos del usuario actual
            c.execute("SELECT COALESCE(points,0) FROM users WHERE username=%s", (user,))
            points = int((c.fetchone() or [0])[0] or 0)

            # Items de la tienda
            c.execute("SELECT id, name, cost, description, icon FROM shop_items ORDER BY cost ASC, name ASC")
            items = c.fetchall()

            # Puntos de ambos usuarios
            c.execute("SELECT username, COALESCE(points,0) AS p FROM users WHERE username IN ('mochito','mochita')")
            rows = c.fetchall()
            points_map = {r['username']: int(r['p']) for r in rows}

            # 🔥 NUEVO: Obtener historial de canjes
            c.execute("""
                SELECT u.username, si.name AS item_name, si.cost, si.icon, p.purchased_at, p.note
                FROM purchases p
                JOIN users u ON p.user_id = u.id
                JOIN shop_items si ON p.item_id = si.id
                ORDER BY p.purchased_at DESC
                LIMIT 50
            """)
            purchases = c.fetchall()

            # Organizar historial por usuario
            redemption_history = {'mochito': [], 'mochita': []}
            for p in purchases:
                username = p['username']
                if username in redemption_history:
                    redemption_history[username].append({
                        'item_name': p['item_name'],
                        'cost': p['cost'],
                        'icon': p['icon'],
                        'ts': p['purchased_at'],
                        'note': p['note']
                    })

    finally:
        conn.close()

    show_admin = is_admin and bool(session.get('shop_admin_view', True))
    return render_template('tienda.html',
                           points=points,
                           items=items,
                           is_admin=is_admin,
                           show_admin=show_admin,
                           points_map=points_map,
                           redemption_history=redemption_history)  # 🔥 Pasar el historial a la plantilla

def other_of(user: str) -> str:
    """Devuelve el nombre del otro usuario"""
    if user == 'mochito':
        return 'mochita'
    if user == 'mochita':
        return 'mochito'
    return 'mochita'  # fallback


@app.route("/medallas", methods=["GET", "POST"])
def medallas():
    # Solo usuarios logueados
    if "username" not in session:
        return redirect("/")

    user = session["username"]
    is_admin = (user == "mochito")

    # Por si acaso, asegurar esquema de gamificación
    try:
        _ensure_gamification_schema()
    except Exception as e:
        app.logger.warning("gamification schema ensure failed (medallas): %s", e)

    # --- POST: acciones admin ---
    if request.method == "POST":
        op = (request.form.get("_op") or "").strip()

        # 1) Toggle vista admin (igual que en /tienda)
        if op == "toggle_admin":
            if not is_admin:
                flash("Solo mochito puede usar la vista admin.", "error")
                return redirect("/medallas")
            current = bool(session.get("medallas_admin_view", False))
            session["medallas_admin_view"] = not current
            flash(
                "Vista admin activada ✅" if not current else "Vista admin desactivada 👀",
                "success"
            )
            return redirect("/medallas")

        # 2) Guardar puntos de cada medalla
        if op == "update_points":
            if not is_admin:
                flash("Solo el admin puede modificar las medallas.", "error")
                return redirect("/medallas")

            conn = get_db_connection()
            try:
                with conn.cursor() as c:
                    # Coger todos los IDs de achievements
                    c.execute("SELECT id FROM achievements ORDER BY id")
                    all_ids = [row[0] for row in c.fetchall()]

                    for ach_id in all_ids:
                        field_name = f"points_{ach_id}"
                        raw = (request.form.get(field_name) or "").strip()
                        if raw == "":
                            pts = 0
                        else:
                            try:
                                pts = int(raw)
                            except ValueError:
                                pts = 0
                        c.execute(
                            "UPDATE achievements SET points_on_award=%s WHERE id=%s",
                            (pts, ach_id),
                        )
                    conn.commit()
                flash("Puntos de medallas actualizados ✅", "success")
            except Exception as e:
                app.logger.exception("[/medallas] update_points error: %s", e)
                try:
                    conn.rollback()
                except Exception:
                    pass
                flash("No se pudieron guardar los puntos.", "error")
            finally:
                conn.close()

            return redirect("/medallas")

        # 3) Otorgar medalla manualmente
        if op == "grant_manual":
            if not is_admin:
                flash("Solo el admin puede otorgar medallas.", "error")
                return redirect("/medallas")

            to_user = (request.form.get("to_user") or "").strip()
            ach_raw = (request.form.get("achievement_id") or "").strip()

            if not to_user or not ach_raw:
                flash("Faltan datos para otorgar la medalla.", "error")
                return redirect("/medallas")

            try:
                ach_id = int(ach_raw)
            except ValueError:
                flash("ID de medalla inválido.", "error")
                return redirect("/medallas")

            try:
                # Usa el helper global que ya tienes definido
                _grant_achievement_to(to_user, ach_id)
                flash(f"Medalla #{ach_id} otorgada a {to_user} ✅", "success")
            except Exception as e:
                app.logger.exception("[/medallas] grant_manual error: %s", e)
                flash("No se pudo otorgar la medalla.", "error")

            return redirect("/medallas")

        # Si llega aquí, op desconocida
        flash("Acción desconocida en medallas.", "error")
        return redirect("/medallas")

    # --- GET: pintar página ---
        # --- GET: pintar página ---
    show_admin = is_admin and bool(session.get("medallas_admin_view", False))

    # Datos para el panel admin (lista de medallas + usuarios)
    ach_admin = []
    admin_users = []
    if is_admin:
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                c.execute("""
                    SELECT id, code, title, description, points_on_award
                    FROM achievements
                    ORDER BY id
                """)
                ach_admin = c.fetchall()

                # Lista de usuarios para el selector
                c.execute("""
                    SELECT username, COALESCE(points,0) AS points
                    FROM users
                    ORDER BY username
                """)
                admin_users = c.fetchall()
        finally:
            conn.close()

    return render_template(
        "medallas.html",
        user=user,
        is_admin=is_admin,
        show_admin=show_admin,
        ach_admin=ach_admin,
        admin_users=admin_users,
    )





@app.post("/admin/achievements/add")
def admin_achievements_add():
    require_admin()
    f = request.form
    code = (f.get("code") or "").strip() or None
    title = (f.get("title") or "").strip()
    description = (f.get("description") or "").strip() or None
    icon = (f.get("icon") or "").strip() or None
    trigger_kind = (f.get("trigger_kind") or "none").strip()
    trigger_value = f.get("trigger_value")
    points = int((f.get("points_on_award") or "0") or 0)
    grant_both = bool(f.get("grant_both"))
    active = bool(f.get("active"))

    tval = int(trigger_value) if (trigger_value and str(trigger_value).isdigit()) else None
    if not title:
        flash("Falta título.", "error"); return redirect("/admin/achievements")
    if trigger_kind not in ("rel_days","manual","none"):
        trigger_kind = "none"

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
              INSERT INTO achievements (code, title, description, icon,
                                        trigger_kind, trigger_value,
                                        points_on_award, grant_both, active)
              VALUES (COALESCE(%s, md5(%s||now()::text)),
                      %s,%s,%s,%s,%s,%s,%s,%s)
            """, (code, code or title,
                  title, description, icon,
                  trigger_kind, tval, points, grant_both, active))
            conn.commit()
        flash("Premio creado ✅", "success")
    finally:
        conn.close()
    return redirect("/admin/achievements")


@app.post("/admin/achievements/<int:aid>/edit")
def admin_achievements_edit(aid):
    require_admin()
    f = request.form
    title = (f.get("title") or "").strip()
    description = (f.get("description") or "").strip() or None
    icon = (f.get("icon") or "").strip() or None
    trigger_kind = (f.get("trigger_kind") or "none").strip()
    trigger_value = f.get("trigger_value")
    points = int((f.get("points_on_award") or "0") or 0)
    grant_both = bool(f.get("grant_both"))
    active = bool(f.get("active"))

    tval = int(trigger_value) if (trigger_value and str(trigger_value).isdigit()) else None
    if trigger_kind not in ("rel_days","manual","none"):
        trigger_kind = "none"

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
              UPDATE achievements
              SET title=%s, description=%s, icon=%s,
                  trigger_kind=%s, trigger_value=%s,
                  points_on_award=%s, grant_both=%s, active=%s
              WHERE id=%s
            """, (title, description, icon,
                  trigger_kind, tval, points, grant_both, active, aid))
            conn.commit()
        flash("Premio actualizado ✏️", "success")
    finally:
        conn.close()
    return redirect("/admin/achievements")


@app.post("/admin/achievements/<int:aid>/grant_now")
def admin_achievements_grant_now(aid):
    """Botón admin para conceder manualmente (a ambos por comodidad)."""
    require_admin()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT id, points_on_award FROM achievements WHERE id=%s", (aid,))
            row = c.fetchone()
        if not row:
            flash("Premio no encontrado.", "error"); return redirect("/admin/achievements")
        pts = int(row['points_on_award'] or 0)
        for u in ('mochito','mochita'):
            _grant_achievement_to(u, aid, pts)
        flash("Premio concedido a ambos ✅", "success")
    finally:
        conn.close()
    return redirect("/admin/achievements")


@app.post("/admin/achievements/check_now")
def admin_achievements_check_now():
    """Forzar evaluación de hitos relación ahora mismo (para pruebas)."""
    require_admin()
    try:
        check_relationship_milestones()
        flash("Evaluación de hitos ejecutada ✅", "success")
    except Exception as e:
        print("[admin check milestones]", e)
        flash("Error al evaluar.", "error")
    return redirect("/admin/achievements")

@app.get("/api/points")
def api_points():
    if 'username' not in session:
        return jsonify({"ok": False, "error": "unauthenticated"}), 401
    u = (request.args.get("user") or session['username']).strip()
    # solo permitimos a mochito/mochita por seguridad
    if u not in ("mochito", "mochita"):
        return jsonify({"ok": False, "error": "bad_user"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT COALESCE(points,0) FROM users WHERE username=%s", (u,))
            row = c.fetchone()
            pts = int((row or [0])[0] or 0)
    finally:
        conn.close()
    return jsonify({"ok": True, "user": u, "points": pts})

@app.route('/debug/points')
def debug_points():
    if 'username' not in session:
        return redirect('/')
    
    user = session['username']
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT points FROM users WHERE username=%s", (user,))
            points = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM answers WHERE username=%s", (user,))
            answer_count = c.fetchone()[0]
            
        return jsonify({
            'user': user,
            'points': points,
            'total_answers': answer_count
        })
    finally:
        conn.close()

@app.route('/historial')
def historial():
    if 'username' not in session:
        return redirect('/')
    
    # Obtener parámetros de paginación
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            # Obtener total de preguntas para paginación
            c.execute("SELECT COUNT(*) FROM daily_questions")
            total_questions = c.fetchone()[0]
            
            # Calcular offset
            offset = (page - 1) * per_page
            
            # Obtener preguntas con respuestas (más recientes primero)
            c.execute("""
                SELECT dq.id, dq.date, dq.question,
                       a_mochito.answer as mochito_answer, 
                       a_mochito.created_at as mochito_created,
                       a_mochito.updated_at as mochito_updated,
                       a_mochita.answer as mochita_answer,
                       a_mochita.created_at as mochita_created,
                       a_mochita.updated_at as mochita_updated
                FROM daily_questions dq
                LEFT JOIN answers a_mochito ON dq.id = a_mochito.question_id AND a_mochito.username = 'mochito'
                LEFT JOIN answers a_mochita ON dq.id = a_mochita.question_id AND a_mochita.username = 'mochita'
                ORDER BY dq.date DESC
                LIMIT %s OFFSET %s
            """, (per_page, offset))
            
            questions = []
            for row in c.fetchall():
                questions.append({
                    'id': row['id'],
                    'date': row['date'],
                    'question': row['question'],
                    'mochito_answer': row['mochito_answer'],
                    'mochita_answer': row['mochita_answer'],
                    'mochito_created': row['mochito_created'],
                    'mochito_updated': row['mochito_updated'],
                    'mochita_created': row['mochita_created'],
                    'mochita_updated': row['mochita_updated']
                })
            
            # Calcular páginas
            total_pages = (total_questions + per_page - 1) // per_page
            
    finally:
        conn.close()
    
    # Pasar today_madrid al template
    return render_template('historial.html',
                            questions=questions,
                            current_page=page,
                            total_pages=total_pages,
                            username=session['username'],
                            today_iso=today_madrid().isoformat())  # <-- Cambiar a today_iso


@app.get("/api/couple_mode")
def api_get_couple_mode():
    if "username" not in session:
        return jsonify(ok=False, error="unauthorized"), 401

    mode = state_get("couple_mode", "") or "separated"
    return jsonify(ok=True, mode=mode)


@app.post("/api/couple_mode")
def api_set_couple_mode():
    if "username" not in session:
        return jsonify(ok=False, error="unauthorized"), 401

    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "").strip()

    allowed = {"separated", "together_alg", "together_cor"}
    if mode not in allowed:
        return jsonify(ok=False, error="bad_mode"), 400

    # Guardamos en app_state (tabla ya creada en init_db)
    state_set("couple_mode", mode)

    # Aviso por SSE para que el otro lado se refresque
    try:
        broadcast("update", {
            "kind": "couple_mode",
            "mode": mode,
            "by": session.get("username") or "unknown"
        })
    except Exception:
        pass

    return jsonify(ok=True, mode=mode)

@app.post("/api/daily-wheel-spin")
def api_daily_wheel_spin():
    if "username" not in session:
        return jsonify(ok=False, error="unauthorized"), 401

    user = session["username"]

    # Por si acaso, asegurar esquema de gamificación
    try:
        _ensure_gamification_schema()
    except Exception:
        pass

    now_md = europe_madrid_now()
    wheel_times = build_wheel_time_payload(now_md)
    today = wheel_times["today"]

    other_user = 'mochita' if user == 'mochito' else 'mochito'

    state_key = f"wheel_spin::{user}::{today}"
    other_key = f"wheel_spin::{other_user}::{today}"

    # --- Info de la otra persona (si ya ha girado hoy) ---
    raw_other = state_get(other_key, "")
    if raw_other:
        try:
            raw_dict = json.loads(raw_other)
        except Exception:
            raw_dict = {}
        other_info = {
            "username": other_user,
            "has_spun": True,
            "delta": int(raw_dict.get("delta", 0)),
            "label": raw_dict.get("label"),
            "date": raw_dict.get("date", today),
            "ts": raw_dict.get("ts"),
        }
    else:
        other_info = {
            "username": other_user,
            "has_spun": False,
        }

    # --- 1) ¿Ya ha girado hoy? -> devolver lo guardado ---
    raw = state_get(state_key, "")
    if raw:
        try:
            info = json.loads(raw)
        except Exception:
            info = {}
        info.setdefault("already", True)
        info.setdefault("date", today)
        return jsonify(ok=True, other=other_info, **info, **wheel_times)

    # --- 1-bis) ¿Aún no es la hora de desbloqueo? ---
    if not wheel_times.get("can_spin_now"):
        msg = "Aún no puedes girar la ruleta, espera un poquito 💫"
        return jsonify(ok=False, error=msg, **wheel_times), 400

    # --- 2) Elegir premio de la ruleta (idx y delta coherentes) ---
    idx, seg = choose_daily_wheel_segment()
    delta = int(seg.get("delta", 0))
    label = seg.get("label", f"+{delta}")

    conn = get_db_connection()
    new_points = None
    now_str = now_madrid_str()

    try:
        with conn.cursor() as c:
            c.execute(
                "SELECT id, COALESCE(points,0) FROM users WHERE username=%s",
                (user,)
            )
            row = c.fetchone()
            if not row:
                return jsonify(ok=False, error="no_user"), 400

            uid = int(row[0])
            current_points = int(row[1])
            new_points = current_points + delta

            # Actualizar puntos + historial solo si delta != 0
            if delta != 0:
                c.execute(
                    "UPDATE users SET points=%s WHERE id=%s",
                    (new_points, uid)
                )
                c.execute(
                    """
                    INSERT INTO points_history (user_id, delta, source, note, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (uid, delta, "wheel", "Ruleta diaria", now_str)
                )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        app.logger.exception("[/api/daily-wheel-spin] error")
        return jsonify(ok=False, error="server_error"), 500
    finally:
        conn.close()

    # Paquete con la info del giro de hoy (lo que ve el frontend)
    info = {
        "already": False,
        "idx": idx,
        "delta": delta,
        "label": label,
        "date": today,
        "new_total": new_points,
        "ts": now_str,
    }

    # 3) Guardar en app_state para bloquear más giros hoy
    try:
        state_set(state_key, json.dumps(info))
    except Exception:
        pass

    # 4) Notificación push SOLO para la otra persona (inmediata)
    try:
        msg_user = "Mochito" if user == "mochito" else "Mochita"
        if delta > 0:
            send_push_to(
                other_user,
                title="🎡 Ruleta diaria de " + msg_user,
                body=f"{msg_user} ha girado la ruleta y ha ganado {delta} puntos",
                url="/"
            )
        else:
            send_push_to(
                other_user,
                title="🎡 Ruleta diaria de " + msg_user,
                body=f"{msg_user} ha girado la ruleta, pero hoy no han caído puntos 😢",
                url="/"
            )
    except Exception:
        pass

    # 5) Devolver también lo que ha hecho la otra persona hoy + tiempos
    return jsonify(ok=True, other=other_info, **info, **wheel_times)


@app.post("/api/daily-wheel-push-self")
def api_daily_wheel_push_self():
    """
    Endpoint para lanzar la notificación al propio usuario DESPUÉS de la animación.
    El frontend lo llama cuando termina el giro.
    """
    if "username" not in session:
        return jsonify(ok=False, error="unauthorized"), 401

    user = session["username"]
    today = today_madrid().isoformat()
    state_key = f"wheel_spin::{user}::{today}"

    raw = state_get(state_key, "")
    if not raw:
        return jsonify(ok=False, error="no_spin_today"), 400

    try:
        info = json.loads(raw)
    except Exception:
        return jsonify(ok=False, error="bad_state"), 500

    delta = int(info.get("delta", 0))

    try:
        if delta > 0:
            send_push_to(
                user,
                title="🎡 Ruleta diaria",
                body=f"Has ganado {delta} puntos en la ruleta 🎁",
                url="/"
            )
        else:
            send_push_to(
                user,
                title="🎡 Ruleta diaria",
                body="Hoy la ruleta no ha dado puntos… ¡mañana más suerte!",
                url="/"
            )
    except Exception:
        # Si falla la push, no rompemos la web
        pass

    return jsonify(ok=True)


@app.get("/api/daily-wheel-status")
def api_daily_wheel_status():
    """
    Estado de la ruleta para el usuario logueado y su pareja.

    - Lee de app_state con las claves:
        wheel_spin::<username>::YYYY-MM-DD
    - Devuelve:
        me        -> info de mi giro (o has_spun=False)
        other     -> info del giro de la pareja (o has_spun=False)
        can_spin_now   -> True solo si YO no he girado hoy
        next_reset_iso -> medianoche de hoy en horario Madrid
    """
    if "username" not in session:
        return jsonify(ok=False, error="unauthorized"), 401

    user = session["username"]

    # Hoy en horario Madrid (date -> 'YYYY-MM-DD')
    today = today_madrid().isoformat()

    other_user = "mochita" if user == "mochito" else "mochito"

    me_key    = f"wheel_spin::{user}::{today}"
    other_key = f"wheel_spin::{other_user}::{today}"

    def load_state(key: str, default_date: str):
        """
        Lee una entrada JSON desde app_state y la convierte
        en el diccionario que usa el frontend.
        """
        raw = state_get(key, "")
        if not raw:
            return {"has_spun": False}
        try:
            info = json.loads(raw)
        except Exception:
            return {"has_spun": False}

        return {
            "has_spun": True,
            "idx": int(info.get("idx", 0)),
            "delta": int(info.get("delta", 0)),
            "label": info.get("label"),
            "date": info.get("date", default_date),
            "ts": info.get("ts"),
        }

    # Yo
    me_info = load_state(me_key, today)

    # Mi pareja
    other_info = load_state(other_key, today)
    other_info["username"] = other_user

    # ¿Puedo girar?
    # -> SOLO si yo NO he girado hoy
    has_spun_me = bool(me_info.get("has_spun"))
    can_spin_now = not has_spun_me

    # Próximo reset: medianoche de hoy (horario Madrid)
    now_md = europe_madrid_now()
    next_midnight = now_md.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    next_reset_iso = next_midnight.isoformat()

    return jsonify(
        ok=True,
        me=me_info,
        other=other_info,
        can_spin_now=can_spin_now,
        next_reset_iso=next_reset_iso,
    )



_old_init_db = init_db
def init_db():
    _old_init_db()
    _ensure_gamification_schema()
