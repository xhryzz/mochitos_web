from flask import Flask, render_template, request, redirect, session, send_from_directory, jsonify
import sqlite3
from datetime import datetime, date
import random
import os
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'tu_clave_secreta_aqui'

# Configuración de uploads
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

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

# Inicializar DB
def init_db():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS daily_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT,
            date TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER,
            username TEXT,
            answer TEXT,
            FOREIGN KEY(question_id) REFERENCES daily_questions(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS meeting (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_date TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS banner (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT
        )
    ''')
    # Tablas para la nueva funcionalidad de viajes
    c.execute('''
        CREATE TABLE IF NOT EXISTS travels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            destination TEXT NOT NULL,
            description TEXT,
            travel_date TEXT,
            is_visited BOOLEAN DEFAULT 0,
            created_by TEXT,
            created_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS travel_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            travel_id INTEGER,
            filename TEXT,
            uploaded_by TEXT,
            uploaded_at TEXT,
            FOREIGN KEY(travel_id) REFERENCES travels(id)
        )
    ''')
    # Tabla para la lista de deseos
    c.execute('''
        CREATE TABLE IF NOT EXISTS wishlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            product_link TEXT,
            notes TEXT,
            created_by TEXT,
            created_at TEXT,
            is_purchased BOOLEAN DEFAULT 0
        )
    ''')
    # Tabla para horarios
    c.execute('''
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            location_name TEXT,
            latitude REAL,
            longitude REAL,
            updated_at TEXT
        )
    ''')
    # Tabla para fotos de perfil
    c.execute('''
        CREATE TABLE IF NOT EXISTS profile_pictures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            filename TEXT,
            uploaded_at TEXT
        )
    ''')
    # Crear usuarios predeterminados
    try:
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)", ('mochito', '1234'))
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)", ('mochita', '1234'))
        # Ubicaciones iniciales
        c.execute("INSERT OR IGNORE INTO locations (username, location_name, latitude, longitude, updated_at) VALUES (?, ?, ?, ?, ?)", 
                 ('mochito', 'Algemesí, Valencia', 39.1925, -0.4353, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        c.execute("INSERT OR IGNORE INTO locations (username, location_name, latitude, longitude, updated_at) VALUES (?, ?, ?, ?, ?)", 
                 ('mochita', 'Córdoba', 37.8882, -4.7794, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    except:
        pass
    conn.commit()
    conn.close()

init_db()

def get_today_question():
    today_str = date.today().isoformat()
    conn = sqlite3.connect('database.db')
    c = conn.cursor()

    # Revisar si ya existe una pregunta para hoy
    c.execute("SELECT id, question FROM daily_questions WHERE date=?", (today_str,))
    q = c.fetchone()
    if q:
        conn.close()
        return q

    # Obtener todas las preguntas que ya salieron
    c.execute("SELECT question FROM daily_questions")
    used_questions = [row[0] for row in c.fetchall()]

    # Filtrar las preguntas restantes
    remaining_questions = [q for q in QUESTIONS if q not in used_questions]

    if not remaining_questions:
        # Se acabaron las preguntas
        conn.close()
        return (None, "Ya no hay más preguntas disponibles ❤️")

    # Escoger una pregunta nueva
    question_text = random.choice(remaining_questions)
    c.execute("INSERT INTO daily_questions (question, date) VALUES (?, ?)", (question_text, today_str))
    conn.commit()
    question_id = c.lastrowid
    conn.close()
    return (question_id, question_text)

def days_together():
    return (date.today() - RELATION_START).days

def days_until_meeting():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT meeting_date FROM meeting ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        meeting_date = datetime.strptime(row[0], "%Y-%m-%d").date()
        delta = (meeting_date - date.today()).days
        return max(delta, 0)
    return None

def get_banner():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT filename FROM banner ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        return row[0]
    return None

def get_user_locations():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT username, location_name, latitude, longitude FROM locations")
    locations = {}
    for row in c.fetchall():
        username, location_name, latitude, longitude = row
        locations[username] = {
            'name': location_name,
            'lat': latitude,
            'lng': longitude
        }
    conn.close()
    return locations

def get_profile_pictures():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT username, filename FROM profile_pictures")
    pictures = {}
    for row in c.fetchall():
        username, filename = row
        pictures[username] = filename
    conn.close()
    return pictures

@app.route('/', methods=['GET', 'POST'])
def index():
    if 'username' not in session:
        if request.method == 'POST':
            username = request.form['username']
            password = request.form['password']
            conn = sqlite3.connect('database.db')
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE username=? AND password=?", (username, password))
            user = c.fetchone()
            conn.close()
            if user:
                session['username'] = username
                return redirect('/')
            else:
                error = "Usuario o contraseña incorrecta"
                return render_template('index.html', login_error=error)
        
        # Para usuarios no logueados, pasar un diccionario vacío de profile_pictures
        profile_pictures = {}
        return render_template('index.html', login_error=None, profile_pictures=profile_pictures)

    user = session['username']
    question_id, question_text = get_today_question()
    conn = sqlite3.connect('database.db')
    c = conn.cursor()

    # Procesar formularios con PRG
    if request.method == 'POST':
        # Actualizar foto de perfil
        if 'update_profile' in request.form and 'profile_picture' in request.files:
            file = request.files['profile_picture']
            if file and allowed_file(file.filename):
                ext = file.filename.rsplit('.', 1)[1].lower()
                unique_name = f"profile_{user}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.{ext}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_name))
                
                # Guardar en la base de datos
                c.execute("INSERT OR REPLACE INTO profile_pictures (username, filename, uploaded_at) VALUES (?, ?, ?)",
                         (user, unique_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            
            conn.close()
            return redirect('/')
            
        if 'answer' in request.form:
            answer = request.form['answer'].strip()
            c.execute("SELECT * FROM answers WHERE question_id=? AND username=?", (question_id, user))
            if not c.fetchone():
                c.execute("INSERT INTO answers (question_id, username, answer) VALUES (?, ?, ?)",
                          (question_id, user, answer))
                conn.commit()
            conn.close()
            return redirect('/')

        if 'meeting_date' in request.form:
            meeting_date = request.form['meeting_date']
            c.execute("INSERT INTO meeting (meeting_date) VALUES (?)", (meeting_date,))
            conn.commit()
            conn.close()
            return redirect('/')

        if 'banner' in request.files:
            file = request.files['banner']
            if file and allowed_file(file.filename):
                ext = file.filename.rsplit('.', 1)[1].lower()
                unique_name = f"banner_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.{ext}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_name))
                c.execute("INSERT INTO banner (filename) VALUES (?)", (unique_name,))
                conn.commit()
            conn.close()
            return redirect('/')
            
        # Nuevos formularios para viajes
        if 'travel_destination' in request.form:
            destination = request.form['travel_destination'].strip()
            description = request.form.get('travel_description', '').strip()
            travel_date = request.form.get('travel_date', '')
            is_visited = 1 if 'travel_visited' in request.form else 0
            
            if destination:
                c.execute("INSERT INTO travels (destination, description, travel_date, is_visited, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                          (destination, description, travel_date, is_visited, user, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            conn.close()
            return redirect('/')
            
        if 'travel_photo' in request.files:
            file = request.files['travel_photo']
            travel_id = request.form.get('travel_id')
            if file and allowed_file(file.filename) and travel_id:
                ext = file.filename.rsplit('.', 1)[1].lower()
                unique_name = f"travel_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.{ext}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_name))
                c.execute("INSERT INTO travel_photos (travel_id, filename, uploaded_by, uploaded_at) VALUES (?, ?, ?, ?)",
                          (travel_id, unique_name, user, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            conn.close()
            return redirect('/')
            
        # Formulario para lista de deseos
        if 'product_name' in request.form:
            product_name = request.form['product_name'].strip()
            product_link = request.form.get('product_link', '').strip()
            notes = request.form.get('wishlist_notes', '').strip()
            
            if product_name:
                c.execute("INSERT INTO wishlist (product_name, product_link, notes, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                          (product_name, product_link, notes, user, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            conn.close()
            return redirect('/')

    # Respuestas
    c.execute("SELECT username, answer FROM answers WHERE question_id=?", (question_id,))
    answers = c.fetchall()
    show_answers = len(answers) == 2
    user_answer = None
    if not show_answers:
        for u, a in answers:
            if u == user:
                user_answer = a

    # Viajes
    c.execute("SELECT id, destination, description, travel_date, is_visited, created_by FROM travels ORDER BY is_visited, travel_date DESC")
    travels = c.fetchall()
    travel_photos_dict = {}
    for travel_id, destination, description, travel_date, is_visited, created_by in travels:
        c.execute("SELECT id, filename, uploaded_by FROM travel_photos WHERE travel_id=? ORDER BY id DESC", (travel_id,))
        travel_photos_dict[travel_id] = c.fetchall()
        
    # Lista de deseos
    c.execute("SELECT id, product_name, product_link, notes, created_by, created_at, is_purchased FROM wishlist ORDER BY is_purchased, created_at DESC")
    wishlist_items = c.fetchall()

    banner_file = get_banner()
    
    # Obtener fotos de perfil
    profile_pictures = get_profile_pictures()

    conn.close()

    return render_template('index.html',
                           question=question_text,
                           show_answers=show_answers,
                           answers=answers,
                           user_answer=user_answer,
                           days_together=days_together(),
                           days_until_meeting=days_until_meeting(),
                           travels=travels,
                           travel_photos_dict=travel_photos_dict,
                           wishlist_items=wishlist_items,
                           username=user,
                           banner_file=banner_file,
                           profile_pictures=profile_pictures,
                           login_error=None)

# Eliminar viaje - CORREGIDO
@app.route('/delete_travel', methods=['POST'])
def delete_travel():
    if 'username' not in session:
        return redirect('/')
    
    try:
        travel_id = request.form['travel_id']
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        
        # Eliminar fotos asociadas al viaje - CORREGIDO: os.path.exists en lugar de os.exists
        c.execute("SELECT filename FROM travel_photos WHERE travel_id=?", (travel_id,))
        photos = c.fetchall()
        for photo in photos:
            filename = photo[0]
            path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    print(f"Error eliminando archivo {filename}: {e}")
        
        c.execute("DELETE FROM travel_photos WHERE travel_id=?", (travel_id,))
        c.execute("DELETE FROM travels WHERE id=?", (travel_id,))
        conn.commit()
        conn.close()
        return redirect('/')
    
    except Exception as e:
        print(f"Error en delete_travel: {e}")
        return redirect('/')

# Marcar viaje como visitado/no visitado
@app.route('/toggle_travel_status', methods=['POST'])
def toggle_travel_status():
    if 'username' not in session:
        return redirect('/')
    
    try:
        travel_id = request.form['travel_id']
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute("SELECT is_visited FROM travels WHERE id=?", (travel_id,))
        current_status = c.fetchone()[0]
        new_status = 0 if current_status else 1
        c.execute("UPDATE travels SET is_visited=? WHERE id=?", (new_status, travel_id))
        conn.commit()
        conn.close()
        return redirect('/')
    
    except Exception as e:
        print(f"Error en toggle_travel_status: {e}")
        return redirect('/')

# Eliminar elemento de la lista de deseos - MEJORADO con manejo de errores
@app.route('/delete_wishlist_item', methods=['POST'])
def delete_wishlist_item():
    if 'username' not in session:
        return redirect('/')
    
    try:
        item_id = request.form['item_id']
        user = session['username']
        
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        
        # Verificar que el usuario es el creador del item para mayor seguridad
        c.execute("SELECT created_by FROM wishlist WHERE id=?", (item_id,))
        result = c.fetchone()
        
        if result and result[0] == user:
            c.execute("DELETE FROM wishlist WHERE id=?", (item_id,))
            conn.commit()
        
        conn.close()
        return redirect('/')
    
    except Exception as e:
        print(f"Error en delete_wishlist_item: {e}")
        return redirect('/')

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
            conn = sqlite3.connect('database.db')
            c = conn.cursor()
            c.execute("UPDATE wishlist SET product_name=?, product_link=?, notes=? WHERE id=?", 
                     (product_name, product_link, notes, item_id))
            conn.commit()
            conn.close()
        return redirect('/')
    
    except Exception as e:
        print(f"Error en edit_wishlist_item: {e}")
        return redirect('/')

# Marcar elemento como comprado/no comprado
@app.route('/toggle_wishlist_status', methods=['POST'])
def toggle_wishlist_status():
    if 'username' not in session:
        return redirect('/')
    
    try:
        item_id = request.form['item_id']
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute("SELECT is_purchased FROM wishlist WHERE id=?", (item_id,))
        current_status = c.fetchone()[0]
        new_status = 0 if current_status else 1
        c.execute("UPDATE wishlist SET is_purchased=? WHERE id=?", (new_status, item_id))
        conn.commit()
        conn.close()
        return redirect('/')
    
    except Exception as e:
        print(f"Error en toggle_wishlist_status: {e}")
        return redirect('/')

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
            conn = sqlite3.connect('database.db')
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO locations (username, location_name, latitude, longitude, updated_at) VALUES (?, ?, ?, ?, ?)",
                     (username, location_name, latitude, longitude, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            conn.close()
            
            return jsonify({'success': True, 'message': 'Ubicación actualizada correctamente'})
        
        return jsonify({'error': 'Datos incompletos'}), 400
    
    except Exception as e:
        print(f"Error en update_location: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

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
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        
        # Obtener todos los horarios de la base de datos
        c.execute("SELECT username, day, time, activity, color FROM schedules")
        rows = c.fetchall()
        conn.close()
        
        # Estructurar los datos
        schedules = {
            'mochito': {},
            'mochita': {}
        }
        
        for username, day, time, activity, color in rows:
            if day not in schedules[username]:
                schedules[username][day] = {}
            schedules[username][day][time] = {
                'activity': activity,
                'color': color
            }
        
        return jsonify(schedules)
    
    except Exception as e:
        print(f"Error en get_schedules: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/api/schedules', methods=['POST'])
def save_schedules():
    if 'username' not in session:
        return jsonify({'error': 'No autorizado'}), 401
    
    try:
        data = request.get_json()
        
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        
        # Limpiar horarios existentes
        c.execute("DELETE FROM schedules")
        
        # Insertar nuevos horarios
        for username in ['mochito', 'mochita']:
            for day, times in data[username].items():
                for time, activity_data in times.items():
                    if activity_data['activity']:  # Solo guardar si hay actividad
                        c.execute(
                            "INSERT INTO schedules (username, day, time, activity, color) VALUES (?, ?, ?, ?, ?)",
                            (username, day, time, activity_data['activity'], activity_data['color'])
                        )
        
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
    
    except Exception as e:
        print(f"Error en save_schedules: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect('/')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

if __name__ == '__main__':
    app.run(debug=True)
