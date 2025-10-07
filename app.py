# app.py — con Web Push, notificaciones (Europe/Madrid a medianoche), seguimiento de precio
# y PRESENCIA tipo WhatsApp (en línea / última vez)
from flask import Flask, render_template, request, redirect, session, send_file, jsonify, flash, Response, make_response
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

def _init_pool():
    """Crea el pool una sola vez."""
    global PG_POOL
    if PG_POOL:
        return
    if not DATABASE_URL:
        raise RuntimeError("Falta DATABASE_URL para PostgreSQL")
    PG_POOL = SimpleConnectionPool(
        1,
        int(os.environ.get("DB_MAX_CONN", "10")),
        dsn=DATABASE_URL,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
        connect_timeout=8,
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

def now_madrid_str() -> str:
    return europe_madrid_now().strftime("%Y-%m-%d %H:%M:%S")

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
        # Push subscriptions
        c.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            endpoint TEXT UNIQUE NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TEXT)''')

        # ======= Presencia (tipo WhatsApp) =======
        c.execute('''CREATE TABLE IF NOT EXISTS presence (
            username   TEXT PRIMARY KEY,
            last_ping  TEXT,
            is_online  BOOLEAN DEFAULT FALSE,
            last_seen  TEXT,
            device     TEXT
        )''')

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
    return dt  # último recurso (naive)

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

def get_today_question(today: date | None = None):
    if today is None:
        today = today_madrid()
    today_str = today.isoformat()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT id, question
                FROM daily_questions
                WHERE TRIM(date) = %s
                ORDER BY id DESC
                LIMIT 1
            """, (today_str,))
            qrow = c.fetchone()
            if qrow:
                return qrow
            c.execute("SELECT question FROM daily_questions")
            used = {row[0] for row in c.fetchall()}
            remaining = [q for q in QUESTIONS if q not in used]
            if not remaining:
                return (None, "Ya no hay más preguntas disponibles ❤️")
            question_text = random.choice(remaining)
            c.execute("""
                INSERT INTO daily_questions (question, date)
                VALUES (%s,%s)
                RETURNING id
            """, (question_text, today_str))
            qid = c.fetchone()[0]
            conn.commit()
            return (qid, question_text)
    finally:
        conn.close()

def days_together():
    return (today_madrid() - RELATION_START).days

def days_until_meeting():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT meeting_date FROM meeting ORDER BY id DESC LIMIT 1")
            row = c.fetchone()
            if row:
                meeting_date = datetime.strptime(row[0], "%Y-%m-%d").date()
                return max((meeting_date - today_madrid()).days, 0)
            return None
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
                   body="Abre el contador y planead los detalles 💖",
                   url="/#contador",
                   tag="meeting-countdown")

# ========= Presencia (tipo WhatsApp) =========
PRESENCE_TTL_SECS = 70  # si no hay ping en ~70s, consideramos offline

def _presence_row_to_status(row):
    """Convierte una fila de DB en estado calculado (online por TTL)."""
    now = europe_madrid_now()
    if not row:
        return {"online": False, "last_seen": None}
    last_ping_dt = _parse_dt(row.get("last_ping") or "")
    online_flag = bool(row.get("is_online"))
    online = False
    if last_ping_dt and online_flag:
        try:
            online = (now - last_ping_dt).total_seconds() < PRESENCE_TTL_SECS
        except Exception:
            online = False
    last_seen = row.get("last_seen") or row.get("last_ping")
    return {"online": online, "last_seen": last_seen}

def presence_set(user: str, online: bool, device: str | None = None):
    """Marca online/offline y emite SSE."""
    now_txt = now_madrid_str()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            if online:
                c.execute("""
                    INSERT INTO presence (username, last_ping, is_online, device)
                    VALUES (%s,%s,TRUE,%s)
                    ON CONFLICT (username) DO UPDATE
                      SET last_ping=EXCLUDED.last_ping,
                          is_online=TRUE,
                          device=EXCLUDED.device
                """, (user, now_txt, (device or "")[:200]))
            else:
                c.execute("""
                    INSERT INTO presence (username, last_ping, is_online, last_seen, device)
                    VALUES (%s,%s,FALSE,%s,%s)
                    ON CONFLICT (username) DO UPDATE
                      SET is_online=FALSE,
                          last_seen=EXCLUDED.last_seen,
                          device=EXCLUDED.device
                """, (user, now_txt, now_txt, (device or "")[:200]))
            conn.commit()
    finally:
        conn.close()
    broadcast("presence", {"user": user, "online": bool(online), "last_seen": now_txt})

def presence_status_for(users=("mochito", "mochita")) -> dict:
    """Devuelve estados calculados de presencia para la pareja."""
    res = {u: {"online": False, "last_seen": None} for u in users}
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""SELECT username, last_ping, is_online, last_seen, device
                         FROM presence
                         WHERE username = ANY(%s)""", (list(users),))
            for r in c.fetchall():
                row = {"username": r["username"], "last_ping": r["last_ping"],
                       "is_online": r["is_online"], "last_seen": r["last_seen"], "device": r["device"]}
                res[r["username"]] = _presence_row_to_status(row)
    finally:
        conn.close()
    return res

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

def fetch_price(url: str) -> tuple[int | None, str | None, str | None]:
    try:
        resp = requests.get(url, timeout=8, headers={"User-Agent": PRICE_UA})
        html = resp.text
        c_jsonld, curr = _find_in_jsonld(html)
        cents_dom = None
        soup = BeautifulSoup(html, "html.parser") if BeautifulSoup else None
        if soup:
            cents_dom = _meta_price(soup)
            title = (soup.title.text.strip() if soup.title else None)
        else:
            title = None
        cents_rx = None
        for pat in [r"€\s*\d[\d\.\,]*", r"\d[\d\.\,]*\s*€", r"\b\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})\b"]:
            m = re.search(pat, html)
            if m:
                cents_rx = _to_cents_from_str(m.group(0)); break
        price = c_jsonld or cents_dom or cents_rx
        if price:
            if not curr:
                curr = "EUR" if "€" in html else None
            return price, curr, title
        return None, None, title
    except Exception as e:
        print("[price] fetch error:", e)
        return None, None, None

def fmt_eur(cents: int | None) -> str:
    if cents is None: return "—"
    euros = cents/100.0
    return f"{euros:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")

def notify_price_drop(row, old_cents: int, new_cents: int):
    """Notifica bajada de precio.
       - Si es regalo (is_gift=True): SOLO al creador.
       - Si no es regalo: a ambos.
    """
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
            send_push_to(created_by, title=title, body=body, url=link, tag=tag)
        else:
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

# ========= Background scheduler (recordatorios) =========
def background_loop():
    """Bucles de 45–60s para:
       - crear pregunta del día a medianoche y notificar a ambos
       - recordatorios 3/2/1 días (2–3 veces entre 09:00 y 23:00)
       - últimas 3h si falta responder
       - barrido de seguimiento de precios cada ~3h
    """
    print("[bg] scheduler iniciado")
    while True:
        try:
            now = europe_madrid_now()
            today = now.date()

            # 1) Pregunta del día + notificación (una vez/día)
            last_dq_push = state_get("last_dq_push_date", "")
            if str(today) != last_dq_push:
                qid, qtext = get_today_question(today)   # usar fecha local
                push_daily_new_question()
                state_set("last_dq_push_date", str(today))
                cache_invalidate('compute_streaks')

            # 2) Recordatorios 3/2/1 días
            try:
                d = days_until_meeting()
            except Exception:
                d = None

            if d is not None and d in (1, 2, 3):
                times = ensure_meet_times(today)
                for hhmm in times:
                    sent_key = _meet_sent_key(today, hhmm)
                    if not state_get(sent_key, "") and due_now(now, hhmm):
                        push_meeting_countdown(d)
                        state_set(sent_key, "1")
            else:
                state_set(_meet_times_key(today), "[]")

            # 3) Últimas 3 horas para responder
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

            # 4) Barrido de precios cada ~3h
            try:
                last_sweep = state_get("last_price_sweep", "")
                do = True
                if last_sweep:
                    last = _parse_dt(last_sweep)
                    if last:
                        # si hace menos de 3 horas que pasamos el sweep, saltamos
                        if (now - last).total_seconds() < 3 * 3600:
                            do = False
                if do:
                    try:
                        sweep_price_checks()
                    finally:
                        state_set("last_price_sweep", now_madrid_str())
            except Exception as e:
                print("[bg] error en sweep:", e)


        except Exception as e:
            # nunca debemos romper el bucle
            print("[bg] loop error:", e)

        # espera entre 45 y 60s (aleatorio) para no sincronizar con otros workers
        time.sleep(random.randint(45, 60))

# ======= Lanzar background una única vez por proceso =======
_BG_STARTED = False
def start_background_once():
    global _BG_STARTED
    if _BG_STARTED:
        return
    _BG_STARTED = True
    th = threading.Thread(target=background_loop, daemon=True)
    th.start()

# arranca salvo que se fuerce a desactivar
if not os.environ.get("DISABLE_BG"):
    start_background_once()

# ========= Auth helpers / decoradores =========
def current_user() -> str | None:
    u = session.get("username")
    if u in ("mochito", "mochita"):
        return u
    return u

def require_user(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(*a, **k):
        if not current_user():
            return jsonify({"ok": False, "error": "auth_required"}), 401
        return fn(*a, **k)
    return wrapped

# ========= Rutas básicas =========
@app.get("/")
def home():
    # Sirve tu plantilla (templates/index.html)
    return render_template("index.html")

@app.get("/api/status")
def api_status():
    return jsonify({
        "ok": True,
        "now_madrid": now_madrid_str(),
        "user": current_user(),
        "vapid_key": get_vapid_public_base64url(),
    })

@app.get("/healthz")
def healthz():
    return ("ok", 200, {"Cache-Control": "no-store"})

# ========= Login / Logout =========
@app.post("/login")
def login():
    data = request.get_json(silent=True) or request.form or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "")
    if not username or not password:
        return jsonify({"ok": False, "error": "missing_credentials"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT password FROM users WHERE username=%s", (username,))
            row = c.fetchone()
    finally:
        conn.close()

    if not row:
        return jsonify({"ok": False, "error": "not_found"}), 404

    stored = row[0]
    try:
        if _is_hashed(stored):
            valid = check_password_hash(stored, password)
        else:
            valid = (stored == password)
    except Exception:
        valid = False

    if not valid:
        send_discord("Login fallo", {"user": username})
        return jsonify({"ok": False, "error": "invalid_credentials"}), 401

    session["username"] = username
    presence_set(username, True, device=request.headers.get("User-Agent"))
    send_discord("Login ok", {"user": username})
    return jsonify({"ok": True, "user": username})

@app.post("/logout")
@require_user
def logout():
    u = current_user()
    try:
        presence_set(u, False, device=request.headers.get("User-Agent"))
    except Exception:
        pass
    session.pop("username", None)
    return jsonify({"ok": True})

# ========= Presencia (tipo WhatsApp) =========
@app.post("/api/presence/ping")
@require_user
def api_presence_ping():
    u = current_user()
    dev = (request.get_json(silent=True) or {}).get("device") or request.headers.get("User-Agent")
    presence_set(u, True, device=str(dev)[:200])
    return jsonify({"ok": True, "status": presence_status_for()})

@app.post("/api/presence/offline")
@require_user
def api_presence_offline():
    u = current_user()
    presence_set(u, False, device=request.headers.get("User-Agent"))
    return jsonify({"ok": True})

@app.get("/api/presence/status")
@require_user
def api_presence_status():
    return jsonify({"ok": True, "status": presence_status_for()})

# ========= Push (Web Push) =========
@app.get("/api/push/public_key")
def api_push_pubkey():
    return jsonify({"ok": True, "key": get_vapid_public_base64url()})

@app.post("/api/push/subscribe")
@require_user
def api_push_subscribe():
    u = current_user()
    data = request.get_json(force=True)
    endpoint = (data.get("endpoint") or "").strip()
    keys = data.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    authk  = (keys.get("auth") or "").strip()
    if not (endpoint and p256dh and authk):
        return jsonify({"ok": False, "error": "bad_subscription"}), 400

    now_txt = now_madrid_str()
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
            """, (u, endpoint, p256dh, authk, now_txt))
            conn.commit()
    finally:
        conn.close()
    send_discord("Push subscribe", {"user": u, "endpoint": endpoint[:60]+"..."})
    return jsonify({"ok": True})

@app.post("/api/push/unsubscribe")
@require_user
def api_push_unsubscribe():
    data = request.get_json(force=True)
    endpoint = (data.get("endpoint") or "").strip()
    if not endpoint:
        return jsonify({"ok": False, "error": "missing_endpoint"}), 400
    _delete_subscription_by_endpoint(endpoint)
    return jsonify({"ok": True})

# ========= Pregunta del día / respuestas =========
@app.get("/api/dq/today")
@require_user
def api_dq_today():
    qid, qtext = get_today_question()
    cur, best = compute_streaks()
    return jsonify({"ok": True, "question_id": qid, "question": qtext, "streak_current": cur, "streak_best": best})

@app.post("/api/dq/answer")
@require_user
def api_dq_answer():
    u = current_user()
    data = request.get_json(force=True)
    qid = int(data.get("question_id") or 0)
    ans = (data.get("answer") or "").strip()
    if not (qid and ans):
        return jsonify({"ok": False, "error": "missing_fields"}), 400
    now_txt = now_madrid_str()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
              INSERT INTO answers (question_id, username, answer, created_at, updated_at)
              VALUES (%s,%s,%s,%s,%s)
              ON CONFLICT (question_id, username) DO UPDATE
                SET answer=EXCLUDED.answer, updated_at=EXCLUDED.updated_at
            """, (qid, u, ans, now_txt, now_txt))
            conn.commit()
    finally:
        conn.close()
    push_answer_notice(u)
    cache_invalidate('compute_streaks')
    broadcast("answer", {"user": u, "question_id": qid})
    return jsonify({"ok": True})

# ========= Intimidad (con PIN) =========
@app.get("/api/intim/list")
@require_user
def api_intim_list():
    return jsonify({"ok": True, "events": get_intim_events(200), "stats": get_intim_stats()})

@app.post("/api/intim/add")
@require_user
def api_intim_add():
    data = request.get_json(force=True)
    pin = str(data.get("pin") or "")
    if pin != str(INTIM_PIN):
        return jsonify({"ok": False, "error": "bad_pin"}), 403
    u = current_user()
    ts = (data.get("ts") or now_madrid_str())
    place = (data.get("place") or "").strip()[:120]
    notes = (data.get("notes") or "").strip()[:500]

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""INSERT INTO intimacy_events (username, ts, place, notes)
                         VALUES (%s,%s,%s,%s)""", (u, ts, place, notes))
            conn.commit()
    finally:
        conn.close()
    broadcast("intim", {"user": u, "ts": ts})
    send_discord("Intim add", {"user": u, "ts": ts, "place": place})
    return jsonify({"ok": True, "stats": get_intim_stats()})

# ========= Wishlist + tracking de precio =========
@app.get("/api/wishlist/list")
@require_user
def api_wishlist_list():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
              SELECT id, product_name, product_link, notes, created_by, created_at,
                     is_purchased, is_gift, priority, size,
                     track_price, last_price_cents, currency, last_checked,
                     alert_drop_percent, alert_below_cents
              FROM wishlist
              ORDER BY is_purchased ASC, created_at DESC, id DESC
            """)
            rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()
    return jsonify({"ok": True, "items": rows})

@app.post("/api/wishlist/add")
@require_user
def api_wishlist_add():
    u = current_user()
    d = request.get_json(force=True)
    name = (d.get("product_name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "missing_name"}), 400
    link = (d.get("product_link") or "").strip()
    notes = (d.get("notes") or "").strip()
    is_gift = bool(d.get("is_gift"))
    priority = (d.get("priority") or "media")
    size = (d.get("size") or "").strip()
    track_price = bool(d.get("track_price"))

    now_txt = now_madrid_str()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
              INSERT INTO wishlist (product_name, product_link, notes, created_by, created_at,
                                    is_purchased, is_gift, priority, size, track_price)
              VALUES (%s,%s,%s,%s,%s, FALSE, %s, %s, %s, %s)
              RETURNING id
            """, (name, link, notes, u, now_txt, is_gift, priority, size, track_price))
            wid = c.fetchone()[0]
            conn.commit()
    finally:
        conn.close()

    push_wishlist_notice(u, is_gift=is_gift)
    send_discord("Wishlist add", {"by": u, "name": name, "gift": is_gift})
    return jsonify({"ok": True, "id": wid})

@app.post("/api/wishlist/purchased")
@require_user
def api_wishlist_purchased():
    d = request.get_json(force=True)
    wid = int(d.get("id") or 0)
    flag = bool(d.get("is_purchased"))
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE wishlist SET is_purchased=%s WHERE id=%s", (flag, wid))
            conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})

@app.post("/api/wishlist/track")
@require_user
def api_wishlist_track():
    d = request.get_json(force=True)
    wid = int(d.get("id") or 0)
    track = bool(d.get("track_price"))
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE wishlist SET track_price=%s WHERE id=%s", (track, wid))
            conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})

@app.post("/api/wishlist/alerts")
@require_user
def api_wishlist_alerts():
    d = request.get_json(force=True)
    wid = int(d.get("id") or 0)
    pct = d.get("alert_drop_percent")
    below = d.get("alert_below_cents")
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
              UPDATE wishlist
                 SET alert_drop_percent=%s,
                     alert_below_cents=%s
               WHERE id=%s
            """, (pct, below, wid))
            conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})

@app.post("/api/wishlist/check")
@require_user
def api_wishlist_check_one():
    d = request.get_json(force=True)
    wid = int(d.get("id") or 0)
    if not wid:
        return jsonify({"ok": False, "error": "missing_id"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
              SELECT id, product_name, product_link, created_by,
                     is_gift, last_price_cents, currency
                FROM wishlist
               WHERE id=%s
            """, (wid,))
            row = c.fetchone()
            if not row:
                return jsonify({"ok": False, "error": "not_found"}), 404
            row = dict(row)
    finally:
        conn.close()

    price_cents, curr, _title = fetch_price(row["product_link"])
    now_txt = now_madrid_str()

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
              UPDATE wishlist
                 SET last_price_cents=%s,
                     currency=%s,
                     last_checked=%s
               WHERE id=%s
            """, (price_cents, curr or row["currency"] or "EUR", now_txt, wid))
            conn.commit()
    finally:
        conn.close()

    old = row.get("last_price_cents")
    if price_cents is not None and old is not None and price_cents < old:
        try:
            notify_price_drop(row, old, price_cents)
        except Exception as e:
            print("[manual notify drop]", e)

    return jsonify({"ok": True, "price_cents": price_cents, "currency": curr or "EUR"})

# ========= Utilidades varias =========
@app.post("/api/meeting/set")
@require_user
def api_meeting_set():
    d = request.get_json(force=True)
    date_txt = (d.get("meeting_date") or "").strip()
    try:
        _ = datetime.strptime(date_txt, "%Y-%m-%d").date()
    except Exception:
        return jsonify({"ok": False, "error": "bad_date"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("INSERT INTO meeting (meeting_date) VALUES (%s)", (date_txt,))
            conn.commit()
    finally:
        conn.close()
    send_discord("Meeting set", {"date": date_txt})
    return jsonify({"ok": True, "days_left": days_until_meeting()})

@app.get("/api/stats")
@require_user
def api_stats():
    qid, qtext = get_today_question()
    cur, best = compute_streaks()
    stats = get_intim_stats()
    return jsonify({
        "ok": True,
        "today_question": {"id": qid, "question": qtext},
        "streak": {"current": cur, "best": best},
        "intim": stats,
        "days_together": days_together(),
        "days_until_meeting": days_until_meeting(),
        "presence": presence_status_for(),
        "banner": get_banner(),
        "profiles": get_profile_pictures(),
        "vapid_public_key": get_vapid_public_base64url()
    })

@app.post("/api/banner/upload")
@require_user
def api_banner_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no_file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "empty_filename"}), 400
    fn = secure_filename(f.filename)
    data = f.read()
    mt = f.mimetype or "application/octet-stream"
    now_txt = now_madrid_str()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""INSERT INTO banner (image_data, filename, mime_type, uploaded_at)
                         VALUES (%s,%s,%s,%s)""", (psycopg2.Binary(data), fn, mt, now_txt))
            conn.commit()
    finally:
        conn.close()
    cache_invalidate('get_banner')
    return jsonify({"ok": True})

@app.post("/api/profile/upload")
@require_user
def api_profile_upload():
    u = current_user()
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no_file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "empty_filename"}), 400
    fn = secure_filename(f.filename)
    data = f.read()
    mt = f.mimetype or "application/octet-stream"
    now_txt = now_madrid_str()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
              INSERT INTO profile_pictures (username, image_data, filename, mime_type, uploaded_at)
              VALUES (%s,%s,%s,%s,%s)
              ON CONFLICT (username) DO UPDATE
                SET image_data=EXCLUDED.image_data,
                    filename=EXCLUDED.filename,
                    mime_type=EXCLUDED.mime_type,
                    uploaded_at=EXCLUDED.uploaded_at
            """, (u, psycopg2.Binary(data), fn, mt, now_txt))
            conn.commit()
    finally:
        conn.close()
    cache_invalidate('get_profile_pictures')
    return jsonify({"ok": True})

# ========= Endpoint util para probar extracción de precio =========
@app.post("/api/price/peek")
@require_user
def api_price_peek():
    d = request.get_json(force=True)
    url = (d.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "missing_url"}), 400
    cents, curr, title = fetch_price(url)
    return jsonify({"ok": True, "cents": cents, "currency": curr, "title": title})

# ========= Main =========
if __name__ == "__main__":
    # Evita doble hilo en modo debug
    if os.environ.get("FLASK_ENV") == "development":
        # el reloader de werkzeug spawnea 2 procesos; arrancamos solo en el principal
        if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
            start_background_once()
    else:
        start_background_once()

    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)

                   
