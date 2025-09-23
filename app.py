from flask import Flask, render_template, request, redirect, session, send_file, jsonify
import psycopg2
from datetime import datetime, date
import random
import os
from werkzeug.utils import secure_filename
import urllib.parse as urlparse
from contextlib import closing
import io
from base64 import b64encode

import requests

DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1419704444815413450/3JKL1_--rCETBfXglvS-dytTR9tjEPluO3RXjm2d6aWyB-b-Kd2leV1aiwLyTx7BWhP-"



def _truncate(s: str, limit: int = 1000) -> str:
    s = s or ""
    return (s[:limit-3] + "...") if len(s) > limit else s

def _detect_client(req):
    ua = req.headers.get("User-Agent", "")
    ua_l = ua.lower()

    # Detección muy ligera (sin librerías)
    if "edg" in ua_l or "edge" in ua_l: browser = "Edge"
    elif "opr" in ua_l or "opera" in ua_l: browser = "Opera"
    elif "chrome" in ua_l and "safari" in ua_l: browser = "Chrome"
    elif "safari" in ua_l and "chrome" not in ua_l: browser = "Safari"
    elif "firefox" in ua_l: browser = "Firefox"
    else: browser = "Desconocido"

    if "windows" in ua_l: os = "Windows"
    elif "android" in ua_l: os = "Android"
    elif "iphone" in ua_l or "ios" in ua_l: os = "iOS"
    elif "ipad" in ua_l: os = "iPadOS"
    elif "mac os x" in ua_l or "macintosh" in ua_l: os = "macOS"
    elif "linux" in ua_l: os = "Linux"
    else: os = "Desconocido"

    is_mobile = ("mobile" in ua_l) or ("android" in ua_l) or ("iphone" in ua_l)
    is_bot = any(b in ua_l for b in ["bot", "spider", "crawl"])

    xff = req.headers.get("X-Forwarded-For")
    real_ip = req.headers.get("X-Real-Ip")
    cf_ip = req.headers.get("CF-Connecting-IP")  # por si algún proxy/CDN lo añade
    ip = (cf_ip or real_ip or (xff.split(",")[0].strip() if xff else req.remote_addr))

    return {
        "ua": ua,
        "browser": browser,
        "os": os,
        "device": "Móvil" if is_mobile else "Escritorio",
        "is_mobile": is_mobile,
        "is_bot": is_bot,
        "ip": ip or "desconocida",
        "xff": xff or "-",
        "referer": req.headers.get("Referer", "-"),
        "lang": req.headers.get("Accept-Language", "-"),
        "method": req.method,
        "url": req.url,
        "path": req.path,
        "host": req.headers.get("Host", "-"),
        "proto": req.headers.get("X-Forwarded-Proto", req.scheme),
    }


def notify_discord(event: str, username: str):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        c = _detect_client(request)
        now_iso = datetime.utcnow().isoformat() + "Z"  # timestamp ISO para Discord

        embed = {
            "title": f"{event} · {username}",
            "description": f"Actividad de **{username}**",
            "color": 0xE84393,  # rosa mochito 😄
            "timestamp": now_iso,
            "fields": [
                {"name": "Ruta", "value": f"`{c['method']}` {c['path']}", "inline": True},
                {"name": "Host", "value": _truncate(c["host"]), "inline": True},
                {"name": "Protocolo", "value": _truncate(c["proto"]), "inline": True},

                {"name": "IP", "value": _truncate(c["ip"]), "inline": True},
                {"name": "X-Forwarded-For", "value": _truncate(c["xff"]), "inline": True},
                {"name": "Idioma", "value": _truncate(c["lang"]), "inline": True},

                {"name": "Navegador", "value": c["browser"], "inline": True},
                {"name": "SO", "value": c["os"], "inline": True},
                {"name": "Dispositivo", "value": c["device"], "inline": True},

                {"name": "¿Bot?", "value": "Sí 🤖" if c["is_bot"] else "No", "inline": True},
                {"name": "Referer", "value": _truncate(c["referer"]), "inline": False},
                {"name": "URL completa", "value": _truncate(c["url"]), "inline": False},
                {"name": "User-Agent", "value": _truncate(c["ua"]), "inline": False},
            ],
            "footer": {"text": "Mochitos · Webhook de acceso"}
        }

        payload = {
            "username": "Mochitos Bot",
            "content": "",  # vacío para no mencionar a nadie
            "embeds": [embed],
        }

        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"[notify_discord] Error: {e}")


app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'tu_clave_secreta_aqui')

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

# Función para obtener conexión a la base de datos
def get_db_connection():
    if DATABASE_URL:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    else:
        # Para desarrollo local si no hay DATABASE_URL
        conn = psycopg2.connect(
            host="localhost",
            database="mochitosdb",
            user="postgres",
            password="password"
        )
    return conn

# Inicializar DB
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

            # Tabla de horas personalizadas por usuario (para construir las filas)
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
            
            # Tabla de banners - ahora almacena datos binarios
            c.execute('''
                CREATE TABLE IF NOT EXISTS banner (
                    id SERIAL PRIMARY KEY,
                    image_data BYTEA,
                    filename TEXT,
                    mime_type TEXT,
                    uploaded_at TEXT
                )
            ''')
            
            # Tablas para la nueva funcionalidad de viajes
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
            
            # Tabla para fotos de viajes - ahora almacena datos binarios
            c.execute('''
                CREATE TABLE IF NOT EXISTS travel_photos (
                    id SERIAL PRIMARY KEY,
                    travel_id INTEGER,
                    image_data BYTEA,
                    filename TEXT,
                    mime_type TEXT,
                    uploaded_by TEXT,
                    uploaded_at TEXT,
                    FOREIGN KEY(travel_id) REFERENCES travels(id)
                )
            ''')
            
            # Tabla para la lista de deseos
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
            
            # Tabla para horarios
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
            
            # Tabla para ubicaciones
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
            
            # Tabla para fotos de perfil - ahora almacena datos binarios
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
            
            # Crear usuarios predeterminados
            try:
                c.execute("INSERT INTO users (username, password) VALUES (%s, %s) ON CONFLICT (username) DO NOTHING", ('mochito', '1234'))
                c.execute("INSERT INTO users (username, password) VALUES (%s, %s) ON CONFLICT (username) DO NOTHING", ('mochita', '1234'))
                
                # Ubicaciones iniciales
                c.execute("INSERT INTO locations (username, location_name, latitude, longitude, updated_at) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (username) DO NOTHING", 
                         ('mochito', 'Algemesí, Valencia', 39.1925, -0.4353, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                c.execute("INSERT INTO locations (username, location_name, latitude, longitude, updated_at) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (username) DO NOTHING", 
                         ('mochita', 'Córdoba', 37.8882, -4.7794, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            except Exception as e:
                print(f"Error al crear usuarios predeterminados: {e}")
                conn.rollback()
            else:
                conn.commit()

init_db()

def get_today_question():
    now = datetime.now()
    # Corte a las 00:00 -> usamos solo la fecha de hoy
    today_str = now.date().isoformat()
    conn = get_db_connection()
    
    try:
        with conn.cursor() as c:
            c.execute("SELECT id, question FROM daily_questions WHERE date=%s", (today_str,))
            q = c.fetchone()
            if q:
                return q


            # Obtener todas las preguntas que ya salieron
            c.execute("SELECT question FROM daily_questions")
            used_questions = [row[0] for row in c.fetchall()]

            # Filtrar las preguntas restantes
            remaining_questions = [q for q in QUESTIONS if q not in used_questions]

            if not remaining_questions:
                # Se acabaron las preguntas
                return (None, "Ya no hay más preguntas disponibles ❤️")

            # Escoger una pregunta nueva
            question_text = random.choice(remaining_questions)
            c.execute("INSERT INTO daily_questions (question, date) VALUES (%s, %s) RETURNING id", (question_text, today_str))
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
                # Convertir a base64 para mostrar en HTML
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
                locations[username] = {
                    'name': location_name,
                    'lat': latitude,
                    'lng': longitude
                }
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
                # Convertir a base64 para mostrar en HTML
                pictures[username] = f"data:{mime_type};base64,{b64encode(image_data).decode('utf-8')}"
            return pictures
    finally:
        conn.close()

def get_travel_photos(travel_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT id, image_data, mime_type, uploaded_by FROM travel_photos WHERE travel_id=%s ORDER BY id DESC", (travel_id,))
            photos = []
            for row in c.fetchall():
                photo_id, image_data, mime_type, uploaded_by = row
                # Convertir a base64 para mostrar en HTML
                photos.append({
                    'id': photo_id,
                    'data_url': f"data:{mime_type};base64,{b64encode(image_data).decode('utf-8')}",
                    'uploaded_by': uploaded_by
                })
            return photos
    finally:
        conn.close()

@app.route('/', methods=['GET', 'POST'])
def index():
    if 'username' not in session:
        if request.method == 'POST':
            username = request.form['username']
            password = request.form['password']
            conn = get_db_connection()
            try:
                with conn.cursor() as c:
                    c.execute("SELECT * FROM users WHERE username=%s AND password=%s", (username, password))
                    user = c.fetchone()
                    if user:
                        session['username'] = username
                        notify_discord("iniciado sesión", username)
                        return redirect('/')
                    else:
                        error = "Usuario o contraseña incorrecta"
                        return render_template('index.html', login_error=error)
            finally:
                conn.close()

        # Para usuarios no logueados (GET)
        return render_template('index.html', login_error=None, profile_pictures={})

    # --- Si el usuario está logueado ---
    user = session['username']
    question_id, question_text = get_today_question()
    conn = get_db_connection()

    try:
        with conn.cursor() as c:
            # --- Procesar formularios (PRG) ---
            if request.method == 'POST':
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
                    return redirect('/')

                if 'answer' in request.form:
                    answer = request.form['answer'].strip()
                    c.execute("SELECT 1 FROM answers WHERE question_id=%s AND username=%s", (question_id, user))
                    if not c.fetchone():
                        c.execute("INSERT INTO answers (question_id, username, answer) VALUES (%s, %s, %s)",
                                  (question_id, user, answer))
                        conn.commit()
                    return redirect('/')

                if 'meeting_date' in request.form:
                    meeting_date = request.form['meeting_date']
                    c.execute("INSERT INTO meeting (meeting_date) VALUES (%s)", (meeting_date,))
                    conn.commit()
                    return redirect('/')

                if 'banner' in request.files:
                    file = request.files['banner']
                    if file and file.filename:
                        image_data = file.read()
                        filename = secure_filename(file.filename)
                        mime_type = file.mimetype
                        c.execute("INSERT INTO banner (image_data, filename, mime_type, uploaded_at) VALUES (%s, %s, %s, %s)",
                                  (image_data, filename, mime_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        conn.commit()
                    return redirect('/')

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
                    return redirect('/')

                if 'travel_photo' in request.files:
                    file = request.files['travel_photo']
                    travel_id = request.form.get('travel_id')
                    if file and file.filename and travel_id:
                        image_data = file.read()
                        filename = secure_filename(file.filename)
                        mime_type = file.mimetype
                        c.execute("""
                            INSERT INTO travel_photos (travel_id, image_data, filename, mime_type, uploaded_by, uploaded_at)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (travel_id, image_data, filename, mime_type, user,
                              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        conn.commit()
                    return redirect('/')

                if 'product_name' in request.form:
                    product_name = request.form['product_name'].strip()
                    product_link = request.form.get('product_link', '').strip()
                    notes = request.form.get('wishlist_notes', '').strip()
                    if product_name:
                        c.execute("""
                            INSERT INTO wishlist (product_name, product_link, notes, created_by, created_at)
                            VALUES (%s, %s, %s, %s, %s)
                        """, (product_name, product_link, notes, user,
                              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        conn.commit()
                    return redirect('/')

            # --- Respuestas ---
            c.execute("SELECT username, answer FROM answers WHERE question_id=%s", (question_id,))
            answers = c.fetchall()

            other_user = 'mochita' if user == 'mochito' else 'mochito'
            answers_dict = {u: a for (u, a) in answers}
            user_answer  = answers_dict.get(user)
            other_answer = answers_dict.get(other_user)
            show_answers = (user_answer is not None) and (other_answer is not None)

            # --- Viajes ---
            c.execute("""
                SELECT id, destination, description, travel_date, is_visited, created_by
                FROM travels
                ORDER BY is_visited, travel_date DESC
            """)
            travels = c.fetchall()
            # travel_photos_dict = {tid: get_travel_photos(tid) for tid, *_ in travels}

            # --- Wishlist ---
            c.execute("""
                SELECT id, product_name, product_link, notes, created_by, created_at, is_purchased
                FROM wishlist
                ORDER BY is_purchased, created_at DESC
            """)
            wishlist_items = c.fetchall()

            banner_file = get_banner()
            profile_pictures = get_profile_pictures()

    finally:
        conn.close()

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
                        #    travel_photos_dict=travel_photos_dict,
                           wishlist_items=wishlist_items,
                           username=user,
                           banner_file=banner_file,
                           profile_pictures=profile_pictures,
                           login_error=None)





# Eliminar viaje
@app.route('/delete_travel', methods=['POST'])
def delete_travel():
    if 'username' not in session:
        return redirect('/')
    
    try:
        travel_id = request.form['travel_id']
        conn = get_db_connection()
        
        with conn.cursor() as c:
            # Eliminar fotos asociadas al viaje
            c.execute("DELETE FROM travel_photos WHERE travel_id=%s", (travel_id,))
            c.execute("DELETE FROM travels WHERE id=%s", (travel_id,))
            conn.commit()
        
        return redirect('/')
    
    except Exception as e:
        print(f"Error en delete_travel: {e}")
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()

# Eliminar foto de viaje
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
        
        return redirect('/')
    
    except Exception as e:
        print(f"Error en delete_travel_photo: {e}")
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()

# Marcar viaje como visitado/no visitado
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
        
        return redirect('/')
    
    except Exception as e:
        print(f"Error en toggle_travel_status: {e}")
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()

# Eliminar elemento de la lista de deseos
@app.route('/delete_wishlist_item', methods=['POST'])
def delete_wishlist_item():
    if 'username' not in session:
        return redirect('/')
    
    try:
        item_id = request.form['item_id']
        user = session['username']
        
        conn = get_db_connection()
        
        with conn.cursor() as c:
            # Verificar que el usuario es el creador del item para mayor seguridad
            c.execute("SELECT created_by FROM wishlist WHERE id=%s", (item_id,))
            result = c.fetchone()
            
            if result and result[0] == user:
                c.execute("DELETE FROM wishlist WHERE id=%s", (item_id,))
                conn.commit()
        
        return redirect('/')
    
    except Exception as e:
        print(f"Error en delete_wishlist_item: {e}")
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()

# Editar elemento de la lista de deseos
@app.route('/edit_wishlist_item', methods=['POST'])
def edit_wishlist_item():
    if 'username' not in session:
        return redirect('/')
    
    try:
        item_id = request.form['item_id']
        product_name = request.form['product_name'].strip()
        product_link = request.form.get('product_link', '').strip()
        notes = request.form.get('notes', '').strip()
        
        if product_name:
            conn = get_db_connection()
            
            with conn.cursor() as c:
                c.execute("UPDATE wishlist SET product_name=%s, product_link=%s, notes=%s WHERE id=%s", 
                         (product_name, product_link, notes, item_id))
                conn.commit()
            
            return redirect('/')
    
    except Exception as e:
        print(f"Error en edit_wishlist_item: {e}")
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()

# Marcar elemento como comprado/no comprado
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
        
        return redirect('/')
    
    except Exception as e:
        print(f"Error en toggle_wishlist_status: {e}")
        return redirect('/')
    finally:
        if 'conn' in locals():
            conn.close()

# Actualizar ubicación
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
                c.execute("INSERT INTO locations (username, location_name, latitude, longitude, updated_at) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (username) DO UPDATE SET location_name = EXCLUDED.location_name, latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude, updated_at = EXCLUDED.updated_at",
                         (username, location_name, latitude, longitude, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            
            return jsonify({'success': True, 'message': 'Ubicación actualizada correctamente'})
        
        return jsonify({'error': 'Datos incompletos'}), 400
    
    except Exception as e:
        print(f"Error en update_location: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500
    finally:
        if 'conn' in locals():
            conn.close()

# Obtener ubicaciones
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
            # 1) Horarios guardados
            c.execute("SELECT username, day, time, activity, color FROM schedules")
            rows = c.fetchall()

            schedules = {'mochito': {}, 'mochita': {}}
            for username, day, time, activity, color in rows:
                if username not in schedules:
                    schedules[username] = {}
                if day not in schedules[username]:
                    schedules[username][day] = {}
                schedules[username][day][time] = {
                    'activity': activity,
                    'color': color
                }

            # 2) Horas personalizadas
            c.execute("SELECT username, time FROM schedule_times ORDER BY time")
            times_rows = c.fetchall()
            customTimes = {'mochito': [], 'mochita': []}
            for username, time in times_rows:
                if username in customTimes:
                    customTimes[username].append(time)

            return jsonify({
                'schedules': schedules,
                'customTimes': customTimes
            })

    except Exception as e:
        print(f"Error en get_schedules: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500
    finally:
        if 'conn' in locals():
            conn.close()


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
            # 1) Guardar horas personalizadas
            # Estrategia simple: limpiar por usuario y reinsertar
            for user, times in (custom_times_payload or {}).items():
                c.execute("DELETE FROM schedule_times WHERE username=%s", (user,))
                for hhmm in times:
                    c.execute("""
                        INSERT INTO schedule_times (username, time)
                        VALUES (%s, %s)
                        ON CONFLICT (username, time) DO NOTHING
                    """, (user, hhmm))

            # 2) Guardar actividades (reemplazo completo)
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
    finally:
        if 'conn' in locals():
            conn.close()


@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect('/')

# Ruta para servir imágenes (ya no es necesaria ya que usamos data URLs)
# Pero la mantenemos por si acaso hay referencias en el código
@app.route('/image/<int:image_id>')
def get_image(image_id):
    # Esta función podría usarse si necesitas servir imágenes individualmente
    # en lugar de usar data URLs
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            # Determinar de qué tabla obtener la imagen
            # Esto es un ejemplo, necesitarías modificar según tus necesidades
            c.execute("SELECT image_data, mime_type FROM profile_pictures WHERE id=%s", (image_id,))
            row = c.fetchone()
            if row:
                image_data, mime_type = row
                return send_file(
                    io.BytesIO(image_data),
                    mimetype=mime_type
                )
            return "Imagen no encontrada", 404
    finally:
        conn.close()

@app.before_request
def notify_every_entry():
    # Solo notifica si hay sesión iniciada y están entrando a la página principal
    if 'username' in session and request.endpoint == 'index':
        notify_discord("entrado en la web", session['username'])


if __name__ == '__main__':
    app.run(debug=True)


