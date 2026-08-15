import os, sqlite3, uuid, time, hashlib, hmac, secrets, math
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, jsonify
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from urllib import request as urllib_request
from urllib.parse import urlencode
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

BASE_DIR=Path(__file__).resolve().parent
DB_PATH=Path(os.environ.get("DATABASE_PATH", str(BASE_DIR/"pedidos_locales.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR=BASE_DIR/"static"/"uploads"; UPLOAD_DIR.mkdir(parents=True,exist_ok=True)

DATABASE_URL=os.environ.get("DATABASE_URL","").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL="postgresql://"+DATABASE_URL[len("postgres://"):]

app=Flask(__name__)
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,
    x_proto=1,
    x_host=1
)
app.secret_key=os.environ["SECRET_KEY"]
csrf=CSRFProtect(app)
app.config["MAX_CONTENT_LENGTH"]=5*1024*1024
ALLOWED={"png","jpg","jpeg","webp","gif"}
ADMIN_USER=os.environ["ADMIN_USER"]
ADMIN_PASSWORD=os.environ["ADMIN_PASSWORD"]
IS_PRODUCTION=os.environ.get("FLASK_ENV","").lower()=="production"
# Protección básica contra intentos repetidos de inicio de sesión
LOGIN_MAX_ATTEMPTS = 5
LOGIN_BLOCK_SECONDS = 300
login_attempts = {}

def login_blocked(ip, scope):
    now = time.time()
    key = (scope, ip)
    data = login_attempts.get(key)

    if not data:
        return False

    if data["blocked_until"] > 0 and now >= data["blocked_until"]:
        login_attempts.pop(key, None)
        return False

    return data["failed"] >= LOGIN_MAX_ATTEMPTS

def register_login_failure(ip, scope):
    now = time.time()
    key = (scope, ip)
    data = login_attempts.get(key)

    if not data:
        data = {"failed": 0, "blocked_until": 0}

    elif data["blocked_until"] > 0 and now >= data["blocked_until"]:
        data = {"failed": 0, "blocked_until": 0}

    data["failed"] += 1

    if data["failed"] >= LOGIN_MAX_ATTEMPTS:
        data["blocked_until"] = now + LOGIN_BLOCK_SECONDS

    login_attempts[key] = data

def clear_login_failures(ip, scope):
    login_attempts.pop((scope, ip), None)
# Seguridad de cookies de sesión
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION
# Encabezados de seguridad HTTP
@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(self), microphone=(), camera=()"
    return response
STATUSES={"nuevo":"🆕 Nuevo","preparando":"👨‍🍳 Preparando","camino":"🛵 En camino","entregado":"✅ Entregado","cancelado":"❌ Cancelado"}
DEFAULT_DELIVERY_FEE=35.00
# Tarifa dinámica de entrega:
# Hasta 5 km: $35. Después de 5 km: +$5 por cada km adicional.
# Si el resultado tiene .50 o más, se redondea al siguiente peso.
DELIVERY_BASE_KM=5.0
DELIVERY_BASE_FEE=35.0
DELIVERY_EXTRA_PER_KM=5.0
DEFAULT_COMMISSION_RATE=15.00
LOCAL_TZ=ZoneInfo("America/Merida")

# PayU WebCheckout
PAYU_MERCHANT_ID=os.environ.get("PAYU_MERCHANT_ID","").strip()
PAYU_ACCOUNT_ID=os.environ.get("PAYU_ACCOUNT_ID","").strip()
PAYU_API_KEY=os.environ.get("PAYU_API_KEY","").strip()
PAYU_TEST=os.environ.get("PAYU_TEST","1").strip() == "1"
PAYU_CURRENCY="MXN"
PAYU_CHECKOUT_URL=(
    "https://sandbox.checkout.payulatam.com/ppp-web-gateway-payu/"
    if PAYU_TEST else
    "https://checkout.payulatam.com/ppp-web-gateway-payu/"
)
# Conekta
CONEKTA_API_KEY=os.environ.get("CONEKTA_API_KEY","").strip()
CONEKTA_WEBHOOK_PUBLIC_KEY=os.environ.get("CONEKTA_WEBHOOK_PUBLIC_KEY","").strip()
CONEKTA_API_URL="https://api.conekta.io/orders"
CONEKTA_API_VERSION="application/vnd.conekta-v2.2.0+json"

GOOGLE_CLIENT_ID=os.environ.get("GOOGLE_CLIENT_ID","").strip()
GOOGLE_CLIENT_SECRET=os.environ.get("GOOGLE_CLIENT_SECRET","").strip()
GOOGLE_REDIRECT_URI=os.environ.get("GOOGLE_REDIRECT_URI","https://pedidos-locales.onrender.com/auth/google/callback").strip()

def conekta_verify_webhook(raw_body,digest):
    if not CONEKTA_WEBHOOK_PUBLIC_KEY or not digest: return False
    try:
        import base64
        key=serialization.load_pem_public_key(CONEKTA_WEBHOOK_PUBLIC_KEY.encode("utf-8"))
        key.verify(base64.b64decode(digest),raw_body,padding.PKCS1v15(),hashes.SHA256())
        return True
    except Exception: return False

def conekta_create_order(order,items):
    if not CONEKTA_API_KEY: raise RuntimeError("Conekta no está configurado en el servidor.")
    line_items=[{"name":str(x["product_name"]),"quantity":int(x["quantity"]),"unit_price":int(round(float(x["price"])*100))} for x in items]
    payload={"currency":"MXN","customer_info":{"name":order["customer_name"],"email":order["customer_email"],"phone":order["customer_phone"]},"metadata":{"pedido_locales_id":str(order["id"])},"line_items":line_items,"checkout":{"type":"HostedPayment","allowed_payment_methods":["card"],"success_url":url_for("conekta_success",order_id=order["id"],_external=True),"failure_url":url_for("conekta_failure",order_id=order["id"],_external=True),"redirection_time":4}}
    req=urllib_request.Request(CONEKTA_API_URL,data=json.dumps(payload).encode("utf-8"),headers={"Authorization":f"Bearer {CONEKTA_API_KEY}","Accept":CONEKTA_API_VERSION,"Content-Type":"application/json","Accept-Language":"es"},method="POST")
    with urllib_request.urlopen(req,timeout=20) as r: return json.loads(r.read().decode("utf-8"))

def payu_signature(value):
    return hashlib.md5(value.encode("utf-8")).hexdigest()

def payu_webcheckout_signature(reference, amount):
    amount_str=f"{float(amount):.2f}"
    raw=f"{PAYU_API_KEY}~{PAYU_MERCHANT_ID}~{reference}~{amount_str}~{PAYU_CURRENCY}"
    return payu_signature(raw)

def payu_response_value(value):
    d=Decimal(str(value))
    return format(d.quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN), "f")

def payu_confirmation_value(value):
    s=str(value or "")
    if "." not in s:
        return s + ".0"
    whole, decimals=s.split(".", 1)
    if not decimals:
        return whole + ".0"
    if len(decimals) == 1:
        return whole + "." + decimals
    if decimals[1] == "0":
        return whole + "." + decimals[0]
    return whole + "." + decimals[:2]



def round_delivery_fee(amount):
    """Redondea al peso superior cuando los centavos son .50 o más."""
    return float(Decimal(str(amount)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_delivery_fee(distance_km):
    """Calcula el envío: 5 km = $35; después, +$5/km."""
    try:
        distance_km=max(0.0, float(distance_km))
    except (TypeError, ValueError):
        distance_km=0.0
    if distance_km <= DELIVERY_BASE_KM:
        raw=DELIVERY_BASE_FEE
    else:
        raw=DELIVERY_BASE_FEE + ((distance_km-DELIVERY_BASE_KM) * DELIVERY_EXTRA_PER_KM)
    return round_delivery_fee(raw)


def haversine_km(lat1, lon1, lat2, lon2):
    """Distancia en línea recta entre dos coordenadas GPS, en km."""
    earth_radius=6371.0088
    p1=math.radians(float(lat1))
    p2=math.radians(float(lat2))
    dp=math.radians(float(lat2)-float(lat1))
    dl=math.radians(float(lon2)-float(lon1))
    a=math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return earth_radius*2*math.atan2(math.sqrt(a), math.sqrt(1-a))


def route_distance_km(lat1, lon1, lat2, lon2):
    # Distancia real de recorrido por calles usando OSRM/OpenStreetMap.
    try:
        coords=f"{float(lon1)},{float(lat1)};{float(lon2)},{float(lat2)}"
        endpoint=(
            "https://router.project-osrm.org/route/v1/driving/"
            + coords
            + "?overview=false&steps=false"
        )
        req=urllib_request.Request(
            endpoint,
            headers={
                "User-Agent":"PedidosLocales/1.0 (https://pedidos-locales.onrender.com)",
                "Accept":"application/json"
            },
            method="GET"
        )
        with urllib_request.urlopen(req, timeout=8) as response:
            result=json.loads(response.read().decode("utf-8"))

        if result.get("code") != "Ok":
            raise RuntimeError("OSRM no encontró una ruta válida.")

        routes=result.get("routes") or []
        if not routes or routes[0].get("distance") is None:
            raise RuntimeError("OSRM no devolvió la distancia de la ruta.")

        distance_km=float(routes[0]["distance"])/1000.0
        if distance_km < 0 or distance_km > 500:
            raise RuntimeError("Distancia de ruta fuera de rango.")

        return distance_km

    except Exception as exc:
        app.logger.warning("No fue posible calcular la ruta por calles: %s", exc)
        raise RuntimeError(
            "No fue posible calcular la distancia por calles en este momento. "
            "Inténtalo nuevamente."
        )


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    if request.path == "/pedido/crear" or request.is_json:
        return jsonify(ok=False, error="Solicitud no autorizada. Recarga la página e inténtalo de nuevo."), 400
    return "Solicitud no autorizada. Recarga la página e inténtalo de nuevo.", 400


class DBConnection:
    """SQLite localmente; PostgreSQL cuando Render proporciona DATABASE_URL."""
    def __init__(self):
        self.postgres=bool(DATABASE_URL)
        if self.postgres:
            if psycopg is None:
                raise RuntimeError("Falta psycopg. Ejecuta pip install -r requirements.txt")
            self.conn=psycopg.connect(DATABASE_URL,row_factory=dict_row)
        else:
            self.conn=sqlite3.connect(DB_PATH)
            self.conn.row_factory=sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")

    def execute(self,sql,params=()):
        if self.postgres:
            sql=sql.replace("?","%s")
        return self.conn.execute(sql,params)

    def executemany(self,sql,seq):
        if self.postgres:
            sql=sql.replace("?", "%s")
            with self.conn.cursor() as cursor:
                return cursor.executemany(sql,seq)
        return self.conn.executemany(sql,seq)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    def executescript(self,script):
        if self.postgres:
            for statement in script.split(";"):
                statement=statement.strip()
                if statement:
                    self.conn.execute(statement)
        else:
            self.conn.executescript(script)

def db():
    return DBConnection()

def init_db():
    c=db()
    if c.postgres:
        c.execute("CREATE TABLE IF NOT EXISTS platform_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    else:
        c.execute("CREATE TABLE IF NOT EXISTS platform_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    if c.execute("SELECT 1 FROM platform_settings WHERE key='commission_rate'").fetchone() is None:
        c.execute("INSERT INTO platform_settings(key,value) VALUES(?,?)",("commission_rate",str(DEFAULT_COMMISSION_RATE)))
    if c.execute("SELECT 1 FROM platform_settings WHERE key='commission_enabled'").fetchone() is None:
        c.execute("INSERT INTO platform_settings(key,value) VALUES(?,?)",("commission_enabled","1"))

    if c.postgres:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS businesses(
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            name TEXT NOT NULL, category TEXT NOT NULL, description TEXT DEFAULT '',
            rating DOUBLE PRECISION DEFAULT 5, delivery_time TEXT DEFAULT '20-30 min',
            phone TEXT DEFAULT '', address TEXT DEFAULT '', featured INTEGER DEFAULT 0,
            image TEXT DEFAULT '', latitude DOUBLE PRECISION, longitude DOUBLE PRECISION,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            name TEXT NOT NULL, description TEXT DEFAULT '', price DOUBLE PRECISION NOT NULL DEFAULT 0,
            category TEXT DEFAULT 'General', image TEXT DEFAULT '', active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            customer_name TEXT NOT NULL, customer_phone TEXT NOT NULL, customer_address TEXT NOT NULL,
            notes TEXT DEFAULT '', payment_method TEXT DEFAULT 'Efectivo',
            total DOUBLE PRECISION NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'nuevo',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, courier_id INTEGER,
            picked_up_at TIMESTAMP, delivered_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS order_items(
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            product_id INTEGER, product_name TEXT NOT NULL, price DOUBLE PRECISION NOT NULL,
            quantity INTEGER NOT NULL, subtotal DOUBLE PRECISION NOT NULL
        );
        CREATE TABLE IF NOT EXISTS couriers(
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            name TEXT NOT NULL, phone TEXT NOT NULL, username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL, active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        for col, typ in [
            ("courier_id","INTEGER"),
            ("picked_up_at","TIMESTAMP"),
            ("delivered_at","TIMESTAMP"),
            ("subtotal","DOUBLE PRECISION NOT NULL DEFAULT 0"),
            ("delivery_fee","DOUBLE PRECISION NOT NULL DEFAULT 0"),
            ("delivery_distance_km","DOUBLE PRECISION NOT NULL DEFAULT 0"),
            ("commission_rate","DOUBLE PRECISION NOT NULL DEFAULT 15"),
            ("commission_amount","DOUBLE PRECISION NOT NULL DEFAULT 0"),
            ("customer_email","TEXT DEFAULT ''"),
            ("payment_status","TEXT NOT NULL DEFAULT 'not_required'"),
            ("payu_reference","TEXT DEFAULT ''"),
            ("payu_transaction_id","TEXT DEFAULT ''"),
            ("payu_state","TEXT DEFAULT ''"),
            ("payu_response_code","TEXT DEFAULT ''"),
            ("conekta_order_id","TEXT DEFAULT ''"),
            ("conekta_checkout_id","TEXT DEFAULT ''"),
            ("conekta_charge_id","TEXT DEFAULT ''"),
            ("conekta_event_id","TEXT DEFAULT ''"),
        ]:
            c.execute(f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {col} {typ}")
        for col, typ in [
            ("delivery_enabled","INTEGER DEFAULT 1"),
            ("delivery_fee","DOUBLE PRECISION DEFAULT 35"),
            ("latitude","DOUBLE PRECISION"),
            ("longitude","DOUBLE PRECISION"),
        ]:
            c.execute(f"ALTER TABLE businesses ADD COLUMN IF NOT EXISTS {col} {typ}")

    else:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS businesses(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,category TEXT NOT NULL,description TEXT DEFAULT '',rating REAL DEFAULT 5,delivery_time TEXT DEFAULT '20-30 min',phone TEXT DEFAULT '',address TEXT DEFAULT '',featured INTEGER DEFAULT 0,image TEXT DEFAULT '',delivery_enabled INTEGER DEFAULT 1,delivery_fee REAL DEFAULT 35,latitude REAL,longitude REAL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT,business_id INTEGER NOT NULL,name TEXT NOT NULL,description TEXT DEFAULT '',price REAL NOT NULL DEFAULT 0,category TEXT DEFAULT 'General',image TEXT DEFAULT '',active INTEGER DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,business_id INTEGER NOT NULL,customer_name TEXT NOT NULL,customer_phone TEXT NOT NULL,customer_address TEXT NOT NULL,notes TEXT DEFAULT '',payment_method TEXT DEFAULT 'Efectivo',total REAL NOT NULL DEFAULT 0,subtotal REAL NOT NULL DEFAULT 0,delivery_fee REAL NOT NULL DEFAULT 0,delivery_distance_km REAL NOT NULL DEFAULT 0,commission_rate REAL NOT NULL DEFAULT 15,commission_amount REAL NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'nuevo',created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS order_items(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER NOT NULL,product_id INTEGER,product_name TEXT NOT NULL,price REAL NOT NULL,quantity INTEGER NOT NULL,subtotal REAL NOT NULL,FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS couriers(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,phone TEXT NOT NULL,username TEXT NOT NULL UNIQUE,password TEXT NOT NULL,active INTEGER DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)
        order_columns={row["name"] for row in c.execute("PRAGMA table_info(orders)").fetchall()}
        if "courier_id" not in order_columns: c.execute("ALTER TABLE orders ADD COLUMN courier_id INTEGER")
        if "picked_up_at" not in order_columns: c.execute("ALTER TABLE orders ADD COLUMN picked_up_at TEXT")
        if "delivered_at" not in order_columns: c.execute("ALTER TABLE orders ADD COLUMN delivered_at TEXT")
        business_columns={row["name"] for row in c.execute("PRAGMA table_info(businesses)").fetchall()}
        if "delivery_enabled" not in business_columns: c.execute("ALTER TABLE businesses ADD COLUMN delivery_enabled INTEGER DEFAULT 1")
        if "delivery_fee" not in business_columns: c.execute("ALTER TABLE businesses ADD COLUMN delivery_fee REAL DEFAULT 35")
        if "latitude" not in business_columns: c.execute("ALTER TABLE businesses ADD COLUMN latitude REAL")
        if "longitude" not in business_columns: c.execute("ALTER TABLE businesses ADD COLUMN longitude REAL")
        for col, typ in [
            ("subtotal","REAL NOT NULL DEFAULT 0"),
            ("delivery_fee","REAL NOT NULL DEFAULT 0"),
            ("delivery_distance_km","REAL NOT NULL DEFAULT 0"),
            ("commission_rate","REAL NOT NULL DEFAULT 15"),
            ("commission_amount","REAL NOT NULL DEFAULT 0"),
            ("customer_email","TEXT DEFAULT ''"),
            ("payment_status","TEXT NOT NULL DEFAULT 'not_required'"),
            ("payu_reference","TEXT DEFAULT ''"),
            ("payu_transaction_id","TEXT DEFAULT ''"),
            ("payu_state","TEXT DEFAULT ''"),
            ("payu_response_code","TEXT DEFAULT ''"),
            ("conekta_order_id","TEXT DEFAULT ''"),
            ("conekta_checkout_id","TEXT DEFAULT ''"),
            ("conekta_charge_id","TEXT DEFAULT ''"),
            ("conekta_event_id","TEXT DEFAULT ''")
        ]:
            if col not in order_columns: c.execute(f"ALTER TABLE orders ADD COLUMN {col} {typ}")

    if c.postgres:
        c.execute("CREATE TABLE IF NOT EXISTS settlement_payments(id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,amount DOUBLE PRECISION NOT NULL,paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,payment_method TEXT DEFAULT 'Transferencia',notes TEXT DEFAULT '',period_start DATE,period_end DATE)")
    else:
        c.execute("CREATE TABLE IF NOT EXISTS settlement_payments(id INTEGER PRIMARY KEY AUTOINCREMENT,business_id INTEGER NOT NULL,amount REAL NOT NULL,paid_at TEXT DEFAULT CURRENT_TIMESTAMP,payment_method TEXT DEFAULT 'Transferencia',notes TEXT DEFAULT '',period_start TEXT,period_end TEXT,FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE)")

    if c.execute("SELECT COUNT(*) AS c FROM businesses").fetchone()["c"]==0:
        c.execute("INSERT INTO businesses(name,category,description,rating,delivery_time,address,featured) VALUES(?,?,?,?,?,?,1)",
                  ("Taquería El Sabor","Comida","Tacos, tortas y bebidas",4.8,"20-30 min","Mérida, Yucatán"))
        b1=c.execute("SELECT id FROM businesses ORDER BY id DESC LIMIT 1").fetchone()["id"]
        c.execute("INSERT INTO businesses(name,category,description,rating,delivery_time,address,featured) VALUES(?,?,?,?,?,?,1)",
                  ("Papelería La Estrella","Papelería","Útiles escolares, copias e impresiones",4.7,"15-25 min","Mérida, Yucatán"))
        b2=c.execute("SELECT id FROM businesses ORDER BY id DESC LIMIT 1").fetchone()["id"]
        ps=[
            (b1,"Taco al Pastor","Carne al pastor con cebolla, cilantro y piña.",18,"Tacos"),
            (b1,"Taco de Bistec","Bistec con cebolla, cilantro y salsa.",20,"Tacos"),
            (b1,"Torta de Milanesa","Milanesa con frijoles y verduras.",45,"Tortas"),
            (b1,"Agua de Jamaica","Agua fresca natural.",25,"Bebidas"),
            (b2,"Cuaderno Profesional","100 hojas, cuadros.",28,"Útiles"),
            (b2,"Lápiz #2","Lápiz escolar.",6,"Escritura"),
            (b2,"Bolígrafo Azul","Tinta azul.",7,"Escritura"),
            (b2,"Resaltador","Varios colores.",12,"Escritura"),
            (b2,"Marcadores","Paquete de 12.",45,"Papelería"),
            (b2,"Pegamento en Barra","Pegamento escolar.",18,"Papelería"),
            (b2,"Hojas Blancas","Paquete de 100.",55,"Papelería"),
            (b2,"Impresión B/N","Tamaño carta.",2,"Impresiones")
        ]
        c.executemany("INSERT INTO products(business_id,name,description,price,category) VALUES(?,?,?,?,?)",ps)

    if c.execute("SELECT COUNT(*) AS c FROM couriers").fetchone()["c"]==0:
        c.executemany(
            "INSERT INTO couriers(name,phone,username,password) VALUES(?,?,?,?)",
            [
                ("Carlos Repartidor","9990000001","carlos",generate_password_hash("1234")),
                ("Ana Repartidora","9990000002","ana",generate_password_hash("1234"))
            ]
        )

    # Convert existing UTC timestamps from the previous versions to Mérida local time once.
    tz_migration_key = "timezone_migration_merida_v1"
    if c.execute("SELECT 1 FROM platform_settings WHERE key=?", (tz_migration_key,)).fetchone() is None:
        for table, columns in [
            ("orders", ["created_at", "picked_up_at", "delivered_at"]),
            ("settlement_payments", ["paid_at"]),
        ]:
            rows = c.execute(
                f"SELECT id,{','.join(columns)} FROM {table}"
            ).fetchall()
            for row in rows:
                for col in columns:
                    value = row[col]
                    if not value:
                        continue
                    try:
                        if isinstance(value, datetime):
                            dt = value
                        else:
                            dt = datetime.fromisoformat(str(value).replace("Z", ""))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
                        local_value = dt.astimezone(LOCAL_TZ).replace(tzinfo=None).isoformat(timespec="seconds")
                        c.execute(
                            f"UPDATE {table} SET {col}=? WHERE id=?",
                            (local_value, row["id"])
                        )
                    except (TypeError, ValueError, OverflowError):
                        pass
        c.execute(
            "INSERT INTO platform_settings(key,value) VALUES(?,?)",
            (tz_migration_key, "1")
        )

    c.commit()
    c.close()

def ensure_user_schema(c):
    if c.postgres:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            google_sub TEXT UNIQUE NOT NULL,email TEXT NOT NULL,name TEXT DEFAULT '',picture TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,last_login_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS user_id INTEGER")
    else:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            google_sub TEXT UNIQUE NOT NULL,email TEXT NOT NULL,name TEXT DEFAULT '',picture TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,last_login_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        cols={r["name"] for r in c.execute("PRAGMA table_info(orders)").fetchall()}
        if "user_id" not in cols: c.execute("ALTER TABLE orders ADD COLUMN user_id INTEGER")
    c.commit()

def auth(f):
    @wraps(f)
    def w(*a, **k):
        # Un repartidor nunca puede acceder a rutas administrativas
        if session.get("courier_logged"):
            abort(403)

        if not session.get("admin_logged"):
            return redirect(url_for("admin_login", next=request.path))

        return f(*a, **k)
    return w

def save_image(file):
    """Sube a Cloudinary si está configurado; si no, usa el almacenamiento local."""
    if not file or not file.filename or "." not in file.filename:
        return ""

    ext=file.filename.rsplit(".",1)[1].lower()
    if ext not in ALLOWED:
        return ""

    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME","").strip()
    upload_preset=os.environ.get("CLOUDINARY_UPLOAD_PRESET","").strip()

    if cloud_name and upload_preset:
        try:
            file.stream.seek(0)
            image_bytes=file.stream.read()

            boundary="----PedidosLocalesCloudinary" + uuid.uuid4().hex
            fields={
                "upload_preset": upload_preset,
                "folder": "pedidos-locales"
            }

            parts=[]
            for key,value in fields.items():
                parts.append(
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                    f"{value}\r\n"
                )

            filename=secure_filename(file.filename) or f"imagen.{ext}"
            mime={
                "jpg":"image/jpeg",
                "jpeg":"image/jpeg",
                "png":"image/png",
                "webp":"image/webp",
                "gif":"image/gif"
            }.get(ext,"application/octet-stream")

            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            )

            payload="".join(parts).encode("utf-8")
            payload += image_bytes
            payload += f"\r\n--{boundary}--\r\n".encode("utf-8")

            endpoint=f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload"
            req=urllib_request.Request(
                endpoint,
                data=payload,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(len(payload))
                },
                method="POST"
            )

            with urllib_request.urlopen(req, timeout=30) as response:
                result=json.loads(response.read().decode("utf-8"))

            secure_url=result.get("secure_url","")
            if secure_url:
                return secure_url

            app.logger.error("Cloudinary no devolvió secure_url: %s", result)
            return ""

        except Exception:
            app.logger.exception("Error al subir imagen a Cloudinary")
            return ""

    # Fallback para desarrollo local cuando Cloudinary no está configurado.
    original=secure_filename(file.filename)
    stem,_=os.path.splitext(original)
    name=f"{stem}_{uuid.uuid4().hex[:8]}.{ext}"
    file.stream.seek(0)
    file.save(UPLOAD_DIR/name)
    return name

def image_url(value):
    """Devuelve una URL Cloudinary o la ruta local equivalente."""
    if not value:
        return ""
    value=str(value)
    if value.startswith(("http://","https://")):
        return value
    return url_for("static", filename=f"uploads/{value}")


@app.context_processor
def helpers():
    return {
        "admin_logged": session.get("admin_logged", False),
        "user_logged": session.get("user_logged", False),
        "user_name": session.get("user_name", ""),
        "user_email": session.get("user_email", ""),
        "user_picture": session.get("user_picture", ""),
        "courier_logged": session.get("courier_logged", False),
        "courier_name": session.get("courier_name", ""),
        "statuses": STATUSES,
        "image_url": image_url
    }

def get_setting(c, key, default):
    row=c.execute("SELECT value FROM platform_settings WHERE key=?",(key,)).fetchone()
    if not row:
        return default
    return row["value"]

def platform_commission(c):
    enabled=get_setting(c,"commission_enabled","1")=="1"
    try: rate=float(get_setting(c,"commission_rate",str(DEFAULT_COMMISSION_RATE)))
    except (TypeError,ValueError): rate=DEFAULT_COMMISSION_RATE
    return enabled, max(0.0,min(rate,100.0))

@app.get("/auth/google")
def google_login():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        flash("El acceso con Google todavía no está configurado.","error"); return redirect(url_for("inicio"))
    state=secrets.token_urlsafe(32); session["google_oauth_state"]=state
    next_url=request.args.get("next") or url_for("inicio")
    if not next_url.startswith("/"): next_url=url_for("inicio")
    session["google_next"]=next_url
    params={"client_id":GOOGLE_CLIENT_ID,"redirect_uri":GOOGLE_REDIRECT_URI,"response_type":"code","scope":"openid email profile","state":state,"access_type":"online","prompt":"select_account"}
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?"+urlencode(params))

@app.get("/auth/google/callback")
def google_callback():
    expected=session.pop("google_oauth_state",None); next_url=session.pop("google_next",url_for("inicio"))
    if not expected or not hmac.compare_digest(expected,request.args.get("state","")): return "Solicitud de inicio de sesión inválida.",400
    if request.args.get("error"): flash("El inicio de sesión con Google fue cancelado.","error"); return redirect(url_for("inicio"))
    code=request.args.get("code","").strip()
    if not code: return "Google no devolvió un código de autorización.",400
    try:
        body=urlencode({"code":code,"client_id":GOOGLE_CLIENT_ID,"client_secret":GOOGLE_CLIENT_SECRET,"redirect_uri":GOOGLE_REDIRECT_URI,"grant_type":"authorization_code"}).encode("utf-8")
        req=urllib_request.Request("https://oauth2.googleapis.com/token",data=body,headers={"Content-Type":"application/x-www-form-urlencoded"},method="POST")
        with urllib_request.urlopen(req,timeout=15) as response: token=json.loads(response.read().decode("utf-8"))
        raw=token.get("id_token","")
        if not raw: raise RuntimeError("Google no devolvió un ID token.")
        from google.oauth2 import id_token as google_id_token
        from google.auth.transport import requests as google_requests
        info=google_id_token.verify_oauth2_token(raw,google_requests.Request(),GOOGLE_CLIENT_ID)
        if info.get("iss") not in ("accounts.google.com","https://accounts.google.com"): raise ValueError("Emisor inválido")
        if not info.get("sub") or not info.get("email") or not info.get("email_verified"): raise ValueError("Identidad de Google no válida")
        google_sub=str(info["sub"]); email=str(info["email"]).strip().lower(); name=str(info.get("name") or email.split("@")[0]).strip(); picture=str(info.get("picture") or "")
        c=db(); ensure_user_schema(c); user=c.execute("SELECT id FROM users WHERE google_sub=?",(google_sub,)).fetchone(); now=datetime.now(LOCAL_TZ).replace(tzinfo=None).isoformat(timespec="seconds")
        if user:
            user_id=user["id"]; c.execute("UPDATE users SET email=?,name=?,picture=?,last_login_at=? WHERE id=?",(email,name,picture,now,user_id))
        elif c.postgres:
            cur=c.execute("INSERT INTO users(google_sub,email,name,picture,last_login_at) VALUES(?,?,?,?,?) RETURNING id",(google_sub,email,name,picture,now)); user_id=cur.fetchone()["id"]
        else:
            cur=c.execute("INSERT INTO users(google_sub,email,name,picture,last_login_at) VALUES(?,?,?,?,?)",(google_sub,email,name,picture,now)); user_id=cur.lastrowid
        c.commit(); c.close()
        session["user_logged"]=True; session["user_id"]=user_id; session["user_name"]=name; session["user_email"]=email; session["user_picture"]=picture
        return redirect(next_url if next_url.startswith("/") else url_for("inicio"))
    except Exception:
        app.logger.exception("Error en inicio de sesión con Google"); flash("No fue posible iniciar sesión con Google. Inténtalo nuevamente.","error"); return redirect(url_for("inicio"))

@app.get("/cuenta")
def cuenta_usuario():
    if not session.get("user_logged"): return redirect(url_for("google_login",next=url_for("cuenta_usuario")))
    c=db(); ensure_user_schema(c)
    pedidos=c.execute("SELECT o.id,o.total,o.status,o.created_at,b.name business_name FROM orders o JOIN businesses b ON b.id=o.business_id WHERE o.user_id=? ORDER BY o.id DESC LIMIT 20",(session["user_id"],)).fetchall(); c.close()
    return render_template("user_account.html",pedidos=pedidos)

@app.get("/auth/logout")
def user_logout():
    for k in ("user_logged","user_id","user_name","user_email","user_picture"): session.pop(k,None)
    return redirect(url_for("inicio"))



@app.get("/terminos")
def terminos():
    return render_template("terms.html")

@app.get("/privacidad")
def privacidad():
    return render_template("privacy.html")

@app.get("/acerca-de")
def acerca_de():
    return render_template("about.html")

@app.route("/")
def inicio():
    c=db()
    destacados=c.execute("SELECT b.*,COUNT(p.id) product_count FROM businesses b LEFT JOIN products p ON p.business_id=b.id AND p.active=1 WHERE b.featured=1 GROUP BY b.id ORDER BY b.name").fetchall()
    negocios=c.execute("SELECT b.*,COUNT(p.id) product_count FROM businesses b LEFT JOIN products p ON p.business_id=b.id AND p.active=1 GROUP BY b.id ORDER BY b.featured DESC,b.name").fetchall()
    c.close()
    return render_template("index.html",negocios=negocios,destacados=destacados)

@app.route("/negocio/<int:negocio_id>")
def negocio(negocio_id):
    c=db(); n=c.execute("SELECT * FROM businesses WHERE id=?",(negocio_id,)).fetchone()
    if not n:c.close();abort(404)
    p=c.execute("SELECT * FROM products WHERE business_id=? AND active=1 ORDER BY category,name",(negocio_id,)).fetchall()
    commission_enabled, commission_rate=platform_commission(c)
    c.close()
    return render_template("negocio.html",negocio=dict(n),productos=p,commission_enabled=commission_enabled,commission_rate=commission_rate)


@app.get("/api/cotizar-envio/<int:business_id>")
def cotizar_envio(business_id):
    try:
        lat=float(request.args.get("lat"))
        lon=float(request.args.get("lon"))
    except (TypeError,ValueError):
        return jsonify(ok=False,error="Ubicación del cliente inválida."),400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify(ok=False,error="Ubicación del cliente inválida."),400
    c=db()
    business=c.execute("SELECT id,delivery_enabled,latitude,longitude FROM businesses WHERE id=?",(business_id,)).fetchone()
    c.close()
    if not business:
        return jsonify(ok=False,error="Negocio no encontrado."),404
    if not bool(business["delivery_enabled"]):
        return jsonify(ok=True,delivery_enabled=False,distance_km=0,delivery_fee=0)
    if business["latitude"] is None or business["longitude"] is None:
        return jsonify(ok=False,error="Este negocio aún no tiene configurada su ubicación para calcular el envío."),409
    try:
        distance=route_distance_km(
            lat,
            lon,
            float(business["latitude"]),
            float(business["longitude"])
        )
    except RuntimeError as exc:
        return jsonify(ok=False,error=str(exc)),503

    return jsonify(
        ok=True,
        delivery_enabled=True,
        distance_km=round(distance,2),
        delivery_fee=calculate_delivery_fee(distance)
    )


@app.post("/pedido/crear")
def crear_pedido():
    data=request.get_json(silent=True) or {}; customer=data.get("customer") or {}; items=data.get("items") or []
    try: bid=int(data.get("business_id"))
    except: return jsonify(ok=False,error="Negocio inválido."),400
    name=str(customer.get("name","")).strip()
    phone=str(customer.get("phone","")).strip()
    address=str(customer.get("address","")).strip()
    email=str(customer.get("email","")).strip().lower()
    user_id=session.get("user_id") if session.get("user_logged") else None
    notes=str(customer.get("notes","")).strip()
    payment=str(customer.get("payment_method","Efectivo")).strip()
    if payment in ("Tarjeta","Tarjeta con PayU","Tarjeta (PayU)"):
        payment="Tarjeta (PayU)"
    if payment in ("Tarjeta con Conekta","Tarjeta (Conekta)","Conekta"):
        payment="Tarjeta (Conekta)"
    if not name or not phone or not address or not items:
        return jsonify(ok=False,error="Completa tus datos y agrega al menos un producto."),400
    if payment in ("Tarjeta (PayU)","Tarjeta (Conekta)") and ("@" not in email or "." not in email.split("@")[-1]):
        return jsonify(ok=False,error="Para pagar con tarjeta necesitamos un correo electrónico válido."),400
    c=db()
    if user_id is not None: ensure_user_schema(c)
    business=c.execute("SELECT * FROM businesses WHERE id=?",(bid,)).fetchone()
    if not business:c.close();return jsonify(ok=False,error="Negocio no encontrado."),404
    clean=[]; total=0
    for x in items:
        try: pid=int(x.get("id")); qty=int(x.get("qty"))
        except: continue
        if qty<1 or qty>99:continue
        p=c.execute("SELECT id,name,price FROM products WHERE id=? AND business_id=? AND active=1",(pid,bid)).fetchone()
        if not p:continue
        sub=float(p["price"])*qty; total+=sub; clean.append((p["id"],p["name"],float(p["price"]),qty,sub))
    if not clean:c.close();return jsonify(ok=False,error="No hay productos válidos."),400
    delivery_enabled=bool(business["delivery_enabled"])
    distance_km=0.0
    if delivery_enabled:
        try:
            customer_lat=float(customer.get("latitude"))
            customer_lon=float(customer.get("longitude"))
        except (TypeError,ValueError):
            c.close()
            return jsonify(ok=False,error="Necesitamos tu ubicación para calcular el costo de envío."),400
        if not (-90 <= customer_lat <= 90 and -180 <= customer_lon <= 180):
            c.close()
            return jsonify(ok=False,error="La ubicación de entrega no es válida."),400
        try:
            business_lat=float(business["latitude"]) if business["latitude"] is not None else None
            business_lon=float(business["longitude"]) if business["longitude"] is not None else None
        except (TypeError,ValueError):
            business_lat=business_lon=None
        if business_lat is None or business_lon is None:
            c.close()
            return jsonify(ok=False,error="Este negocio aún no tiene configurada su ubicación para calcular el envío."),409
        try:
            distance_km=route_distance_km(
                customer_lat,
                customer_lon,
                business_lat,
                business_lon
            )
        except RuntimeError as exc:
            c.close()
            return jsonify(ok=False,error=str(exc)),503

        delivery_fee=calculate_delivery_fee(distance_km)
    else:
        delivery_fee=0.0
    commission_enabled, commission_rate=platform_commission(c)
    commission_amount=round(total*commission_rate/100,2) if commission_enabled else 0.0
    grand_total=round(total+delivery_fee+commission_amount,2)
    # Guardamos la hora local de Mérida como timestamp sin zona horaria.
    now=datetime.now(LOCAL_TZ).replace(tzinfo=None).isoformat(timespec="seconds")
    if c.postgres:
        cur=c.execute(
            "INSERT INTO orders(business_id,user_id,customer_name,customer_phone,customer_address,customer_email,notes,payment_method,payment_status,total,subtotal,delivery_fee,delivery_distance_km,commission_rate,commission_amount,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (bid,user_id,name,phone,address,email,notes,payment,"pending" if payment in ("Tarjeta (PayU)","Tarjeta (Conekta)") else "not_required",grand_total,total,delivery_fee,distance_km,commission_rate,commission_amount,"nuevo",now)
        )
        oid=cur.fetchone()["id"]
    else:
        cur=c.execute(
            "INSERT INTO orders(business_id,user_id,customer_name,customer_phone,customer_address,customer_email,notes,payment_method,payment_status,total,subtotal,delivery_fee,delivery_distance_km,commission_rate,commission_amount,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bid,user_id,name,phone,address,email,notes,payment,"pending" if payment in ("Tarjeta (PayU)","Tarjeta (Conekta)") else "not_required",grand_total,total,delivery_fee,distance_km,commission_rate,commission_amount,"nuevo",now)
        )
        oid=cur.lastrowid

    c.executemany(
        "INSERT INTO order_items(order_id,product_id,product_name,price,quantity,subtotal) VALUES(?,?,?,?,?,?)",
        [(oid,*x) for x in clean]
    )
    c.commit();c.close();return jsonify(ok=True,order_id=oid,subtotal=total,delivery_fee=delivery_fee,delivery_distance_km=round(distance_km,2),commission_rate=commission_rate,commission_amount=commission_amount,total=grand_total)


@app.get("/pago/conekta/<int:order_id>")
def conekta_checkout(order_id):
    if not CONEKTA_API_KEY: return "Conekta no está configurado en el servidor.",500
    c=db(); order=c.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone(); items=c.execute("SELECT * FROM order_items WHERE order_id=?",(order_id,)).fetchall(); c.close()
    if not order: abort(404)
    if order["payment_method"]!="Tarjeta (Conekta)": return redirect(url_for("confirmado",order_id=order_id))
    try:
        result=conekta_create_order(order,items); checkout=result.get("checkout") or {}; url_pago=checkout.get("url"); conekta_id=result.get("id",""); checkout_id=checkout.get("id","")
        if not url_pago or not conekta_id: return "Conekta no devolvió una URL de pago válida.",502
        c=db(); c.execute("UPDATE orders SET payment_status='pending', conekta_order_id=?, conekta_checkout_id=? WHERE id=?",(conekta_id,checkout_id,order_id)); c.commit(); c.close()
        return redirect(url_pago)
    except Exception as e:
        app.logger.exception("Error creando checkout Conekta para pedido %s",order_id); return f"No se pudo iniciar el pago con Conekta: {e}",502

@app.get("/pago/conekta/exito/<int:order_id>")
def conekta_success(order_id): return redirect(url_for("confirmado",order_id=order_id))

@app.get("/pago/conekta/fallo/<int:order_id>")
def conekta_failure(order_id):
    c=db(); c.execute("UPDATE orders SET payment_status='rejected' WHERE id=? AND payment_status='pending'",(order_id,)); c.commit(); c.close()
    return redirect(url_for("confirmado",order_id=order_id))

@csrf.exempt
@app.post("/webhooks/conekta")
def conekta_webhook():
    raw=request.get_data(cache=False); digest=request.headers.get("DIGEST","")
    if not conekta_verify_webhook(raw,digest): return "Firma inválida",401
    try: event=json.loads(raw.decode("utf-8"))
    except Exception: return "JSON inválido",400
    event_id=str(event.get("id","")); event_type=str(event.get("type","")); obj=((event.get("data") or {}).get("object") or {}); conekta_id=str(obj.get("id",""))
    if not event_id or not conekta_id: return "OK",200
    c=db(); order=c.execute("SELECT id,total,payment_status,conekta_event_id FROM orders WHERE conekta_order_id=?",(conekta_id,)).fetchone()
    if not order: c.close(); return "OK",200

    # Idempotencia: Conekta puede reenviar el mismo evento.
    if order["conekta_event_id"] == event_id:
        c.close()
        return "OK",200

    amount=int(obj.get("amount",0) or 0); expected=int(round(float(order["total"])*100)); currency=str(obj.get("currency","")).upper(); status=str(obj.get("status","")).lower()
    if event_type=="order.paid" and status=="paid" and amount==expected and currency=="MXN":
        charges=((obj.get("charges") or {}).get("data") or []); charge_id=str(charges[0].get("id","")) if charges else ""
        c.execute("UPDATE orders SET payment_status='paid', conekta_charge_id=?, conekta_event_id=? WHERE id=?",(charge_id,event_id,order["id"]))
    elif event_type in ("order.declined","charge.declined"):
        c.execute("UPDATE orders SET payment_status='rejected', conekta_event_id=? WHERE id=?",(event_id,order["id"]))
    elif event_type in ("order.expired","charge.expired"):
        c.execute("UPDATE orders SET payment_status='expired', conekta_event_id=? WHERE id=?",(event_id,order["id"]))
    else:
        c.execute("UPDATE orders SET conekta_event_id=? WHERE id=?",(event_id,order["id"]))
    c.commit(); c.close(); return "OK",200

@app.get("/pago/payu/<int:order_id>")
def payu_checkout(order_id):
    if not PAYU_MERCHANT_ID or not PAYU_ACCOUNT_ID or not PAYU_API_KEY:
        return "PayU no está configurado en el servidor.", 500

    c=db()
    order=c.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
    c.close()

    if not order:
        abort(404)

    if order["payment_method"] != "Tarjeta (PayU)":
        return redirect(url_for("confirmado", order_id=order_id))

    reference=f"PEDIDO{order_id}"
    amount=f"{float(order['total']):.2f}"
    signature=payu_webcheckout_signature(reference, amount)

    c=db()
    c.execute(
        "UPDATE orders SET payu_reference=?, payment_status='pending' WHERE id=?",
        (reference,order_id)
    )
    c.commit()
    c.close()

    fields={
        "lng":"es",
        "merchantId":PAYU_MERCHANT_ID,
        "accountId":PAYU_ACCOUNT_ID,
        "algorithmSignature":"MD5",
        "description":f"Pedido Locales #{order_id}",
        "referenceCode":reference,
        "amount":amount,
        "tax":"0",
        "taxReturnBase":"0",
        "currency":PAYU_CURRENCY,
        "signature":signature,
        "test":"1" if PAYU_TEST else "0",
        "buyerFullName":order["customer_name"],
        "buyerEmail":order["customer_email"],
        "telephone":order["customer_phone"],
        "mobilePhone":order["customer_phone"],
        "shippingCountry":"MX",
        "shippingCity":"Merida",
        "shippingAddress":order["customer_address"],
        "responseUrl":url_for("payu_response",_external=True),
        "confirmationUrl":url_for("payu_confirmation",_external=True),
    }

    return render_template(
        "payu_checkout.html",
        action=PAYU_CHECKOUT_URL,
        fields=fields,
        order_id=order_id,
        amount=amount
    )

@app.get("/pago/respuesta")
def payu_response():
    q=request.args
    merchant=q.get("merchantId","")
    reference=q.get("referenceCode","")
    value=q.get("TX_VALUE","")
    currency=q.get("currency","")
    state=q.get("transactionState","")
    received=q.get("signature","")

    if not PAYU_API_KEY or merchant != PAYU_MERCHANT_ID or not reference:
        return "Respuesta de PayU inválida.",400

    try:
        formatted=payu_response_value(value)
    except Exception:
        return "Monto de PayU inválido.",400

    raw=f"{PAYU_API_KEY}~{merchant}~{reference}~{formatted}~{currency}~{state}"
    expected=payu_signature(raw)

    if not received or not hmac.compare_digest(received.lower(), expected.lower()):
        return "Firma de PayU inválida.",403

    if reference.startswith("PEDIDO"):
        try:
            order_id=int(reference[len("PEDIDO"):])
            return redirect(url_for("confirmado",order_id=order_id,payu_state=state))
        except (TypeError,ValueError):
            pass

    return "Pago procesado por PayU.",200

@csrf.exempt
@app.post("/pago/confirmacion")
def payu_confirmation():
    data=request.form

    merchant=data.get("merchant_id","")
    reference=data.get("reference_sale","")
    value=data.get("value","")
    currency=data.get("currency","")
    state=data.get("state_pol","")
    received=data.get("sign","")

    if not PAYU_API_KEY or merchant != PAYU_MERCHANT_ID:
        return "OK",200

    try:
        formatted=payu_confirmation_value(value)
    except Exception:
        return "OK",200

    raw=f"{PAYU_API_KEY}~{merchant}~{reference}~{formatted}~{currency}~{state}"
    expected=payu_signature(raw)

    if not received or not hmac.compare_digest(received.lower(), expected.lower()):
        app.logger.warning("PayU confirmation con firma inválida para %s", reference)
        return "Invalid signature",403

    if not reference.startswith("PEDIDO"):
        return "OK",200

    try:
        order_id=int(reference[len("PEDIDO"):])
    except (TypeError,ValueError):
        return "OK",200

    c=db()
    order=c.execute("SELECT id,total,payment_status FROM orders WHERE id=?",(order_id,)).fetchone()

    if order:
        # Una vez aprobado, no permitimos que otra notificación posterior
        # cambie el pedido a rechazado/expirado.
        if order["payment_status"] == "paid":
            c.close()
            return "OK",200

        try:
            payu_value=float(value)
        except (TypeError,ValueError):
            payu_value=-1

        expected_total=round(float(order["total"]),2)

        if abs(payu_value-expected_total) > 0.01 or currency != PAYU_CURRENCY:
            c.close()
            app.logger.warning("PayU monto/moneda no coincide para pedido %s", order_id)
            return "Invalid amount",400

        if state=="4":
            payment_status="paid"
        elif state=="6":
            payment_status="rejected"
        elif state=="5":
            payment_status="expired"
        else:
            payment_status=f"state_{state or 'unknown'}"

        c.execute(
            "UPDATE orders SET payment_status=?, payu_reference=?, payu_transaction_id=?, payu_state=?, payu_response_code=? WHERE id=?",
            (
                payment_status,
                reference,
                data.get("transaction_id",""),
                state,
                data.get("response_code_pol",""),
                order_id
            )
        )
        c.commit()

    c.close()
    return "OK",200

@app.route("/pedido/<int:order_id>/estado")
def pedido_estado(order_id):
    c = db()
    o = c.execute("""
        SELECT o.id,o.status,o.total,o.created_at,o.picked_up_at,o.delivered_at,
               b.name business_name
        FROM orders o
        JOIN businesses b ON b.id=o.business_id
        WHERE o.id=?
    """,(order_id,)).fetchone()
    c.close()
    if not o:
        return jsonify(ok=False,error="Pedido no encontrado."),404
    return jsonify(ok=True, pedido={
        "id": o["id"],
        "status": o["status"],
        "total": float(o["total"]),
        "business_name": o["business_name"],
        "created_at": o["created_at"],
        "picked_up_at": o["picked_up_at"],
        "delivered_at": o["delivered_at"]
    })

@app.route("/pedido/<int:order_id>/confirmado")
def confirmado(order_id):
    c=db(); o=c.execute("SELECT o.*,b.name business_name FROM orders o JOIN businesses b ON b.id=o.business_id WHERE o.id=?",(order_id,)).fetchone()
    if not o:c.close();abort(404)
    items=c.execute("SELECT * FROM order_items WHERE order_id=?",(order_id,)).fetchall();c.close()
    return render_template("pedido_confirmado.html",pedido=o,items=items)

@app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    if session.get("admin_logged"):
        return redirect(url_for("admin_dashboard"))

    ip = request.remote_addr or "unknown"

    if login_blocked(ip, "admin"):
        flash("Demasiados intentos fallidos. Inténtalo nuevamente en unos minutos.", "error")
        return render_template("admin_login.html"), 429

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if username == ADMIN_USER and password == ADMIN_PASSWORD:
            clear_login_failures(ip, "admin")
            session.clear()
            session["admin_logged"] = True

            return redirect(
                request.args.get("next") or url_for("admin_dashboard")
            )

        register_login_failure(ip, "admin")
        flash("Usuario o contraseña incorrectos.", "error")

    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():session.clear();return redirect(url_for("inicio"))

@app.route("/admin")
@auth
def admin_dashboard():
    c=db(); negocios=c.execute("SELECT b.*,COUNT(p.id) product_count FROM businesses b LEFT JOIN products p ON p.business_id=b.id GROUP BY b.id ORDER BY b.featured DESC,b.name").fetchall()
    totals={"businesses":c.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"],"products":c.execute("SELECT COUNT(*) c FROM products").fetchone()["c"],"featured":c.execute("SELECT COUNT(*) c FROM businesses WHERE featured=1").fetchone()["c"],"orders":c.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"],
        "new_orders":c.execute("SELECT COUNT(*) c FROM orders WHERE status='nuevo'").fetchone()["c"],
        "couriers":c.execute("SELECT COUNT(*) c FROM couriers WHERE active=1").fetchone()["c"]}
    recent=c.execute("""SELECT o.*,b.name business_name,c.name courier_name
        FROM orders o JOIN businesses b ON b.id=o.business_id
        LEFT JOIN couriers c ON c.id=o.courier_id
        ORDER BY o.id DESC LIMIT 8""").fetchall();c.close()
    return render_template("admin_dashboard.html",negocios=negocios,totals=totals,recent_orders=recent)

@app.route("/admin/pedidos")
@auth
def admin_orders():
    s = request.args.get("status", "")
    c = db()

    # Contadores de pedidos por estado
    status_counts = {}

    for key in STATUSES:
        row = c.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE status=?",
            (key,)
        ).fetchone()
        status_counts[key] = row["c"]

    # Total de pedidos
    total_orders = c.execute(
        "SELECT COUNT(*) AS c FROM orders"
    ).fetchone()["c"]

    # Pedidos del filtro seleccionado.
    # Los nuevos siempre aparecen primero cuando mostramos "Todos".
    if s in STATUSES:
        o = c.execute(
            """
            SELECT o.*, b.name business_name
            FROM orders o
            JOIN businesses b ON b.id=o.business_id
            WHERE o.status=?
            ORDER BY o.id DESC
            """,
            (s,)
        ).fetchall()
    else:
        o = c.execute(
            """
            SELECT o.*, b.name business_name
            FROM orders o
            JOIN businesses b ON b.id=o.business_id
            ORDER BY
                CASE
                    WHEN o.status='nuevo' THEN 0
                    WHEN o.status='preparando' THEN 1
                    WHEN o.status='camino' THEN 2
                    WHEN o.status='entregado' THEN 3
                    WHEN o.status='cancelado' THEN 4
                    ELSE 5
                END,
                o.id DESC
            """
        ).fetchall()

    c.close()

    return render_template(
        "admin_orders.html",
        pedidos=o,
        selected_status=s,
        statuses=STATUSES,
        status_counts=status_counts,
        total_orders=total_orders
    )
@app.route("/admin/pedidos/nuevos-count")
@auth
def admin_new_orders_count():
    c = db()

    row = c.execute(
        "SELECT COUNT(*) AS c FROM orders WHERE status='nuevo'"
    ).fetchone()

    total = c.execute(
        "SELECT COUNT(*) AS c FROM orders"
    ).fetchone()

    latest = c.execute(
        "SELECT id FROM orders WHERE status='nuevo' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    c.close()

    return jsonify(
        ok=True,
        count=row["c"],
        total=total["c"],
        latest_id=latest["id"] if latest else None
    )
@app.route("/admin/pedidos/<int:order_id>")
@auth
def admin_order_detail(order_id):
    c=db();o=c.execute("""SELECT o.*,b.name business_name,b.phone business_phone,c.name courier_name,c.phone courier_phone
        FROM orders o JOIN businesses b ON b.id=o.business_id
        LEFT JOIN couriers c ON c.id=o.courier_id
        WHERE o.id=?""",(order_id,)).fetchone()
    if not o:c.close();abort(404)
    items=c.execute("SELECT * FROM order_items WHERE order_id=?",(order_id,)).fetchall()
    repartidores=c.execute("SELECT * FROM couriers WHERE active=1 ORDER BY name").fetchall()
    c.close()
    return render_template("admin_order_detail.html",pedido=o,items=items,repartidores=repartidores)

@app.post("/admin/pedidos/<int:order_id>/estado")
@auth
def admin_order_status(order_id):
    s=request.form.get("status","")
    if s not in STATUSES:flash("Estado inválido.","error");return redirect(url_for("admin_order_detail",order_id=order_id))
    from datetime import datetime
    now=datetime.now(LOCAL_TZ).replace(tzinfo=None).isoformat(timespec="seconds")
    c=db()
    if s=="camino":
        c.execute("UPDATE orders SET status=?,picked_up_at=COALESCE(picked_up_at,?) WHERE id=?",(s,now,order_id))
    elif s=="entregado":
        c.execute("UPDATE orders SET status=?,delivered_at=COALESCE(delivered_at,?) WHERE id=?",(s,now,order_id))
    else:
        c.execute("UPDATE orders SET status=? WHERE id=?",(s,order_id))
    c.commit();c.close();flash("Estado actualizado.","success");return redirect(url_for("admin_order_detail",order_id=order_id))

@app.post("/admin/pedidos/<int:order_id>/eliminar")
@auth
def admin_order_delete(order_id):
    c=db();c.execute("DELETE FROM orders WHERE id=?",(order_id,));c.commit();c.close();flash("Pedido eliminado.","success");return redirect(url_for("admin_orders"))


# ---------------- COURIERS ----------------

@app.route("/admin/repartidores")
@auth
def admin_couriers():
    c=db()
    couriers=c.execute("SELECT * FROM couriers ORDER BY active DESC,name").fetchall()
    c.close()
    return render_template("admin_couriers.html", repartidores=couriers)

@app.route("/admin/repartidores/nuevo", methods=["GET","POST"])
@auth
def admin_courier_new():
    if request.method=="POST":
        name=request.form.get("name","").strip()
        phone=request.form.get("phone","").strip()
        username=request.form.get("username","").strip().lower()
        password=request.form.get("password","").strip()
        if not name or not phone or not username or not password:
            flash("Completa todos los campos.","error")
            return render_template("admin_courier_form.html",repartidor=None)
        c=db()
        try:
            c.execute("INSERT INTO couriers(name,phone,username,password) VALUES(?,?,?,?)",(name,phone,username,generate_password_hash(password)))
            c.commit()
        except Exception as e:
            c.rollback()
            if "unique" not in str(e).lower() and "duplicate" not in str(e).lower():
                c.close()
                raise
            c.close()
            flash("Ese usuario ya existe.","error")
            return render_template("admin_courier_form.html",repartidor=None)
        c.close()
        flash("Repartidor creado.","success")
        return redirect(url_for("admin_couriers"))
    return render_template("admin_courier_form.html",repartidor=None)

@app.post("/admin/repartidores/<int:courier_id>/toggle")
@auth
def admin_courier_toggle(courier_id):
    c=db()
    row=c.execute("SELECT active FROM couriers WHERE id=?",(courier_id,)).fetchone()
    if row:
        c.execute("UPDATE couriers SET active=? WHERE id=?",(0 if row["active"] else 1,courier_id))
        c.commit()
    c.close()
    return redirect(url_for("admin_couriers"))

@app.post("/admin/pedidos/<int:order_id>/asignar")
@auth
def admin_assign_courier(order_id):
    courier_id=request.form.get("courier_id")
    c=db()
    if courier_id:
        courier=c.execute("SELECT * FROM couriers WHERE id=? AND active=1",(courier_id,)).fetchone()
        if not courier:
            c.close(); flash("Repartidor inválido.","error")
            return redirect(url_for("admin_order_detail",order_id=order_id))
        c.execute("UPDATE orders SET courier_id=? WHERE id=?",(courier_id,order_id))
        c.commit()
        flash(f"Pedido asignado a {courier['name']}.","success")
    else:
        c.execute("UPDATE orders SET courier_id=NULL WHERE id=?",(order_id,))
        c.commit()
        flash("Pedido sin repartidor asignado.","success")
    c.close()
    return redirect(url_for("admin_order_detail",order_id=order_id))

# ---------------- COURIER APP ----------------

def courier_auth(f):
    @wraps(f)
    def w(*a, **k):
        # Un administrador no puede usar las rutas de repartidor
        if session.get("admin_logged"):
            abort(403)

        if not session.get("courier_logged"):
            return redirect(url_for("courier_login", next=request.path))

        return f(*a, **k)
    return w

@app.route("/repartidor/login", methods=["GET","POST"])
def courier_login():
    if session.get("courier_logged"):
        return redirect(url_for("courier_dashboard"))

    ip = request.remote_addr or "unknown"

    if login_blocked(ip, "courier"):
        flash("Demasiados intentos fallidos. Inténtalo nuevamente en unos minutos.", "error")
        return render_template("courier_login.html"), 429

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "").strip()

        c = db()
        courier = c.execute(
            "SELECT * FROM couriers WHERE username=? AND active=1",
            (username,)
        ).fetchone()

        valid = courier is not None and check_password_hash(
            courier["password"],
            password
        )

        c.close()

        if valid:
            clear_login_failures(ip, "courier")

            session.clear()
            session["courier_logged"] = True
            session["courier_id"] = courier["id"]
            session["courier_name"] = courier["name"]

            return redirect(
                request.args.get("next") or url_for("courier_dashboard")
            )

        register_login_failure(ip, "courier")
        flash("Usuario o contraseña incorrectos.", "error")

    return render_template("courier_login.html")

@app.route("/repartidor/logout")
def courier_logout():
    session.pop("courier_logged",None)
    session.pop("courier_id",None)
    session.pop("courier_name",None)
    return redirect(url_for("courier_login"))

@app.route("/repartidor")
@courier_auth
def courier_dashboard():
    courier_id=session["courier_id"]
    c=db()
    pedidos=c.execute("""
        SELECT o.*,b.name business_name,b.address business_address,b.phone business_phone
        FROM orders o JOIN businesses b ON b.id=o.business_id
        WHERE o.courier_id=? AND o.status IN ('preparando','camino')
        ORDER BY CASE WHEN o.status='camino' THEN 0 ELSE 1 END,o.id DESC
    """,(courier_id,)).fetchall()
    history=c.execute("""
        SELECT o.*,b.name business_name
        FROM orders o JOIN businesses b ON b.id=o.business_id
        WHERE o.courier_id=? AND o.status='entregado'
        ORDER BY o.id DESC LIMIT 10
    """,(courier_id,)).fetchall()
    c.close()
    return render_template("courier_dashboard.html",pedidos=pedidos,historial=history)

@app.route("/repartidor/pedido/<int:order_id>")
@courier_auth
def courier_order_detail(order_id):
    courier_id=session["courier_id"]
    c=db()
    o=c.execute("""SELECT o.*,b.name business_name,b.address business_address,b.phone business_phone
        FROM orders o JOIN businesses b ON b.id=o.business_id
        WHERE o.id=? AND o.courier_id=?""",(order_id,courier_id)).fetchone()
    if not o:
        c.close();abort(404)
    items=c.execute("SELECT * FROM order_items WHERE order_id=?",(order_id,)).fetchall()
    c.close()
    return render_template("courier_order_detail.html",pedido=o,items=items)

@app.post("/repartidor/pedido/<int:order_id>/estado")
@courier_auth
def courier_order_status(order_id):
    courier_id=session["courier_id"]
    s=request.form.get("status","")
    if s not in {"camino","entregado"}:
        flash("Acción no válida.","error")
        return redirect(url_for("courier_order_detail",order_id=order_id))
    from datetime import datetime
    now=datetime.now(LOCAL_TZ).replace(tzinfo=None).isoformat(timespec="seconds")
    c=db()
    if s=="camino":
        cursor = c.execute(
            "UPDATE orders SET status='camino',picked_up_at=COALESCE(picked_up_at,?) WHERE id=? AND courier_id=?",
            (now,order_id,courier_id)
        )
    else:
        cursor = c.execute(
            "UPDATE orders SET status='entregado',delivered_at=COALESCE(delivered_at,?) WHERE id=? AND courier_id=?",
            (now,order_id,courier_id)
        )
    changed = cursor.rowcount
    c.commit();c.close()
    flash("Pedido actualizado." if changed else "No se pudo actualizar el pedido.","success" if changed else "error")
    return redirect(url_for("courier_dashboard"))

@app.route("/admin/configuracion",methods=["GET","POST"])
@auth
def admin_configuracion():
    c=db()
    if request.method=="POST":
        try:
            rate=float(request.form.get("commission_rate","15") or 0)
        except ValueError:
            rate=DEFAULT_COMMISSION_RATE
        rate=max(0,min(rate,100))
        enabled="1" if request.form.get("commission_enabled") else "0"
        c.execute("UPDATE platform_settings SET value=? WHERE key='commission_rate'",(str(rate),))
        c.execute("UPDATE platform_settings SET value=? WHERE key='commission_enabled'",(enabled,))
        c.commit()
        c.close()
        flash("Configuración de comisiones actualizada.","success")
        return redirect(url_for("admin_configuracion"))
    enabled, rate=platform_commission(c)
    c.close()
    return render_template("admin_settings.html",commission_enabled=enabled,commission_rate=rate)

# ---------------- SETTLEMENTS / LIQUIDATIONS ----------------

def parse_period():
    today=datetime.now(LOCAL_TZ).date()
    try:
        end=date.fromisoformat(request.args.get("to", "")) if request.args.get("to") else today
    except ValueError:
        end=today
    try:
        start=date.fromisoformat(request.args.get("from", "")) if request.args.get("from") else end-timedelta(days=6)
    except ValueError:
        start=end-timedelta(days=6)
    if start>end:
        start,end=end,start
    return start,end

@app.route("/admin/liquidaciones")
@auth
def admin_settlements():
    start,end=parse_period()
    start_ts=f"{start.isoformat()} 00:00:00"
    end_exclusive=end+timedelta(days=1)
    end_ts=f"{end_exclusive.isoformat()} 00:00:00"
    c=db()
    summary=c.execute("""
        SELECT b.id,b.name,b.category,
               COUNT(o.id) AS orders,
               COALESCE(SUM(o.subtotal),0) AS sales,
               COALESCE(SUM(o.commission_amount),0) AS commission,
               COALESCE((SELECT SUM(sp.amount) FROM settlement_payments sp WHERE sp.business_id=b.id AND sp.paid_at >= ? AND sp.paid_at < ?),0) AS paid
        FROM businesses b
        LEFT JOIN orders o ON o.business_id=b.id AND o.created_at >= ? AND o.created_at < ? AND o.status <> 'cancelado'
        GROUP BY b.id,b.name,b.category ORDER BY b.name
    """,(start_ts,end_ts,start_ts,end_ts)).fetchall()
    summary=[dict(x) for x in summary]
    for x in summary: x["pending"]=round(float(x["commission"] or 0)-float(x["paid"] or 0),2)
    total_sales=sum(float(x["sales"] or 0) for x in summary)
    total_commission=sum(float(x["commission"] or 0) for x in summary)
    total_paid=sum(float(x["paid"] or 0) for x in summary)
    total_pending=round(total_commission-total_paid,2)
    delivery=c.execute("""
        SELECT CASE WHEN delivery_fee>0 THEN 'Pedidos Locales' ELSE 'Repartidor propio' END AS method,
               COUNT(*) AS orders, COALESCE(SUM(delivery_fee),0) AS delivery_total
        FROM orders WHERE created_at >= ? AND created_at <= ? AND status <> 'cancelado'
        GROUP BY CASE WHEN delivery_fee>0 THEN 'Pedidos Locales' ELSE 'Repartidor propio' END
    """,(start_ts,end_ts)).fetchall()
    period_orders=c.execute("""
        SELECT o.id,o.created_at,o.subtotal,o.commission_amount,o.delivery_fee,b.name business_name
        FROM orders o JOIN businesses b ON b.id=o.business_id
        WHERE o.created_at >= ? AND o.created_at < ? AND o.status <> 'cancelado'
        ORDER BY o.id DESC LIMIT 100
    """,(start_ts,end_ts)).fetchall()
    payments=c.execute("""
        SELECT sp.*,b.name business_name FROM settlement_payments sp
        JOIN businesses b ON b.id=sp.business_id
        WHERE sp.paid_at >= ? AND sp.paid_at < ? ORDER BY sp.id DESC LIMIT 30
    """,(start_ts,end_ts)).fetchall()
    c.close()
    return render_template("admin_settlements.html",start=start,end=end,summary=summary,total_sales=total_sales,total_commission=total_commission,total_paid=total_paid,total_pending=total_pending,delivery=delivery,period_orders=period_orders,payments=payments)

@app.post("/admin/liquidaciones/pago")
@auth
def admin_settlement_payment():
    try: business_id=int(request.form.get("business_id")); amount=float(request.form.get("amount","0"))
    except (TypeError,ValueError):
        flash("Datos de pago inválidos.","error"); return redirect(url_for("admin_settlements"))
    if amount<=0:
        flash("El monto debe ser mayor a cero.","error"); return redirect(url_for("admin_settlements"))
    method=request.form.get("payment_method","Transferencia").strip() or "Transferencia"
    notes=request.form.get("notes","").strip()
    period_start=request.form.get("period_start") or None; period_end=request.form.get("period_end") or None
    c=db()
    business=c.execute("SELECT id FROM businesses WHERE id=?",(business_id,)).fetchone()
    if not business:
        c.close(); flash("Negocio no encontrado.","error"); return redirect(url_for("admin_settlements"))
    paid_now=datetime.now(LOCAL_TZ).replace(tzinfo=None).isoformat(timespec="seconds")
    c.execute("INSERT INTO settlement_payments(business_id,amount,paid_at,payment_method,notes,period_start,period_end) VALUES(?,?,?,?,?,?,?)",(business_id,amount,paid_now,method,notes,period_start,period_end))
    c.commit();c.close()
    flash("Pago registrado correctamente.","success")
    return redirect(url_for("admin_settlements", **({"from":period_start,"to":period_end} if period_start and period_end else {})))

# Business and product admin
@app.route("/admin/negocios/nuevo",methods=["GET","POST"])
@auth
def admin_business_new():
    if request.method=="POST":
        name=request.form.get("name","").strip();cat=request.form.get("category","").strip();desc=request.form.get("description","").strip();rating=request.form.get("rating","5") or "5";dt=request.form.get("delivery_time","20-30 min").strip();phone=request.form.get("phone","").strip();addr=request.form.get("address","").strip();featured=1 if request.form.get("featured") else 0;image=save_image(request.files.get("image"));delivery_enabled=1 if request.form.get("delivery_enabled") else 0
        try: delivery_fee=float(request.form.get("delivery_fee","35") or 0)
        except ValueError: delivery_fee=DEFAULT_DELIVERY_FEE
        try:
            latitude=float(request.form.get("latitude")) if request.form.get("latitude","").strip() else None
            longitude=float(request.form.get("longitude")) if request.form.get("longitude","").strip() else None
        except ValueError:
            latitude=longitude=None
        if not name or not cat:flash("Nombre y categoría son obligatorios.","error");return render_template("admin_business_form.html",negocio=None)
        c=db();c.execute("INSERT INTO businesses(name,category,description,rating,delivery_time,phone,address,featured,image,delivery_enabled,delivery_fee,latitude,longitude) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(name,cat,desc,float(rating),dt,phone,addr,featured,image,delivery_enabled,delivery_fee,latitude,longitude));c.commit();c.close();flash("Negocio creado.","success");return redirect(url_for("admin_dashboard"))
    return render_template("admin_business_form.html",negocio=None)

@app.route("/admin/negocios/<int:business_id>/editar",methods=["GET","POST"])
@auth
def admin_business_edit(business_id):
    c=db();n=c.execute("SELECT * FROM businesses WHERE id=?",(business_id,)).fetchone();c.close()
    if not n:abort(404)
    if request.method=="POST":
        name=request.form.get("name","").strip();cat=request.form.get("category","").strip();desc=request.form.get("description","").strip();rating=request.form.get("rating","5") or "5";dt=request.form.get("delivery_time","20-30 min").strip();phone=request.form.get("phone","").strip();addr=request.form.get("address","").strip();featured=1 if request.form.get("featured") else 0;ni=save_image(request.files.get("image"));image=ni or n["image"];delivery_enabled=1 if request.form.get("delivery_enabled") else 0
        try: delivery_fee=float(request.form.get("delivery_fee","35") or 0)
        except ValueError: delivery_fee=DEFAULT_DELIVERY_FEE
        try:
            latitude=float(request.form.get("latitude")) if request.form.get("latitude","").strip() else None
            longitude=float(request.form.get("longitude")) if request.form.get("longitude","").strip() else None
        except ValueError:
            latitude=longitude=None
        if not name or not cat:flash("Nombre y categoría son obligatorios.","error");return render_template("admin_business_form.html",negocio=n)
        c=db();c.execute("UPDATE businesses SET name=?,category=?,description=?,rating=?,delivery_time=?,phone=?,address=?,featured=?,image=?,delivery_enabled=?,delivery_fee=?,latitude=?,longitude=? WHERE id=?",(name,cat,desc,float(rating),dt,phone,addr,featured,image,delivery_enabled,delivery_fee,latitude,longitude,business_id));c.commit();c.close();flash("Negocio actualizado.","success");return redirect(url_for("admin_dashboard"))
    return render_template("admin_business_form.html",negocio=n)

@app.post("/admin/negocios/<int:business_id>/eliminar")
@auth
def admin_business_delete(business_id):
    c=db();c.execute("DELETE FROM businesses WHERE id=?",(business_id,));c.commit();c.close();flash("Negocio eliminado.","success");return redirect(url_for("admin_dashboard"))

@app.post("/admin/negocios/<int:business_id>/destacado")
@auth
def admin_business_featured(business_id):
    c=db();x=c.execute("SELECT featured FROM businesses WHERE id=?",(business_id,)).fetchone()
    if x:c.execute("UPDATE businesses SET featured=? WHERE id=?",(0 if x["featured"] else 1,business_id));c.commit()
    c.close();return redirect(url_for("admin_dashboard"))

@app.route("/admin/negocios/<int:business_id>/productos")
@auth
def admin_products(business_id):
    c=db();n=c.execute("SELECT * FROM businesses WHERE id=?",(business_id,)).fetchone();p=c.execute("SELECT * FROM products WHERE business_id=? ORDER BY active DESC,category,name",(business_id,)).fetchall();c.close()
    if not n:abort(404)
    return render_template("admin_products.html",negocio=n,productos=p)

@app.route("/admin/negocios/<int:business_id>/productos/nuevo",methods=["GET","POST"])
@auth
def admin_product_new(business_id):
    c=db();n=c.execute("SELECT * FROM businesses WHERE id=?",(business_id,)).fetchone();c.close()
    if not n:abort(404)
    if request.method=="POST":
        name=request.form.get("name","").strip();desc=request.form.get("description","").strip();price=request.form.get("price","0") or "0";cat=request.form.get("category","General").strip() or "General";image=save_image(request.files.get("image"))
        if not name:flash("El nombre del producto es obligatorio.","error");return render_template("admin_product_form.html",negocio=n,producto=None)
        c=db();c.execute("INSERT INTO products(business_id,name,description,price,category,image) VALUES(?,?,?,?,?,?)",(business_id,name,desc,float(price),cat,image));c.commit();c.close();flash("Producto creado.","success");return redirect(url_for("admin_products",business_id=business_id))
    return render_template("admin_product_form.html",negocio=n,producto=None)

@app.route("/admin/productos/<int:product_id>/editar",methods=["GET","POST"])
@auth
def admin_product_edit(product_id):
    c=db();p=c.execute("SELECT * FROM products WHERE id=?",(product_id,)).fetchone();n=c.execute("SELECT * FROM businesses WHERE id=?",(p["business_id"],)).fetchone() if p else None;c.close()
    if not p or not n:abort(404)
    if request.method=="POST":
        name=request.form.get("name","").strip();desc=request.form.get("description","").strip();price=request.form.get("price","0") or "0";cat=request.form.get("category","General").strip() or "General";active=1 if request.form.get("active") else 0;ni=save_image(request.files.get("image"));image=ni or p["image"]
        if not name:flash("El nombre del producto es obligatorio.","error");return render_template("admin_product_form.html",negocio=n,producto=p)
        c=db();c.execute("UPDATE products SET name=?,description=?,price=?,category=?,image=?,active=? WHERE id=?",(name,desc,float(price),cat,image,active,product_id));c.commit();c.close();flash("Producto actualizado.","success");return redirect(url_for("admin_products",business_id=n["id"]))
    return render_template("admin_product_form.html",negocio=n,producto=p)

@app.post("/admin/productos/<int:product_id>/eliminar")
@auth
def admin_product_delete(product_id):
    c=db();p=c.execute("SELECT business_id FROM products WHERE id=?",(product_id,)).fetchone()
    if not p:c.close();abort(404)
    c.execute("DELETE FROM products WHERE id=?",(product_id,));c.commit();c.close();flash("Producto eliminado.","success");return redirect(url_for("admin_products",business_id=p["business_id"]))

# Inicializar la base de datos también cuando Flask
# es iniciado mediante Gunicorn/Render.
init_db()

if __name__=="__main__":
    port=int(os.environ.get("PORT","5000"))
    host=os.environ.get("HOST","127.0.0.1")
    debug=os.environ.get("FLASK_DEBUG","0")=="1"
    app.run(host=host,port=port,debug=debug)
