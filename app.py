from flask import Flask, render_template, request, redirect, session, jsonify
import sqlite3
from datetime import datetime, date
import random
import base64

app = Flask(__name__)
app.secret_key = 'tu_clave_secreta_aqui'

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

QUESTIONS = [
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
]

RELATION_START = date(2025, 8, 2)

def init_db():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_questions (id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT, date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS answers (id INTEGER PRIMARY KEY AUTOINCREMENT, question_id INTEGER, username TEXT, answer TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS meeting (id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS banner (id INTEGER PRIMARY KEY AUTOINCREMENT, image BLOB)''')
    c.execute('''CREATE TABLE IF NOT EXISTS travels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        destination TEXT NOT NULL,
        description TEXT,
        travel_date TEXT,
        is_visited BOOLEAN DEFAULT 0,
        created_by TEXT,
        created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS travel_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        travel_id INTEGER,
        image BLOB,
        uploaded_by TEXT,
        uploaded_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS wishlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_name TEXT,
        product_link TEXT,
        notes TEXT,
        created_by TEXT,
        created_at TEXT,
        is_purchased BOOLEAN DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        day TEXT NOT NULL,
        time TEXT NOT NULL,
        activity TEXT,
        color TEXT,
        UNIQUE(username, day, time)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS locations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        location_name TEXT,
        latitude REAL,
        longitude REAL,
        updated_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS profile_pictures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        image BLOB,
        uploaded_at TEXT
    )''')
    try:
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)", ('mochito', '1234'))
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)", ('mochita', '1234'))
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
    c.execute("SELECT id, question FROM daily_questions WHERE date=?", (today_str,))
    q = c.fetchone()
    if q:
        conn.close()
        return q
    c.execute("SELECT question FROM daily_questions")
    used = [row[0] for row in c.fetchall()]
    remaining = [q for q in QUESTIONS if q not in used]
    if not remaining:
        conn.close()
        return (None, "Ya no hay más preguntas disponibles ❤️")
    question_text = random.choice(remaining)
    c.execute("INSERT INTO daily_questions (question, date) VALUES (?, ?)", (question_text, today_str))
    conn.commit()
    qid = c.lastrowid
    conn.close()
    return (qid, question_text)

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
        return max((meeting_date - date.today()).days, 0)
    return None

def get_banner():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT image FROM banner ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    return base64.b64encode(row[0]).decode('utf-8') if row else None

def get_profile_pictures():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT username, image FROM profile_pictures")
    pics = {}
    for username, blob in c.fetchall():
        pics[username] = base64.b64encode(blob).decode('utf-8')
    conn.close()
    return pics

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
            return render_template('index.html', login_error="Usuario o contraseña incorrecta", profile_pictures={})
        return render_template('index.html', login_error=None, profile_pictures={})

    user = session['username']
    question_id, question_text = get_today_question()
    conn = sqlite3.connect('database.db')
    c = conn.cursor()

    # POST
    if request.method == 'POST':
        if 'update_profile' in request.form and 'profile_picture' in request.files:
            file = request.files['profile_picture']
            if file and allowed_file(file.filename):
                c.execute("INSERT OR REPLACE INTO profile_pictures (username, image, uploaded_at) VALUES (?, ?, ?)",
                         (user, file.read(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            conn.close()
            return redirect('/')
        if 'answer' in request.form:
            c.execute("INSERT INTO answers (question_id, username, answer) VALUES (?, ?, ?)",
                      (question_id, user, request.form['answer'].strip()))
            conn.commit()
            conn.close()
            return redirect('/')
        if 'meeting_date' in request.form:
            c.execute("INSERT INTO meeting (meeting_date) VALUES (?)", (request.form['meeting_date'],))
            conn.commit()
            conn.close()
            return redirect('/')
        if 'banner' in request.files:
            file = request.files['banner']
            if file and allowed_file(file.filename):
                c.execute("INSERT INTO banner (image) VALUES (?)", (file.read(),))
                conn.commit()
            conn.close()
            return redirect('/')
        if 'travel_destination' in request.form:
            c.execute("INSERT INTO travels (destination, description, travel_date, is_visited, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                      (request.form['travel_destination'], request.form.get('travel_description',''),
                       request.form.get('travel_date',''), 1 if 'travel_visited' in request.form else 0,
                       user, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            conn.close()
            return redirect('/')
        if 'travel_photo' in request.files:
            travel_id = request.form.get('travel_id')
            file = request.files['travel_photo']
            if file and allowed_file(file.filename):
                c.execute("INSERT INTO travel_photos (travel_id, image, uploaded_by, uploaded_at) VALUES (?, ?, ?, ?)",
                          (travel_id, file.read(), user, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            conn.close()
            return redirect('/')
        if 'product_name' in request.form:
            c.execute("INSERT INTO wishlist (product_name, product_link, notes, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                      (request.form['product_name'], request.form.get('product_link',''),
                       request.form.get('wishlist_notes',''), user, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            conn.close()
            return redirect('/')

    # GET
    c.execute("SELECT username, answer FROM answers WHERE question_id=?", (question_id,))
    answers = c.fetchall()
    show_answers = len(answers) == 2
    user_answer = next((a for u,a in answers if u == user), None) if not show_answers else None

    c.execute("SELECT id, destination, description, travel_date, is_visited, created_by FROM travels ORDER BY is_visited, travel_date DESC")
    travels = c.fetchall()
    travel_photos_dict = {}
    for t in travels:
        c.execute("SELECT id, image, uploaded_by FROM travel_photos WHERE travel_id=?", (t[0],))
        travel_photos_dict[t[0]] = [(id, base64.b64encode(img).decode('utf-8'), u) for id,img,u in c.fetchall()]

    c.execute("SELECT id, product_name, product_link, notes, created_by, created_at, is_purchased FROM wishlist ORDER BY is_purchased, created_at DESC")
    wishlist_items = c.fetchall()
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
                           banner_file=get_banner(),
                           profile_pictures=get_profile_pictures())

@app.route('/toggle_wishlist_status', methods=['POST'])
def toggle_wishlist_status():
    if 'username' not in session:
        return redirect('/')
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT is_purchased FROM wishlist WHERE id=?", (request.form['item_id'],))
    current = c.fetchone()[0]
    c.execute("UPDATE wishlist SET is_purchased=? WHERE id=?", (0 if current else 1, request.form['item_id']))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/edit_wishlist_item', methods=['POST'])
def edit_wishlist_item():
    if 'username' not in session:
        return redirect('/')
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("UPDATE wishlist SET product_name=?, product_link=?, notes=? WHERE id=?",
             (request.form['product_name'], request.form.get('product_link',''), request.form.get('notes',''), request.form['item_id']))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/delete_wishlist_item', methods=['POST'])
def delete_wishlist_item():
    if 'username' not in session:
        return redirect('/')
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("DELETE FROM wishlist WHERE id=?", (request.form['item_id'],))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/toggle_travel_status', methods=['POST'])
def toggle_travel_status():
    if 'username' not in session:
        return redirect('/')
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT is_visited FROM travels WHERE id=?", (request.form['travel_id'],))
    current = c.fetchone()[0]
    c.execute("UPDATE travels SET is_visited=? WHERE id=?", (0 if current else 1, request.form['travel_id']))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/delete_travel', methods=['POST'])
def delete_travel():
    if 'username' not in session:
        return redirect('/')
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("DELETE FROM travel_photos WHERE travel_id=?", (request.form['travel_id'],))
    c.execute("DELETE FROM travels WHERE id=?", (request.form['travel_id'],))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/horario')
def horario():
    if 'username' not in session:
        return redirect('/')
    return render_template('schedule.html')

@app.route('/api/schedules', methods=['GET'])
def get_schedules():
    if 'username' not in session:
        return jsonify({'error': 'No autorizado'}), 401
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT username, day, time, activity, color FROM schedules")
    rows = c.fetchall()
    conn.close()
    schedules = {'mochito': {}, 'mochita': {}}
    for username, day, time, activity, color in rows:
        schedules[username].setdefault(day, {})[time] = {'activity': activity, 'color': color}
    return jsonify(schedules)

@app.route('/api/schedules', methods=['POST'])
def save_schedules():
    if 'username' not in session:
        return jsonify({'error': 'No autorizado'}), 401
    data = request.get_json()
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("DELETE FROM schedules")
    for username in ['mochito', 'mochita']:
        for day, times in data[username].items():
            for time, activity_data in times.items():
                if activity_data['activity']:
                    c.execute("INSERT INTO schedules (username, day, time, activity, color) VALUES (?, ?, ?, ?, ?)",
                             (username, day, time, activity_data['activity'], activity_data['color']))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect('/')

if __name__ == '__main__':
    app.run(debug=True)
