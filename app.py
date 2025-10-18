from flask import Flask, render_template, request, redirect, session, send_file, jsonify, flash, Response, make_response, abort
import psycopg2, psycopg2.extras
from psycopg2.pool import SimpleConnectionPool
from datetime import datetime, date, timedelta, timezone
import random, os, io, json, time, queue, threading, hashlib, base64, binascii
from werkzeug.utils import secure_filename
from contextlib import closing
from base64 import b64encode
from werkzeug.security import generate_password_hash, check_password_hash
import requests, re
from pywebpush import webpush, WebPushException

try:
    import pytz
except Exception:
    pytz = None

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

# Configuración optimizada de Flask
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'tu_clave_secreta_aqui')

@app.before_request
def _fast_head_for_healthchecks():
    if request.method == 'HEAD' and request.path == '/':
        return '', 204

# Configuraciones críticas de rendimiento
app.config['TEMPLATES_AUTO_RELOAD'] = False
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
app.config['JSON_SORT_KEYS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# Comprimir respuestas
try:
    from flask_compress import Compress
    app.config['COMPRESS_MIMETYPES'] = [
        'text/html', 'text/css', 'application/json', 
        'application/javascript', 'text/javascript'
    ]
    app.config['COMPRESS_LEVEL'] = 6
    app.config['COMPRESS_MIN_SIZE'] = 500
    Compress(app)
except Exception:
    pass

# Headers de cache para static files
@app.after_request
def add_header(response):
    if request.path.startswith('/static/'):
        response.cache_control.max_age = 31536000
        response.cache_control.public = True
    return response

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
            'title': event,
            'color': 15221651,
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'fields': []
        }
        if display_user:
            embed['fields'].append({
                'name': 'Usuario',
                'value': display_user,
                'inline': True
            })
        try:
            ruta = f"{request.method} {request.path}"
        except Exception:
            ruta = '(sin request)'
        embed['fields'] += [
            {'name': 'Ruta', 'value': ruta, 'inline': True},
            {'name': 'IP', 'value': client_ip() or '?', 'inline': True}
        ]
        if payload:
            raw = json.dumps(payload, ensure_ascii=False)
            for (i, ch) in enumerate([raw[i:i+1000] for i in range(0, len(raw), 1000)][:3]):
                embed['fields'].append({
                    'name': 'Datos' + (f" ({i+1})" if i else ''),
                    'value': f"```json\n{ch}\n```",
                    'inline': False
                })
        _DISCORD_Q.put_nowait((DISCORD_WEBHOOK, {
            'username': 'Mochitos Logs',
            'embeds': [embed]
        }))
    except Exception as e:
        print(f"[discord] prep error: {e}")

DATABASE_URL = os.environ.get('DATABASE_URL', '')

def _normalize_database_url(url: str) -> str:
    if not url:
        return url
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    if 'sslmode=' not in url:
        sep = '&' if '?' in url else '?'
        url = f"{url}{sep}sslmode=require"
    return url

DATABASE_URL = _normalize_database_url(DATABASE_URL)

from psycopg2 import OperationalError, InterfaceError, DatabaseError

PG_POOL = None

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
            c.execute('''
                INSERT INTO user_presence (username, last_seen, device, user_agent)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (username) DO UPDATE
                SET last_seen = EXCLUDED.last_seen,
                    device    = EXCLUDED.device,
                    user_agent= EXCLUDED.user_agent;
            ''', (user, now, device, ua))
            conn.commit()
    finally:
        conn.close()

def _init_pool():
    global PG_POOL
    if PG_POOL:
        return
    if not DATABASE_URL:
        raise RuntimeError('Falta DATABASE_URL para PostgreSQL')
    
    PG_POOL = SimpleConnectionPool(
        1, 
        int(os.environ.get('DB_MAX_CONN', '15')),
        dsn=DATABASE_URL,
        keepalives=1,
        keepalives_idle=60,
        keepalives_interval=30,
        keepalives_count=5,
        connect_timeout=10
    )

class PooledConn:
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
        if 'cursor_factory' not in k:
            k['cursor_factory'] = psycopg2.extras.DictCursor
        if not self._inited:
            try:
                with self._conn.cursor() as c:
                    c.execute("SET application_name = 'mochitos';")
                    c.execute("SET idle_in_transaction_session_timeout = '10s';")
                    c.execute("SET statement_timeout = '8s';")
                self._inited = True
            except (OperationalError, InterfaceError):
                self._reconnect()
                with self._conn.cursor() as c:
                    c.execute("SET application_name = 'mochitos';")
                    c.execute("SET idle_in_transaction_session_timeout = '10s';")
                    c.execute("SET statement_timeout = '8s';")
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
    _init_pool()
    conn = PG_POOL.getconn()
    wrapped = PooledConn(PG_POOL, conn)
    try:
        with wrapped.cursor() as c:
            c.execute('SELECT 1;')
            _ = c.fetchone()
    except (OperationalError, InterfaceError, DatabaseError):
        wrapped._reconnect()
        with wrapped.cursor() as c:
            c.execute('SELECT 1;')
            _ = c.fetchone()
    return wrapped

# Sistema de caché optimizado
class EfficientCache:
    def __init__(self, max_size=1000):
        self._cache = {}
        self._max_size = max_size
        self._access_count = {}
    
    def get(self, key):
        if key in self._cache:
            expires, value = self._cache[key]
            if time.time() < expires:
                self._access_count[key] = self._access_count.get(key, 0) + 1
                return value
            else:
                del self._cache[key]
                self._access_count.pop(key, None)
        return None
    
    def set(self, key, value, ttl=60):
        if len(self._cache) >= self._max_size:
            self._evict()
        self._cache[key] = (time.time() + ttl, value)
        self._access_count[key] = self._access_count.get(key, 0) + 1
    
    def _evict(self):
        if not self._cache:
            return
        least_used = min(self._access_count.items(), key=lambda x: x[1])
        del self._cache[least_used[0]]
        del self._access_count[least_used[0]]
    
    def clear(self, pattern=None):
        if pattern:
            keys_to_delete = [k for k in self._cache.keys() if pattern in k]
            for key in keys_to_delete:
                del self._cache[key]
                self._access_count.pop(key, None)
        else:
            self._cache.clear()
            self._access_count.clear()

_cache_store = EfficientCache(max_size=500)

def _cache_key(fn_name):
    return f"_cache::{fn_name}"

def ttl_cache(seconds=30):
    def deco(fn):
        key = _cache_key(fn.__name__)
        def wrapped(*a, **k):
            cached = _cache_store.get(key)
            if cached is not None:
                return cached
            val = fn(*a, **k)
            _cache_store.set(key, val, seconds)
            return val
        wrapped.__wrapped__ = fn
        wrapped._cache_key = key
        return wrapped
    return deco

def cache_invalidate(*fn_names):
    for name in fn_names:
        _cache_store.clear(_cache_key(name))

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
    end_dst_utc = datetime(y, 10, _eu_last_sunday(y, 10).day, 1, 0, 0, tzinfo=timezone.utc)
    offset_hours = 2 if start_dst_utc <= utc_now < end_dst_utc else 1
    return (utc_now + timedelta(hours=offset_hours)).replace(tzinfo=None)

def europe_madrid_now() -> datetime:
    utc_now = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return utc_now.astimezone(ZoneInfo('Europe/Madrid'))
    except Exception:
        pass
    if pytz:
        try:
            tz = pytz.timezone('Europe/Madrid')
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
    try:
        hh, mm = map(int, hhmm.split(':'))
    except Exception:
        return False
    scheduled = now_madrid.replace(hour=hh, minute=mm, second=0, microsecond=0)
    delta = (now_madrid - scheduled).total_seconds()
    return delta >= 0 and delta <= grace_minutes * 60

def now_madrid_str() -> str:
    return europe_madrid_now().strftime('%Y-%m-%d %H:%M:%S')

_subscribers_lock = threading.Lock()
_subscribers: set[queue.Queue] = set()

def broadcast(event_name: str, data: dict):
    with _subscribers_lock:
        for qcli in list(_subscribers):
            try:
                qcli.put_nowait({'event': event_name, 'data': data})
            except Exception:
                pass

@app.route('/events')
def sse_events():
    client_q: queue.Queue = queue.Queue(maxsize=200)
    with _subscribers_lock:
        _subscribers.add(client_q)

    def gen():
        try:
            yield ':\n\n'
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

    return Response(gen(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache, no-transform',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no'
    })

QUESTIONS = [
    '¿Qué fue lo que más te atrajo de mí al principio?',
    '¿Qué parte de nuestra relación te hace sentir más feliz?',
    '¿Qué canción te recuerda a nosotros?',
    '¿Qué harías si solo tuviéramos un día más juntos?',
    '¿Qué detalle pequeño que hago te enamora más?',
    '¿Cómo describirías nuestro amor en tres palabras?',
    '¿Qué es lo que más amas de nuestras conversaciones?',
    '¿Qué sueñas para nuestro futuro juntos?',
    '¿Qué te hace sentir más amado/a por mí?',
    '¿Qué te gustaría que nunca cambiara entre nosotros?',
    '¿Qué promesa me harías hoy sin pensarlo dos veces?',
    'Si fuéramos un dúo cómico, ¿cómo nos llamaríamos?',
    '¿Qué harías si despertaras y fueras yo por un día?',
    '¿Cuál es el apodo más ridículo que se te ocurre para mí?',
    '¿Qué canción cantarías desnudo/a en la ducha como si fuera un show?',
    '¿Qué superpoder inútil te gustaría tener?',
    '¿Qué animal representa mejor nuestra relación y por qué?',
    '¿Cuál es el momento más tonto que hemos vivido juntos?',
    '¿Qué harías si estuviéramos atrapados en un supermercado por 24 horas?',
    '¿Qué serie seríamos si nuestra vida fuera una comedia?',
    '¿Con qué personaje de dibujos animados me comparas?',
    '¿Qué parte de mi cuerpo te gusta más tocar?',
    '¿Dónde te gustaría que te besara ahora mismo?',
    '¿Has fantaseado conmigo hoy?',
    '¿Cuál fue la última vez que soñaste algo caliente conmigo?',
    '¿En qué lugar prohibido te gustaría hacerlo conmigo?',
    '¿Qué prenda mía te gustaría quitarme con los dientes?',
    '¿Qué harías si estuviéramos solos en un ascensor por 30 minutos?',
    '¿Cuál es tu fantasía secreta conmigo que aún no me has contado?',
    '¿Qué juguete usarías conmigo esta noche?',
    '¿Te gustaría que te atara o prefieres atarme a mí?',
    'Si tuviéramos una casa del árbol, ¿cómo sería por dentro?',
    'Si hiciéramos una película sobre nosotros, ¿cómo se llamaría?',
    '¿Cómo sería nuestro planeta si fuéramos los únicos habitantes?',
    'Si pudieras diseñar una cita perfecta desde cero, ¿cómo sería?',
    'Si nos perdiéramos en el tiempo, ¿en qué época te gustaría vivir conmigo?',
    'Si nuestra historia de amor fuera un libro, ¿cómo sería el final?',
    'Si pudieras regalarme una experiencia mágica, ¿ cuál sería?',
    '¿Qué mundo ficticio te gustaría explorar conmigo?',
    '¿Qué aprendiste sobre ti mismo/a desde que estamos juntos?',
    '¿Qué miedos tienes sobre el futuro y cómo puedo ayudarte con ellos?',
    '¿Cómo te gustaría crecer como pareja conmigo?',
    '¿Qué errores cometiste en el pasado que no quieres repetir conmigo?',
    '¿Qué significa para ti una relación sana?',
    '¿Cuál es el mayor sueño que quieres cumplir y cómo puedo ayudarte?',
    '¿Qué necesitas escuchar más seguido de mí?',
    '¿Qué momento de tu infancia quisieras revivir conmigo al lado?',
    '¿Cuál es el olor que más te recuerda a mí?',
    '¿Qué comida describes como ‘sexy’?',
    '¿Qué harías si fueras invisible por un día y solo yo te pudiera ver?',
    '¿Qué parte de mi rutina diaria te parece más adorable?',
    '¿Si pudieras clonar una parte de mí, cuál sería?',
    '¿Qué emoji usarías para describir nuestra relación?',
    '¿Si solo pudieras besarme o abrazarme por un mes, qué eliges?'
]

RELATION_START = date(2025, 8, 2)
INTIM_PIN = os.environ.get('INTIM_PIN', '6969')

def init_db():
    with closing(get_db_connection()) as conn, conn.cursor() as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY, 
                username TEXT UNIQUE, 
                password TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS schedule_times (
                id SERIAL PRIMARY KEY, 
                username TEXT NOT NULL, 
                time TEXT NOT NULL,
                UNIQUE (username, time)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS daily_questions (
                id SERIAL PRIMARY KEY, 
                question TEXT, 
                date TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS answers (
                id SERIAL PRIMARY KEY, 
                question_id INTEGER, 
                username TEXT, 
                answer TEXT
            )
        ''')
        c.execute('ALTER TABLE answers ADD COLUMN IF NOT EXISTS created_at TEXT')
        c.execute('ALTER TABLE answers ADD COLUMN IF NOT EXISTS updated_at TEXT')
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS answers_unique ON answers (question_id, username)')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS meeting (
                id SERIAL PRIMARY KEY, 
                meeting_date TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS banner (
                id SERIAL PRIMARY KEY, 
                image_data BYTEA, 
                filename TEXT, 
                mime_type TEXT, 
                uploaded_at TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS travels (
                id SERIAL PRIMARY KEY, 
                destination TEXT NOT NULL, 
                description TEXT, 
                travel_date TEXT,
                is_visited BOOLEAN DEFAULT FALSE, 
                created_by TEXT, 
                created_at TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS travel_photos (
                id SERIAL PRIMARY KEY, 
                travel_id INTEGER, 
                image_url TEXT NOT NULL,
                uploaded_by TEXT, 
                uploaded_at TEXT, 
                FOREIGN KEY(travel_id) REFERENCES travels(id)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS wishlist (
                id SERIAL PRIMARY KEY, 
                product_name TEXT NOT NULL, 
                product_link TEXT, 
                notes TEXT,
                created_by TEXT, 
                created_at TEXT, 
                is_purchased BOOLEAN DEFAULT FALSE
            )
        ''')
        c.execute("ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS priority TEXT CHECK (priority IN ('alta','media','baja')) DEFAULT 'media'")
        c.execute('ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS is_gift BOOLEAN DEFAULT FALSE')
        c.execute('ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS size TEXT')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS schedules (
                id SERIAL PRIMARY KEY, 
                username TEXT NOT NULL, 
                day TEXT NOT NULL,
                time TEXT NOT NULL, 
                activity TEXT, 
                color TEXT, 
                UNIQUE(username, day, time)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS locations (
                id SERIAL PRIMARY KEY, 
                username TEXT UNIQUE, 
                location_name TEXT,
                latitude REAL, 
                longitude REAL, 
                updated_at TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS profile_pictures (
                id SERIAL PRIMARY KEY, 
                username TEXT UNIQUE, 
                image_data BYTEA,
                filename TEXT, 
                mime_type TEXT, 
                uploaded_at TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS intimacy_events (
                id SERIAL PRIMARY KEY, 
                username TEXT NOT NULL, 
                ts TEXT NOT NULL, 
                place TEXT, 
                notes TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY, 
                value TEXT
            )
        ''')
        c.execute('''
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
                when_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','cancelled')),
                created_at TEXT NOT NULL,
                sent_at TEXT
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_sched_status_when ON scheduled_notifications (status, when_at)')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS media_items (
                id SERIAL PRIMARY KEY,
                title           TEXT        NOT NULL,
                cover_url       TEXT,
                link_url        TEXT,
                on_netflix      BOOLEAN     DEFAULT FALSE,
                on_prime        BOOLEAN     DEFAULT FALSE,
                priority        TEXT        CHECK (priority IN ('alta','media','baja')) DEFAULT 'media',
                comment         TEXT,
                created_by      TEXT,
                created_at      TEXT,
                is_watched      BOOLEAN     DEFAULT FALSE,
                watched_at      TEXT,
                rating          INTEGER     CHECK (rating BETWEEN 1 AND 5),
                watched_comment TEXT
            );
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_media_watched_prio ON media_items (is_watched, priority, created_at DESC);')
        c.execute('CREATE INDEX IF NOT EXISTS idx_media_watched_at   ON media_items (is_watched, watched_at DESC);')
        c.execute("ALTER TABLE media_items ADD COLUMN IF NOT EXISTS reviews JSONB DEFAULT '{}'::jsonb")
        c.execute('ALTER TABLE media_items ADD COLUMN IF NOT EXISTS avg_rating REAL')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS question_bank (
                id SERIAL PRIMARY KEY,
                text TEXT UNIQUE NOT NULL,
                used BOOLEAN NOT NULL DEFAULT FALSE,
                used_at TEXT,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TEXT NOT NULL
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_qbank_used_active ON question_bank (used, active)')
        c.execute('ALTER TABLE daily_questions ADD COLUMN IF NOT EXISTS bank_id INTEGER')
        
        c.execute('SELECT COUNT(*) FROM question_bank')
        if (c.fetchone()[0] or 0) == 0:
            now = now_madrid_str()
            for q in QUESTIONS:
                try:
                    c.execute('INSERT INTO question_bank (text, created_at) VALUES (%s,%s)', (q, now))
                except Exception:
                    pass
            conn.commit()
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                endpoint TEXT UNIQUE NOT NULL,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at TEXT
            )
        ''')
        
        try:
            c.execute('INSERT INTO users (username, password) VALUES (%s, %s) ON CONFLICT (username) DO NOTHING', ('mochito', '1234'))
            c.execute('INSERT INTO users (username, password) VALUES (%s, %s) ON CONFLICT (username) DO NOTHING', ('mochita', '1234'))
            now = now_madrid_str()
            c.execute('''
                INSERT INTO locations (username, location_name, latitude, longitude, updated_at)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING
            ''', ('mochito', 'Algemesí, Valencia', 39.1925, -.4353, now))
            c.execute('''
                INSERT INTO locations (username, location_name, latitude, longitude, updated_at)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING
            ''', ('mochita', 'Córdoba', 37.8882, -4.7794, now))
            conn.commit()
        except Exception as e:
            print('Seed error:', e)
            conn.rollback()
        
        c.execute('CREATE INDEX IF NOT EXISTS idx_answers_q_user ON answers (question_id, username)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_travels_vdate ON travels (is_visited, travel_date DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_wishlist_state_prio ON wishlist (is_purchased, priority, created_at DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_intim_user_ts ON intimacy_events (username, ts DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions (username)')
        
        c.execute('ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS track_price BOOLEAN DEFAULT FALSE')
        c.execute('ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS last_price_cents INTEGER')
        c.execute('ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS currency TEXT')
        c.execute('ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS last_checked TEXT')
        c.execute('ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS alert_drop_percent REAL')
        c.execute('ALTER TABLE wishlist ADD COLUMN IF NOT EXISTS alert_below_cents INTEGER')
        c.execute('CREATE INDEX IF NOT EXISTS idx_wishlist_track ON wishlist (track_price, is_purchased)')
        conn.commit()

init_db()

def _parse_dt(txt: str):
    if not txt:
        return
    try:
        dt = datetime.strptime(txt.strip(), '%Y-%m-%d %H:%M:%S')
    except Exception:
        return
    try:
        from zoneinfo import ZoneInfo
        return dt.replace(tzinfo=ZoneInfo('Europe/Madrid'))
    except Exception:
        if pytz:
            try:
                return pytz.timezone('Europe/Madrid').localize(dt)
            except Exception:
                pass
    return dt

def _reviews_dict(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except Exception:
            return {}
    return {}

def _compute_avg_from_reviews(rv: dict) -> float:
    vals = []
    for (u, obj) in (rv or {}).items():
        try:
            r = int((obj or {}).get('rating') or 0)
            if 1 <= r <= 5:
                vals.append(r)
        except Exception:
            pass
    return round(sum(vals) / len(vals), 1) if vals else 0.0

def _enrich_media_row(row, user):
    d = dict(row)
    rv = _reviews_dict(d.get('reviews') or {})
    other = other_of(user)
    
    def g(u, key):
        return (rv.get(u) or {}).get(key)
    
    d['rating_me'] = int(g(user, 'rating') or 0)
    d['rating_other'] = int(g(other, 'rating') or 0)
    d['comment_me'] = g(user, 'comment') or ''
    d['comment_other'] = g(other, 'comment') or ''
    d['avg_rating'] = d.get('avg_rating') or 0
    return d

@ttl_cache(seconds=60)
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
            'today_count': 0, 'month_total': 0, 'year_total': 0,
            'days_since_last': None, 'last_dt': None, 'streak_days': 0
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
        'today_count': today_count, 'month_total': month_total, 'year_total': year_total,
        'days_since_last': days_since_last, 'last_dt': last_dt, 'streak_days': streak_days
    }

@ttl_cache(seconds=30)
def get_intim_events(limit: int = 100):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('''
                SELECT id, username, ts, place, notes
                FROM intimacy_events
                WHERE username IN ('mochito','mochita')
                ORDER BY ts DESC LIMIT %s
            ''', (limit,))
            rows = c.fetchall()
            return [{
                'id': r[0], 'username': r[1], 'ts': r[2], 
                'place': r[3] or '', 'notes': r[4] or ''
            } for r in rows]
    finally:
        conn.close()

def require_admin():
    if 'username' not in session or session['username'] != 'mochito':
        abort(403)

def qbank_pick_random(conn) -> dict | None:
    with conn.cursor() as c:
        c.execute('''
            SELECT id, text
            FROM question_bank
            WHERE active=TRUE AND used=FALSE
            ORDER BY RANDOM()
            LIMIT 1
        ''')
        row = c.fetchone()
        return dict(row) if row else None

def qbank_mark_used(conn, qid: int, used: bool):
    with conn.cursor() as c:
        if used:
            c.execute('UPDATE question_bank SET used=TRUE, used_at=%s WHERE id=%s', (now_madrid_str(), qid))
        else:
            c.execute('UPDATE question_bank SET used=FALSE, used_at=NULL WHERE id=%s', (qid,))
    conn.commit()

def parse_datetime_local_to_madrid(dt_local: str) -> str | None:
    if not dt_local:
        return
    try:
        naive = datetime.strptime(dt_local.strip(), '%Y-%m-%dT%H:%M')
        return naive.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return

@ttl_cache(seconds=300)
def get_today_question(today: date | None = None):
    if today is None:
        today = today_madrid()
    today_str = today.isoformat()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('''
                SELECT id, question, bank_id
                FROM daily_questions
                WHERE TRIM(date)=%s
                ORDER BY id DESC
                LIMIT 1
            ''', (today_str,))
            row = c.fetchone()
            if row:
                return row['id'], row['question']
        
        picked = qbank_pick_random(conn)
        if not picked:
            return None, 'Ya no quedan preguntas sin usar. Añade más en Admin → Preguntas.'
        
        with conn.cursor() as c:
            c.execute('''
                INSERT INTO daily_questions (question, date, bank_id)
                VALUES (%s,%s,%s)
                RETURNING id
            ''', (picked['text'], today_str, picked['id']))
            qid = c.fetchone()[0]
            conn.commit()
        
        qbank_mark_used(conn, picked['id'], True)
        return qid, picked['text']
    finally:
        conn.close()

def reroll_today_question() -> bool:
    today = today_madrid()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('''
                SELECT id, bank_id
                FROM daily_questions
                WHERE date=%s
                ORDER BY id DESC
                LIMIT 1
            ''', (today.isoformat(),))
            row = c.fetchone()
            if not row:
                return False
            dq_id, old_bank_id = row['id'], row['bank_id']
        
        if old_bank_id:
            qbank_mark_used(conn, old_bank_id, False)
        
        newq = qbank_pick_random(conn)
        if not newq:
            if old_bank_id:
                qbank_mark_used(conn, old_bank_id, True)
            return False
        
        with conn.cursor() as c:
            c.execute('DELETE FROM answers WHERE question_id=%s', (dq_id,))
            c.execute('UPDATE daily_questions SET question=%s, bank_id=%s WHERE id=%s', 
                     (newq['text'], newq['id'], dq_id))
            conn.commit()
        
        qbank_mark_used(conn, newq['id'], True)
        try:
            broadcast('dq_change', {'id': int(dq_id)})
        except Exception:
            pass
        return True
    finally:
        conn.close()

def days_together():
    return max((today_madrid() - RELATION_START).days, 0)

@ttl_cache(seconds=60)
def days_until_meeting():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('SELECT meeting_date FROM meeting ORDER BY id DESC LIMIT 1')
            row = c.fetchone()
            if not row:
                return
            meeting_date = datetime.strptime(row[0], '%Y-%m-%d').date()
            return (meeting_date - today_madrid()).days
    finally:
        conn.close()

@ttl_cache(seconds=300)
def get_banner():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('SELECT image_data, mime_type FROM banner ORDER BY id DESC LIMIT 1')
            row = c.fetchone()
            if row:
                return f"data:{row[1]};base64,{b64encode(row[0]).decode('utf-8')}"
            return
    finally:
        conn.close()

@ttl_cache(seconds=300)
def get_profile_pictures():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('SELECT username, image_data, mime_type FROM profile_pictures')
            pics = {}
            for (u, img, mt) in c.fetchall():
                pics[u] = f"data:{mt};base64,{b64encode(img).decode('utf-8')}"
            return pics
    finally:
        conn.close()

@ttl_cache(seconds=60)
def compute_streaks():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('''
                SELECT dq.id, dq.date, COUNT(DISTINCT a.username) AS cnt
                FROM daily_questions dq
                LEFT JOIN answers a 
                  ON a.question_id = dq.id
                 AND a.username IN ('mochito','mochita')
                GROUP BY dq.id, dq.date
                ORDER BY dq.date ASC
            ''')
            rows = c.fetchall()
    finally:
        conn.close()
    
    if not rows:
        return 0, 0
    
    def parse_d(dtxt):
        return datetime.strptime(dtxt, '%Y-%m-%d').date()
    
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
            latest = d
            break
    
    if latest is None:
        return 0, best
    
    cur = 1
    d = latest - timedelta(days=1)
    sset = set(compl)
    while d in sset:
        cur += 1
        d -= timedelta(days=1)
    
    return cur, best

VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '').strip()
VAPID_PUBLIC_KEY_HEX = os.environ.get('VAPID_PUBLIC_KEY', '').strip()
VAPID_SUBJECT = os.environ.get('VAPID_SUBJECT', 'mailto:admin@example.com').strip()

def hex_to_base64url(hexstr: str) -> str:
    try:
        b = binascii.unhexlify(hexstr)
        s = base64.urlsafe_b64encode(b).decode().rstrip('=')
        return s
    except Exception:
        return ''

def get_vapid_public_base64url() -> str:
    return hex_to_base64url(VAPID_PUBLIC_KEY_HEX)

@ttl_cache(seconds=300)
def get_subscriptions_for(user: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE username=%s', (user,))
            return [{
                'id': r[0], 'endpoint': r[1], 
                'keys': {'p256dh': r[2], 'auth': r[3]}
            } for r in c.fetchall()]
    finally:
        conn.close()

def _delete_subscription_by_endpoint(endpoint: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('DELETE FROM push_subscriptions WHERE endpoint=%s', (endpoint,))
            conn.commit()
    finally:
        conn.close()

def send_push_raw(sub: dict, payload: dict):
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY_HEX:
        return False
    try:
        webpush(
            subscription_info={
                'endpoint': sub['endpoint'],
                'keys': {'p256dh': sub['keys']['p256dh'], 'auth': sub['keys']['auth']}
            },
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={'sub': VAPID_SUBJECT},
            timeout=5
        )
        return True
    except WebPushException as e:
        status = getattr(e.response, 'status_code', None)
        if status in (404, 410):
            try:
                _delete_subscription_by_endpoint(sub['endpoint'])
            except Exception:
                pass
        print(f"[push] error: {e}")
        return False
    except Exception as e:
        print(f"[push] error gen: {e}")
        return False

def send_push_to(user: str, title: str, body: str | None = None, data: dict | None = None, 
                url: str | None = None, tag: str | None = None, icon: str | None = None, 
                badge: str | None = None):
    subs = get_subscriptions_for(user)
    payload = {'title': title}
    if body is not None and body != '':
        payload['body'] = body
    if url:
        payload['url'] = url
    if tag:
        payload['tag'] = tag
    if icon:
        payload['icon'] = icon
    if badge:
        payload['badge'] = badge
    if data:
        payload['data'] = data
    
    ok = False
    for sub in subs:
        ok = send_push_raw(sub, payload) or ok
    return ok

def send_push_both(title: str, body: str | None = None, **kw):
    send_push_to('mochito', title, body, **kw)
    send_push_to('mochita', title, body, **kw)

def push_wishlist_notice(creator: str, is_gift: bool):
    other = 'mochita' if creator == 'mochito' else 'mochito'
    if is_gift:
        title = '🎁 Nuevo regalo'
        body = 'Tu pareja ha añadido un regalo (oculto) a la lista.'
        tag = 'wishlist-gift'
    else:
        title = '🛍️ Nuevo deseo'
        body = 'Se ha añadido un artículo a la lista de deseos.'
        tag = 'wishlist-item'
    send_push_to(other, title, body, url='/#wishlist', tag=tag)

def push_answer_notice(responder: str):
    other = 'mochita' if responder == 'mochito' else 'mochito'
    who = 'tu pareja' if responder in ('mochito', 'mochita') else responder
    send_push_to(other, 
        title='Pregunta del día',
        body=f"{who} ha respondido la pregunta de hoy. ¡Mírala! 💬",
        url='/#pregunta',
        tag='dq-answer'
    )

def push_daily_new_question():
    send_push_both(
        title='Nueva pregunta del día',
        body='Ya puedes responder la pregunta de hoy. ¡Mantén la racha! 🔥',
        url='/#pregunta',
        tag='dq-new'
    )

def push_last_hours(user: str):
    send_push_to(user,
        title='Últimas horas para contestar',
        body='Te quedan pocas horas para responder la pregunta de hoy.',
        url='/#pregunta',
        tag='dq-last-hours'
    )

def push_meeting_countdown(dleft: int):
    title = f"¡Queda {dleft} día para veros!" if dleft == 1 else f"¡Quedan {dleft} días para veros!"
    send_push_both(
        title=title,
        body='Que ganaaasssss!! ❤️\u200d🔥',
        url='/#contador',
        tag='meeting-countdown'
    )

def push_relationship_day(day_count: int):
    title = f"💖 Hoy cumplís {day_count} días juntos"
    body = 'Gracias por estar cada dia a mi lado.'
    tag = f"relationship-{day_count}"
    send_push_both(title=title, body=body, url='/#contador', tag=tag)
    send_discord('Relationship milestone', {'days': day_count})

def push_relationship_milestones():
    days = days_together()
    milestones = [75, 100, 150, 200, 250, 300, 365]
    if days in milestones:
        key = f"milestone_{days}"
        if not state_get(key, ''):
            push_relationship_day(days)
            state_set(key, '1')

def push_relationship_daily():
    days = days_together()
    if days <= 0:
        return
    milestones = {75, 100, 150, 200, 250, 300, 365}
    if days in milestones:
        return
    key = f"rel_daily::{today_madrid().isoformat()}"
    if not state_get(key, ''):
        send_push_both(
            title=f"💖 Hoy cumplís {days} días juntos",
            body='Un día más, y cada vez mejor 🥰',
            url='/#contador',
            tag=f"relationship-day-{days}"
        )
        state_set(key, '1')

def push_gift_purchased(row):
    created_by = row.get('created_by')
    other = other_of(created_by)
    pname = row.get('product_name', 'Regalo')
    send_push_to(other,
        title='🎁 Tu regalo está listo',
        body='Tu pareja ha marcado como comprado un regalo 💝',
        url='/#wishlist',
        tag=f"gift-purchased-{row['id']}"
    )
    send_discord('Gift purchased', {
        'id': row['id'], 'by': created_by, 'name': pname
    })

def state_get(key: str, default: str | None = None) -> str | None:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('SELECT value FROM app_state WHERE key=%s', (key,))
            row = c.fetchone()
            return row[0] if row else default
    finally:
        conn.close()

def state_set(key: str, value: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('''
                INSERT INTO app_state (key,value) VALUES (%s,%s)
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
            ''', (key, value))
            conn.commit()
    finally:
        conn.close()

def _meet_times_key(day: date):
    return f"meet_times::{day.isoformat()}"

def _meet_sent_key(day: date, hhmm: str):
    return f"meet_sent::{day.isoformat()}::{hhmm}"

def ensure_meet_times(day: date):
    key = _meet_times_key(day)
    raw = state_get(key, '')
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
    key = _rel_daily_time_key(day)
    raw = (state_get(key, '') or '').strip()
    if raw == '-':
        return
    if re.fullmatch('\\d{2}:\\d{2}', raw or ''):
        return raw
    
    start_min = 540
    end_min = 1439
    if now_md.date() == day:
        now_min = now_md.hour * 60 + now_md.minute
        low = max(start_min, now_min)
    else:
        low = start_min
    
    if low > end_min:
        state_set(key, '-')
        return
    
    pick = random.randint(low, end_min)
    hh, mm = divmod(pick, 60)
    hhmm = f"{hh:02d}:{mm:02d}"
    state_set(key, hhmm)
    return hhmm

def maybe_push_relationship_daily_windowed(now_md: datetime):
    dcount = days_together()
    if dcount <= 0:
        return
    milestones = {75, 100, 150, 200, 250, 300, 365}
    if dcount in milestones:
        return
    
    day = now_md.date()
    hhmm = ensure_rel_daily_time(day, now_md)
    if not hhmm:
        return
    
    sent_key = _rel_daily_sent_key(day, hhmm)
    if state_get(sent_key, ''):
        return
    
    if is_due_or_overdue(now_md, hhmm, 720):
        send_push_both(
            title=f"💖 Hoy cumplís {dcount} días juntos",
            body='Un día más, y cada vez mejor 🥰',
            url='/#contador',
            tag=f"relationship-day-{dcount}"
        )
        state_set(sent_key, '1')

def due_now(now_madrid: datetime, hhmm: str) -> bool:
    try:
        hh, mm = map(int, hhmm.split(':'))
    except Exception:
        return False
    scheduled = now_madrid.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return abs((now_madrid - scheduled).total_seconds()) <= 90

PRICE_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36'

def _to_cents_from_str(s: str) -> int | None:
    if not s:
        return
    s = s.strip()
    s_plain = re.sub('[^\\d,.\\-]', '', s)
    if not s_plain:
        return
    
    if ',' in s_plain and '.' in s_plain:
        last = max(s_plain.rfind(','), s_plain.rfind('.'))
        int_part = re.sub('[.,]', '', s_plain[:last])
        frac = s_plain[last+1:last+3].ljust(2, '0')
        num = f"{int_part}.{frac}"
    elif ',' in s_plain:
        parts = s_plain.split(',')
        if len(parts[-1]) in (1, 2):
            num = f"{''.join(parts[:-1])}.{parts[-1].ljust(2, '0')}"
        else:
            num = s_plain.replace(',', '')
    else:
        num = s_plain
    
    try:
        return int(round(float(num) * 100))
    except Exception:
        return

def _find_in_jsonld(html: str):
    for m in re.finditer('<script[^>]+type=["\\\']application/ld\\+json["\\\'][^>]*>(.*?)</script>', html, flags=re.S | re.I):
        blob = m.group(1)
        pm = re.search('"price"\\s*:\\s*"?(?P<p>[\\d.,]+)"?', blob)
        cm = re.search('"priceCurrency"\\s*:\\s*"?(?P<c>[A-Z]{3})"?', blob)
        if pm:
            cents = _to_cents_from_str(pm.group('p'))
            curr = (cm.group('c') if cm else None) or ('EUR' if '€' in html else None)
            if cents:
                return cents, curr
    return None, None

def _meta_price(soup):
    for selector in [
        ('meta', {'property': 'product:price:amount'}),
        ('meta', {'property': 'og:price:amount'}),
        ('meta', {'name': 'twitter:data1'}),
        ('meta', {'itemprop': 'price'})
    ]:
        tag = soup.find(*selector)
        val = tag.get('content') if tag else None
        if val:
            cents = _to_cents_from_str(val)
            if cents:
                return cents
    
    tag = soup.find(attrs=lambda at: at and any('price' in k for k in at.keys()))
    if tag:
        for (k, v) in tag.attrs.items():
            if 'price' in k and v:
                cents = _to_cents_from_str(str(v))
                if cents:
                    return cents

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
                c.execute('''
                    CREATE TABLE IF NOT EXISTS user_presence (
                        username    text PRIMARY KEY,
                        last_seen   timestamptz NOT NULL,
                        device      text,
                        user_agent  text
                    );
                ''')
                conn.commit()
                _presence_ready = True
        finally:
            conn.close()

def other_of(u: str) -> str:
    if u == 'mochito':
        return 'mochita'
    if u == 'mochita':
        return 'mochito'
    return 'mochita'

def fetch_price(url: str) -> tuple[int | None, str | None, str | None]:
    try:
        resp = requests.get(url, timeout=8, headers={'User-Agent': PRICE_UA})
        html = resp.text
        c_jsonld, curr = _find_in_jsonld(html)
        cents_dom = None
        soup = BeautifulSoup(html, 'html.parser') if BeautifulSoup else None
        if soup:
            cents_dom = _meta_price(soup)
            title = soup.title.text.strip() if soup.title else None
        else:
            title = None
        
        cents_rx = None
        for pat in ['€\\s*\\d[\\d\\.\\,]*', '\\d[\\d\\.\\,]*\\s*€', '\\b\\d{1,3}(?:[.,]\\d{3})*(?:[.,]\\d{2})\\b']:
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
    if cents is None:
        return '—'
    euros = cents / 100
    return f"{euros:,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.')

def notify_price_drop(row, old_cents: int, new_cents: int):
    wid = row['id']
    pname = row['product_name']
    link = row.get('product_link')
    created_by = row.get('created_by')
    is_gift = bool(row.get('is_gift'))
    drop = old_cents - new_cents
    pct = drop / old_cents * 100 if old_cents else 0.0
    title = '⬇️ ¡Bajada de precio!'
    body = f"{pname}\nDe {fmt_eur(old_cents)} a {fmt_eur(new_cents)} (−{pct:.1f}%)."
    tag = f"price-drop-{wid}"
    
    try:
        if is_gift and created_by in ('mochito', 'mochita'):
            send_push_to(created_by, title=title, body=body, url=link, tag=tag)
        else:
            send_push_both(title=title, body=body, url=link, tag=tag)
    except Exception as e:
        print('[push price]', e)
    
    send_discord('Price drop', {
        'wid': int(wid), 'name': pname, 'old': old_cents, 'new': new_cents,
        'pct': round(pct, 1), 'is_gift': is_gift, 'created_by': created_by
    })

def form_or_json(*keys, default=None):
    j = request.get_json(silent=True) or {}
    for k in keys:
        if k in request.form:
            return request.form.get(k)
        if k in j:
            return j.get(k)
    return default

def sweep_price_checks(max_items: int = 6, min_age_minutes: int = 180):
    now_txt = now_madrid_str()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('''
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
            ''', (max_items,))
            rows = c.fetchall()
        
        for row in rows:
            wid = row['id']
            last_checked = _parse_dt(row['last_checked'] or '')
            if last_checked:
                from datetime import timedelta as _td
                if europe_madrid_now() - last_checked < _td(minutes=min_age_minutes):
                    continue
            
            price_cents, curr, _title = fetch_price(row['product_link'])
            if not price_cents:
                with conn.cursor() as c2:
                    c2.execute('UPDATE wishlist SET last_checked=%s WHERE id=%s', (now_txt, wid))
                    conn.commit()
                continue
            
            old = row['last_price_cents']
            notify = False
            if old is None:
                pass
            elif price_cents < old:
                drop_pct = (old - price_cents) * 100 / old if old else 0
                if row['alert_below_cents'] is not None and price_cents <= int(row['alert_below_cents']):
                    notify = True
                elif row['alert_pct'] and drop_pct < float(row['alert_pct']):
                    notify = False
                else:
                    notify = True
            
            with conn.cursor() as c3:
                c3.execute('''
                    UPDATE wishlist
                    SET last_price_cents=%s,
                        currency=%s,
                        last_checked=%s
                    WHERE id=%s
                ''', (price_cents, curr or row['currency'] or 'EUR', now_txt, wid))
                conn.commit()
            
            if notify and old is not None:
                notify_price_drop(row, old, price_cents)
    finally:
        conn.close()

# Background jobs optimizados
def optimized_background_loop():
    print('[bg] scheduler optimizado iniciado')
    
    price_check_interval = 3600  # 1 hora
    push_check_interval = 60     # 1 minuto
    rel_daily_interval = 300     # 5 minutos
    
    last_price_check = 0
    last_push_check = 0
    last_rel_daily = 0
    
    while True:
        try:
            now_time = time.time()
            current_time = europe_madrid_now()
            
            # Verificar notificaciones programadas cada minuto
            if now_time - last_push_check >= push_check_interval:
                _check_scheduled_notifications()
                last_push_check = now_time
            
            # Verificar relación diaria cada 5 minutos
            if now_time - last_rel_daily >= rel_daily_interval:
                _check_relationship_daily(current_time)
                last_rel_daily = now_time
            
            # Verificar precios cada hora
            if now_time - last_price_check >= price_check_interval:
                _check_prices()
                last_price_check = now_time
            
            # Verificar pregunta del día una vez al día
            _check_daily_question(current_time)
            
            # Verificar meeting countdown
            _check_meeting_countdown(current_time)
            
        except Exception as e:
            print(f"[bg error] {e}")
        
        time.sleep(30)

def _check_scheduled_notifications():
    """Verificar notificaciones programadas de forma eficiente"""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('''
                SELECT id, target, title, body, url, tag, icon, badge, when_at
                FROM scheduled_notifications 
                WHERE status='pending' AND when_at <= %s
                LIMIT 10
            ''', (now_madrid_str(),))
            
            for row in c.fetchall():
                target = row['target']
                if target == 'both':
                    send_push_both(
                        title=row['title'], 
                        body=row['body'] or None,
                        url=row['url'], 
                        tag=row['tag'],
                        icon=row['icon'], 
                        badge=row['badge']
                    )
                else:
                    send_push_to(
                        target,
                        title=row['title'], 
                        body=row['body'] or None,
                        url=row['url'], 
                        tag=row['tag'],
                        icon=row['icon'], 
                        badge=row['badge']
                    )
                
                with conn.cursor() as c2:
                    c2.execute('''
                        UPDATE scheduled_notifications
                        SET status='sent', sent_at=%s
                        WHERE id=%s
                    ''', (now_madrid_str(), row['id']))
                    conn.commit()
                
                send_discord('Admin: push sent (scheduled)', {
                    'id': int(row['id']), 'title': row['title']
                })
    finally:
        conn.close()

def _check_relationship_daily(now_md):
    """Verificar notificación diaria de relación"""
    try:
        maybe_push_relationship_daily_windowed(now_md)
        push_relationship_milestones()
    except Exception as e:
        print('[bg rel_daily]', e)

def _check_prices():
    """Verificar precios"""
    try:
        last_sweep = state_get('last_price_sweep', '')
        do = True
        if last_sweep:
            dt = _parse_dt(last_sweep)
            if dt:
                from datetime import timedelta as _td
                do = europe_madrid_now() - dt >= _td(hours=3)
        if do:
            sweep_price_checks(max_items=6, min_age_minutes=180)
            state_set('last_price_sweep', now_madrid_str())
    except Exception as e:
        print('[bg price sweep]', e)

def _check_daily_question(current_time):
    """Verificar pregunta del día"""
    today = current_time.date()
    last_dq_push = state_get('last_dq_push_date', '')
    if str(today) != last_dq_push:
        qid, _ = get_today_question(today)
        if qid:
            push_daily_new_question()
            state_set('last_dq_push_date', str(today))
            cache_invalidate('compute_streaks')
        else:
            send_discord('Daily question skipped (empty bank)', {'date': str(today)})

def _check_meeting_countdown(current_time):
    """Verificar countdown de meeting"""
    try:
        d = days_until_meeting()
        today = current_time.date()
        if d is not None and d in (1, 2, 3):
            times = ensure_meet_times(today)
            for hhmm in times:
                sent_key = _meet_sent_key(today, hhmm)
                if not state_get(sent_key, '') and is_due_or_overdue(current_time, hhmm, 720):
                    push_meeting_countdown(d)
                    state_set(sent_key, '1')
        else:
            state_set(_meet_times_key(today), json.dumps([]))
    except Exception as e:
        print('[bg meeting countdown]', e)

@app.route('/', methods=['GET', 'POST'])
def index():
    if 'username' not in session:
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '').strip()
            conn = get_db_connection()
            try:
                with conn.cursor() as c:
                    c.execute('SELECT password FROM users WHERE username=%s', (username,))
                    row = c.fetchone()
                    if not row:
                        send_discord('Login FAIL', {'username_intent': username, 'reason': 'user_not_found'})
                        return render_template('index.html', error='Usuario o contraseña incorrecta', profile_pictures={})
                    
                    stored = row[0]
                    if _is_hashed(stored):
                        ok = check_password_hash(stored, password)
                        mode = 'hashed'
                    else:
                        ok = stored == password
                        mode = 'plaintext'
                    
                    if ok:
                        session['username'] = username
                        send_discord('Login OK', {'username': username, 'mode': mode})
                        return redirect('/')
                    else:
                        send_discord('Login FAIL', {'username_intent': username, 'reason': 'bad_password', 'mode': mode})
                        return render_template('index.html', error='Usuario o contraseña incorrecta', profile_pictures={})
            finally:
                conn.close()
        return render_template('index.html', error=None, profile_pictures={})
    
    user = session.get('username')
    
    # Touch presence de forma asíncrona para no bloquear
    try:
        threading.Thread(target=touch_presence, args=(user, 'page-view'), daemon=True).start()
    except Exception as e:
        app.logger.warning('touch_presence failed: %s', e)
    
    question_id, question_text = get_today_question()
    conn = get_db_connection()
    
    try:
        with conn.cursor() as c:
            # Manejo de formularios POST
            if request.method == 'POST':
                if 'update_profile' in request.form and 'profile_picture' in request.files:
                    file = request.files['profile_picture']
                    if file and file.filename:
                        image_data = file.read()
                        filename = secure_filename(file.filename)
                        mime_type = file.mimetype
                        c.execute('''
                            INSERT INTO profile_pictures (username, image_data, filename, mime_type, uploaded_at)
                            VALUES (%s,%s,%s,%s,%s)
                            ON CONFLICT (username) DO UPDATE
                            SET image_data=EXCLUDED.image_data, filename=EXCLUDED.filename,
                                mime_type=EXCLUDED.mime_type, uploaded_at=EXCLUDED.uploaded_at
                        ''', (user, image_data, filename, mime_type, now_madrid_str()))
                        conn.commit()
                        flash('Foto de perfil actualizada ✅', 'success')
                        send_discord('Profile picture updated', {'user': user, 'filename': filename})
                        cache_invalidate('get_profile_pictures')
                        broadcast('profile_update', {'user': user})
                    return redirect('/')
                
                elif 'change_password' in request.form:
                    current_password = request.form.get('current_password', '').strip()
                    new_password = request.form.get('new_password', '').strip()
                    confirm_password = request.form.get('confirm_password', '').strip()
                    
                    if not current_password or not new_password or not confirm_password:
                        flash('Completa todos los campos de contraseña.', 'error')
                        return redirect('/')
                    if new_password != confirm_password:
                        flash('La nueva contraseña y la confirmación no coinciden.', 'error')
                        return redirect('/')
                    if len(new_password) < 4:
                        flash('La nueva contraseña debe tener al menos 4 caracteres.', 'error')
                        return redirect('/')
                    
                    c.execute('SELECT password FROM users WHERE username=%s', (user,))
                    row = c.fetchone()
                    if not row:
                        flash('Usuario no encontrado.', 'error')
                        return redirect('/')
                    
                    stored = row[0]
                    valid_current = check_password_hash(stored, current_password) if _is_hashed(stored) else stored == current_password
                    if not valid_current:
                        flash('La contraseña actual no es correcta.', 'error')
                        send_discord('Change password FAIL', {'user': user, 'reason': 'wrong_current'})
                        return redirect('/')
                    
                    new_hash = generate_password_hash(new_password)
                    c.execute('UPDATE users SET password=%s WHERE username=%s', (new_hash, user))
                    conn.commit()
                    flash('Contraseña cambiado correctamente 🎉', 'success')
                    send_discord('Change password OK', {'user': user})
                    return redirect('/')
                
                elif 'answer' in request.form:
                    answer = request.form['answer'].strip()
                    if question_id is not None and answer:
                        now_txt = datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'
                        c.execute('SELECT id, answer, created_at, updated_at FROM answers WHERE question_id=%s AND username=%s', (question_id, user))
                        prev = c.fetchone()
                        if not prev:
                            c.execute('''
                                INSERT INTO answers (question_id, username, answer, created_at, updated_at)
                                VALUES (%s,%s,%s,%s,%s) ON CONFLICT (question_id, username) DO NOTHING
                            ''', (question_id, user, answer, now_txt, now_txt))
                            conn.commit()
                            send_discord('Answer submitted', {'user': user, 'question_id': question_id})
                        else:
                            prev_id, prev_text, prev_created, prev_updated = prev
                            if answer != (prev_text or ''):
                                if prev_created is None:
                                    c.execute('UPDATE answers SET answer=%s, updated_at=%s, created_at=%s WHERE id=%s', 
                                             (answer, now_txt, prev_updated or now_txt, prev_id))
                                else:
                                    c.execute('UPDATE answers SET answer=%s, updated_at=%s WHERE id=%s', 
                                             (answer, now_txt, prev_id))
                                conn.commit()
                                send_discord('Answer edited', {'user': user, 'question_id': question_id})
                        
                        cache_invalidate('compute_streaks')
                        broadcast('dq_answer', {'user': user})
                        try:
                            push_answer_notice(user)
                        except Exception as e:
                            print('[push answer] ', e)
                    return redirect('/')
                
                elif 'dq_reroll' in request.form:
                    ok = reroll_today_question()
                    if ok:
                        cache_invalidate('compute_streaks')
                        flash('Pregunta cambiada para hoy ✅', 'success')
                        send_discord('DQ reroll', {'user': user})
                    else:
                        flash('No quedan preguntas disponibles para cambiar 😅', 'error')
                    return redirect('/')
                
                elif 'meeting_date' in request.form:
                    meeting_date = request.form['meeting_date']
                    c.execute('INSERT INTO meeting (meeting_date) VALUES (%s)', (meeting_date,))
                    conn.commit()
                    flash('Fecha actualizada 📅', 'success')
                    send_discord('Meeting date updated', {'user': user, 'date': meeting_date})
                    broadcast('meeting_update', {'date': meeting_date})
                    return redirect('/')
                
                elif 'banner' in request.files:
                    file = request.files['banner']
                    if file and file.filename:
                        image_data = file.read()
                        filename = secure_filename(file.filename)
                        mime_type = file.mimetype
                        c.execute('INSERT INTO banner (image_data, filename, mime_type, uploaded_at) VALUES (%s,%s,%s,%s)', (image_data, filename, mime_type, now_madrid_str()))
                        conn.commit()
                        flash('Banner actualizado 🖼️', 'success')
                        send_discord('Banner updated', {'user': user, 'filename': filename})
                        cache_invalidate('get_banner')
                        broadcast('banner_update', {'by': user})
                    return redirect('/')
                
                elif 'travel_destination' in request.form:
                    destination = request.form['travel_destination'].strip()
                    description = request.form.get('travel_description', '').strip()
                    travel_date = request.form.get('travel_date', '')
                    is_visited = 'travel_visited' in request.form
                    if destination:
                        c.execute('''
                            INSERT INTO travels (destination, description, travel_date, is_visited, created_by, created_at)
                            VALUES (%s,%s,%s,%s,%s,%s)
                        ''', (destination, description, travel_date, is_visited, user, now_madrid_str()))
                        conn.commit()
                        flash('Viaje añadido ✈️', 'success')
                        send_discord('Travel added', {'user': user, 'dest': destination, 'visited': is_visited})
                        broadcast('travel_update', {'type': 'add'})
                    return redirect('/')
                
                elif 'travel_photo_url' in request.form:
                    travel_id = request.form.get('travel_id')
                    image_url = request.form['travel_photo_url'].strip()
                    if image_url and travel_id:
                        c.execute('''
                            INSERT INTO travel_photos (travel_id, image_url, uploaded_by, uploaded_at)
                            VALUES (%s,%s,%s,%s)
                        ''', (travel_id, image_url, user, now_madrid_str()))
                        conn.commit()
                        flash('Foto añadida 📸', 'success')
                        send_discord('Travel photo added', {'user': user, 'travel_id': travel_id})
                        broadcast('travel_update', {'type': 'photo_add', 'id': int(travel_id)})
                    return redirect('/')
                
                elif 'intim_unlock_pin' in request.form:
                    pin_try = request.form.get('intim_pin', '').strip()
                    if pin_try == INTIM_PIN:
                        session['intim_unlocked'] = True
                        flash('Módulo Intimidad desbloqueado ✅', 'success')
                        send_discord('Intimidad unlock OK', {'user': user})
                    else:
                        session.pop('intim_unlocked', None)
                        flash('PIN incorrecto ❌', 'error')
                        send_discord('Intimidad unlock FAIL', {'user': user})
                    return redirect('/')
                
                elif 'intim_lock' in request.form:
                    session.pop('intim_unlocked', None)
                    flash('Módulo Intimidad ocultado 🔒', 'info')
                    return redirect('/')
                
                elif 'intim_register' in request.form:
                    if not session.get('intim_unlocked'):
                        flash('Debes desbloquear con PIN para registrar.', 'error')
                        return redirect('/')
                    place = (request.form.get('intim_place') or '').strip()
                    notes = (request.form.get('intim_notes') or '').strip()
                    now_txt = now_madrid_str()
                    with conn.cursor() as c2:
                        c2.execute('INSERT INTO intimacy_events (username, ts, place, notes) VALUES (%s,%s,%s,%s)', (user, now_txt, place or None, notes or None))
                        conn.commit()
                    flash('Momento registrado ❤️', 'success')
                    send_discord('Intimidad registered', {'user': user, 'place': place, 'notes_len': len(notes)})
                    broadcast('intim_update', {'type': 'add'})
                    return redirect('/')
                
                # Wishlist
                data = request.get_json(silent=True) or request.form
                def g(*keys, default=''):
                    for k in keys:
                        v = data.get(k)
                        if v is not None:
                            return v.strip() if isinstance(v, str) else v
                    return default
                
                product_name = g('product_name', 'productName')
                product_link = g('product_link', 'url', 'link')
                notes = g('wishlist_notes', 'notes', 'description')
                size = g('size', 'talla')
                priority = (g('priority') or 'media').lower()
                is_gift = str(g('is_gift', 'isGift', 'gift')).lower() in ('1', 'true', 'on', 'yes', 'si', 'sí')
                track_price = str(g('track_price', 'trackPrice')).lower() in ('1', 'true', 'on', 'yes', 'si', 'sí')
                alert_drop_percent = g('alert_drop_percent', 'alertDropPercent')
                alert_below_price = g('alert_below_price', 'alertBelowPrice')
                
                if priority not in ('alta', 'media', 'baja'):
                    priority = 'media'
                
                def euros_to_cents(v):
                    v = (v or '').strip()
                    if not v:
                        return
                    return _to_cents_from_str(v.replace('€', ''))
                
                alert_below_cents = euros_to_cents(alert_below_price)
                alert_pct = float(alert_drop_percent) if alert_drop_percent else None
                
                if product_name:
                    c.execute('''
                        INSERT INTO wishlist
                            (product_name, product_link, notes, size, created_by, created_at,
                            is_purchased, priority, is_gift,
                            track_price, alert_drop_percent, alert_below_cents)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ''', (product_name, product_link, notes, size, user, now_madrid_str(), False, priority, is_gift, track_price, alert_pct, alert_below_cents))
                    conn.commit()
                    flash('Producto añadido 🛍️', 'success')
                    send_discord('Wishlist added (compat)', {
                        'user': user, 'name': product_name, 'priority': priority, 
                        'is_gift': is_gift, 'size': size, 'track_price': track_price
                    })
                    broadcast('wishlist_update', {'type': 'add'})
                    try:
                        push_wishlist_notice(user, is_gift)
                    except Exception as e:
                        print('[push wishlist] ', e)
                    if request.is_json or request.headers.get('Accept', '').startswith('application/json'):
                        return jsonify({'ok': True})
                    return redirect('/')
            
            # Cargar datos necesarios para GET
            c.execute('SELECT username, answer, created_at, updated_at FROM answers WHERE question_id=%s', (question_id,))
            ans_rows = c.fetchall()
            
            # Limitar consultas pesadas
            c.execute('''
                SELECT id, destination, description, travel_date, is_visited, created_by
                FROM travels
                ORDER BY is_visited, travel_date DESC
                LIMIT 50
            ''')
            travels = c.fetchall()
            
            c.execute('SELECT travel_id, id, image_url, uploaded_by FROM travel_photos ORDER BY id DESC LIMIT 100')
            all_ph = c.fetchall()
            
            c.execute('''
                SELECT
                    id, product_name, product_link, notes, created_by,
                    created_at, is_purchased, priority, is_gift, size
                FROM wishlist
                ORDER BY is_purchased ASC,
                         CASE COALESCE(priority,'media')
                             WHEN 'alta' THEN 0
                             WHEN 'media' THEN 1
                             ELSE 2
                         END,
                         created_at DESC
                LIMIT 100
            ''')
            wl_rows = c.fetchall()
            
            # Media items
            c.execute('''
                SELECT
                id, title, cover_url, link_url, on_netflix, on_prime,
                comment, priority,
                created_by, created_at,
                reviews,
                avg_rating
                FROM media_items
                WHERE is_watched = FALSE
                ORDER BY
                CASE priority WHEN 'alta' THEN 0 WHEN 'media' THEN 1 ELSE 2 END,
                COALESCE(created_at, '1970-01-01') DESC,
                id DESC
            ''')
            media_to_watch = c.fetchall()
            
            c.execute('''
                SELECT
                id, title, cover_url, link_url,
                reviews,
                avg_rating,
                watched_at,
                created_by
                FROM media_items
                WHERE is_watched = TRUE
                ORDER BY COALESCE(watched_at, '1970-01-01') DESC, id DESC
            ''')
            media_watched = c.fetchall()
            
    finally:
        conn.close()
    
    # Procesar datos
    answers = [(r[0], r[1]) for r in ans_rows]
    answers_created_at = {r[0]: r[2] for r in ans_rows}
    answers_updated_at = {r[0]: r[3] for r in ans_rows}
    answers_edited = {r[0]: r[2] is not None and r[3] is not None and r[2] != r[3] for r in ans_rows}
    
    other_user = 'mochita' if user == 'mochito' else 'mochito'
    dict_ans = {u: a for (u, a) in answers}
    user_answer, other_answer = dict_ans.get(user), dict_ans.get(other_user)
    show_answers = user_answer is not None and other_answer is not None
    
    travel_photos_dict = {}
    for (tr_id, pid, url, up) in all_ph:
        travel_photos_dict.setdefault(tr_id, []).append({'id': pid, 'url': url, 'uploaded_by': up})
    
    # Cargar datos cacheados
    banner_file = get_banner()
    profile_pictures = get_profile_pictures()
    current_streak, best_streak = compute_streaks()
    intim_unlocked = bool(session.get('intim_unlocked'))
    intim_stats = get_intim_stats()
    intim_events = get_intim_events(100) if intim_unlocked else []
    
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
        wishlist_items=wl_rows,
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
        media_watched=media_watched
    )

# Rutas adicionales (mantenidas por brevedad, pero optimizadas)
@app.route('/delete_travel', methods=['POST'])
def delete_travel():
    if 'username' not in session:
        return redirect('/')
    try:
        travel_id = request.form['travel_id']
        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute('DELETE FROM travel_photos WHERE travel_id=%s', (travel_id,))
            c.execute('DELETE FROM travels WHERE id=%s', (travel_id,))
            conn.commit()
        flash('Viaje eliminado 🗑️', 'success')
        broadcast('travel_update', {'type': 'delete', 'id': int(travel_id)})
        return redirect('/')
    except Exception as e:
        print(f"Error en delete_travel: {e}")
        flash('No se pudo eliminar el viaje.', 'error')
        return redirect('/')
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
            c.execute('DELETE FROM travel_photos WHERE id=%s', (photo_id,))
            conn.commit()
        flash('Foto eliminada 🗑️', 'success')
        broadcast('travel_update', {'type': 'photo_delete', 'id': int(photo_id)})
        return redirect('/')
    except Exception as e:
        print(f"Error en delete_travel_photo: {e}")
        flash('No se pudo eliminar la foto.', 'error')
        return redirect('/')
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
            c.execute('SELECT is_visited FROM travels WHERE id=%s', (travel_id,))
            current_status = c.fetchone()[0]
            new_status = not current_status
            c.execute('UPDATE travels SET is_visited=%s WHERE id=%s', (new_status, travel_id))
            conn.commit()
        flash('Estado del viaje actualizado ✅', 'success')
        broadcast('travel_update', {'type': 'toggle', 'id': int(travel_id), 'is_visited': bool(new_status)})
        return redirect('/')
    except Exception as e:
        print(f"Error en toggle_travel_status: {e}")
        flash('No se pudo actualizar el estado del viaje.', 'error')
        return redirect('/')
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
            c.execute('DELETE FROM wishlist WHERE id=%s', (item_id,))
            conn.commit()
        flash('Elemento eliminado de la lista 🗑️', 'success')
        send_discord('Wishlist delete', {'user': user, 'item_id': item_id})
        broadcast('wishlist_update', {'type': 'delete', 'id': int(item_id)})
        return redirect('/')
    except Exception as e:
        print(f"Error en delete_wishlist_item: {e}")
        flash('No se pudo eliminar el elemento.', 'error')
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
            c.execute('SELECT id, product_name, is_purchased, is_gift, created_by FROM wishlist WHERE id=%s', (item_id,))
            row = c.fetchone()
            if not row:
                flash('Elemento no encontrado.', 'error')
                return redirect('/')
            wid, pname, current_state, is_gift, created_by = row
            new_state = not bool(current_state)
            c.execute('UPDATE wishlist SET is_purchased=%s WHERE id=%s', (new_state, wid))
            conn.commit()
            if is_gift and new_state:
                try:
                    push_gift_purchased({'id': wid, 'product_name': pname, 'created_by': created_by})
                except Exception as e:
                    print('[push gift_purchased]', e)
        flash('Estado del elemento actualizado ✅', 'success')
        send_discord('Wishlist toggle', {'user': user, 'item_id': item_id, 'is_purchased': bool(new_state)})
        broadcast('wishlist_update', {'type': 'toggle', 'id': int(item_id), 'is_purchased': bool(new_state)})
        return redirect('/')
    except Exception as e:
        print(f"Error en toggle_wishlist_item: {e}")
        flash('No se pudo actualizar el estado del elemento.', 'error')
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()

@app.route('/edit_wishlist_item', methods=['POST'])
def edit_wishlist_item():
    if 'username' not in session:
        return redirect('/')
    try:
        item_id = request.form.get('item_id')
        product_name = (request.form.get('product_name') or '').strip()
        product_link = (request.form.get('product_link') or '').strip()
        notes = (request.form.get('wishlist_notes') or '').strip()
        size = (request.form.get('size') or '').strip()
        priority = (request.form.get('priority') or 'media').strip()
        is_gift = bool(request.form.get('is_gift'))
        alert_drop_percent = (request.form.get('alert_drop_percent') or '').strip()
        alert_below_price = (request.form.get('alert_below_price') or '').strip()
        track_price = bool(request.form.get('track_price'))
        
        if priority not in ('alta', 'media', 'baja'):
            priority = 'media'
        
        def euros_to_cents(v):
            v = (v or '').strip()
            if not v:
                return
            return _to_cents_from_str(v.replace('€', ''))
        
        alert_below_cents = euros_to_cents(alert_below_price)
        alert_pct = float(alert_drop_percent) if alert_drop_percent else None
        
        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute('''
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
                 WHERE id=%s
            ''', (product_name, product_link, notes, size, priority, is_gift, track_price, alert_pct, alert_below_cents, item_id))
            conn.commit()
        flash('Elemento actualizado ✏️', 'success')
        send_discord('Wishlist edit', {
            'user': session['username'], 'item_id': item_id, 'priority': priority, 
            'is_gift': is_gift, 'track_price': track_price, 
            'alert_pct': alert_pct, 'alert_below': alert_below_cents
        })
        broadcast('wishlist_update', {'type': 'edit', 'id': int(item_id)})
        return redirect('/')
    except Exception as e:
        print(f"Error en edit_wishlist_item: {e}")
        flash('No se pudo editar el elemento.', 'error')
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()

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
            c.execute('''
                INSERT INTO locations (username, location_name, latitude, longitude, updated_at)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (username) DO UPDATE
                SET location_name=EXCLUDED.location_name,
                    latitude=EXCLUDED.latitude,
                    longitude=EXCLUDED.longitude,
                    updated_at=EXCLUDED.updated_at
            ''', (user, location_name, lat, lon, now_txt))
            conn.commit()
        flash('Ubicación actualizada 📍', 'success')
        send_discord('Location updated', {'user': user, 'name': location_name, 'lat': lat, 'lon': lon})
        broadcast('location_update', {'user': user})
        return redirect('/')
    except Exception as e:
        print(f"Error en update_location: {e}")
        flash('No se pudo actualizar la ubicación.', 'error')
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()

@app.route('/logout')
def logout():
    try:
        user = session.get('username')
        session.clear()
        send_discord('Logout', {'user': user})
    except Exception:
        pass
    return redirect('/')

@app.get('/push/vapid-public')
def push_vapid_public():
    key_b64url = get_vapid_public_base64url()
    if not key_b64url:
        return jsonify({'error': 'vapid_public_missing'}), 500
    return jsonify({'vapidPublicKey': key_b64url})

@app.post('/push/subscribe')
def push_subscribe():
    if 'username' not in session:
        return jsonify({'error': 'unauthenticated'}), 401
    user = session['username']
    sub = request.get_json(silent=True) or {}
    endpoint = sub.get('endpoint')
    p256dh = (sub.get('keys') or {}).get('p256dh')
    auth = (sub.get('keys') or {}).get('auth')
    if not (endpoint and p256dh and auth):
        return jsonify({'error': 'invalid_subscription'}), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('''
                INSERT INTO push_subscriptions (username, endpoint, p256dh, auth, created_at)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (endpoint) DO UPDATE
                SET username=EXCLUDED.username,
                    p256dh=EXCLUDED.p256dh,
                    auth=EXCLUDED.auth,
                    created_at=EXCLUDED.created_at
            ''', (user, endpoint, p256dh, auth, datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')))
            conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True})

@app.route('/push/unsubscribe', methods=['POST'])
def push_unsubscribe():
    if 'username' not in session:
        return jsonify({'ok': False, 'error': 'not_logged'}), 401
    try:
        payload = request.get_json(force=True, silent=False)
        endpoint = (payload.get('endpoint') or '').strip()
        if not endpoint:
            return jsonify({'ok': False, 'error': 'bad_payload'}), 400
        _delete_subscription_by_endpoint(endpoint)
        send_discord('Push unsubscribed', {'user': session['username']})
        return jsonify({'ok': True})
    except Exception as e:
        print(f"[push_unsubscribe] {e}")
        return jsonify({'ok': False, 'error': 'server_error'}), 500

@app.route('/push/test', methods=['POST', 'GET'])
def push_test():
    if 'username' not in session:
        return jsonify({'ok': False, 'error': 'not_logged'}), 401
    try:
        who = request.values.get('who', 'both')
        title = request.values.get('title', 'Prueba de notificación 🔔')
        body = request.values.get('body', 'Esto es un test desde el servidor.')
        if who == 'both':
            send_push_both(title, body)
        elif who in ('mochito', 'mochita'):
            send_push_to(who, title, body)
        else:
            return jsonify({'ok': False, 'error': 'bad_who'}), 400
        return jsonify({'ok': True})
    except Exception as e:
        print(f"[push_test] {e}")
        return jsonify({'ok': False, 'error': 'server_error'}), 500

@app.get('/push/list')
def push_list():
    if 'username' not in session:
        return jsonify({'ok': False, 'error': 'not_logged'}), 401
    try:
        subs = get_subscriptions_for(session['username'])
        return jsonify({'ok': True, 'count': len(subs), 'subs': subs})
    except Exception as e:
        print(f"[push_list] {e}")
        return jsonify({'ok': False, 'error': 'server_error'}), 500

@app.post('/price_check_now')
def price_check_now():
    if 'username' not in session:
        return jsonify({'ok': False, 'error': 'unauthenticated'}), 401
    try:
        wid = request.form.get('id')
        if not wid:
            return jsonify({'ok': False, 'error': 'missing id'}), 400
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                c.execute('''
                    SELECT id, product_name, product_link, created_by,
                           is_gift,
                           track_price, last_price_cents, currency,
                           alert_drop_percent, alert_below_cents
                    FROM wishlist WHERE id=%s
                ''', (wid,))
                row = c.fetchone()
                if not row or not row['product_link']:
                    return jsonify({'ok': False, 'error': 'not_found_or_no_link'}), 404
                price, curr, _ = fetch_price(row['product_link'])
                if not price:
                    return jsonify({'ok': False, 'error': 'no_price'}), 200
                old = row['last_price_cents']
                with conn.cursor() as c2:
                    c2.execute('UPDATE wishlist SET last_price_cents=%s, currency=COALESCE(%s,currency), last_checked=%s WHERE id=%s', (price, curr, now_madrid_str(), wid))
                    conn.commit()
                if old is not None and price < old:
                    notify_price_drop(row, old, price)
                return jsonify({'ok': True, 'old': old, 'new': price})
        finally:
            conn.close()
    except Exception as e:
        print('[price_check_now]', e)
        return jsonify({'ok': False, 'error': 'server_error'}), 500

@app.errorhandler(404)
def not_found(_):
    return render_template('404.html'), 404

@app.errorhandler(500)
def server_error(_):
    return render_template('500.html'), 500

@app.route('/horario', methods=['GET'])
def schedule_page():
    if 'username' not in session:
        return redirect('/')
    return render_template('schedule.html')

@app.route('/schedule', methods=['GET'])
def schedule_alias():
    return redirect('/horario')

@app.get('/api/schedules')
def api_get_schedules():
    if 'username' not in session:
        return jsonify({'error': 'unauthenticated'}), 401
    schedules = {'mochito': {}, 'mochita': {}}
    custom_times_sets = {'mochito': set(), 'mochita': set()}
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('SELECT username, day, time, activity, color FROM public.schedules')
            for row in c.fetchall():
                username = row['username']
                day = row['day']
                time_ = row['time']
                activity = row['activity'] or ''
                color = row['color'] or '#888888'
                if username not in schedules:
                    continue
                day_map = schedules[username].setdefault(day, {})
                day_map[time_] = {'activity': activity, 'color': color}
            c.execute('SELECT username, time FROM public.schedule_times')
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
                return int(h), int(m)
            except Exception:
                return 0, 0
        return sorted(lst, key=key)
    
    custom_times = {
        'mochito': sort_hhmm(list(custom_times_sets['mochito'])),
        'mochita': sort_hhmm(list(custom_times_sets['mochita']))
    }
    return jsonify({'schedules': schedules, 'customTimes': custom_times})

@app.post('/api/schedules/save')
def api_save_schedules():
    if 'username' not in session:
        return jsonify({'ok': False, 'error': 'unauthenticated'}), 401
    payload = request.get_json(silent=True) or {}
    schedules = payload.get('schedules') or {}
    custom_times = payload.get('customTimes') or {}
    if not isinstance(schedules, dict) or not isinstance(custom_times, dict):
        return jsonify({'ok': False, 'error': 'bad_payload'}), 400
    users = 'mochito', 'mochita'
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            for user in users:
                c.execute('DELETE FROM public.schedules WHERE username=%s', (user,))
                us = schedules.get(user) or {}
                for (day, time_map) in us.items():
                    time_map = time_map or {}
                    for (t, obj) in time_map.items():
                        activity = (obj or {}).get('activity', '')
                        color = (obj or {}).get('color', '#888888')
                        c.execute('''
                            INSERT INTO public.schedules (username, day, time, activity, color)
                            VALUES (%s,%s,%s,%s,%s)
                        ''', (user, day, t, activity, color))
                c.execute('DELETE FROM public.schedule_times WHERE username=%s', (user,))
                for t in custom_times.get(user) or []:
                    c.execute('''
                        INSERT INTO public.schedule_times (username, time)
                        VALUES (%s,%s)
                    ''', (user, t))
        conn.commit()
        send_discord('Schedules saved', {'by': session.get('username', '?')})
        broadcast('schedule_update', {'by': session.get('username', '?')})
        return jsonify({'ok': True})
    except Exception as e:
        print('[api_save_schedules] error:', e)
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': 'server_error'}), 500
    finally:
        conn.close()

@app.route('/sw.js')
def sw():
    import os as _os
    try:
        resp = send_file(_os.path.join(app.static_folder, 'sw.js'))
    except Exception:
        resp = send_file('static/sw.js')
    resp = make_response(resp)
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    resp.mimetype = 'application/javascript'
    return resp

@app.get('/push/debug')
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
    <div class="row"><span id="chk-standalone" class="dot"></span> <b>Modo app</b> (iOS: desde "Pantalla de inicio")</div>
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

@app.get('/_debug/tz')
def _debug_tz():
    n = europe_madrid_now()
    return jsonify({
        'utc': datetime.now(timezone.utc).isoformat(),
        'madrid': n.isoformat(),
        'date_madrid': n.date().isoformat(),
        'secs_to_midnight_madrid': int(seconds_until_next_midnight_madrid())
    })

@app.post('/presence/ping')
def presence_ping():
    if 'username' not in session:
        return jsonify(ok=False, error='unauthorized'), 401
    ensure_presence_table()
    u = session['username']
    now = europe_madrid_now()
    ua = (request.headers.get('User-Agent') or '')[:300]
    data = request.get_json(silent=True) or {}
    device = (data.get('device') or '')[:80]
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('''
                INSERT INTO user_presence (username, last_seen, device, user_agent)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (username) DO UPDATE
                SET last_seen = EXCLUDED.last_seen,
                    device    = EXCLUDED.device,
                    user_agent= EXCLUDED.user_agent;
            ''', (u, now, device, ua))
            conn.commit()
    finally:
        conn.close()
    try:
        broadcast('presence', {'kind': 'presence', 'user': u, 'ts': now.isoformat()})
    except Exception:
        pass
    return jsonify(ok=True)

@app.get('/presence/other')
def presence_other():
    if 'username' not in session:
        return jsonify(ok=False, error='unauthorized'), 401
    ensure_presence_table()
    me = session['username']
    other = request.args.get('user') or ('mochita' if me == 'mochito' else 'mochito')
    last_seen = None
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('SELECT last_seen FROM user_presence WHERE username=%s;', (other,))
            row = c.fetchone()
            if row:
                last_seen = row['last_seen']
    finally:
        conn.close()
    now = europe_madrid_now()
    window = int(os.environ.get('PRESENCE_ONLINE_WINDOW', '70'))
    online = False
    ago_seconds = None
    if last_seen:
        ago_seconds = max(0, int((now - last_seen).total_seconds()))
        online = ago_seconds <= window
    return jsonify(ok=True, user=other, online=online, last_seen=last_seen.isoformat() if last_seen else None, ago_seconds=ago_seconds)

@app.get('/presence/_debug_dump')
def presence_debug_dump():
    ensure_presence_table()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('SELECT username, last_seen FROM user_presence ORDER BY username;')
            rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()
    return jsonify(rows)

@app.get('/admin')
def admin_home():
    require_admin()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('''
                SELECT id, target, title, body, url, tag, icon, badge, when_at, status, created_at
                FROM scheduled_notifications
                WHERE status='pending'
                ORDER BY when_at ASC
                LIMIT 50
            ''')
            pending = c.fetchall()
            c.execute('''
                SELECT id, target, title, when_at, sent_at
                FROM scheduled_notifications
                WHERE status='sent'
                ORDER BY sent_at DESC
                LIMIT 10
            ''')
            recent = c.fetchall()
    finally:
        conn.close()
    return render_template('admin.html', username=session['username'], pending=pending, recent=recent, vapid_public_key=get_vapid_public_base64url())

@app.post('/admin/reset_pw')
def admin_reset_pw():
    require_admin()
    user = request.form.get('user')
    new_password = (request.form.get('new_password') or '').strip()
    if user not in ('mochito', 'mochita') or len(new_password) < 4:
        flash('Datos inválidos (usuario o contraseña demasiado corta).', 'error')
        return redirect('/admin')
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('UPDATE users SET password=%s WHERE username=%s', (generate_password_hash(new_password), user))
            conn.commit()
        flash(f"Contraseña de {user} cambiada ✅", 'success')
        send_discord('Admin: password reset', {'by': 'mochito', 'user': user})
    finally:
        conn.close()
    return redirect('/admin')

@app.post('/admin/push/send_now')
def admin_push_send_now():
    require_admin()
    target = request.form.get('target', 'both')
    title = (request.form.get('title') or '').strip()
    body = (request.form.get('body') or '').strip()
    url = (request.form.get('url') or '').strip() or None
    tag = (request.form.get('tag') or '').strip() or None
    icon = (request.form.get('icon') or '').strip() or None
    badge = (request.form.get('badge') or '').strip() or None
    if not title:
        flash('Título requerido.', 'error')
        return redirect('/admin')
    if target == 'both':
        send_push_both(title=title, body=body, url=url, tag=tag, icon=icon, badge=badge)
    elif target in ('mochito', 'mochita'):
        send_push_to(target, title=title, body=body, url=url, tag=tag, icon=icon, badge=badge)
    else:
        flash('Destino inválido.', 'error')
        return redirect('/admin')
    flash('Notificación enviada ✅', 'success')
    send_discord('Admin: push now', {'target': target, 'title': title})
    return redirect('/admin')

@app.post('/admin/push/schedule')
def admin_push_schedule():
    require_admin()
    target = request.form.get('target', 'both')
    title = (request.form.get('title') or '').strip()
    body = (request.form.get('body') or '').strip()
    when_local = request.form.get('when_local')
    url = (request.form.get('url') or '').strip() or None
    tag = (request.form.get('tag') or '').strip() or None
    icon = (request.form.get('icon') or '').strip() or None
    badge = (request.form.get('badge') or '').strip() or None
    when_txt = parse_datetime_local_to_madrid(when_local)
    if not (title and when_txt and target in ('mochito', 'mochita', 'both')):
        flash('Faltan datos (título/fecha o destino incorrecto).', 'error')
        return redirect('/admin')
    now_txt = now_madrid_str()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('''
                INSERT INTO scheduled_notifications
                (created_by, target, title, body, url, tag, icon, badge, when_at, status, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)
                RETURNING id
            ''', ('mochito', target, title, body, url, tag, icon, badge, when_txt, now_txt))
            _new_id = c.fetchone()[0]
            conn.commit()
        flash('Notificación programada 📆', 'success')
        send_discord('Admin: push scheduled', {'target': target, 'title': title, 'when_at': when_txt})
    finally:
        conn.close()
    return redirect('/admin')

@app.post('/admin/push/cancel')
def admin_push_cancel():
    require_admin()
    sid = request.form.get('id')
    if not sid:
        flash('Falta ID.', 'error')
        return redirect('/admin')
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE scheduled_notifications SET status='cancelled' WHERE id=%s AND status='pending'", (sid,))
            affected = c.rowcount
            conn.commit()
        if affected:
            flash('Programada cancelada 🛑', 'info')
            send_discord('Admin: push cancelled', {'id': int(sid)})
        else:
            flash('No se pudo cancelar (¿ya enviada o inexistente?).', 'error')
    finally:
        conn.close()
    return redirect('/admin')

ADMIN_RESET_TOKEN = os.environ.get('ADMIN_RESET_TOKEN', '').strip()

@app.get('/__reset_pw')
def __reset_pw():
    if not ADMIN_RESET_TOKEN:
        return 'Not found', 404
    t = request.args.get('token', '')
    u = request.args.get('u', '')
    pw = request.args.get('pw', '')
    if t != ADMIN_RESET_TOKEN:
        abort(403)
    if u not in ('mochito', 'mochita') or len(pw) < 4:
        return jsonify(ok=False, error='bad_params'), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('UPDATE users SET password=%s WHERE username=%s', (generate_password_hash(pw), u))
            conn.commit()
        send_discord('Admin: reset via token', {'user': u})
        return jsonify(ok=True)
    finally:
        conn.close()

@app.get('/admin/questions')
def admin_questions():
    require_admin()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('''
                SELECT id, text, used, used_at, active, created_at
                FROM question_bank
                ORDER BY active DESC, used, id DESC
            ''')
            rows = c.fetchall()
            c.execute('SELECT COUNT(*) FROM question_bank')
            total = c.fetchone()[0]
            c.execute('SELECT COUNT(*) FROM question_bank WHERE used=FALSE AND active=TRUE')
            pend = c.fetchone()[0]
            c.execute('SELECT COUNT(*) FROM question_bank WHERE used=TRUE')
            used = c.fetchone()[0]
    finally:
        conn.close()
    return render_template('admin_questions.html', username=session['username'], rows=rows, stats={'total': total, 'pend': pend, 'used': used})

@app.post('/admin/questions')
def admin_questions_add():
    require_admin()
    text = (request.form.get('text') or '').strip()
    if text:
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                c.execute('''
                    INSERT INTO question_bank (text, created_at)
                    VALUES (%s,%s)
                    ON CONFLICT (text) DO NOTHING
                ''', (text, now_madrid_str()))
                conn.commit()
        finally:
            conn.close()
        flash('Pregunta añadida ✅', 'success')
    return redirect('/admin/questions')

@app.post('/admin/questions/import')
def admin_questions_import():
    require_admin()
    bulk = request.form.get('bulk') or ''
    lines = [l.strip() for l in bulk.splitlines() if l.strip()]
    if lines:
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                now = now_madrid_str()
                for t in lines:
                    try:
                        c.execute('INSERT INTO question_bank (text, created_at) VALUES (%s,%s)', (t, now))
                    except Exception:
                        pass
                conn.commit()
        finally:
            conn.close()
        flash(f"Importadas {len(lines)} preguntas ✅", 'success')
    return redirect('/admin/questions')

@app.post('/admin/questions/<int:qid>/edit')
def admin_questions_edit(qid):
    require_admin()
    text = (request.form.get('text') or '').strip()
    if text:
        conn = get_db_connection()
        try:
            with conn.cursor() as c:
                c.execute('UPDATE question_bank SET text=%s WHERE id=%s', (text, qid))
                conn.commit()
        finally:
            conn.close()
        flash('Pregunta actualizada ✏️', 'success')
    return redirect('/admin/questions')

@app.post('/admin/questions/<int:qid>/delete')
def admin_questions_delete(qid):
    require_admin()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('UPDATE question_bank SET active=FALSE WHERE id=%s', (qid,))
            conn.commit()
    finally:
        conn.close()
    flash('Pregunta desactivada 🗑️', 'info')
    return redirect('/admin/questions')

@app.post('/admin/questions/<int:qid>/toggle_used')
def admin_questions_toggle_used(qid):
    require_admin()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('SELECT used FROM question_bank WHERE id=%s', (qid,))
            row = c.fetchone()
            if row:
                new_used = not bool(row['used'])
                if new_used:
                    c.execute('UPDATE question_bank SET used=TRUE, used_at=%s WHERE id=%s', (now_madrid_str(), qid))
                else:
                    c.execute('UPDATE question_bank SET used=FALSE, used_at=NULL WHERE id=%s', (qid,))
                conn.commit()
    finally:
        conn.close()
    return redirect('/admin/questions')

@app.post('/admin/questions/reset_used')
def admin_questions_reset_used():
    require_admin()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('UPDATE question_bank SET used=FALSE, used_at=NULL WHERE active=TRUE')
            conn.commit()
    finally:
        conn.close()
    flash("Marcados 'usada' reseteados ✅", 'success')
    return redirect('/admin/questions')

@app.post('/admin/questions/rotate_now')
def admin_questions_rotate_now():
    require_admin()
    ok = reroll_today_question()
    flash('Pregunta rotada ahora ✅' if ok else 'No hay preguntas disponibles para rotar 😅', 'success' if ok else 'error')
    return redirect('/admin/questions')

@app.post('/admin/questions/delete_all')
def admin_questions_delete_all():
    require_admin()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('DELETE FROM question_bank')
            conn.commit()
    finally:
        conn.close()
    flash('Se han borrado TODAS las preguntas del banco 🗑️', 'info')
    return redirect('/admin/questions')

@app.route('/__cron/tick', methods=['GET', 'POST'])
def __cron_tick():
    token = (request.headers.get('X-CRON-TOKEN') or request.args.get('token', '')).strip()
    need = os.environ.get('CRON_TOKEN', '').strip()
    if not need or token != need:
        return jsonify(ok=False, error='forbidden'), 403
    try:
        run_scheduled_jobs()
        return jsonify(ok=True, ran_at=now_madrid_str())
    except Exception as e:
        print('[cron_tick]', e)
        return jsonify(ok=False, error='server_error'), 500

def run_scheduled_jobs(now=None):
    now = now or europe_madrid_now()
    _check_daily_question(now)
    _check_relationship_daily(now)
    _check_scheduled_notifications()
    _check_meeting_countdown(now)
    _check_prices()

@app.cli.command('tick')
def cli_tick():
    run_scheduled_jobs()
    print('tick OK', now_madrid_str())

# Media routes
@app.post('/media/add')
def media_add():
    if 'username' not in session:
        return redirect('/')
    user = session['username']
    title = (request.form.get('title') or '').strip()
    cover_url = (request.form.get('cover_url') or '').strip()
    link_url = (request.form.get('link_url') or '').strip()
    on_netflix = bool(request.form.get('on_netflix'))
    on_prime = bool(request.form.get('on_prime'))
    priority = (request.form.get('priority') or 'media').strip()
    comment = (request.form.get('comment') or '').strip()
    if priority not in ('alta', 'media', 'baja'):
        priority = 'media'
    if not title:
        flash('Falta el título.', 'error')
        return redirect('/#pelis')
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('''
                INSERT INTO media_items
                    (title, cover_url, link_url, on_netflix, on_prime, priority, comment,
                     created_by, created_at, is_watched)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)
            ''', (title, cover_url, link_url, on_netflix, on_prime, priority, comment, user, now_madrid_str()))
            conn.commit()
        try:
            send_discord('Media add', {'by': user, 'title': title, 'priority': priority})
        except Exception:
            pass
        flash('Añadido a "Por ver" 🎬', 'success')
    except Exception as e:
        print('[media_add] ', e)
        try:
            conn.rollback()
        except Exception:
            pass
        flash('No se pudo añadir.', 'error')
    finally:
        conn.close()
    return redirect('/#pelis')

@app.post('/media/watch')
def media_mark_watched():
    if 'username' not in session:
        return redirect('/')
    user = session['username']
    mid = request.form.get('id')
    rating_raw = (request.form.get('rating') or '').strip()
    watched_comment = (request.form.get('watched_comment') or '').strip()
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
        with conn.cursor() as c:
            c.execute('SELECT reviews FROM media_items WHERE id=%s', (mid,))
            row = c.fetchone()
            if not row:
                flash('Elemento no encontrado.', 'error')
                return redirect('/#pelis')
            raw_reviews = row['reviews'] if isinstance(row, dict) else row[0]
            rv = {}
            if isinstance(raw_reviews, dict):
                rv = dict(raw_reviews)
            elif isinstance(raw_reviews, str) and raw_reviews.strip():
                try:
                    rv = json.loads(raw_reviews)
                except Exception:
                    rv = {}
            rv[user] = {'rating': rating, 'comment': watched_comment or ''}
            vals = []
            for (_, obj) in (rv or {}).items():
                try:
                    rr = int((obj or {}).get('rating') or 0)
                    if 1 <= rr <= 5:
                        vals.append(rr)
                except Exception:
                    pass
            avg = round(sum(vals) / len(vals), 1) if vals else None
            c.execute('''
                UPDATE media_items
                   SET is_watched    = TRUE,
                       watched_at     = %s,
                       reviews        = %s::jsonb,
                       avg_rating     = %s,
                       rating         = NULL,
                       watched_comment= NULL
                 WHERE id = %s
            ''', (now_madrid_str(), json.dumps(rv, ensure_ascii=False), avg, mid))
            conn.commit()
        try:
            send_discord('Media watched', {'by': user, 'id': int(mid), 'rating': rating})
        except Exception:
            pass
        flash('Marcado como visto ✅', 'success')
    except Exception as e:
        print('[media_mark_watched] ', e)
        try:
            conn.rollback()
        except Exception:
            pass
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
            c.execute('''
                UPDATE media_items
                SET rating = %s,
                    watched_comment = NULLIF(%s,'')
                WHERE id = %s AND is_watched = TRUE
            ''', (rating, watched_comment, mid))
            conn.commit()
        flash('Valoración guardada ⭐', 'success')
    except Exception as e:
        print('[media_rate_update] ', e)
        try:
            conn.rollback()
        except Exception:
            pass
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
            c.execute('''
                UPDATE media_items
                SET is_watched = FALSE,
                    watched_at = NULL,
                    rating = NULL,
                    watched_comment = NULL
                WHERE id = %s
            ''', (mid,))
            conn.commit()
        flash('Devuelto a "Por ver".', 'info')
    except Exception as e:
        print('[media_unwatch] ', e)
        try:
            conn.rollback()
        except Exception:
            pass
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
            c.execute('DELETE FROM media_items WHERE id = %s', (mid,))
            conn.commit()
        flash('Eliminado 🗑️', 'success')
    except Exception as e:
        print('[media_delete] ', e)
        try:
            conn.rollback()
        except Exception:
            pass
        flash('No se pudo eliminar.', 'error')
    finally:
        conn.close()
    return redirect('/#pelis')

@app.route('/media/update', methods=['GET', 'POST'])
def media_update():
    if request.method == 'GET':
        return redirect('/#pelis')
    if 'username' not in session:
        return redirect('/')
    u = session['username']
    mid = (request.form.get('id') or '').strip()
    if not mid:
        flash('Falta ID.', 'error')
        return redirect('/#pelis')
    title = (request.form.get('title') or '').strip()
    cover_url = (request.form.get('cover_url') or '').strip()
    link_url = (request.form.get('link_url') or '').strip()
    on_netflix = bool(request.form.get('on_netflix'))
    on_prime = bool(request.form.get('on_prime'))
    priority = (request.form.get('priority') or 'media').strip()
    comment = (request.form.get('comment') or '').strip()
    mark_watched = bool(request.form.get('mark_watched') or request.form.get('watch'))
    unwatch = bool(request.form.get('unwatch'))
    rating_raw = (request.form.get('rating') or '').strip()
    watched_comment = (request.form.get('watched_comment') or '').strip()
    rating = None
    if rating_raw.isdigit():
        rr = int(rating_raw)
        if 1 <= rr <= 5:
            rating = rr
    if priority not in ('alta', 'media', 'baja'):
        priority = 'media'
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute('''
                UPDATE media_items SET
                    title      = COALESCE(NULLIF(%s,''), title),
                    cover_url  = COALESCE(NULLIF(%s,''), cover_url),
                    link_url   = COALESCE(NULLIF(%s,''), link_url),
                    on_netflix = %s,
                    on_prime   = %s,
                    priority   = %s,
                    comment    = %s
                WHERE id=%s
            ''', (title, cover_url, link_url, on_netflix, on_prime, priority, comment, mid))
            if unwatch:
                c.execute('''
                    UPDATE media_items
                       SET is_watched = FALSE,
                           watched_at = NULL,
                           rating = NULL,
                           watched_comment = NULL
                     WHERE id=%s
                ''', (mid,))
            elif mark_watched or rating is not None or watched_comment:
                c.execute('SELECT reviews FROM media_items WHERE id=%s', (mid,))
                row = c.fetchone()
                if not row:
                    flash('Elemento no encontrado.', 'error')
                    conn.rollback()
                    return redirect('/#pelis')
                raw_reviews = row['reviews'] if isinstance(row, dict) else row[0]
                try:
                    rv = dict(raw_reviews) if isinstance(raw_reviews, dict) else json.loads(raw_reviews) if isinstance(raw_reviews, str) and raw_reviews.strip() else {}
                except Exception:
                    rv = {}
                prev = rv.get(u) or {}
                rv[u] = {'rating': rating if rating is not None else prev.get('rating'), 'comment': watched_comment or prev.get('comment') or ''}
                vals = []
                for obj in rv.values():
                    try:
                        r = int((obj or {}).get('rating') or 0)
                        if 1 <= r <= 5:
                            vals.append(r)
                    except Exception:
                        pass
                avg = round(sum(vals) / len(vals), 1) if vals else None
                c.execute('''
                    UPDATE media_items
                       SET is_watched    = TRUE,
                           watched_at     = %s,
                           reviews        = %s::jsonb,
                           avg_rating     = %s,
                           rating         = NULL,
                           watched_comment= NULL
                     WHERE id=%s
                ''', (now_madrid_str(), json.dumps(rv, ensure_ascii=False), avg, mid))
        conn.commit()
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

# Iniciar background jobs optimizados
if os.environ.get('RUN_SCHEDULER', '1') == '1':
    threading.Thread(target=optimized_background_loop, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=bool(os.environ.get('FLASK_DEBUG')))
