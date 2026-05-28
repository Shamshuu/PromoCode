import os
import secrets
from datetime import datetime, timedelta
from functools import wraps

import requests
import psycopg2
from dotenv import load_dotenv
from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor


load_dotenv()

MIN_PURCHASE_AMOUNT = 2500.0
DISCOUNT_PERCENT = 15
EXPIRY_DAYS = 20
def normalize_database_url(raw_value: str) -> str:
    value = raw_value.strip()
    if value.startswith("psql "):
        value = value[len("psql ") :].strip()
    if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
        value = value[1:-1].strip()
    return value


DATABASE_URL = normalize_database_url(os.environ.get("DATABASE_URL", ""))
REMINDER_DAYS_BEFORE_EXPIRY = int(os.environ.get("PROMO_REMINDER_DAYS_BEFORE_EXPIRY", "3"))


app = Flask(__name__)
app.secret_key = os.environ.get("PROMO_APP_SECRET", "change-me-in-production")


class QueryResult:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class PostgresDB:
    def __init__(self, dsn: str):
        self._conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
        self._conn.autocommit = False

    @staticmethod
    def _to_postgres_query(query: str) -> str:
        # Keep existing sqlite-style placeholders and map them to PostgreSQL.
        return query.replace("?", "%s")

    def execute(self, query: str, params=()):
        with self._conn.cursor() as cursor:
            cursor.execute(self._to_postgres_query(query), params)
            if cursor.description:
                return QueryResult(cursor.fetchall())
            return QueryResult([])

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_db() -> PostgresDB:
    if "db" not in g:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set. Configure a PostgreSQL connection string.")
        g.db = PostgresDB(DATABASE_URL)
    return g.db


@app.teardown_appcontext
def close_db(_exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS promo_codes (
            id BIGSERIAL PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            phone_number TEXT,
            customer_ref TEXT,
            purchase_amount REAL NOT NULL,
            purchase_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL,
            discount_percent INTEGER NOT NULL,
            used_at TEXT,
            verified_by TEXT
        );
        """
    )
    # Lightweight migration for schemas created before phone_number support.
    columns = {
        row["column_name"]
        for row in db.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'promo_codes'
            """
        ).fetchall()
    }
    if "phone_number" not in columns:
        db.execute("ALTER TABLE promo_codes ADD COLUMN phone_number TEXT")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id BIGSERIAL PRIMARY KEY,
            action TEXT NOT NULL,
            promo_code TEXT,
            actor TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS first_order_phone_verifications (
            id BIGSERIAL PRIMARY KEY,
            phone_number TEXT NOT NULL UNIQUE,
            verified_at TEXT NOT NULL,
            verified_by TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'verified',
            used_at TEXT,
            used_by TEXT
        );
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS promo_reminders (
            id BIGSERIAL PRIMARY KEY,
            promo_code TEXT NOT NULL,
            phone_number TEXT NOT NULL,
            remind_on TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            sent_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    first_order_columns = {
        row["column_name"]
        for row in db.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'first_order_phone_verifications'
            """
        ).fetchall()
    }
    if "status" not in first_order_columns:
        db.execute(
            "ALTER TABLE first_order_phone_verifications ADD COLUMN status TEXT NOT NULL DEFAULT 'verified'"
        )
    if "used_at" not in first_order_columns:
        db.execute("ALTER TABLE first_order_phone_verifications ADD COLUMN used_at TEXT")
    if "used_by" not in first_order_columns:
        db.execute("ALTER TABLE first_order_phone_verifications ADD COLUMN used_by TEXT")
    db.commit()


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return func(*args, **kwargs)

    return wrapper


def admin_username() -> str:
    return os.environ.get("PROMO_ADMIN_USER", "admin")


def admin_password() -> str:
    return os.environ.get("PROMO_ADMIN_PASSWORD", "admin123")


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def normalize_phone(phone_number: str) -> str:
    return "".join(ch for ch in phone_number if ch.isdigit())


def messaging_sender() -> str:
    return os.environ.get("PROMO_MESSAGE_FROM", "").strip()


def twilio_account_sid() -> str:
    return os.environ.get("TWILIO_ACCOUNT_SID", "").strip()


def twilio_auth_token() -> str:
    return os.environ.get("TWILIO_AUTH_TOKEN", "").strip()


def messaging_configured() -> bool:
    return bool(messaging_sender() and twilio_account_sid() and twilio_auth_token())


def send_message(phone_number: str, message_body: str) -> tuple[bool, str]:
    if not messaging_configured():
        return False, "Messaging provider not configured"

    sid = twilio_account_sid()
    token = twilio_auth_token()
    sender = messaging_sender()
    to_number = phone_number if phone_number.startswith("+") else f"+{phone_number}"
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    response = requests.post(
        url,
        data={"From": sender, "To": to_number, "Body": message_body},
        auth=(sid, token),
        timeout=15,
    )
    if 200 <= response.status_code < 300:
        return True, "Message sent"
    return False, f"Message failed ({response.status_code})"


def create_unique_code() -> str:
    db = get_db()
    while True:
        raw = secrets.token_hex(4).upper()
        code = f"PROMO-{raw}"
        existing = db.execute("SELECT 1 FROM promo_codes WHERE code = ?", (code,)).fetchone()
        if existing is None:
            return code


def insert_audit(action: str, actor: str, promo_code: str = "", details: str = ""):
    db = get_db()
    db.execute(
        """
        INSERT INTO audit_logs (action, promo_code, actor, details, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (action, promo_code, actor, details, now_iso()),
    )
    db.commit()


@app.before_request
def startup():
    init_db()


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == admin_username() and password == admin_password():
            session["user"] = username
            insert_audit("login", username, details="Staff login successful")
            return redirect(url_for("dashboard"))
        flash("Invalid username or password", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    user = session.get("user", "unknown")
    insert_audit("logout", user, details="Staff logged out")
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@login_required
def dashboard():
    db = get_db()
    search_query = request.args.get("q", "").strip()
    promo_codes = db.execute(
        """
        SELECT code, customer_ref, purchase_amount, purchase_date, expires_at, status,
               discount_percent, used_at, verified_by, created_at
        FROM promo_codes
        ORDER BY id DESC
        LIMIT 20
        """
    ).fetchall()
    first_order_verifications = db.execute(
        """
        SELECT phone_number, verified_at, verified_by, status, used_at, used_by
        FROM first_order_phone_verifications
        ORDER BY id DESC
        LIMIT 20
        """
    ).fetchall()
    logs = db.execute(
        """
        SELECT action, promo_code, actor, details, created_at
        FROM audit_logs
        ORDER BY id DESC
        LIMIT 20
        """
    ).fetchall()
    reminders = db.execute(
        """
        SELECT promo_code, phone_number, remind_on, status, sent_at, last_error
        FROM promo_reminders
        ORDER BY id DESC
        LIMIT 20
        """
    ).fetchall()
    promo_search_results = []
    phone_search_results = []
    if search_query:
        promo_search_results = db.execute(
            """
            SELECT code, status, discount_percent, purchase_date, expires_at
            FROM promo_codes
            WHERE code LIKE ? OR customer_ref LIKE ? OR phone_number LIKE ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (f"%{search_query}%", f"%{search_query}%", f"%{search_query}%"),
        ).fetchall()
        phone_search_results = db.execute(
            """
            SELECT phone_number, status, verified_at, used_at, used_by
            FROM first_order_phone_verifications
            WHERE phone_number LIKE ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (f"%{search_query}%",),
        ).fetchall()
    return render_template(
        "dashboard.html",
        promo_codes=promo_codes,
        logs=logs,
        first_order_verifications=first_order_verifications,
        reminders=reminders,
        promo_search_results=promo_search_results,
        phone_search_results=phone_search_results,
        search_query=search_query,
        min_purchase=MIN_PURCHASE_AMOUNT,
        discount_percent=DISCOUNT_PERCENT,
        expiry_days=EXPIRY_DAYS,
        current_user=session.get("user", ""),
    )


@app.route("/generate", methods=["POST"])
@login_required
def generate_code():
    notification_phone_raw = request.form.get("notification_phone", "").strip()
    notification_phone = normalize_phone(notification_phone_raw)
    customer_ref = request.form.get("customer_ref", "").strip()
    amount_raw = request.form.get("purchase_amount", "").strip()
    purchase_date_raw = request.form.get("purchase_date", "").strip()
    actor = session.get("user", "unknown")

    if not amount_raw or not purchase_date_raw:
        flash("Purchase amount and purchase date are required", "error")
        return redirect(url_for("dashboard"))

    try:
        purchase_amount = float(amount_raw)
    except ValueError:
        flash("Invalid purchase amount", "error")
        return redirect(url_for("dashboard"))

    if purchase_amount < MIN_PURCHASE_AMOUNT:
        flash(f"Purchase amount must be at least {MIN_PURCHASE_AMOUNT:.2f}", "error")
        insert_audit(
            "generate_failed",
            actor,
            details=f"Amount below minimum: {purchase_amount}",
        )
        return redirect(url_for("dashboard"))

    try:
        purchase_date = datetime.strptime(purchase_date_raw, "%Y-%m-%d").date()
    except ValueError:
        flash("Purchase date format is invalid", "error")
        return redirect(url_for("dashboard"))

    expires_at = purchase_date + timedelta(days=EXPIRY_DAYS)
    code = create_unique_code()
    db = get_db()
    db.execute(
        """
        INSERT INTO promo_codes (
            code, customer_ref, purchase_amount, purchase_date, created_at, expires_at,
            status, discount_percent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            code,
            customer_ref,
            purchase_amount,
            purchase_date.isoformat(),
            now_iso(),
            expires_at.isoformat(),
            "generated",
            DISCOUNT_PERCENT,
        ),
    )
    db.commit()
    if notification_phone:
        db.execute(
            "UPDATE promo_codes SET phone_number = ? WHERE code = ?",
            (notification_phone, code),
        )
        reminder_date = expires_at - timedelta(days=REMINDER_DAYS_BEFORE_EXPIRY)
        db.execute(
            """
            INSERT INTO promo_reminders (promo_code, phone_number, remind_on, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (code, notification_phone, reminder_date.isoformat(), now_iso()),
        )
        db.commit()
    insert_audit(
        "generate_success",
        actor,
        promo_code=code,
        details=f"Generated for purchase amount {purchase_amount}",
    )
    if notification_phone:
        message = (
            f"Your promo code is {code}. You get {DISCOUNT_PERCENT}% discount. "
            f"Valid till {expires_at.isoformat()}."
        )
        sent, note = send_message(notification_phone, message)
        if sent:
            flash(f"Promo code generated and sent to {notification_phone}: {code}", "success")
            insert_audit("promo_auto_send_success", actor, promo_code=code, details=f"Phone {notification_phone}")
        else:
            flash(f"Promo code generated ({code}), but auto-send failed: {note}", "error")
            insert_audit(
                "promo_auto_send_failed",
                actor,
                promo_code=code,
                details=f"Phone {notification_phone}. {note}",
            )
        return redirect(url_for("dashboard"))
    flash(f"Promo code generated: {code}", "success")
    return redirect(url_for("dashboard"))


@app.route("/verify", methods=["POST"])
@login_required
def verify_code():
    code = request.form.get("promo_code", "").strip().upper()
    actor = session.get("user", "unknown")
    if not code:
        flash("Promo code is required for verification", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    row = db.execute("SELECT * FROM promo_codes WHERE code = ?", (code,)).fetchone()
    if row is None:
        flash("Promo code not found", "error")
        insert_audit("verify_not_found", actor, promo_code=code)
        return redirect(url_for("dashboard"))

    if row["status"] == "used":
        flash("Promo code already used (one-time only)", "error")
        insert_audit("verify_already_used", actor, promo_code=code)
        return redirect(url_for("dashboard"))

    expiry_date = datetime.strptime(row["expires_at"], "%Y-%m-%d").date()
    today = datetime.utcnow().date()
    if today > expiry_date:
        db.execute("UPDATE promo_codes SET status = 'expired' WHERE code = ?", (code,))
        db.commit()
        flash("Promo code expired", "error")
        insert_audit("verify_expired", actor, promo_code=code)
        return redirect(url_for("dashboard"))

    discount = row["discount_percent"]
    flash(f"Code is valid. Discount available: {discount}%. Click Mark Used after applying discount.", "success")
    insert_audit("verify_valid", actor, promo_code=code, details=f"Checked valid code with {discount}%")
    return redirect(url_for("dashboard"))


@app.route("/mark-used", methods=["POST"])
@login_required
def mark_used():
    code = request.form.get("promo_code", "").strip().upper()
    actor = session.get("user", "unknown")
    if not code:
        flash("Promo code is required to mark as used", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    row = db.execute("SELECT * FROM promo_codes WHERE code = ?", (code,)).fetchone()
    if row is None:
        flash("Promo code not found", "error")
        insert_audit("mark_used_not_found", actor, promo_code=code)
        return redirect(url_for("dashboard"))

    if row["status"] == "used":
        flash("Promo code already used", "error")
        insert_audit("mark_used_already_used", actor, promo_code=code)
        return redirect(url_for("dashboard"))

    expiry_date = datetime.strptime(row["expires_at"], "%Y-%m-%d").date()
    today = datetime.utcnow().date()
    if today > expiry_date:
        db.execute("UPDATE promo_codes SET status = 'expired' WHERE code = ?", (code,))
        db.commit()
        flash("Promo code expired and cannot be marked used", "error")
        insert_audit("mark_used_expired", actor, promo_code=code)
        return redirect(url_for("dashboard"))

    db.execute(
        """
        UPDATE promo_codes
        SET status = 'used', used_at = ?, verified_by = ?
        WHERE code = ?
        """,
        (now_iso(), actor, code),
    )
    db.commit()
    discount = row["discount_percent"]
    flash(f"Promo code marked as used. Applied discount: {discount}%.", "success")
    insert_audit("mark_used_success", actor, promo_code=code, details=f"Discount {discount}% applied")
    return redirect(url_for("dashboard"))


@app.route("/first-order/verify-phone", methods=["POST"])
@login_required
def verify_first_order_phone():
    phone_number = request.form.get("phone_number", "").strip()
    actor = session.get("user", "unknown")

    if not phone_number:
        flash("Phone number is required for first-order verification", "error")
        return redirect(url_for("dashboard"))

    if not phone_number.isdigit() or len(phone_number) < 10:
        flash("Enter a valid phone number (at least 10 digits)", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    existing = db.execute(
        "SELECT * FROM first_order_phone_verifications WHERE phone_number = ? LIMIT 1",
        (phone_number,),
    ).fetchone()
    if existing is not None:
        if existing["status"] == "used":
            flash("Phone already used for first-order discount", "error")
            insert_audit("first_order_phone_already_used", actor, details=f"Phone {phone_number}")
        else:
            flash("Phone already verified. Click Mark Phone Used to consume first-order discount.", "success")
            insert_audit("first_order_phone_already_verified", actor, details=f"Phone {phone_number}")
        return redirect(url_for("dashboard"))

    db.execute(
        """
        INSERT INTO first_order_phone_verifications (phone_number, verified_at, verified_by)
        VALUES (?, ?, ?)
        """,
        (phone_number, now_iso(), actor),
    )
    db.commit()
    flash("Phone verified for first-order discount.", "success")
    insert_audit("first_order_phone_verified", actor, details=f"Phone {phone_number}")
    return redirect(url_for("dashboard"))


@app.route("/first-order/mark-used", methods=["POST"])
@login_required
def mark_first_order_phone_used():
    phone_number = request.form.get("phone_number", "").strip()
    actor = session.get("user", "unknown")

    if not phone_number:
        flash("Phone number is required to mark first-order as used", "error")
        return redirect(url_for("dashboard"))

    if not phone_number.isdigit() or len(phone_number) < 10:
        flash("Enter a valid phone number (at least 10 digits)", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    row = db.execute(
        "SELECT * FROM first_order_phone_verifications WHERE phone_number = ? LIMIT 1",
        (phone_number,),
    ).fetchone()
    if row is None:
        flash("Phone not verified yet. Verify first before marking used.", "error")
        insert_audit("first_order_phone_mark_used_without_verify", actor, details=f"Phone {phone_number}")
        return redirect(url_for("dashboard"))

    if row["status"] == "used":
        flash("Phone already marked as used for first-order discount", "error")
        insert_audit("first_order_phone_mark_used_already_used", actor, details=f"Phone {phone_number}")
        return redirect(url_for("dashboard"))

    db.execute(
        """
        UPDATE first_order_phone_verifications
        SET status = 'used', used_at = ?, used_by = ?
        WHERE phone_number = ?
        """,
        (now_iso(), actor, phone_number),
    )
    db.commit()
    flash("Phone marked as used for first-order discount.", "success")
    insert_audit("first_order_phone_mark_used_success", actor, details=f"Phone {phone_number}")
    return redirect(url_for("dashboard"))


@app.route("/reminders/send-due", methods=["POST"])
@login_required
def send_due_reminders():
    actor = session.get("user", "unknown")
    db = get_db()
    today = datetime.utcnow().date().isoformat()
    due_rows = db.execute(
        """
        SELECT id, promo_code, phone_number, remind_on
        FROM promo_reminders
        WHERE status = 'pending' AND remind_on <= ?
        ORDER BY id ASC
        LIMIT 100
        """,
        (today,),
    ).fetchall()
    if not due_rows:
        flash("No due reminders right now.", "success")
        return redirect(url_for("dashboard"))

    sent_count = 0
    failed_count = 0
    for row in due_rows:
        message = f"Reminder: Your promo code {row['promo_code']} will expire soon. Use it before expiry."
        sent, note = send_message(row["phone_number"], message)
        if sent:
            db.execute(
                """
                UPDATE promo_reminders
                SET status = 'sent', sent_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (now_iso(), row["id"]),
            )
            sent_count += 1
        else:
            db.execute(
                """
                UPDATE promo_reminders
                SET status = 'failed', last_error = ?
                WHERE id = ?
                """,
                (note, row["id"]),
            )
            failed_count += 1
    db.commit()
    insert_audit(
        "send_due_reminders",
        actor,
        details=f"Sent: {sent_count}, Failed: {failed_count}",
    )
    flash(f"Reminder run complete. Sent: {sent_count}, Failed: {failed_count}", "success")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
