from flask import flash
import os
import csv
import logging
import random
from time import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from functools import wraps


from flask import (
    Flask, render_template, request,
    redirect, url_for, session, jsonify
)
from flask_sqlalchemy import SQLAlchemy
import bcrypt


# ================= INFLUXDB =================
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

INFLUX_URL = "https://us-east-1-1.aws.cloud2.influxdata.com"
INFLUX_TOKEN = "FxleuySQonOPcYXXYu5IH4Zy_TC_n4poiod5omk5qq8eo4YXnut2GrYCGZuI7XzQTUVI2jm9ycHoMvSQEchprQ=="
INFLUX_ORG = "flask"
INFLUX_BUCKET = "sensor_bucket"

influx_client = InfluxDBClient(
    url=INFLUX_URL,
    token=INFLUX_TOKEN,
    org=INFLUX_ORG
)

write_api = influx_client.write_api(write_options=SYNCHRONOUS)
# ================= APP SETUP =================
app = Flask(__name__)
app.secret_key = "super_secret_key"

# ================= DATABASE =================
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db' #DATA BASE FOR USERS
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ================= FOLDERS =================
UPLOAD_FOLDER = "uploads"
DATA_FOLDER = "data_logs"
LOG_FOLDER = "logs"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(DATA_FOLDER, exist_ok=True)
os.makedirs(LOG_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"csv", "cmc3"}
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# ================= LOGS =================
log_file = os.path.join(LOG_FOLDER, "app.log")

for h in list(app.logger.handlers):
    app.logger.removeHandler(h)

handler = RotatingFileHandler(
    log_file,
    maxBytes=10 * 1024 * 1024,   # 10 MB
    backupCount=5,
    delay=True
)
handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s"
))
handler.setLevel(logging.INFO)

app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)

app.logger.info("APPLICATION STARTED")

# ================= GLOBAL LIVE CACHE =================
LIVE_DATA_CACHE = {}

# ================= LOGIN DECORATOR =================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

# ================= HELPERS =================
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def data_session_active():
    expiry = session.get("data_expires_at")
    return expiry and time() < expiry

# ================= DATABASE MODEL =================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    email = db.Column(db.String(100), unique=True)
    password = db.Column(db.String(200))

    def __init__(self, name, email, password):
        self.name = name
        self.email = email
        self.password = bcrypt.hashpw(
            password.encode(), bcrypt.gensalt()
        ).decode()

    def check_password(self, password):
        return bcrypt.checkpw(password.encode(), self.password.encode())

with app.app_context():
    db.create_all()

# ================= MODBUS FILE READER =================
def read_modbus_file(filepath):
    descriptions = []

    with open(filepath, "rb") as f:
        raw = f.read()

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="ignore")

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return descriptions

    header_index = -1
    for i, line in enumerate(lines):
        if "description" in line.lower():
            header_index = i
            break

    if header_index == -1:
        return descriptions

    delimiter = "\t" if "\t" in lines[header_index] else ";" if ";" in lines[header_index] else ","

    for line in lines[header_index + 1:]:
        cols = [c.strip() for c in line.split(delimiter)]
        if len(cols) < 7:
            continue

        if len(cols) >= 8 and cols[6] and cols[7]:
            descriptions.append(f"{cols[6]} {cols[7]}")
        else:
            descriptions.append(cols[-1])
        

    return descriptions

# ================= DATA GENERATOR =================
def generate_and_cache_live_data():
    if not data_session_active():
        return None, None

    user_id = session['user_id']

    # Prevent double generation in same second
    last_gen = session.get("last_generated_ts")
    now_ts = int(time())

    if last_gen == now_ts and user_id in LIVE_DATA_CACHE:
        cached = LIVE_DATA_CACHE[user_id]
        return cached["table"], cached["graph"]

    session["last_generated_ts"] = now_ts

    now = datetime.now()
    date = now.strftime("%m-%d-%Y")
    t = now.strftime("%H:%M:%S.%f")[:-3]

    table_data = []
    graph_data = []

    for p in session.get("selected_params", []):
        val = round(random.uniform(10, 100), 2)
        # WRITE TO INFLUXDB
        point = (
            Point("sensor_data")
            .tag("user", session.get("user_name"))
            .tag("parameter", p)
            .field("value", float(val))
        )

        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)

        # SAVE FOR CSV (IMPORTANT)
        session['collected_data'].append([date, t, p, val])

        table_data.append({
            "name": p,
            "register": "N/A",
            "value": val
        })

        graph_data.append({
            "name": p,
            "time": t,
            "value": val
        })

    # CACHE ONCE
    LIVE_DATA_CACHE[user_id] = {
        "table": table_data,
        "graph": graph_data
    }

    session.modified = True
    return table_data, graph_data

# ================= CSV SAVE =================
def save_data_to_csv():
    data = session.get("collected_data", [])
    if not data:
        return

    filename = f"{session.get('user_name')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    filepath = os.path.join(DATA_FOLDER, filename)

    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Time", "Parameter", "Value"])
        writer.writerows(data)

    app.logger.info(f"DATA SAVED | {filename}")

# ================= SESSION EXPIRY HANDLER =================
def handle_session_expiry():
    if not data_session_active():
        if session.get("data_active"):
            save_data_to_csv()
            session['data_active'] = False
            app.logger.info("DATA SESSION END | CSV saved")
        return True
    return False

# ================= ROUTES =================
@app.route('/')
def home():
    return redirect(url_for('login'))

# ---------- REGISTER ----------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        if User.query.filter_by(email=request.form['email']).first():
            app.logger.warning("REGISTER FAILED | Email exists")
            flash("Email already registered", "register_error")
            return render_template("register.html")

        user = User(
            request.form['name'],
            request.form['email'],
            request.form['password']
        )
        db.session.add(user)
        db.session.commit()

        app.logger.info(f"REGISTER SUCCESS | {user.email}")
        return redirect(url_for('login'))

    return render_template('register.html')

# ---------- LOGIN ----------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        user = User.query.filter_by(email=email).first()

        if not user:
            app.logger.warning(f"LOGIN FAILED | Unknown email | {email}")
            flash("Invalid email or password", "login_error")
            return render_template("login.html")

        if not user.check_password(request.form['password']):
            app.logger.warning(f"LOGIN FAILED | Wrong password | {email}")
            flash("Invalid email or password", "login_error")
            return render_template("login.html")

        session.clear()
        session['user_id'] = user.id
        session['user_name'] = user.name

        app.logger.info(f"LOGIN SUCCESS | {email}")
        return redirect(url_for('upload_modbus'))

    return render_template('login.html')

# ---------- FORGOT ----------
@app.route('/forgot', methods=['GET', 'POST'])
def forgot():
    if request.method == 'POST':
        user = User.query.filter_by(email=request.form['email']).first()
        if not user:
            app.logger.warning("FORGOT FAILED | Email not found")
            flash("Email not found", "forgot_error")
            return render_template("forgot.html")

        user.password = bcrypt.hashpw(
            request.form['password'].encode(),
            bcrypt.gensalt()
        ).decode()
        db.session.commit()

        app.logger.info(f"PASSWORD RESET | {user.email}")
        return redirect(url_for('login'))

    return render_template('forgot.html')

# ---------- UPLOAD ----------
@app.route('/upload-modbus', methods=['GET', 'POST'])
@login_required
def upload_modbus():
    if request.method == 'POST':
        file = request.files.get('modbus_file')

        if not file:
            app.logger.warning("UPLOAD FAILED | No file selected")
            flash("No file selected", "upload_error")
            return render_template("upload_modbus.html")

        if not allowed_file(file.filename):
            app.logger.warning(f"UPLOAD FAILED | Invalid file {file.filename}")
            flash("Invalid file format. Only CSV or CMC3 allowed.", "upload_error")
            return render_template("upload_modbus.html")
        
        path = os.path.join(app.config["UPLOAD_FOLDER"], file.filename)
        file.save(path)
        session['modbus_file'] = path

        app.logger.info(f"FILE UPLOADED | {file.filename}")
        return redirect(url_for('select_parameters'))

    return render_template('upload_modbus.html')

# ---------- SELECT PARAMETERS ----------
@app.route('/select-parameters', methods=['GET', 'POST'])
@login_required
def select_parameters():
    if request.method == 'POST':
        selected = request.form.getlist('params')

        if not selected:
            app.logger.warning("PARAM SELECT FAILED | No parameters selected")
            return redirect(url_for('select_parameters'))

        duration = int(request.form.get("session_time", 10))

        session['selected_params'] = selected
        session['data_expires_at'] = int(time()) + duration * 60
        session['data_active'] = True
        session['collected_data'] = []

        LIVE_DATA_CACHE.pop(session['user_id'], None)
        session.pop("last_generated_ts", None)

        app.logger.info(
            f"DATA SESSION START | params={len(selected)} | duration={duration}min"
        )

        return redirect(url_for(request.form.get('action')))

    #  SAFE FILE CHECK
    modbus_path = session.get('modbus_file')

    if not modbus_path or not os.path.exists(modbus_path):
        app.logger.warning("SELECT PARAMETERS FAILED | No modbus file in session")
        return redirect(url_for('upload_modbus'))

    descriptions = read_modbus_file(modbus_path)
    return render_template('select_parameters.html', descriptions=descriptions)
# ---------- DASHBOARD ----------
@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/api/dashboard-data')
@login_required
def dashboard_data():

    if handle_session_expiry():
        params = session.get("selected_params", [])
        return jsonify([
            {
                "name": p,
                "register": "N/A",
                "value": "-"
            }
            for p in params
        ])

    table_data, _ = generate_and_cache_live_data()
    return jsonify(table_data)

# ---------- GRAPH ----------
@app.route('/graph')
@login_required
def graph():
    return render_template('graph.html')

@app.route('/api/graph-data')
@login_required
def graph_data():

    if handle_session_expiry():
        return jsonify([])

    _, graph_data = generate_and_cache_live_data()
    return jsonify(graph_data)

# ---------- LOGOUT ----------
@app.route('/logout')
def logout():
    LIVE_DATA_CACHE.pop(session.get('user_id'), None)
    session.clear()
    return redirect(url_for('login'))

# ---------- GLOBAL ERROR HANDLER ----------
# @app.errorhandler(Exception)
# def handle_exception(e):
#     app.logger.error(f"UNHANDLED ERROR | {str(e)}", exc_info=True)
#     return "Internal Server Error", 500 
#  //removed becaus of miss dead code

# ================= RUN =================
if __name__ == '__main__':
    app.run(debug=True, use_reloader=False, port=5001)
