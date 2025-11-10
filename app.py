

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

def _ensure_gamification_schema():
    """Crea columnas/tablas si faltan (seguro de repetir)."""
    global _gami_ready
    if _gami_ready:
        return
    with _gami_lock:
        if _gami_ready:
            return
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
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

                # Tienda y compras
                c.execute("""
                CREATE TABLE IF NOT EXISTS shop_items (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    cost INTEGER NOT NULL,
                    description TEXT,
                    icon TEXT
                )
                """)
                c.execute("""
                CREATE TABLE IF NOT EXISTS purchases (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    quantity INTEGER DEFAULT 1,
                    note TEXT,
                    purchased_at TEXT
                )
                """)
            conn.commit()
        finally:
            conn.close()
        _seed_gamification()
        _gami_ready = True

def _seed_gamification():
    """Semillas idempotentes de medallas y tienda."""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            achs = [
                ("answers_30",   "Constante 30",  "Has respondido 30 preguntas",                      "🧩", 30),
                ("answers_100",  "Constante 100", "Has respondido 100 preguntas",                     "🧩", 100),
                ("first_answer_1","¡Primera del día!", "Has sido la primera en contestar",            "⚡", 1),
                ("first_answer_10","Velocista ×10",    "Primera en 10 ocasiones",                    "⚡", 10),
                ("streak_7",     "Racha 7",       "7 días seguidos",                                  "🔥", 7),
                ("streak_30",    "Racha 30",      "30 días seguidos",                                 "🔥", 30),
                ("streak_50",    "Racha 50",      "50 días seguidos",                                 "🔥", 50),
                ("days_100",     "100 días",      "El juego ya tiene 100 preguntas publicadas",       "💯", 100),
            ]
            for code, title, desc, icon, goal in achs:
                c.execute("""
                    INSERT INTO achievements (code,title,description,icon,goal)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (code) DO UPDATE
                    SET title=EXCLUDED.title, description=EXCLUDED.description,
                        icon=EXCLUDED.icon, goal=EXCLUDED.goal
                """, (code,title,desc,icon,goal))

            items = [
                ("Cena gratis",       120, "Vale por una cena pagada por tu pareja", "🍝"),
                ("Cine juntos",        90, "Entradas para el cine (+ palomitas)",    "🎬"),
                ("Desayuno a la cama", 60, "Cafecito + croissants servido con amor", "☕"),
                ("Masaje 30’",         50, "30 minutos de masaje relajante",         "💆‍♀️"),
                ("Día sin fregar",     40, "Te libras hoy de fregar platos",         "🧽"),
            ]
            for name, cost, desc, icon in items:
                c.execute("""
                    INSERT INTO shop_items (name,cost,description,icon)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT (name) DO UPDATE
                    SET cost=EXCLUDED.cost, description=EXCLUDED.description, icon=EXCLUDED.icon
                """, (name,cost,desc,icon))
        conn.commit()
    finally:
        conn.close()

def _grant_achievement_to(user: str, achievement_id: int, points_on_award: int = 0):
    """Concede (si falta) la medalla a 'user' y suma puntos."""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT id FROM users WHERE username=%s", (user,))
            uid = (c.fetchone() or [None])[0]
            if not uid:
                return

            c.execute("""
              SELECT 1 FROM user_achievements
              WHERE user_id=%s AND achievement_id=%s
            """, (uid, achievement_id))
            if c.fetchone():
                return  # ya concedida

            if points_on_award and int(points_on_award) != 0:
                c.execute("UPDATE users SET points = COALESCE(points,0) + %s WHERE id=%s",
                          (int(points_on_award), uid))

            c.execute("""
              INSERT INTO user_achievements (user_id, achievement_id, earned_at)
              VALUES (%s,%s,%s)
              ON CONFLICT (user_id, achievement_id) DO NOTHING
            """, (uid, achievement_id, now_madrid_str()))
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

def award_points_for_answer(question_id, username, is_new_insert):
    if not is_new_insert:
        return
    with closing(get_db_connection()) as conn, conn.cursor() as c:
        base = 5
        c.execute("SELECT COUNT(*) FROM answers WHERE question_id=%s", (question_id,))
        count_after = c.fetchone()[0] or 0
        bonus = 10 if count_after == 1 else 0
        _award_points(conn, username, base + bonus, "answer")
        _maybe_award_achievements(conn, username)


_ensure_gamification_schema()

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
    return jsonify({
        "user": user,
        "points": points,
        "total_days": total_days,
        "streak": streak,
        "first_count": first_count,
        "achievements": achs,
        "ranking": ranking,
        "shop": shop
    })

@app.route("/tienda", methods=["GET", "POST"])
def tienda():
    user = session.get("username")
    if not user:
        return redirect(url_for("index"))
    with closing(get_db_connection()) as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
        if request.method == "POST":
            item_id = request.form.get("item_id")
            if item_id:
                c.execute("SELECT id, name, cost FROM shop_items WHERE id=%s", (item_id,))
                item = c.fetchone()
                if item:
                    c.execute("SELECT COALESCE(points,0) FROM users WHERE username=%s", (user,))
                    pts = (c.fetchone() or [0])[0]
                    if pts >= item["cost"]:
                        c.execute("UPDATE users SET points = COALESCE(points,0)-%s WHERE username=%s", (item["cost"], user))
                        c.execute(
                            "INSERT INTO purchases (user_id, item_id, quantity, purchased_at) "
                            "VALUES ((SELECT id FROM users WHERE username=%s), %s, 1, %s)",
                            (user, item["id"], now_madrid_str())
                        )
                        conn.commit()
                        flash(f"Has canjeado: {item['name']} 🎉", "success")
                    else:
                        flash("No tienes puntos suficientes :(", "warning")
                return redirect(url_for("tienda"))
        c.execute("SELECT id, name, cost, description, icon FROM shop_items ORDER BY cost ASC")
        items = c.fetchall()
        c.execute("SELECT COALESCE(points,0) FROM users WHERE username=%s", (user,))
        pts = (c.fetchone() or [0])[0]
    return render_template("tienda.html", items=items, points=pts, user=user)

@app.route("/medallas")
def medallas():
    user = session.get("username")
    return render_template("medallas.html", user=user)


@app.get("/admin/achievements")
def admin_achievements_list():
    require_admin()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
              SELECT id, code, title, description, icon,
                     trigger_kind, trigger_value, points_on_award, grant_both, active
              FROM achievements
              ORDER BY active DESC, trigger_kind, trigger_value NULLS LAST, id DESC
            """)
            rows = c.fetchall()
    finally:
        conn.close()
    return render_template("admin_achievements.html",
                           username=session['username'],
                           rows=rows)


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



_old_init_db = init_db
def init_db():
    _old_init_db()
    _ensure_gamification_schema()



@app.post("/admin/points/adjust")
def admin_points_adjust():
    require_admin()
    username = (request.form.get("username") or "").strip()
    try:
        delta = int((request.form.get("delta") or "0") or 0)
    except Exception:
        delta = 0
    reason = (request.form.get("reason") or "").strip() or "manual_adjust"
    if not username or delta == 0:
        flash("Elige usuario y una cantidad distinta de 0.", "error")
        return redirect("/admin/achievements")
    with closing(get_db_connection()) as conn, conn.cursor() as c:
        c.execute("UPDATE users SET points = COALESCE(points,0) + %s WHERE username=%s", (delta, username))
        conn.commit()
    try:
        send_discord("Points adjusted", {"user": username, "delta": delta, "reason": reason})
    except Exception:
        pass
    flash(f"Se {'sumaron' if delta>0 else 'restaron'} {abs(delta)} puntos a {username}.", "success")
    return redirect("/admin/achievements")


@app.post("/admin/shop/add")
def admin_shop_add():
    require_admin()
    f = request.form
    name = (f.get("name") or "").strip()
    description = (f.get("description") or "").strip() or None
    icon = (f.get("icon") or "").strip() or None
    try:
        cost = int((f.get("cost") or "0") or 0)
    except Exception:
        cost = 0
    if not name or cost <= 0:
        flash("Nombre y coste (>0) son obligatorios.", "error")
        return redirect("/admin/achievements")
    with closing(get_db_connection()) as conn, conn.cursor() as c:
        c.execute("INSERT INTO shop_items (name, cost, description, icon) VALUES (%s,%s,%s,%s) ON CONFLICT (name) DO NOTHING",
                  (name, cost, description, icon))
        conn.commit()
    flash("Premio añadido a la tienda.", "success")
    return redirect("/admin/achievements")


@app.post("/admin/shop/<int:item_id>/delete")
def admin_shop_delete(item_id):
    require_admin()
    with closing(get_db_connection()) as conn, conn.cursor() as c:
        c.execute("DELETE FROM shop_items WHERE id=%s", (item_id,))
        conn.commit()
    flash("Premio eliminado.", "success")
    return redirect("/admin/achievements")
