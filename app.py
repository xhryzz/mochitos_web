from flask import Flask, render_template, request, redirect, session, send_from_directory, jsonify
import sqlite3
from datetime import datetime, date
import random
import os
from werkzeug.utils import secure_filename
import math

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
    "¿Cuál fue el mejor momento de tu día?",
    "¿Qué te hizo sonreír hoy?",
    "¿Qué sueño te gustaría cumplir pronto?",
    "¿Qué agradeces hoy?",
    "Si pudieras describir tu día en una palabra, ¿cuál sería?"
]

RELATION_START = date(2025, 8, 2)  # <- fecha de inicio relación

# Coordenadas por defecto (se actualizarán con la geolocalización en tiempo real)
VALENCIA_COORDS = (39.46975, -0.37739)
CORDOBA_COORDS = (37.89155, -4.77275)

# Almacenamiento simple de ubicaciones (en producción usarías una base de datos)
user_locations = {
    'tu': {'lat': VALENCIA_COORDS[0], 'lng': VALENCIA_COORDS[1], 'name': 'Valencia', 'last_update': None},
    'novia': {'lat': CORDOBA_COORDS[0], 'lng': CORDOBA_COORDS[1], 'name': 'Córdoba', 'last_update': None}
}

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
        CREATE TABLE IF NOT EXISTS albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            created_by TEXT,
            created_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            album_id INTEGER,
            filename TEXT,
            uploaded_by TEXT,
            uploaded_at TEXT,
            FOREIGN KEY(album_id) REFERENCES albums(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS banner (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT
        )
    ''')
    # Crear usuarios predeterminados
    try:
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)", ('tu', '1234'))
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)", ('novia', '1234'))
    except:
        pass
    conn.commit()
    conn.close()

init_db()

def get_today_question():
    today_str = date.today().isoformat()
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT id, question FROM daily_questions WHERE date=?", (today_str,))
    q = c.fetchone()
    if q:
        conn.close()
        return q
    question_text = random.choice(QUESTIONS)
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

# Ruta para obtener las ubicaciones
@app.route('/get_locations')
def get_locations():
    return jsonify(user_locations)

# Ruta para actualizar la ubicación
@app.route('/update_location', methods=['POST'])
def update_location():
    if 'username' not in session:
        return jsonify({'error': 'No autenticado'}), 401
        
    username = session['username']
    data = request.get_json()
    
    if 'lat' in data and 'lng' in data:
        user_locations[username] = {
            'lat': data['lat'],
            'lng': data['lng'],
            'name': username,
            'last_update': datetime.now().isoformat()
        }
        return jsonify({'status': 'success'})
    
    return jsonify({'error': 'Datos inválidos'}), 400

# Función para calcular distancia entre dos puntos (fórmula haversine)
def calculate_distance(coord1, coord2):
    lat1, lon1 = coord1
    lat2, lon2 = coord2
    
    # Radio de la Tierra en kilómetros
    R = 6371.0
    
    # Convertir grados a radianes
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    
    # Diferencia entre las coordenadas
    dlon = lon2_rad - lon1_rad
    dlat = lat2_rad - lat1_rad
    
    # Fórmula de Haversine
    a = math.sin(dlat / 2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    distance = R * c
    return round(distance, 2)

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
        return render_template('index.html')

    user = session['username']
    question_id, question_text = get_today_question()
    conn = sqlite3.connect('database.db')
    c = conn.cursor()

    # Procesar formularios con PRG
    if request.method == 'POST':
        if 'answer' in request.form:
            answer = request.form['answer'].strip()
            c.execute("SELECT * FROM answers WHERE question_id=? AND username=?", (question_id, user))
            if not c.fetchone():
                c.execute("INSERT INTO answers (question_id, username, answer) VALUES (?, ?, ?)",
                          (question_id, user, answer))
                conn.commit()
            conn.close()
            return redirect('/')

        if 'album_name' in request.form:
            album_name = request.form['album_name'].strip()
            if album_name:
                c.execute("INSERT INTO albums (name, created_by, created_at) VALUES (?, ?, ?)",
                          (album_name, user, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            conn.close()
            return redirect('/')

        if 'meeting_date' in request.form:
            meeting_date = request.form['meeting_date']
            c.execute("INSERT INTO meeting (meeting_date) VALUES (?)", (meeting_date,))
            conn.commit()
            conn.close()
            return redirect('/')

        if 'photo' in request.files:
            file = request.files['photo']
            album_id = request.form.get('album_id')
            if file and allowed_file(file.filename) and album_id:
                ext = file.filename.rsplit('.', 1)[1].lower()
                unique_name = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}.{ext}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_name))
                c.execute("INSERT INTO photos (album_id, filename, uploaded_by, uploaded_at) VALUES (?, ?, ?, ?)",
                          (album_id, unique_name, user, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
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

    # Respuestas
    c.execute("SELECT username, answer FROM answers WHERE question_id=?", (question_id,))
    answers = c.fetchall()
    show_answers = len(answers) == 2
    user_answer = None
    if not show_answers:
        for u, a in answers:
            if u == user:
                user_answer = a

    # Álbumes
    c.execute("SELECT id, name, created_by FROM albums ORDER BY id DESC")
    albums = c.fetchall()
    photos_dict = {}
    for album_id, name, created_by in albums:
        c.execute("SELECT id, filename, uploaded_by FROM photos WHERE album_id=? ORDER BY id DESC", (album_id,))
        photos_dict[album_id] = c.fetchall()

    banner_file = get_banner()

    conn.close()

    # Calcular distancia actual
    current_distance = None
    if 'tu' in user_locations and 'novia' in user_locations:
        coord1 = (user_locations['tu']['lat'], user_locations['tu']['lng'])
        coord2 = (user_locations['novia']['lat'], user_locations['novia']['lng'])
        current_distance = calculate_distance(coord1, coord2)

    return render_template('index.html',
                           question=question_text,
                           show_answers=show_answers,
                           answers=answers,
                           user_answer=user_answer,
                           days_together=days_together(),
                           days_until_meeting=days_until_meeting(),
                           albums=albums,
                           photos_dict=photos_dict,
                           username=user,
                           banner_file=banner_file,
                           login_error=None,
                           current_distance=current_distance)

# Editar título álbum
@app.route('/edit_album_title', methods=['POST'])
def edit_album_title():
    if 'username' not in session:
        return redirect('/')
    album_id = request.form['album_id']
    new_title = request.form['new_title'].strip()
    if new_title:
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute("UPDATE albums SET name=? WHERE id=?", (new_title, album_id))
        conn.commit()
        conn.close()
    return redirect('/')

# Eliminar foto
@app.route('/delete_photo', methods=['POST'])
def delete_photo():
    if 'username' not in session:
        return redirect('/')
    photo_id = request.form['photo_id']
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT filename FROM photos WHERE id=?", (photo_id,))
    row = c.fetchone()
    if row:
        filename = row[0]
        path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.exists(path):
            os.remove(path)
        c.execute("DELETE FROM photos WHERE id=?", (photo_id,))
        conn.commit()
    conn.close()
    return redirect('/')

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect('/')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)