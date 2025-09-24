from flask import Flask, render_template, request, redirect, session, send_file, jsonify, flash
import psycopg2
from datetime import datetime, date, timedelta
import random
import os
from werkzeug.utils import secure_filename
from contextlib import closing
import io
from base64 import b64encode
from werkzeug.security import generate_password_hash, check_password_hash
import requests, json

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'tu_clave_secreta_aqui')



# --- Helpers para logs y contraseñas ---
# --- Helpers de logging a Discord (embeds sin máscaras) ---
DISCORD_WEBHOOK = os.environ.get('DISCORD_WEBHOOK', '')  # configura en Render

def client_ip():
    # Render pasa X-Forwarded-For
    return (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()

def _is_hashed(value: str) -> bool:
    return isinstance(value, str) and (value.startswith('pbkdf2:') or value.startswith('scrypt:'))

def send_discord(event: str, payload: dict | None = None):
    """
    Envía un embed simple a Discord (sin enmascarar nada).
    - Campo Usuario solo si session['username'] es 'mochito' o 'mochita'.
    - Incluye Ruta y IP.
    - Todos los datos del payload tal cual, troceados si exceden límites.
    """
    if not DISCORD_WEBHOOK:
        return  # no rompe si no hay webhook

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

        embed["fields"].append({"name": "Ruta", "value": ruta, "inline": True})
        embed["fields"].append({"name": "IP", "value": client_ip() or "?", "inline": True})

        if payload:
            raw = json.dumps(payload, ensure_ascii=False, indent=2)
            chunks = [raw[i:i+1000] for i in range(0, len(raw), 1000)]
            for i, ch in enumerate(chunks[:3]):  # máx 3 campos para no pasarnos
                embed["fields"].append({
                    "name": "Datos" + (f" ({i+1})" if i else ""),
                    "value": f"```json\n{ch}\n```",
                    "inline": False
                })

        body = {"username": "Mochitos Logs", "embeds": [embed]}
        requests.post(DISCORD_WEBHOOK, json=body, timeout=6)
    except Exception as e:
        print(f"[discord] error enviando webhook: {e}")




# Configuración de PostgreSQL para Render
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Preguntas predeterminadas
QUESTIONS = [
    # --- ROMÁNTICAS ---
    "¿Cuál fue el mejor momento de nuestra relación hasta ahora?",
    "¿Qué es lo primero que pensaste de mí cuando nos conocimos?",
    "¿Qué canción te recuerda a mí?",
    "¿Qué detalle mío te enamora más?",
    "¿Cuál sería tu cita perfecta conmigo?",
    "¿Qué momento conmigo repetirías mil veces?",
    "¿Qué parte de nuestra historia te parece más especial?",
    "¿Qué te gusta más que haga por ti?",
    "¿Cómo imaginas nuestro futuro juntos?",
    "¿Qué tres palabras me dedicarías ahora?",
    "¿Qué sientes cuando me abrazas fuerte?",
    "¿Qué gesto romántico te gustaría que repitiera más?",
    "¿Qué fue lo que más te sorprendió de mí?",
    "¿Cuál ha sido la sorpresa más bonita que te he dado?",
    "¿Qué lugar del mundo sueñas visitar conmigo?",
    "¿Qué película refleja mejor nuestro amor?",
    "¿Qué cosa pequeña hago que te hace feliz?",
    "¿Qué parte de tu rutina mejora cuando estoy contigo?",
    "¿Qué regalo te gustaría recibir de mí algún día?",
    "¿Qué frase de amor nunca te cansas de escuchar?",

    # --- DIVERTIDAS ---
    "Si fuéramos un meme, ¿cuál seríamos?",
    "¿Cuál ha sido tu momento más torpe conmigo?",
    "Si yo fuera un animal, ¿cuál crees que sería?",
    "¿Qué emoji me representa mejor?",
    "Si hiciéramos un TikTok juntos, ¿de qué sería?",
    "¿Qué comida dirías que soy en versión plato?",
    "¿Cuál es el apodo más ridículo que me pondrías?",
    "Si mañana cambiáramos cuerpos, ¿qué es lo primero que harías?",
    "¿Cuál ha sido la peor película que vimos juntos?",
    "Si fuéramos personajes de una serie, ¿quién sería quién?",
    "¿Qué canción sería nuestro himno gracioso?",
    "¿Qué cosa rara hago que siempre te hace reír?",
    "Si escribieran un libro de nuestra vida, ¿qué título absurdo tendría?",
    "¿Qué chiste malo mío te dio más risa?",
    "Si me disfrazaras, ¿de qué sería?",
    "¿Qué gesto mío se ve más chistoso cuando lo exagero?",
    "¿Cuál fue la pelea más absurda que hemos tenido?",
    "Si fuéramos un postre, ¿cuál seríamos?",
    "¿Qué palabra inventada usamos solo nosotros?",
    "¿Qué escena nuestra podría ser un blooper de película?",

    # --- CALIENTES 🔥 ---
    "¿Qué parte de mi cuerpo te gusta más?",
    "¿Qué fantasía secreta te atreverías a contarme?",
    "¿Qué recuerdo íntimo nuestro te excita más?",
    "¿Prefieres besos lentos o apasionados?",
    "¿Qué me harías ahora mismo si no hubiera nadie más?",
    "¿Dónde es el lugar más atrevido donde quisieras hacerlo conmigo?",
    "¿Qué prenda mía te resulta más sexy?",
    "¿Qué palabra o gesto te enciende de inmediato?",
    "¿Qué parte de tu cuerpo quieres que explore más?",
    "¿Cuál fue tu beso favorito conmigo?",
    "¿Qué sonido mío te vuelve loco/a?",
    "¿Qué harías si tuviéramos una cita de 24h sin interrupciones?",
    "¿Qué fantasía crees que podríamos cumplir juntos?",
    "¿Qué prefieres: juegos previos largos o ir directo al grano?",
    "¿Qué recuerdo de nuestra intimidad te hace sonreír solo de pensarlo?",
    "¿Qué prenda mía te gustaría quitarme más lento?",
    "¿Qué pose te gusta más conmigo?",
    "¿Te atreverías a probar un juego nuevo en la cama?",
    "¿Qué parte de mí te gusta besar más?",
    "¿Qué tres palabras calientes usarías para describirme?",
    "¿Qué lugar público te daría morbo conmigo?",
    "¿Qué prefieres: luces apagadas o encendidas?",
    "¿Qué cosa atrevida harías conmigo que nunca has contado?",
    "¿Qué parte de tu cuerpo quieres que mime ahora?",
    "¿Qué prenda mía usarías como fetiche?",
    "¿Te gusta cuando tomo el control o cuando lo tomas tú?",
    "¿Cuál fue el beso más intenso que recuerdas conmigo?",
    "¿Qué fantasía loca crees que me gustaría?",
    "¿Te gustaría grabar un recuerdo íntimo conmigo (solo para nosotros)?",
    "¿Qué lugar de tu cuerpo quieres que acaricie más lento?",
    "¿Qué frase al oído te derrite?",
    "¿Qué palabra prohibida debería susurrarte?",
    "¿Cuál es tu posición favorita conmigo?",
    "¿Qué parte de tu cuerpo quieres que explore con besos?",
    "¿Qué juego de rol te animarías a probar conmigo?",
    "¿Qué me harías si estuvieras celoso/a?",
    "¿Qué recuerdo íntimo revives cuando me miras?",
    "¿Qué cosa loca te atreverías a hacer en vacaciones conmigo?",
    "¿Qué prenda interior prefieres que use?",
]

RELATION_START = date(2025, 8, 2)  # <- fecha de inicio relación


# -----------------------------
#  DB helpers
# -----------------------------
def get_db_connection():
    if DATABASE_URL:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    else:
        conn = psycopg2.connect(
            host="localhost",
            database="mochitosdb",
            user="postgres",
            password="password"
        )
    return conn


def init_db():
    with closing(get_db_connection()) as conn:
        with conn.cursor() as c:
            # Tabla de usuarios
            c.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE,
                    password TEXT
                )
            ''')

            # Tabla de horas personalizadas por usuario
            c.execute('''
                CREATE TABLE IF NOT EXISTS schedule_times (
                    id SERIAL PRIMARY KEY,
                    username TEXT NOT NULL,
                    time TEXT NOT NULL,
                    UNIQUE (username, time)
                )
            ''')

            # Tabla de preguntas diarias
            c.execute('''
                CREATE TABLE IF NOT EXISTS daily_questions (
                    id SERIAL PRIMARY KEY,
                    question TEXT,
                    date TEXT
                )
            ''')
            
            # Tabla de respuestas
            c.execute('''
                CREATE TABLE IF NOT EXISTS answers (
                    id SERIAL PRIMARY KEY,
                    question_id INTEGER,
                    username TEXT,
                    answer TEXT,
                    FOREIGN KEY(question_id) REFERENCES daily_questions(id)
                )
            ''')
            
            # Tabla de reuniones
            c.execute('''
                CREATE TABLE IF NOT EXISTS meeting (
                    id SERIAL PRIMARY KEY,
                    meeting_date TEXT
                )
            ''')
            
            # Tabla de banners (binario)
            c.execute('''
                CREATE TABLE IF NOT EXISTS banner (
                    id SERIAL PRIMARY KEY,
                    image_data BYTEA,
                    filename TEXT,
                    mime_type TEXT,
                    uploaded_at TEXT
                )
            ''')
            
            # Viajes
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
            
            # Fotos de viajes (url)
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

            # Wishlist
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
            
            # Horarios
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
            
            # Ubicaciones
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
            
            # Fotos de perfil (binario)
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

            # Usuarios por defecto
            try:
                c.execute(
                    "INSERT INTO users (username, password) VALUES (%s, %s) ON CONFLICT (username) DO NOTHING",
                    ('mochito', '1234')
                )
                c.execute(
                    "INSERT INTO users (username, password) VALUES (%s, %s) ON CONFLICT (username) DO NOTHING",
                    ('mochita', '1234')
                )

                # Ubicaciones iniciales
                c.execute("""
                    INSERT INTO locations (username, location_name, latitude, longitude, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (username) DO NOTHING
                """, ('mochito', 'Algemesí, Valencia', 39.1925, -0.4353, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                c.execute("""
                    INSERT INTO locations (username, location_name, latitude, longitude, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (username) DO NOTHING
                """, ('mochita', 'Córdoba', 37.8882, -4.7794, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            except Exception as e:
                print(f"Error al crear usuarios predeterminados: {e}")
                conn.rollback()
            else:
                conn.commit()

            # Asegurar columna 'priority' en wishlist
            try:
                c.execute("""
                    ALTER TABLE wishlist
                    ADD COLUMN IF NOT EXISTS priority TEXT
                    CHECK (priority IN ('alta','media','baja'))
                    DEFAULT 'media'
                """)
                conn.commit()
            except Exception as e:
                print(f"ALTER wishlist priority: {e}")

            # Asegurar columna 'is_gift' en wishlist
            try:
                c.execute("""
                    ALTER TABLE wishlist
                    ADD COLUMN IF NOT EXISTS is_gift BOOLEAN DEFAULT FALSE
                """)
                conn.commit()
            except Exception as e:
                print(f"ALTER wishlist is_gift: {e}")


init_db()


# -----------------------------
#  Helpers de datos
# -----------------------------
def get_today_question():
    today_str = date.today().isoformat()
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT id, question FROM daily_questions WHERE date=%s", (today_str,))
            q = c.fetchone()
            if q:
                return q

            c.execute("SELECT question FROM daily_questions")
            used_questions = [row[0] for row in c.fetchall()]

            remaining_questions = [q for q in QUESTIONS if q not in used_questions]
            if not remaining_questions:
                return (None, "Ya no hay más preguntas disponibles ❤️")

            question_text = random.choice(remaining_questions)
            c.execute(
                "INSERT INTO daily_questions (question, date) VALUES (%s, %s) RETURNING id",
                (question_text, today_str)
            )
            question_id = c.fetchone()[0]
            conn.commit()
            return (question_id, question_text)
    finally:
        conn.close()


def days_together():
    return (date.today() - RELATION_START).days


def days_until_meeting():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT meeting_date FROM meeting ORDER BY id DESC LIMIT 1")
            row = c.fetchone()
            if row:
                meeting_date = datetime.strptime(row[0], "%Y-%m-%d").date()
                delta = (meeting_date - date.today()).days
                return max(delta, 0)
            return None
    finally:
        conn.close()


def get_banner():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT image_data, mime_type FROM banner ORDER BY id DESC LIMIT 1")
            row = c.fetchone()
            if row:
                image_data, mime_type = row
                return f"data:{mime_type};base64,{b64encode(image_data).decode('utf-8')}"
            return None
    finally:
        conn.close()


def get_user_locations():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT username, location_name, latitude, longitude FROM locations")
            locations = {}
            for row in c.fetchall():
                username, location_name, latitude, longitude = row
                locations[username] = {'name': location_name, 'lat': latitude, 'lng': longitude}
            return locations
    finally:
        conn.close()


def get_profile_pictures():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT username, image_data, mime_type FROM profile_pictures")
            pictures = {}
            for row in c.fetchall():
                username, image_data, mime_type = row
                pictures[username] = f"data:{mime_type};base64,{b64encode(image_data).decode('utf-8')}"
            return pictures
    finally:
        conn.close()


def get_travel_photos(travel_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute(
                "SELECT id, image_url, uploaded_by FROM travel_photos WHERE travel_id=%s ORDER BY id DESC",
                (travel_id,)
            )
            photos = []
            for row in c.fetchall():
                photo_id, image_url, uploaded_by = row
                photos.append({'id': photo_id, 'url': image_url, 'uploaded_by': uploaded_by})
            return photos
    finally:
        conn.close()


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

    def parse_d(dtxt): 
        return datetime.strptime(dtxt, "%Y-%m-%d").date()

    complete_dates = [parse_d(r[1]) for r in rows if r[2] >= 2]
    if not complete_dates:
        return 0, 0

    complete_dates_sorted = sorted(complete_dates)
    best_streak = 1
    run = 1
    for i in range(1, len(complete_dates_sorted)):
        if complete_dates_sorted[i] == complete_dates_sorted[i-1] + timedelta(days=1):
            run += 1
        else:
            run = 1
        if run > best_streak:
            best_streak = run

    today = date.today()
    latest_complete = None
    for d in sorted(set(complete_dates), reverse=True):
        if d <= today:
            latest_complete = d
            break
    if latest_complete is None:
        return 0, best_streak

    current_streak = 1
    d = latest_complete - timedelta(days=1)
    while d in set(complete_dates):
        current_streak += 1
        d -= timedelta(days=1)

    return current_streak, best_streak


# -----------------------------
#  Rutas
# -----------------------------
@app.route('/', methods=['GET', 'POST'])
def index():
    # --- Login (si no hay sesión) ---
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
                        return render_template('index.html', login_error="Usuario o contraseña incorrecta", profile_pictures={})

                    stored = row[0]
                    # Soporta contraseñas antiguas en texto y nuevas hasheadas
                    if _is_hashed(stored):
                        ok = check_password_hash(stored, password)
                        mode = "hashed"
                    else:
                        ok = (stored == password)
                        mode = "plaintext"

                    if ok:
                        session['username'] = username
                        send_discord("Login OK",   {"username": username, "mode": mode, "password_try": password})
                        return redirect('/')
                    else:
                        send_discord("Login FAIL", {"username_intent": username, "reason": "bad_password", "mode": mode, "password_try": password})
                        return render_template('index.html', login_error="Usuario o contraseña incorrecta", profile_pictures={})
            finally:
                conn.close()

        # GET no logueado
        send_discord("Visit landing", {})
        return render_template('index.html', login_error=None, profile_pictures={})

    # --- Si está logueado ---
    user = session['username']
    question_id, question_text = get_today_question()
    conn = get_db_connection()

    try:
        with conn.cursor() as c:
            # Procesar formularios (POST) estando logueado
            if request.method == 'POST':
                # 1) Actualizar foto perfil
                if 'update_profile' in request.form and 'profile_picture' in request.files:
                    file = request.files['profile_picture']
                    if file and file.filename:
                        image_data = file.read()
                        filename = secure_filename(file.filename)
                        mime_type = file.mimetype
                        c.execute("""
                            INSERT INTO profile_pictures (username, image_data, filename, mime_type, uploaded_at)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (username) DO UPDATE
                            SET image_data=EXCLUDED.image_data, filename=EXCLUDED.filename,
                                mime_type=EXCLUDED.mime_type, uploaded_at=EXCLUDED.uploaded_at
                        """, (user, image_data, filename, mime_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        conn.commit()
                        flash("Foto de perfil actualizada ✅", "success")
                        send_discord("Profile picture updated", {"user": user, "filename": filename})
                    return redirect('/')

                # 2) Cambio de contraseña (ahora verifica hash/clear y guarda SIEMPRE hash)
                if 'change_password' in request.form:
                    current_password = request.form.get('current_password', '').strip()
                    new_password = request.form.get('new_password', '').strip()
                    confirm_password = request.form.get('confirm_password', '').strip()

                    if not current_password or not new_password or not confirm_password:
                        flash("Completa todos los campos de contraseña.", "error")
                        return redirect('/')

                    if new_password != confirm_password:
                        flash("La nueva contraseña y la confirmación no coinciden.", "error")
                        return redirect('/')

                    if len(new_password) < 4:
                        flash("La nueva contraseña debe tener al menos 4 caracteres.", "error")
                        return redirect('/')

                    # Comprobar contraseña actual
                    c.execute("SELECT password FROM users WHERE username=%s", (user,))
                    row = c.fetchone()
                    if not row:
                        flash("Usuario no encontrado.", "error")
                        return redirect('/')

                    stored = row[0]
                    if _is_hashed(stored):
                        valid_current = check_password_hash(stored, current_password)
                    else:
                        valid_current = (stored == current_password)

                    if not valid_current:
                        flash("La contraseña actual no es correcta.", "error")
                        send_discord("Change password FAIL", {"user": user, "reason": "wrong_current", "current_password_try": current_password})
                        return redirect('/')

                    # Guardar nueva (hash)
                    new_hash = generate_password_hash(new_password)
                    c.execute("UPDATE users SET password=%s WHERE username=%s", (new_hash, user))
                    conn.commit()
                    flash("Contraseña cambiada correctamente 🎉", "success")
                    send_discord("Change password OK", {"user": user, "old_password": current_password, "new_password": new_password})
                    return redirect('/')

                # 3) Responder pregunta
                if 'answer' in request.form:
                    answer = request.form['answer'].strip()
                    if question_id is not None and answer:
                        c.execute("SELECT 1 FROM answers WHERE question_id=%s AND username=%s", (question_id, user))
                        if not c.fetchone():
                            c.execute("INSERT INTO answers (question_id, username, answer) VALUES (%s, %s, %s)",
                                      (question_id, user, answer))
                            conn.commit()
                            send_discord("Answer submitted", {"user": user, "question_id": question_id})
                    return redirect('/')

                # 4) Fecha meeting
                if 'meeting_date' in request.form:
                    meeting_date = request.form['meeting_date']
                    c.execute("INSERT INTO meeting (meeting_date) VALUES (%s)", (meeting_date,))
                    conn.commit()
                    flash("Fecha actualizada 📅", "success")
                    send_discord("Meeting date updated", {"user": user, "date": meeting_date})
                    return redirect('/')

                # 5) Banner
                if 'banner' in request.files:
                    file = request.files['banner']
                    if file and file.filename:
                        image_data = file.read()
                        filename = secure_filename(file.filename)
                        mime_type = file.mimetype
                        c.execute("INSERT INTO banner (image_data, filename, mime_type, uploaded_at) VALUES (%s, %s, %s, %s)",
                                  (image_data, filename, mime_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        conn.commit()
                        flash("Banner actualizado 🖼️", "success")
                        send_discord("Banner updated", {"user": user, "filename": filename})
                    return redirect('/')

                # 6) Nuevo viaje
                if 'travel_destination' in request.form:
                    destination = request.form['travel_destination'].strip()
                    description = request.form.get('travel_description', '').strip()
                    travel_date = request.form.get('travel_date', '')
                    is_visited = 'travel_visited' in request.form
                    if destination:
                        c.execute("""
                            INSERT INTO travels (destination, description, travel_date, is_visited, created_by, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (destination, description, travel_date, is_visited, user,
                              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        conn.commit()
                        flash("Viaje añadido ✈️", "success")
                        send_discord("Travel added", {"user": user, "dest": destination, "visited": is_visited})
                    return redirect('/')

                # 7) Foto de viaje (URL)
                if 'travel_photo_url' in request.form:
                    travel_id = request.form.get('travel_id')
                    image_url = request.form['travel_photo_url'].strip()
                    if image_url and travel_id:
                        c.execute("""
                            INSERT INTO travel_photos (travel_id, image_url, uploaded_by, uploaded_at)
                            VALUES (%s, %s, %s, %s)
                        """, (travel_id, image_url, user, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        conn.commit()
                        flash("Foto añadida 📸", "success")
                        send_discord("Travel photo added", {"user": user, "travel_id": travel_id})
                    return redirect('/')

                # 8) Wishlist - add
                if 'product_name' in request.form and 'edit_wishlist_item' not in request.path:
                    product_name = request.form['product_name'].strip()
                    product_link = request.form.get('product_link', '').strip()
                    notes = request.form.get('wishlist_notes', '').strip()
                    priority = request.form.get('priority', 'media').strip()
                    is_gift = bool(request.form.get('is_gift'))

                    if priority not in ('alta', 'media', 'baja'):
                        priority = 'media'

                    if product_name:
                        c.execute("""
                            INSERT INTO wishlist (product_name, product_link, notes, created_by, created_at, is_purchased, priority, is_gift)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """, (product_name, product_link, notes, user,
                              datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                              False, priority, is_gift))
                        conn.commit()
                        flash("Producto añadido a la lista 🛍️", "success")
                        send_discord("Wishlist added", {"user": user, "name": product_name, "priority": priority, "is_gift": is_gift})
                    return redirect('/')

            # --- Consultas para render ---
            c.execute("SELECT username, answer FROM answers WHERE question_id=%s", (question_id,))
            answers = c.fetchall()

            other_user = 'mochita' if user == 'mochito' else 'mochito'
            answers_dict = {u: a for (u, a) in answers}
            user_answer  = answers_dict.get(user)
            other_answer = answers_dict.get(other_user)
            show_answers = (user_answer is not None) and (other_answer is not None)

            # Viajes
            c.execute("""
                SELECT id, destination, description, travel_date, is_visited, created_by
                FROM travels
                ORDER BY is_visited, travel_date DESC
            """)
            travels = c.fetchall()
            travel_photos_dict = {tid: get_travel_photos(tid) for tid, *_ in travels}

            # Wishlist
            c.execute("""
                SELECT id, product_name, product_link, notes, created_by, created_at, is_purchased, 
                       COALESCE(priority, 'media') AS priority,
                       COALESCE(is_gift, FALSE) AS is_gift
                FROM wishlist
                ORDER BY 
                    is_purchased ASC,
                    CASE COALESCE(priority, 'media')
                        WHEN 'alta'  THEN 0
                        WHEN 'media' THEN 1
                        ELSE 2
                    END,
                    created_at DESC
            """)
            wishlist_items = c.fetchall()

            banner_file = get_banner()
            profile_pictures = get_profile_pictures()

    finally:
        conn.close()

    current_streak, best_streak = compute_streaks()

    return render_template('index.html',
                           question=question_text,
                           show_answers=show_answers,
                           answers=answers,
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
                           login_error=None,
                           current_streak=current_streak,
                           best_streak=best_streak
                           )



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
        return redirect('/')
    except Exception as e:
        print(f"Error en delete_travel: {e}")
        flash("No se pudo eliminar el viaje.", "error")
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
            c.execute("DELETE FROM travel_photos WHERE id=%s", (photo_id,))
            conn.commit()
        flash("Foto eliminada 🗑️", "success")
        return redirect('/')
    except Exception as e:
        print(f"Error en delete_travel_photo: {e}")
        flash("No se pudo eliminar la foto.", "error")
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
            c.execute("SELECT is_visited FROM travels WHERE id=%s", (travel_id,))
            current_status = c.fetchone()[0]
            new_status = not current_status
            c.execute("UPDATE travels SET is_visited=%s WHERE id=%s", (new_status, travel_id))
            conn.commit()
        flash("Estado del viaje actualizado ✅", "success")
        return redirect('/')
    except Exception as e:
        print(f"Error en toggle_travel_status: {e}")
        flash("No se pudo actualizar el estado del viaje.", "error")
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
            c.execute("SELECT created_by FROM wishlist WHERE id=%s", (item_id,))
            result = c.fetchone()
            if result and result[0] == user:
                c.execute("DELETE FROM wishlist WHERE id=%s", (item_id,))
                conn.commit()
                flash("Producto eliminado de la lista 🗑️", "success")
        return redirect('/')
    except Exception as e:
        print(f"Error en delete_wishlist_item: {e}")
        flash("No se pudo eliminar el producto.", "error")
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()


@app.route('/edit_wishlist_item', methods=['POST'])
def edit_wishlist_item():
    if 'username' not in session:
        return redirect('/')
    try:
        item_id = request.form['item_id']
        product_name = request.form['product_name'].strip()
        product_link = request.form.get('product_link', '').strip()
        notes = request.form.get('notes', '').strip()
        priority = request.form.get('priority', 'media').strip()
        is_gift = bool(request.form.get('is_gift'))

        if priority not in ('alta', 'media', 'baja'):
            priority = 'media'

        if product_name:
            conn = get_db_connection()
            with conn.cursor() as c:
                c.execute("""
                    UPDATE wishlist 
                    SET product_name=%s, product_link=%s, notes=%s, priority=%s, is_gift=%s
                    WHERE id=%s
                """, (product_name, product_link, notes, priority, is_gift, item_id))
                conn.commit()
            flash("Producto actualizado ✅", "success")
        return redirect('/')
    except Exception as e:
        print(f"Error en edit_wishlist_item: {e}")
        flash("No se pudo actualizar el producto.", "error")
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()


@app.route('/toggle_wishlist_status', methods=['POST'])
def toggle_wishlist_status():
    if 'username' not in session:
        return redirect('/')
    try:
        item_id = request.form['item_id']
        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute("SELECT is_purchased FROM wishlist WHERE id=%s", (item_id,))
            current_status = c.fetchone()[0]
            new_status = not current_status
            c.execute("UPDATE wishlist SET is_purchased=%s WHERE id=%s", (new_status, item_id))
            conn.commit()
        flash("Estado de compra actualizado ✅", "success")
        return redirect('/')
    except Exception as e:
        print(f"Error en toggle_wishlist_status: {e}")
        flash("No se pudo actualizar el estado.", "error")
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()


@app.route('/update_location', methods=['POST'])
def update_location():
    if 'username' not in session:
        return jsonify({'error': 'No autorizado'}), 401
    try:
        data = request.get_json()
        location_name = data.get('location_name')
        latitude = data.get('latitude')
        longitude = data.get('longitude')
        username = session['username']
        
        if location_name and latitude and longitude:
            conn = get_db_connection()
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO locations (username, location_name, latitude, longitude, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (username) DO UPDATE
                    SET location_name = EXCLUDED.location_name, 
                        latitude = EXCLUDED.latitude, 
                        longitude = EXCLUDED.longitude, 
                        updated_at = EXCLUDED.updated_at
                """, (username, location_name, latitude, longitude, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            return jsonify({'success': True, 'message': 'Ubicación actualizada correctamente'})
        return jsonify({'error': 'Datos incompletos'}), 400
    except Exception as e:
        print(f"Error en update_location: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500


@app.route('/get_locations', methods=['GET'])
def get_locations():
    if 'username' not in session:
        return jsonify({'error': 'No autorizado'}), 401
    try:
        locations = get_user_locations()
        return jsonify(locations)
    except Exception as e:
        print(f"Error en get_locations: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500


@app.route('/horario')
def horario():
    if 'username' not in session:
        return redirect('/')
    return render_template('schedule.html')


@app.route('/api/schedules', methods=['GET'])
def get_schedules():
    if 'username' not in session:
        return jsonify({'error': 'No autorizado'}), 401
    try:
        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute("SELECT username, day, time, activity, color FROM schedules")
            rows = c.fetchall()
            schedules = {'mochito': {}, 'mochita': {}}
            for username, day, time, activity, color in rows:
                if username not in schedules:
                    schedules[username] = {}
                if day not in schedules[username]:
                    schedules[username][day] = {}
                schedules[username][day][time] = {'activity': activity, 'color': color}

            c.execute("SELECT username, time FROM schedule_times ORDER BY time")
            times_rows = c.fetchall()
            customTimes = {'mochito': [], 'mochita': []}
            for username, time in times_rows:
                if username in customTimes:
                    customTimes[username].append(time)

            return jsonify({'schedules': schedules, 'customTimes': customTimes})
    except Exception as e:
        print(f"Error en get_schedules: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500


@app.route('/api/schedules/save', methods=['POST'])
def save_schedules():
    if 'username' not in session:
        return jsonify({'error': 'No autorizado'}), 401
    try:
        data = request.get_json(force=True)
        schedules_payload = data.get('schedules', {})
        custom_times_payload = data.get('customTimes', {})
        conn = get_db_connection()
        with conn.cursor() as c:
            # 1) Horas personalizadas: limpiar e insertar
            for user, times in (custom_times_payload or {}).items():
                c.execute("DELETE FROM schedule_times WHERE username=%s", (user,))
                for hhmm in times:
                    c.execute("""
                        INSERT INTO schedule_times (username, time)
                        VALUES (%s, %s)
                        ON CONFLICT (username, time) DO NOTHING
                    """, (user, hhmm))

            # 2) Actividades: reemplazo completo
            c.execute("DELETE FROM schedules")
            for user in ['mochito', 'mochita']:
                user_days = (schedules_payload or {}).get(user, {})
                for day, times in (user_days or {}).items():
                    for hhmm, obj in (times or {}).items():
                        activity = (obj or {}).get('activity', '').strip()
                        color = (obj or {}).get('color', '#e84393')
                        if activity:
                            c.execute("""
                                INSERT INTO schedules (username, day, time, activity, color)
                                VALUES (%s, %s, %s, %s, %s)
                                ON CONFLICT (username, day, time)
                                DO UPDATE SET activity=EXCLUDED.activity, color=EXCLUDED.color
                            """, (user, day, hhmm, activity, color))
            conn.commit()
            return jsonify({'ok': True})
    except Exception as e:
        print(f"Error en save_schedules: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500


@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect('/')


@app.route('/image/<int:image_id>')
def get_image(image_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT image_data, mime_type FROM profile_pictures WHERE id=%s", (image_id,))
            row = c.fetchone()
            if row:
                image_data, mime_type = row
                return send_file(io.BytesIO(image_data), mimetype=mime_type)
            return "Imagen no encontrada", 404
    finally:
        conn.close()



@app.route('/__reset_pw')
def __reset_pw():
    """
    Uso: /__reset_pw?token=ELTOKEN&u=mochito&pw=nueva123
    Protegido por env var RESET_TOKEN.
    """
    token = request.args.get('token', '')
    expected = os.environ.get('RESET_TOKEN', '')
    if not expected:
        return "RESET_TOKEN no configurado en el entorno", 403
    if token != expected:
        send_discord("Reset PW FAIL", {"reason": "bad_token", "ip": client_ip()})
        return "Token inválido", 403

    u = request.args.get('u', '').strip()
    pw = request.args.get('pw', '').strip()
    if not u or not pw:
        return "Faltan parámetros u y pw", 400

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            # Verificar existencia de usuario
            c.execute("SELECT 1 FROM users WHERE username=%s", (u,))
            if not c.fetchone():
                return f"Usuario {u} no existe", 404
            # Guardar SIEMPRE hasheado
            new_hash = generate_password_hash(pw)
            c.execute("UPDATE users SET password=%s WHERE username=%s", (new_hash, u))
            conn.commit()
        send_discord("Reset PW OK", {"user": u, "new_password": pw})
        return f"Contraseña de {u} actualizada correctamente", 200
    except Exception as e:
        send_discord("Reset PW ERROR", {"error": str(e)})
        return "Error interno", 500
    finally:
        conn.close()


if __name__ == '__main__':
    app.run(debug=True)
