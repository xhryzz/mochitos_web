tengo esta web, los dias no bajan los de vernos no se porque, y la pregunta ya son las 00:00 y no ha cambiado : 

te passo el app.py, si te hace falta el html dimelo

# app.py — con Web Push y notificaciones
from flask import Flask, render_template, request, redirect, session, send_file, jsonify, flash, Response
import psycopg2, psycopg2.extras
from psycopg2.pool import SimpleConnectionPool
from datetime import datetime, date, timedelta
import random, os, io, json, time, queue, threading, hashlib, base64, binascii
from werkzeug.utils import secure_filename
from contextlib import closing
from base64 import b64encode
from werkzeug.security import generate_password_hash, check_password_hash
import requests


# Web Push
from pywebpush import webpush, WebPushException
import pytz

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'tu_clave_secreta_aqui')

# ======= Opciones de app =======
app.config['TEMPLATES_AUTO_RELOAD'] = False
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

# ======= Compresión (si está) =======
try:
    from flask_compress import Compress
    app.config['COMPRESS_MIMETYPES'] = ['text/html','text/css','application/json','application/javascript']
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
    if not DISCORD_WEBHOOK: return
    try:
        display_user = None
        if 'username' in session and session['username'] in ('mochito','mochita'):
            display_user = session['username']
        embed = {"title": event, "color": 0xE84393, "timestamp": datetime.utcnow().isoformat()+"Z", "fields":[]}
        if display_user:
            embed["fields"].append({"name":"Usuario","value":display_user,"inline":True})
        try: ruta = f"{request.method} {request.path}"
        except Exception: ruta="(sin request)"
        embed["fields"] += [
            {"name":"Ruta","value":ruta,"inline":True},
            {"name":"IP","value":client_ip() or "?", "inline":True}
        ]
        if payload:
            raw = json.dumps(payload, ensure_ascii=False)
            for i, ch in enumerate([raw[i:i+1000] for i in range(0, len(raw), 1000)][:3]):
                embed["fields"].append({"name":"Datos"+(f" ({i+1})" if i else ""), "value":f"```json\n{ch}\n```","inline":False})
        _DISCORD_Q.put_nowait((DISCORD_WEBHOOK, {"username":"Mochitos Logs","embeds":[embed]}))
    except Exception as e:
        print(f"[discord] prep error: {e}")

# ========= Config Postgres + POOL (robusto contra sockets/SSL rotos) =========
DATABASE_URL = os.environ.get('DATABASE_URL', '')

def _normalize_database_url(url: str) -> str:
    if not url:
        return url
    # Normaliza el prefijo heroku-style
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    # Forzamos sslmode=require en el propio DSN (evita discrepancias)
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
    # Pasamos TODO via dsn ya normalizado
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
        # Devuelve la conexión rota y obtiene una nueva del pool
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

        # Inicialización de parámetros de sesión (una vez por conexión viva)
        if not self._inited:
            try:
                with self._conn.cursor() as c:
                    c.execute("SET application_name = 'mochitos';")
                    c.execute("SET idle_in_transaction_session_timeout = '5s';")
                    c.execute("SET statement_timeout = '5s';")
                self._inited = True
            except (OperationalError, InterfaceError):
                # Conexión rota: reconecta y vuelve a aplicar los SET
                self._reconnect()
                with self._conn.cursor() as c:
                    c.execute("SET application_name = 'mochitos';")
                    c.execute("SET idle_in_transaction_session_timeout = '5s';")
                    c.execute("SET statement_timeout = '5s';")
                self._inited = True

        try:
            return self._conn.cursor(*a, **k)
        except (OperationalError, InterfaceError):
            # Si al crear el cursor falla (socket/SSL roto), reconecta y devuelve cursor nuevo
            self._reconnect()
            return self._conn.cursor(*a, **k)

    def close(self):
        try:
            self._pool.putconn(self._conn)
        except Exception:
            pass

def get_db_connection():
    """Saca una conexión del pool, la 'pingea' y, si falla, la recicla antes de devolverla."""
    _init_pool()
    conn = PG_POOL.getconn()
    wrapped = PooledConn(PG_POOL, conn)
    # Ping ligero para detectar sockets/SSL rotos antes de usarlos
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


# ========= SSE =========
_subscribers_lock = threading.Lock()
_subscribers: set[queue.Queue] = set()

def broadcast(event_name: str, data: dict):
    with _subscribers_lock:
        for qcli in list(_subscribers):
            try: qcli.put_nowait({"event": event_name, "data": data})
            except Exception: pass

@app.route("/events")
def sse_events():
    client_q: queue.Queue = queue.Queue(maxsize=200)
    with _subscribers_lock: _subscribers.add(client_q)
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
            with _subscribers_lock: _subscribers.discard(client_q)
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
        # App state para flags de notificaciones ya enviadas
        c.execute('''CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY, value TEXT)''')
        # Push subscriptions (si no la creaste manualmente, la crea aquí)
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
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        conn.commit()

init_db()

# ========= Helpers =========
def _parse_dt(txt: str):
    try: return datetime.strptime(txt, "%Y-%m-%d %H:%M:%S")
    except: return None

def get_intim_stats():
    today = date.today()
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
        if dt: dts.append(dt)
    if not dts:
        return {'today_count':0,'month_total':0,'year_total':0,'days_since_last':None,'last_dt':None,'streak_days':0}
    today_count = sum(1 for dt in dts if dt.date()==today)
    month_total = sum(1 for dt in dts if dt.year==today.year and dt.month==today.month)
    year_total  = sum(1 for dt in dts if dt.year==today.year)
    last_dt = max(dts); days_since_last = (today - last_dt.date()).days
    if today_count == 0:
        streak_days = 0
    else:
        dates_with = {dt.date() for dt in dts}
        streak_days = 0; cur = today
        while cur in dates_with:
            streak_days += 1; cur = cur - timedelta(days=1)
    return {'today_count':today_count,'month_total':month_total,'year_total':year_total,
            'days_since_last':days_since_last,'last_dt':last_dt,'streak_days':streak_days}

def get_intim_events(limit: int = 200):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""SELECT id, username, ts, place, notes
                         FROM intimacy_events
                         WHERE username IN ('mochito','mochita')
                         ORDER BY ts DESC LIMIT %s""", (limit,))
            rows = c.fetchall()
            return [{'id':r[0],'username':r[1],'ts':r[2],'place':r[3] or '','notes':r[4] or ''} for r in rows]
    finally:
        conn.close()

def get_today_question():
    today_str = date.today().isoformat()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT id, question FROM daily_questions WHERE date=%s", (today_str,))
            qrow = c.fetchone()
            if qrow: return qrow
            c.execute("SELECT question FROM daily_questions")
            used = {row[0] for row in c.fetchall()}
            remaining = [q for q in QUESTIONS if q not in used]
            if not remaining: return (None, "Ya no hay más preguntas disponibles ❤️")
            question_text = random.choice(remaining)
            c.execute("INSERT INTO daily_questions (question, date) VALUES (%s,%s) RETURNING id", (question_text, today_str))
            qid = c.fetchone()[0]; conn.commit()
            return (qid, question_text)
    finally:
        conn.close()

def days_together(): return (date.today() - RELATION_START).days

def days_until_meeting():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT meeting_date FROM meeting ORDER BY id DESC LIMIT 1")
            row = c.fetchone()
            if row:
                meeting_date = datetime.strptime(row[0], "%Y-%m-%d").date()
                return max((meeting_date - date.today()).days, 0)
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
    if not rows: return 0, 0
    def parse_d(dtxt): return datetime.strptime(dtxt, "%Y-%m-%d").date()
    compl = [parse_d(r[1]) for r in rows if r[2] and int(r[2])>=2]
    if not compl: return 0, 0
    compl = sorted(compl)
    best = run = 1
    for i in range(1, len(compl)):
        if compl[i] == compl[i-1] + timedelta(days=1): run += 1
        else: run = 1
        best = max(best, run)
    today = date.today()
    latest = None
    for d in sorted(set(compl), reverse=True):
        if d <= today: latest = d; break
    if latest is None: return 0, best
    cur = 1; d = latest - timedelta(days=1)
    sset = set(compl)
    while d in sset: cur += 1; d -= timedelta(days=1)
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
    # Web Push JS espera base64url del punto no comprimido (65 bytes). Tú has guardado HEX.
    return hex_to_base64url(VAPID_PUBLIC_KEY_HEX)

def get_subscriptions_for(user: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE username=%s""",(user,))
            return [{"id":r[0],"endpoint":r[1],"keys":{"p256dh":r[2],"auth":r[3]}} for r in c.fetchall()]
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
        # si está caducada/410/404, la borramos
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):
            try: _delete_subscription_by_endpoint(sub["endpoint"])
            except: pass
        print(f"[push] error: {e}")
        return False
    except Exception as e:
        print(f"[push] error gen: {e}")
        return False

def send_push_to(user: str, title: str, body: str | None = None, data: dict | None = None):
    subs = get_subscriptions_for(user)
    payload = {"title": title, "data": data or {}}
    if body is not None and body != "":
        payload["body"] = body
    ok = False
    for sub in subs:
        ok = send_push_raw(sub, payload) or ok
    return ok


def send_push_both(title: str, body: str, data: dict | None = None):
    send_push_to('mochito', title, body, data)
    send_push_to('mochita', title, body, data)

# ---- Plantillas de notificaciones (ES) + planificador de avisos de encuentro ----

def push_wishlist_notice(creator: str, is_gift: bool):
    """Aviso al otro cuando se añade deseo/regalo a la lista (solo título)."""
    other = 'mochita' if creator == 'mochito' else 'mochito'
    title = "Nuevo regalo en la lista" if is_gift else "Nuevo deseo en la lista"
    # Sin body -> lo quitamos
    send_push_to(other, title, None)

def push_answer_notice(responder: str):
    """Aviso al otro cuando su pareja responde la pregunta del día (solo título)."""
    other = 'mochita' if responder == 'mochito' else 'mochito'
    send_push_to(other, "Pregunta del día", None)

def push_daily_new_question():
    """Aviso a ambos cuando cambia la pregunta diaria (solo título)."""
    send_push_both("Pregunta del día", None)

def push_last_hours(user: str):
    """Aviso a quien no haya respondido y queden pocas horas (solo título)."""
    send_push_to(user, "Pregunta del día", None)

def push_meeting_countdown(dleft: int):
    """Avisos 3/2/1 días antes (solo título)."""
    title = f"¡Quedan {dleft} día{'s' if dleft != 1 else ''} para veros!"
    send_push_both(title, None)


def _meet_times_key(day: date): return f"meet_times::{day.isoformat()}"
def _meet_sent_key(day: date, hhmm: str): return f"meet_sent::{day.isoformat()}::{hhmm}"

def ensure_meet_times(day: date):
    """
    Devuelve la lista de horas 'HH:MM' programadas para hoy entre 09:00 y 23:59.
    Si no existen, crea 2 o 3 aleatorias y las guarda en app_state.
    """
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
            h = random.randint(9, 23)     # 09..23
            m = random.randint(0, 59)     # 00..59
            chosen.add(f"{h:02d}:{m:02d}")
        times = sorted(chosen)
        state_set(key, json.dumps(times))
    return times

def due_now(now_madrid: datetime, hhmm: str) -> bool:
    """
    ¿Estamos dentro de una ventana ±90s de la hora programada?
    (el bucle corre cada ~45s, así evitamos perder el tiro)
    """
    try:
        hh, mm = map(int, hhmm.split(":"))
    except Exception:
        return False
    scheduled = now_madrid.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return abs((now_madrid - scheduled).total_seconds()) <= 90

# ========= App state helpers =========
def state_get(key: str, default: str | None = None) -> str | None:
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT value FROM app_state WHERE key=%s",(key,))
            row = c.fetchone()
            return row[0] if row else default
    finally:
        conn.close()

def state_set(key: str, value: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""INSERT INTO app_state (key,value) VALUES (%s,%s)
                         ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""",(key,value))
            conn.commit()
    finally:
        conn.close()

# ========= Background scheduler (recordatorios) =========
def europe_madrid_now():
    return datetime.now(pytz.timezone("Europe/Madrid"))

def seconds_until_next_midnight_madrid():
    now = europe_madrid_now()
    nxt = now.replace(hour=0,minute=0,second=0,microsecond=0) + timedelta(days=1)
    return (nxt - now).total_seconds()

def background_loop():
    """Bucles de 45–60s para:
       - crear pregunta del día a medianoche y notificar a ambos
       - recordatorios 3/2/1 días (2–3 veces entre 09:00 y 23:00)
       - últimas 3h si falta responder
    """
    print("[bg] scheduler iniciado")
    while True:
        try:
            now = europe_madrid_now()
            today = now.date()

            # 1) Asegurar pregunta de hoy + notificación “cambio de pregunta” (una vez/día)
            last_dq_push = state_get("last_dq_push_date", "")
            if str(today) != last_dq_push:
                qid, qtext = get_today_question()   # crea si falta
                push_daily_new_question()
                state_set("last_dq_push_date", str(today))
                cache_invalidate('compute_streaks')

            # 2) Recordatorios 3/2/1 días para veros (2–3 veces/día en horas aleatorias entre 09:00 y 23:00)
            try:
                d = days_until_meeting()
            except Exception:
                d = None

            if d is not None and d in (1, 2, 3):
                times = ensure_meet_times(today)  # lista de 'HH:MM' para HOY
                for hhmm in times:
                    sent_key = _meet_sent_key(today, hhmm)
                    if not state_get(sent_key, "") and due_now(now, hhmm):
                        push_meeting_countdown(d)
                        state_set(sent_key, "1")
            else:
                # Si no estamos en 1/2/3, limpia programación del día (sin machacar histórico)
                state_set(_meet_times_key(today), "[]")

            # 3) Últimas 3 horas para responder si falta
            try:
                qid, _ = get_today_question()
            except Exception:
                qid = None
            if qid:
                # ¿Quién respondió?
                conn = get_db_connection()
                try:
                    with conn.cursor() as c:
                        c.execute("SELECT username FROM answers WHERE question_id=%s", (qid,))
                        have = {r[0] for r in c.fetchall()}
                finally:
                    conn.close()

                secs_left = seconds_until_next_midnight_madrid()
                # envía recordatorio si quedan <= 3h y no se ha mandado ya hoy a esa persona
                if secs_left <= 3*3600:
                    for u in ('mochito','mochita'):
                        if u not in have:
                            key = f"late_reminder_{today.isoformat()}_{u}"
                            if not state_get(key, ""):
                                push_last_hours(u)
                                state_set(key, "1")

        except Exception as e:
            print(f"[bg] error: {e}")

        time.sleep(45)  # periodo

# Lanzar scheduler en segundo plano
threading.Thread(target=background_loop, daemon=True).start()

# ========= Rutas =========
@app.route('/', methods=['GET', 'POST'])
def index():
    # LOGIN
    if 'username' not in session:
        if request.method == 'POST':
            username = request.form.get('username','').strip()
            password = request.form.get('password','').strip()
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
                        ok = check_password_hash(stored, password); mode="hashed"
                    else:
                        ok = (stored == password); mode="plaintext"
                    if ok:
                        session['username'] = username
                        send_discord("Login OK", {"username": username, "mode": mode})
                        return redirect('/')
                    else:
                        send_discord("Login FAIL", {"username_intent": username, "reason":"bad_password","mode":mode})
                        return render_template('index.html', error="Usuario o contraseña incorrecta", profile_pictures={})
            finally:
                conn.close()
        return render_template('index.html', error=None, profile_pictures={})

    # LOGUEADO
    user = session['username']
    question_id, question_text = get_today_question()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            # ------------ POST acciones ------------
            if request.method == 'POST':
                # 1) Foto perfil
                if 'update_profile' in request.form and 'profile_picture' in request.files:
                    file = request.files['profile_picture']
                    if file and file.filename:
                        image_data = file.read()
                        filename = secure_filename(file.filename)
                        mime_type = file.mimetype
                        c.execute("""
                            INSERT INTO profile_pictures (username, image_data, filename, mime_type, uploaded_at)
                            VALUES (%s,%s,%s,%s,%s)
                            ON CONFLICT (username) DO UPDATE
                            SET image_data=EXCLUDED.image_data, filename=EXCLUDED.filename,
                                mime_type=EXCLUDED.mime_type, uploaded_at=EXCLUDED.uploaded_at
                        """, (user, image_data, filename, mime_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        conn.commit(); flash("Foto de perfil actualizada ✅","success")
                        send_discord("Profile picture updated", {"user": user, "filename": filename})
                        cache_invalidate('get_profile_pictures')
                        broadcast("profile_update", {"user": user})
                    return redirect('/')

                # 2) Cambio de contraseña
                if 'change_password' in request.form:
                    current_password = request.form.get('current_password','').strip()
                    new_password = request.form.get('new_password','').strip()
                    confirm_password = request.form.get('confirm_password','').strip()
                    if not current_password or not new_password or not confirm_password:
                        flash("Completa todos los campos de contraseña.", "error"); return redirect('/')
                    if new_password != confirm_password:
                        flash("La nueva contraseña y la confirmación no coinciden.", "error"); return redirect('/')
                    if len(new_password) < 4:
                        flash("La nueva contraseña debe tener al menos 4 caracteres.", "error"); return redirect('/')
                    c.execute("SELECT password FROM users WHERE username=%s", (user,))
                    row = c.fetchone()
                    if not row: flash("Usuario no encontrado.","error"); return redirect('/')
                    stored = row[0]
                    valid_current = check_password_hash(stored, current_password) if _is_hashed(stored) else (stored==current_password)
                    if not valid_current:
                        flash("La contraseña actual no es correcta.", "error")
                        send_discord("Change password FAIL", {"user": user, "reason":"wrong_current"}); return redirect('/')
                    new_hash = generate_password_hash(new_password)
                    c.execute("UPDATE users SET password=%s WHERE username=%s", (new_hash, user))
                    conn.commit(); flash("Contraseña cambiada correctamente 🎉","success")
                    send_discord("Change password OK", {"user": user}); return redirect('/')

                # 3) Responder pregunta
                if 'answer' in request.form:
                    answer = request.form['answer'].strip()
                    if question_id is not None and answer:
                        now_txt = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
                        c.execute("SELECT id, answer, created_at, updated_at FROM answers WHERE question_id=%s AND username=%s",
                                  (question_id, user))
                        prev = c.fetchone()
                        if not prev:
                            c.execute("""INSERT INTO answers (question_id, username, answer, created_at, updated_at)
                                         VALUES (%s,%s,%s,%s,%s) ON CONFLICT (question_id, username) DO NOTHING""",
                                      (question_id, user, answer, now_txt, now_txt))
                            conn.commit(); send_discord("Answer submitted", {"user":user,"question_id":question_id})
                        else:
                            prev_id, prev_text, prev_created, prev_updated = prev
                            if answer != (prev_text or ""):
                                if prev_created is None:
                                    c.execute("""UPDATE answers SET answer=%s, updated_at=%s, created_at=%s WHERE id=%s""",
                                              (answer, now_txt, prev_updated or now_txt, prev_id))
                                else:
                                    c.execute("""UPDATE answers SET answer=%s, updated_at=%s WHERE id=%s""",
                                              (answer, now_txt, prev_id))
                                conn.commit(); send_discord("Answer edited", {"user":user,"question_id":question_id})
                        cache_invalidate('compute_streaks')
                        broadcast("dq_answer", {"user": user})
                        try:
                            push_answer_notice(user)
                        except Exception as e:
                            print("[push answer] ", e)
                    return redirect('/')

                # 4) Meeting date
                if 'meeting_date' in request.form:
                    meeting_date = request.form['meeting_date']
                    c.execute("INSERT INTO meeting (meeting_date) VALUES (%s)", (meeting_date,))
                    conn.commit(); flash("Fecha actualizada 📅","success")
                    send_discord("Meeting date updated", {"user": user, "date": meeting_date})
                    broadcast("meeting_update", {"date": meeting_date}); return redirect('/')

                # 5) Banner
                if 'banner' in request.files:
                    file = request.files['banner']
                    if file and file.filename:
                        image_data = file.read()
                        filename = secure_filename(file.filename)
                        mime_type = file.mimetype
                        c.execute("INSERT INTO banner (image_data, filename, mime_type, uploaded_at) VALUES (%s,%s,%s,%s)",
                                  (image_data, filename, mime_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        conn.commit(); flash("Banner actualizado 🖼️","success")
                        send_discord("Banner updated", {"user": user, "filename": filename})
                        cache_invalidate('get_banner')
                        broadcast("banner_update", {"by": user})
                    return redirect('/')

                # 6) Nuevo viaje
                if 'travel_destination' in request.form:
                    destination = request.form['travel_destination'].strip()
                    description = request.form.get('travel_description','').strip()
                    travel_date = request.form.get('travel_date','')
                    is_visited = 'travel_visited' in request.form
                    if destination:
                        c.execute("""INSERT INTO travels (destination, description, travel_date, is_visited, created_by, created_at)
                                     VALUES (%s,%s,%s,%s,%s,%s)""",
                                  (destination, description, travel_date, is_visited, user, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        conn.commit(); flash("Viaje añadido ✈️","success")
                        send_discord("Travel added", {"user": user, "dest": destination, "visited": is_visited})
                        broadcast("travel_update", {"type":"add"})
                    return redirect('/')

                # 7) Foto de viaje (URL)
                if 'travel_photo_url' in request.form:
                    travel_id = request.form.get('travel_id')
                    image_url = request.form['travel_photo_url'].strip()
                    if image_url and travel_id:
                        c.execute("""INSERT INTO travel_photos (travel_id, image_url, uploaded_by, uploaded_at)
                                     VALUES (%s,%s,%s,%s)""",
                                  (travel_id, image_url, user, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        conn.commit(); flash("Foto añadida 📸","success")
                        send_discord("Travel photo added", {"user": user, "travel_id": travel_id})
                        broadcast("travel_update", {"type":"photo_add","id":int(travel_id)})
                    return redirect('/')

                # 8) Wishlist add
                if 'product_name' in request.form and 'edit_wishlist_item' not in request.path:
                    product_name = request.form['product_name'].strip()
                    product_link = request.form.get('product_link','').strip()
                    notes = request.form.get('wishlist_notes','').strip()
                    product_size = request.form.get('size','').strip()
                    priority = request.form.get('priority','media').strip()
                    is_gift = bool(request.form.get('is_gift'))
                    if priority not in ('alta','media','baja'): priority='media'
                    if product_name:
                        c.execute("""INSERT INTO wishlist (product_name, product_link, notes, size, created_by, created_at, is_purchased, priority, is_gift)
                                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                                  (product_name, product_link, notes, product_size, user, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                   False, priority, is_gift))
                        conn.commit(); flash("Producto añadido a la lista 🛍️","success")
                        send_discord("Wishlist added", {"user":user,"name":product_name,"priority":priority,"is_gift":is_gift,"size":product_size})
                        broadcast("wishlist_update", {"type":"add"})
                        try:
                            push_wishlist_notice(user, is_gift)
                        except Exception as e:
                            print("[push wishlist] ", e)
                    return redirect('/')

                # INTIMIDAD
                if 'intim_unlock_pin' in request.form:
                    pin_try = request.form.get('intim_pin','').strip()
                    if pin_try == INTIM_PIN:
                        session['intim_unlocked'] = True; flash("Módulo Intimidad desbloqueado ✅","success")
                        send_discord("Intimidad unlock OK", {"user": user})
                    else:
                        session.pop('intim_unlocked', None); flash("PIN incorrecto ❌","error")
                        send_discord("Intimidad unlock FAIL", {"user": user})
                    return redirect('/')

                if 'intim_lock' in request.form:
                    session.pop('intim_unlocked', None); flash("Módulo Intimidad ocultado 🔒","info"); return redirect('/')

                if 'intim_register' in request.form:
                    if not session.get('intim_unlocked'):
                        flash("Debes desbloquear con PIN para registrar.","error"); return redirect('/')
                    place = (request.form.get('intim_place') or '').strip()
                    notes = (request.form.get('intim_notes') or '').strip()
                    now_txt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    with conn.cursor() as c2:
                        c2.execute("""INSERT INTO intimacy_events (username, ts, place, notes) VALUES (%s,%s,%s,%s)""",
                                   (user, now_txt, place or None, notes or None))
                        conn.commit()
                    flash("Momento registrado ❤️","success")
                    send_discord("Intimidad registered", {"user":user,"place":place,"notes_len":len(notes)})
                    broadcast("intim_update", {"type":"add"})
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

            # Wishlist (sin blindaje aún)
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
                    size
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
            wl_rows = c.fetchall()

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
             is_purchased, priority, is_gift, size) in wl_rows:

            priority = priority or 'media'
            is_gift  = bool(is_gift)

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
                    None
                ))
            else:
                safe_items.append((
                    wid, product_name, product_link, notes,
                    created_by, created_at, is_purchased,
                    priority, is_gift, size
                ))
        wishlist_items = safe_items

        # Recursos cacheados
        banner_file = get_banner()
        profile_pictures = get_profile_pictures()

    finally:
        conn.close()

    # Stats + render
    current_streak, best_streak = compute_streaks()
    intim_unlocked = bool(session.get('intim_unlocked'))
    intim_stats = get_intim_stats()
    intim_events = get_intim_events(200) if intim_unlocked else []

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
                           intim_events=intim_events
                           )


# ======= Rutas REST extra (con broadcast) =======
@app.route('/delete_travel', methods=['POST'])
def delete_travel():
    if 'username' not in session: return redirect('/')
    try:
        travel_id = request.form['travel_id']
        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute("DELETE FROM travel_photos WHERE travel_id=%s", (travel_id,))
            c.execute("DELETE FROM travels WHERE id=%s", (travel_id,))
            conn.commit()
        flash("Viaje eliminado 🗑️", "success")
        broadcast("travel_update", {"type":"delete","id":int(travel_id)})
        return redirect('/')
    except Exception as e:
        print(f"Error en delete_travel: {e}"); flash("No se pudo eliminar el viaje.", "error"); return redirect('/')
    finally:
        if 'conn' in locals(): conn.close()

@app.route('/delete_travel_photo', methods=['POST'])
def delete_travel_photo():
    if 'username' not in session: return redirect('/')
    try:
        photo_id = request.form['photo_id']
        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute("DELETE FROM travel_photos WHERE id=%s", (photo_id,))
            conn.commit()
        flash("Foto eliminada 🗑️", "success")
        broadcast("travel_update", {"type":"photo_delete","id":int(photo_id)})
        return redirect('/')
    except Exception as e:
        print(f"Error en delete_travel_photo: {e}"); flash("No se pudo eliminar la foto.", "error"); return redirect('/')
    finally:
        if 'conn' in locals(): conn.close()

@app.route('/toggle_travel_status', methods=['POST'])
def toggle_travel_status():
    if 'username' not in session: return redirect('/')
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
        broadcast("travel_update", {"type":"toggle","id":int(travel_id),"is_visited":bool(new_status)})
        return redirect('/')
    except Exception as e:
        print(f"Error en toggle_travel_status: {e}"); flash("No se pudo actualizar el estado del viaje.", "error"); return redirect('/')
    finally:
        if 'conn' in locals(): conn.close()

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
            c.execute("SELECT is_purchased FROM wishlist WHERE id=%s", (item_id,))
            row = c.fetchone()
            if not row:
                flash("Elemento no encontrado.", "error")
                return redirect('/')
            new_state = not bool(row[0])
            c.execute("UPDATE wishlist SET is_purchased=%s WHERE id=%s", (new_state, item_id))
            conn.commit()
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

        if priority not in ('alta','media','baja'):
            priority = 'media'

        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute("""UPDATE wishlist SET
                            product_name=%s,
                            product_link=%s,
                            notes=%s,
                            size=%s,
                            priority=%s,
                            is_gift=%s
                         WHERE id=%s""",
                      (product_name, product_link, notes, size, priority, is_gift, item_id))
            conn.commit()
        flash("Elemento actualizado ✏️", "success")
        send_discord("Wishlist edit", {
            "user": session['username'], "item_id": item_id,
            "priority": priority, "is_gift": is_gift
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
        now_txt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
    # Devuelve base64url del punto público (tú guardas HEX en env)
    key_b64url = get_vapid_public_base64url()
    if not key_b64url:
        return jsonify({"error":"vapid_public_missing"}), 500
    return jsonify({"vapidPublicKey": key_b64url})
@app.route('/push/subscribe', methods=['POST'])
@app.post("/push/subscribe")
def push_subscribe():
    if "username" not in session:
        return jsonify({"error":"unauthenticated"}), 401
    user = session["username"]
    sub = request.get_json(silent=True) or {}
    endpoint = sub.get("endpoint")
    p256dh = (sub.get("keys") or {}).get("p256dh")
    auth   = (sub.get("keys") or {}).get("auth")
    if not (endpoint and p256dh and auth):
        return jsonify({"error":"invalid_subscription"}), 400

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
    # Permite probar rápidamente el envío de una notificación
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

# ======= Errores simpáticos =======
@app.errorhandler(404)
def not_found(_):
    return render_template('404.html'), 404

@app.errorhandler(500)
def server_error(_):
    # Intenta no revelar trazas al usuario final
    return render_template('500.html'), 500



# --- Service Worker en la raíz ---
from flask import make_response

@app.route("/sw.js")
def sw():
    from flask import make_response, send_file
    import os
    try:
        resp = send_file(os.path.join(app.static_folder, "sw.js"))
    except Exception:
        resp = send_file("static/sw.js")
    resp = make_response(resp)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.mimetype = "application/javascript"
    return resp


# ======= WSGI / Run =======
if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=bool(os.environ.get('FLASK_DEBUG')))


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
:root{
  --brand:#e84393; --ink:#202124; --muted:#5f6368; --ok:#10b981; --warn:#f59e0b; --err:#ef4444; --bg:#fafafa; --card:#fff; --line:#ececec;
}
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
.hr{height:1px;background:var(--line);margin:14px 0}
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
/* ===== Utilidades ===== */
function log(){ const el=document.getElementById('log'); el.textContent += Array.from(arguments).join(' ') + "\\n"; el.scrollTop = el.scrollHeight; }
function b64ToBytes(s){ const pad='='.repeat((4 - s.length % 4) % 4); const b64=(s+pad).replace(/-/g,'+').replace(/_/g,'/'); const raw=atob(b64); const arr=new Uint8Array(raw.length); for(let i=0;i<raw.length;i++)arr[i]=raw.charCodeAt(i); return arr; }
function setDot(id, state){ const el=document.getElementById(id); el.className='dot'; if(state==='ok') el.classList.add('ok'); else if(state==='warn') el.classList.add('warn'); else if(state==='err') el.classList.add('err'); }
function pctDone(parts){ const sum = parts.reduce((a,b)=>a+(b?1:0),0); return Math.round((sum/parts.length)*100); }
function setProgress(p){ document.getElementById('progressBar').style.width = p + '%'; }

/* ===== Detección entorno ===== */
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

/* Mostrar ayuda iOS si aplica */
if(isIOS){
  document.getElementById('iosHowTo').style.display = isStandalone ? 'none' : 'block';
}

/* Estado inicial */
setDot('chk-https', isHTTPS ? 'ok' : 'err');
setDot('chk-standalone', (isIOS ? (isStandalone ? 'ok' : 'warn') : 'ok'));

/* ===== SW auto-registro en carga si es posible ===== */
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

/* ===== Permiso actual ===== */
function refreshPermission(){
  if(!('Notification' in window)) { setDot('chk-perm','err'); return; }
  const st = Notification.permission; // default | granted | denied
  if(st === 'granted') setDot('chk-perm','ok');
  else if(st === 'denied') setDot('chk-perm','err');
  else setDot('chk-perm','warn');
}

/* ===== Suscripción actual ===== */
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

/* ===== Progreso ===== */
function refreshProgress(){
  const dots = ['chk-https','chk-standalone','chk-sw','chk-perm','chk-sub'];
  const ok = dots.map(id => document.getElementById(id).classList.contains('ok'));
  setProgress(pctDone(ok));
}

/* ===== Botones ===== */
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

/* ===== Inicial ===== */
(function init(){
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
