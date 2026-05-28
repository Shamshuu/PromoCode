import os
import sqlite3

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values


load_dotenv()

SQLITE_PATH = os.environ.get("PROMO_DB_PATH", "promo_codes.db")


def normalize_database_url(raw_value: str) -> str:
    value = raw_value.strip()
    if value.startswith("psql "):
        value = value[len("psql ") :].strip()
    if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
        value = value[1:-1].strip()
    return value


DATABASE_URL = normalize_database_url(os.environ.get("DATABASE_URL", ""))


def fetch_all(sqlite_conn, table_name: str, columns: list[str]):
    cols = ", ".join(columns)
    query = f"SELECT {cols} FROM {table_name}"
    cursor = sqlite_conn.execute(query)
    return [tuple(row) for row in cursor.fetchall()]


def ensure_postgres_schema(pg_conn):
    with pg_conn, pg_conn.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_codes (
                id BIGSERIAL PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                phone_number TEXT,
                customer_ref TEXT,
                purchase_amount DOUBLE PRECISION NOT NULL,
                purchase_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL,
                discount_percent INTEGER NOT NULL,
                used_at TEXT,
                verified_by TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id BIGSERIAL PRIMARY KEY,
                action TEXT NOT NULL,
                promo_code TEXT,
                actor TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS first_order_phone_verifications (
                id BIGSERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL UNIQUE,
                verified_at TEXT NOT NULL,
                verified_by TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'verified',
                used_at TEXT,
                used_by TEXT
            )
            """
        )
        cursor.execute(
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
            )
            """
        )
        cursor.execute(
            "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS phone_number TEXT"
        )
        cursor.execute(
            "ALTER TABLE first_order_phone_verifications ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'verified'"
        )
        cursor.execute(
            "ALTER TABLE first_order_phone_verifications ADD COLUMN IF NOT EXISTS used_at TEXT"
        )
        cursor.execute(
            "ALTER TABLE first_order_phone_verifications ADD COLUMN IF NOT EXISTS used_by TEXT"
        )


def main():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required to migrate data into PostgreSQL.")

    if not os.path.exists(SQLITE_PATH):
        print(f"SQLite file not found at '{SQLITE_PATH}'. Skipping data migration.")
        return

    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = psycopg2.connect(DATABASE_URL)

    try:
        ensure_postgres_schema(pg_conn)
        with pg_conn, pg_conn.cursor() as cursor:
            promo_rows = fetch_all(
                sqlite_conn,
                "promo_codes",
                [
                    "code",
                    "phone_number",
                    "customer_ref",
                    "purchase_amount",
                    "purchase_date",
                    "created_at",
                    "expires_at",
                    "status",
                    "discount_percent",
                    "used_at",
                    "verified_by",
                ],
            )
            if promo_rows:
                execute_values(
                    cursor,
                    """
                    INSERT INTO promo_codes (
                        code, phone_number, customer_ref, purchase_amount, purchase_date,
                        created_at, expires_at, status, discount_percent, used_at, verified_by
                    ) VALUES %s
                    ON CONFLICT (code) DO NOTHING
                    """,
                    promo_rows,
                )

            audit_rows = fetch_all(
                sqlite_conn,
                "audit_logs",
                ["action", "promo_code", "actor", "details", "created_at"],
            )
            if audit_rows:
                execute_values(
                    cursor,
                    """
                    INSERT INTO audit_logs (action, promo_code, actor, details, created_at)
                    VALUES %s
                    """,
                    audit_rows,
                )

            first_order_rows = fetch_all(
                sqlite_conn,
                "first_order_phone_verifications",
                ["phone_number", "verified_at", "verified_by", "status", "used_at", "used_by"],
            )
            if first_order_rows:
                execute_values(
                    cursor,
                    """
                    INSERT INTO first_order_phone_verifications (
                        phone_number, verified_at, verified_by, status, used_at, used_by
                    ) VALUES %s
                    ON CONFLICT (phone_number) DO NOTHING
                    """,
                    first_order_rows,
                )

            reminder_rows = fetch_all(
                sqlite_conn,
                "promo_reminders",
                ["promo_code", "phone_number", "remind_on", "status", "sent_at", "last_error", "created_at"],
            )
            if reminder_rows:
                execute_values(
                    cursor,
                    """
                    INSERT INTO promo_reminders (
                        promo_code, phone_number, remind_on, status, sent_at, last_error, created_at
                    ) VALUES %s
                    """,
                    reminder_rows,
                )

        print("Migration complete.")
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    main()
