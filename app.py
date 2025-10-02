# app.py — versión optimizada para PostgreSQL en Render (pool + SSE + keepalive + cache TTL + Discord async)
from flask import Flask, render_template, request, redirect, session, send_file, jsonify, flash, Response
import psycopg2, psycopg2.extras
from psycopg2.pool import SimpleConnectionPool
from datetime import datetime, date, timedelta
import random, os, io, json, time, queue, threading, hashlib
from werkzeug.utils import secure_filename
from contextlib import closing
from base64 import b64encode
from werkzeug.security import generate_password_hash, check_password_hash
import requests

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'tu_clave_secreta_aqui')

# ======= Opciones de app (micro-opt) =======
app.config['TEMPLATES_AUTO_RELOAD'] = False
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # caché para estáticos
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False

# ======= Compresión (no afecta a SSE) =======
try:
    from flask_compress import Compress
    app.config['COMPRESS_MIMETYPES'] = ['text/html', 'text/css', 'application/json', 'application/javascript']
    app.config['COMPRESS_LEVEL'] = 6
    app.config['COMPRESS_MIN_SIZE'] = 1024
    Compress(app)
except Exception:
    pass  # si no está instalado, seguimos sin compresión

# ========= Discord logs (asíncrono, no bloquea) =========
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

# ========= Config Postgres + POOL =========
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

PG_POOL = None
def _init_pool():
    global PG_POOL
    if PG_POOL: return
    if not DATABASE_URL:
        raise RuntimeError("Falta DATABASE_URL para PostgreSQL")
    # Pool con keepalives y SSL (Render)
    PG_POOL = SimpleConnectionPool(
        1, int(os.environ.get("DB_MAX_CONN", "10")),
        dsn=DATABASE_URL,
        sslmode='require',
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
        connect_timeout=8
    )

class PooledConn:
    def __init__(self, pool, conn):
        self._pool, self._conn = pool, conn
        self._inited = False
    def __getattr__(self, name): return getattr(self._conn, name)
    def cursor(self, *a, **k):
        if "cursor_factory" not in k:
            k["cursor_factory"] = psycopg2.extras.DictCursor
        if not self._inited:
            with self._conn.cursor() as c:
                c.execute("SET application_name = 'mochitos';")
                c.execute("SET idle_in_transaction_session_timeout = '5s';")
                c.execute("SET statement_timeout = '5s';")
            self._inited = True
        return self._conn.cursor(*a, **k)
    def close(self):
        try: self._pool.putconn(self._conn)
        except Exception: pass

def get_db_connection():
    _init_pool()
    raw = PG_POOL.getconn()
    return PooledConn(PG_POOL, raw)

# ========= SSE: “casi tiempo real” + keepalive =========
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
            # abre el stream
            yield ":\n\n"
            while True:
                try:
                    ev = client_q.get(timeout=15)
                    payload = json.dumps(ev['data'], separators=(',', ':'))  # JSON compacto
                    yield f"event: {ev['event']}\ndata: {payload}\n\n"
                except queue.Empty:
                    # keepalive cada 15s: evita cierre por Render/proxies
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
    
    # Divertidas / De Risa
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
    
    # Calientes / Picantes 🔥
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
    
    # Creativas / Imaginación
    "Si tuviéramos una casa del árbol, ¿cómo sería por dentro?",
    "Si hiciéramos una película sobre nosotros, ¿cómo se llamaría?",
    "¿Cómo sería nuestro planeta si fuéramos los únicos habitantes?",
    "Si pudieras diseñar una cita perfecta desde cero, ¿cómo sería?",
    "Si nos perdiéramos en el tiempo, ¿en qué época te gustaría vivir conmigo?",
    "Si nuestra historia de amor fuera un libro, ¿cómo sería el final?",
    "Si pudieras regalarme una experiencia mágica, ¿cuál sería?",
    "¿Qué mundo ficticio te gustaría explorar conmigo?",
    
    # Reflexivas / Profundas
    "¿Qué aprendiste sobre ti mismo/a desde que estamos juntos?",
    "¿Qué miedos tienes sobre el futuro y cómo puedo ayudarte con ellos?",
    "¿Cómo te gustaría crecer como pareja conmigo?",
    "¿Qué errores cometiste en el pasado que no quieres repetir conmigo?",
    "¿Qué significa para ti una relación sana?",
    "¿Cuál es el mayor sueño que quieres cumplir y cómo puedo ayudarte?",
    "¿Qué necesitas escuchar más seguido de mí?",
    "¿Qué momento de tu infancia quisieras revivir conmigo al lado?",
    
    # Random / Curiosas
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

# ========= Mini-cache en memoria con TTL =========
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
        wrapped.__wrapped__ = fn  # permite saltar caché si hace falta
        wrapped._cache_key = key
        return wrapped
    return deco

def cache_invalidate(*fn_names):
    for name in fn_names:
        _cache_store.pop(_cache_key(name), None)

# ========= DB init (igual que el tuyo, con índices extra) =========
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
        # usuarios + ubicaciones por defecto
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
        # Índices útiles
        c.execute("CREATE INDEX IF NOT EXISTS idx_answers_q_user ON answers (question_id, username)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_travels_vdate ON travels (is_visited, travel_date DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_wishlist_state_prio ON wishlist (is_purchased, priority, created_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_intim_user_ts ON intimacy_events (username, ts DESC)")
        conn.commit()

init_db()

# ========= Helpers de datos =========
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

@ttl_cache(seconds=10)
def get_user_locations():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT username, location_name, latitude, longitude FROM locations")
            out = {}
            for u, name, lat, lng in c.fetchall():
                out[u] = {'name':name, 'lat':lat, 'lng':lng}
            return out
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

def get_travel_photos(travel_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT id, image_url, uploaded_by FROM travel_photos WHERE travel_id=%s ORDER BY id DESC", (travel_id,))
            return [{'id':r[0],'url':r[1],'uploaded_by':r[2]} for r in c.fetchall()]
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
                    priority = request.form.get('priority','media').strip()
                    is_gift = bool(request.form.get('is_gift'))
                    if priority not in ('alta','media','baja'): priority='media'
                    if product_name:
                        c.execute("""INSERT INTO wishlist (product_name, product_link, notes, created_by, created_at, is_purchased, priority, is_gift)
                                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                                  (product_name, product_link, notes, user, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                   False, priority, is_gift))
                        conn.commit(); flash("Producto añadido a la lista 🛍️","success")
                        send_discord("Wishlist added", {"user":user,"name":product_name,"priority":priority,"is_gift":is_gift})
                        broadcast("wishlist_update", {"type":"add"})
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
            c.execute("SELECT username, answer, created_at, updated_at FROM answers WHERE question_id=%s", (question_id,))
            rows = c.fetchall()
            answers = [(r[0], r[1]) for r in rows]
            answers_created_at = {r[0]: r[2] for r in rows}
            answers_updated_at = {r[0]: r[3] for r in rows}
            answers_edited = {r[0]: (r[2] is not None and r[3] is not None and r[2]!=r[3]) for r in rows}

            other_user = 'mochita' if user == 'mochito' else 'mochito'
            dict_ans = {u:a for (u,a) in answers}
            user_answer, other_answer = dict_ans.get(user), dict_ans.get(other_user)
            show_answers = (user_answer is not None) and (other_answer is not None)

            # Viajes (1 consulta + dict de fotos por id)
            c.execute("""SELECT id, destination, description, travel_date, is_visited, created_by
                         FROM travels ORDER BY is_visited, travel_date DESC""")
            travels = c.fetchall()
            c.execute("SELECT travel_id, id, image_url, uploaded_by FROM travel_photos ORDER BY id DESC")
            all_ph = c.fetchall()
            travel_photos_dict = {}
            for tr_id, pid, url, up in all_ph:
                travel_photos_dict.setdefault(tr_id, []).append({'id': pid, 'url': url, 'uploaded_by': up})

            # Wishlist
            c.execute("""SELECT id, product_name, product_link, notes, created_by, created_at, is_purchased,
                                COALESCE(priority,'media') AS priority, COALESCE(is_gift,false) AS is_gift
                         FROM wishlist
                         ORDER BY is_purchased ASC,
                                  CASE COALESCE(priority,'media') WHEN 'alta' THEN 0 WHEN 'media' THEN 1 ELSE 2 END,
                                  created_at DESC""")
            wishlist_items = c.fetchall()

            banner_file = get_banner()
            profile_pictures = get_profile_pictures()
    finally:
        conn.close()

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
    if 'username' not in session: return redirect('/')
    try:
        item_id = request.form['item_id']
        user = session['username']
        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute("SELECT created_by FROM wishlist WHERE id=%s", (item_id,))
            result = c.fetchone()
            if result and result[0] == user:
                c.execute("DELETE FROM wishlist WHERE id=%s", (item_id,))
                conn.commit(); flash("Producto eliminado de la lista 🗑️","success")
                broadcast("wishlist_update", {"type":"delete","id":int(item_id)})
        return redirect('/')
    except Exception as e:
        print(f"Error en delete_wishlist_item: {e}"); flash("No se pudo eliminar el producto.","error"); return redirect('/')
    finally:
        if 'conn' in locals(): conn.close()

@app.route('/edit_wishlist_item', methods=['POST'])
def edit_wishlist_item():
    if 'username' not in session: return redirect('/')
    try:
        item_id = request.form['item_id']
        product_name = request.form['product_name'].strip()
        product_link = request.form.get('product_link','').strip()
        notes = request.form.get('notes','').strip()
        priority = request.form.get('priority','media').strip()
        is_gift = bool(request.form.get('is_gift'))
        if priority not in ('alta','media','baja'): priority='media'
        if product_name:
            conn = get_db_connection()
            with conn.cursor() as c:
                c.execute("""UPDATE wishlist SET product_name=%s, product_link=%s, notes=%s, priority=%s, is_gift=%s WHERE id=%s""",
                          (product_name, product_link, notes, priority, is_gift, item_id))
                conn.commit(); flash("Producto actualizado ✅","success")
                broadcast("wishlist_update", {"type":"edit","id":int(item_id)})
        return redirect('/')
    except Exception as e:
        print(f"Error en edit_wishlist_item: {e}"); flash("No se pudo actualizar el producto.","error"); return redirect('/')
    finally:
        if 'conn' in locals(): conn.close()

@app.route('/toggle_wishlist_status', methods=['POST'])
def toggle_wishlist_status():
    if 'username' not in session: return redirect('/')
    try:
        item_id = request.form['item_id']
        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute("SELECT is_purchased FROM wishlist WHERE id=%s", (item_id,))
            current_status = c.fetchone()[0]
            new_status = not current_status
            c.execute("UPDATE wishlist SET is_purchased=%s WHERE id=%s", (new_status, item_id))
            conn.commit()
        flash("Estado de compra actualizado ✅","success")
        broadcast("wishlist_update", {"type":"toggle","id":int(item_id),"is_purchased":bool(new_status)})
        return redirect('/')
    except Exception as e:
        print(f"Error en toggle_wishlist_status: {e}"); flash("No se pudo actualizar el estado.","error"); return redirect('/')
    finally:
        if 'conn' in locals(): conn.close()

# ===== Intimidad: editar/borrar =====
@app.route('/edit_intim_event', methods=['POST'])
def edit_intim_event():
    if 'username' not in session:
        flash("Sesión no iniciada.", "error"); return redirect('/')
    if not session.get('intim_unlocked'):
        flash("Debes desbloquear con PIN para editar.", "error"); return redirect('/')
    user = session['username']
    event_id = (request.form.get('event_id') or '').strip()
    place = (request.form.get('intim_place_edit') or '').strip()
    notes = (request.form.get('intim_notes_edit') or '').strip()
    if not event_id.isdigit():
        flash("ID de evento inválido o vacío.", "error"); return redirect('/')
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT username FROM intimacy_events WHERE id=%s", (event_id,))
            row = c.fetchone()
            if not row: flash("Evento no encontrado.","error"); return redirect('/')
            owner = row[0]
            if owner not in ('mochito','mochita'):
                flash("Evento con propietario desconocido.", "error"); return redirect('/')
            c.execute("""UPDATE intimacy_events SET place=%s, notes=%s WHERE id=%s""", (place or None, notes or None, event_id))
            conn.commit()
        flash("Momento actualizado ✅","success")
        broadcast("intim_update", {"type":"edit","id":int(event_id)})
        return redirect('/')
    except Exception as e:
        print(f"[edit_intim_event] {e}"); flash("No se pudo actualizar el momento.","error"); return redirect('/')
    finally:
        conn.close()

@app.route('/delete_intim_event', methods=['POST'])
def delete_intim_event():
    if 'username' not in session: return redirect('/')
    if not session.get('intim_unlocked'):
        flash("Debes desbloquear con PIN para borrar.", "error"); return redirect('/')
    event_id = (request.form.get('event_id') or '').strip()
    if not event_id:
        flash("Falta el ID del evento.", "error"); return redirect('/')
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT username FROM intimacy_events WHERE id=%s", (event_id,))
            row = c.fetchone()
            if not row: flash("Evento no encontrado.", "error"); return redirect('/')
            owner = row[0]
            if owner not in ('mochito','mochita'):
                flash("Evento con propietario desconocido.", "error"); return redirect('/')
            c.execute("DELETE FROM intimacy_events WHERE id=%s", (event_id,))
            conn.commit()
        flash("Momento eliminado 🗑️","success")
        broadcast("intim_update", {"type":"delete","id":int(event_id)})
        return redirect('/')
    except Exception as e:
        print(f"[delete_intim_event] {e}"); flash("No se pudo eliminar el momento.","error"); return redirect('/')
    finally:
        conn.close()

# ===== Ubicaciones (AJAX) =====
@app.route('/update_location', methods=['POST'])
def update_location():
    if 'username' not in session: return jsonify({'error':'No autorizado'}), 401
    try:
        data = request.get_json(force=True)
        location_name, latitude, longitude = data.get('location_name'), data.get('latitude'), data.get('longitude')
        username = session['username']
        if location_name and latitude and longitude:
            conn = get_db_connection()
            with conn.cursor() as c:
                c.execute("""INSERT INTO locations (username, location_name, latitude, longitude, updated_at)
                             VALUES (%s,%s,%s,%s,%s)
                             ON CONFLICT (username) DO UPDATE
                             SET location_name = EXCLUDED.location_name,
                                 latitude = EXCLUDED.latitude,
                                 longitude = EXCLUDED.longitude,
                                 updated_at = EXCLUDED.updated_at""",
                          (username, location_name, latitude, longitude, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            cache_invalidate('get_user_locations')
            broadcast("location_update", {"user": username})
            return jsonify({'success':True,'message':'Ubicación actualizada correctamente'})
        return jsonify({'error':'Datos incompletos'}), 400
    except Exception as e:
        print(f"Error en update_location: {e}"); return jsonify({'error':'Error interno del servidor'}), 500

@app.route('/get_locations', methods=['GET'])
def get_locations():
    if 'username' not in session: return jsonify({'error':'No autorizado'}), 401
    try:
        return jsonify(get_user_locations())
    except Exception as e:
        print(f"Error en get_locations: {e}"); return jsonify({'error':'Error interno del servidor'}), 500

# ===== Horarios =====
@app.route('/horario')
def horario():
    if 'username' not in session: return redirect('/')
    return render_template('schedule.html')

@app.route('/api/schedules', methods=['GET'])
def get_schedules():
    if 'username' not in session: return jsonify({'error':'No autorizado'}), 401
    try:
        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute("SELECT username, day, time, activity, color FROM schedules")
            rows = c.fetchall()
            schedules = {'mochito': {}, 'mochita': {}}
            for username, day, time, activity, color in rows:
                schedules.setdefault(username, {}).setdefault(day, {})[time] = {'activity':activity, 'color':color}
            c.execute("SELECT username, time FROM schedule_times ORDER BY time")
            times_rows = c.fetchall()
            customTimes = {'mochito': [], 'mochita': []}
            for username, time in times_rows:
                if username in customTimes: customTimes[username].append(time)
            return jsonify({'schedules': schedules, 'customTimes': customTimes})
    except Exception as e:
        print(f"Error en get_schedules: {e}"); return jsonify({'error':'Error interno del servidor'}), 500

@app.route('/api/schedules/save', methods=['POST'])
def save_schedules():
    if 'username' not in session: return jsonify({'error':'No autorizado'}), 401
    try:
        data = request.get_json(force=True)
        schedules_payload = data.get('schedules', {})
        custom_times_payload = data.get('customTimes', {})
        conn = get_db_connection()
        with conn.cursor() as c:
            for user, times in (custom_times_payload or {}).items():
                c.execute("DELETE FROM schedule_times WHERE username=%s", (user,))
                for hhmm in times:
                    c.execute("""INSERT INTO schedule_times (username, time)
                                 VALUES (%s,%s) ON CONFLICT (username, time) DO NOTHING""", (user, hhmm))
            c.execute("DELETE FROM schedules")
            for user in ['mochito','mochita']:
                for day, times in (schedules_payload or {}).items():
                    day_times = times if isinstance(times, dict) else {}
                    for hhmm, obj in (day_times or {}).items():
                        activity = (obj or {}).get('activity','').strip()
                        color = (obj or {}).get('color','#e84393')
                        if activity:
                            c.execute("""INSERT INTO schedules (username, day, time, activity, color)
                                         VALUES (%s,%s,%s,%s,%s)
                                         ON CONFLICT (username, day, time)
                                         DO UPDATE SET activity=EXCLUDED.activity, color=EXCLUDED.color""",
                                      (user, day, hhmm, activity, color))
            conn.commit()
        broadcast("schedule_update", {"by": session['username']})
        return jsonify({'ok': True})
    except Exception as e:
        print(f"Error en save_schedules: {e}"); return jsonify({'error':'Error interno del servidor'}), 500

# ===== Logout / Imágenes / Reset PW =====
@app.route('/logout')
def logout():
    session.pop('username', None); return redirect('/')

@app.route('/image/<int:image_id>')
def get_image(image_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT image_data, mime_type FROM profile_pictures WHERE id=%s", (image_id,))
            row = c.fetchone()
            if row:
                return send_file(io.BytesIO(row[0]), mimetype=row[1])
            return "Imagen no encontrada", 404
    finally:
        conn.close()

@app.route('/__reset_pw')
def __reset_pw():
    token = request.args.get('token',''); expected = os.environ.get('RESET_TOKEN','')
    if not expected: return "RESET_TOKEN no configurado en el entorno", 403
    if token != expected:
        send_discord("Reset PW FAIL", {"reason":"bad_token","ip":client_ip()}); return "Token inválido", 403
    u = request.args.get('u','').strip(); pw = request.args.get('pw','').strip()
    if not u or not pw: return "Faltan parámetros u y pw", 400
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT 1 FROM users WHERE username=%s", (u,))
            if not c.fetchone(): return f"Usuario {u} no existe", 404
            new_hash = generate_password_hash(pw)
            c.execute("UPDATE users SET password=%s WHERE username=%s", (new_hash, u))
            conn.commit()
        send_discord("Reset PW OK", {"user": u}); return f"Contraseña de {u} actualizada correctamente", 200
    except Exception as e:
        send_discord("Reset PW ERROR", {"error": str(e)}); return "Error interno", 500
    finally:
        conn.close()

# ===== Micro-optimizaciones HTTP (ETag/caché en binarios) =====
@app.after_request
def after(resp):
    try:
        if request.method == "GET" and resp.status_code == 200 and request.path.startswith(("/image/")):
            body = resp.get_data()
            etag = hashlib.md5(body).hexdigest()
            resp.set_etag(etag)
            resp.headers.setdefault("Cache-Control", "public, max-age=2592000, immutable")
        else:
            if request.path == "/" or request.path.startswith(("/delete_","/edit_","/toggle_","/add_","/intim_","/answer","/api/")):
                resp.headers["Cache-Control"] = "no-store"
    except Exception:
        pass
    return resp

if __name__ == '__main__':
    # threaded=True para SSE + peticiones concurrentes en dev
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)
