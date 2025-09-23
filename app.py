from flask import Flask, render_template, request, redirect, session, send_file, jsonify
import psycopg2
from datetime import datetime, date, timedelta
import random
import os
from werkzeug.utils import secure_filename
import urllib.parse as urlparse
from contextlib import closing
import io
from base64 import b64encode

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
            
            # Tabla para fotos de viajes - ahora solo almacena enlaces
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

                # Asegurar columna 'priority' (alta/media/baja) en wishlist
            # Asegurar columna 'priority' (alta/media/baja) en wishlist
            try:
                c.execute("""
                    ALTER TABLE wishlist
                    ADD COLUMN IF NOT EXISTS priority TEXT
                    CHECK (priority IN ('alta','media','baja'))
                    DEFAULT 'media'
                """)
                conn.commit()  # <-- añade esto
            except Exception as e:
                print(f"ALTER wishlist priority: {e}")



init_db()

def get_today_question():
    today_str = date.today().isoformat()
    conn = get_db_connection()
    
    try:
        with conn.cursor() as c:
            # Revisar si ya existe una pregunta para hoy
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
            c.execute("SELECT id, image_url, uploaded_by FROM travel_photos WHERE travel_id=%s ORDER BY id DESC", (travel_id,))
            photos = []
            for row in c.fetchall():
                photo_id, image_url, uploaded_by = row
                photos.append({
                    'id': photo_id,
                    'url': image_url,
                    'uploaded_by': uploaded_by
                })
            return photos
    finally:
        conn.close()

# -----------------------------
#  Cálculo de rachas (NEW)
# -----------------------------
def compute_streaks():
    """
    Calcula:
      - current_streak: racha actual de días consecutivos (hasta hoy si hoy está completo,
                        o hasta el último día completo si hoy no lo está).
      - best_streak: mejor racha histórica.
    Un día cuenta como 'completo' si existen respuestas de 'mochito' y 'mochita'
    para la pregunta de ese día.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            # Traemos preguntas y cuántos usuarios distintos (de los 2) respondieron
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

    # Conjuntos de fechas
    def parse_d(dtxt): 
        return datetime.strptime(dtxt, "%Y-%m-%d").date()

    question_dates = set(parse_d(r[1]) for r in rows)
    complete_dates = [parse_d(r[1]) for r in rows if r[2] >= 2]
    complete_dates_set = set(complete_dates)

    if not complete_dates:
        return 0, 0

    # Mejor racha histórica
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

    # Racha actual: desde el último día completo más reciente hacia atrás sin huecos
    today = date.today()
    latest_complete = None
    for d in sorted(complete_dates_set, reverse=True):
        if d <= today:
            latest_complete = d
            break

    if latest_complete is None:
        return 0, best_streak

    current_streak = 1
    d = latest_complete - timedelta(days=1)
    while d in complete_dates_set:
        current_streak += 1
        d -= timedelta(days=1)

    return current_streak, best_streak


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
                        return redirect('/')
                    else:
                        error = "Usuario o contraseña incorrecta"
                        return render_template('index.html', login_error=error)
            finally:
                conn.close()
        
        # Para usuarios no logueados
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

                if 'travel_photo_url' in request.form:
                    travel_id = request.form.get('travel_id')
                    image_url = request.form['travel_photo_url'].strip()
                    if image_url and travel_id:
                        c.execute("""
                            INSERT INTO travel_photos (travel_id, image_url, uploaded_by, uploaded_at)
                            VALUES (%s, %s, %s, %s)
                        """, (travel_id, image_url, user,
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        conn.commit()
                    return redirect('/')

                if 'product_name' in request.form:
                    product_name = request.form['product_name'].strip()
                    product_link = request.form.get('product_link', '').strip()
                    notes = request.form.get('wishlist_notes', '').strip()
                    priority = request.form.get('priority', 'media').strip()
                    is_gift = 'is_gift' in request.form
                    gift_for = request.form.get('gift_for', '')
                    
                    if priority not in ('alta', 'media', 'baja'):
                        priority = 'media'
                        
                    if product_name:
                        c.execute("""
                            INSERT INTO wishlist (product_name, product_link, notes, created_by, created_at, 
                                                is_purchased, priority, is_gift, gift_for)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (product_name, product_link, notes, user,
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            False, priority, is_gift, gift_for if is_gift else None))
                        conn.commit()
                    return redirect('/')


            # --- Respuestas ---
            c.execute("SELECT username, answer FROM answers WHERE question_id=%s", (question_id,))
            answers = c.fetchall()

            # Quién es la otra persona (somos dos: mochito / mochita)
            other_user = 'mochita' if user == 'mochito' else 'mochito'

            # Diccionario usuario -> respuesta
            answers_dict = {u: a for (u, a) in answers}
            user_answer  = answers_dict.get(user)
            other_answer = answers_dict.get(other_user)

            # Solo mostrar ambas respuestas si ambos contestaron
            show_answers = (user_answer is not None) and (other_answer is not None)

            # --- Viajes ---
            c.execute("""
                SELECT id, destination, description, travel_date, is_visited, created_by
                FROM travels
                ORDER BY is_visited, travel_date DESC
            """)
            travels = c.fetchall()
            travel_photos_dict = {tid: get_travel_photos(tid) for tid, *_ in travels}

            # --- Wishlist --- (ACTUALIZAR ESTA CONSULTA)
            c.execute("""
                SELECT id, product_name, product_link, notes, created_by, created_at, is_purchased, 
                    COALESCE(priority, 'media') AS priority,
                    COALESCE(is_gift, FALSE) as is_gift,
                    gift_for
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

    # --- Rachas (NEW) ---
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
                           # --- NUEVOS CONTEXT VARS ---
                           current_streak=current_streak,
                           best_streak=best_streak
                           )

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
# Actualiza la edición de productos
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
        is_gift = 'is_gift' in request.form
        gift_for = request.form.get('gift_for', '')
        
        if priority not in ('alta', 'media', 'baja'):
            priority = 'media'
            
        if product_name:
            conn = get_db_connection()
            with conn.cursor() as c:
                c.execute("""
                    UPDATE wishlist 
                    SET product_name=%s, product_link=%s, notes=%s, priority=%s, 
                        is_gift=%s, gift_for=%s
                    WHERE id=%s
                """, (product_name, product_link, notes, priority, 
                      is_gift, gift_for if is_gift else None, item_id))
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

if __name__ == '__main__':
    app.run(debug=True)
