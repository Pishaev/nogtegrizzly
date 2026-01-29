
import os
import psycopg
try:
    from psycopg_pool import ConnectionPool
except ImportError:
    # Fallback if pool package not available
    from psycopg import pool
    ConnectionPool = pool.ConnectionPool
from datetime import datetime, date

# --- Подключение к базе ---
# Get DATABASE_URL from environment variable (Railway provides this)
# Don't check at import time - check when actually using the database

# Connection pool for better performance
connection_pool = None

def get_connection():
    """Get a connection from the pool"""
    global connection_pool
    
    # Get DATABASE_URL when actually needed
    DATABASE_URL = os.environ.get("DATABASE_URL")
    
    if not DATABASE_URL:
        raise ValueError(
            "DATABASE_URL environment variable is not set. "
            "Please set it in your Railway environment variables."
        )
    
    if connection_pool is None:
        # Create connection pool using DATABASE_URL directly
        connection_pool = ConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=20
        )
    return connection_pool.getconn()

def return_connection(conn):
    """Return connection to the pool"""
    # Ensure transaction is properly closed before returning
    try:
        # Rollback any open transaction to clean state
        if conn.info.transaction_status != 0:  # Not IDLE
            conn.rollback()
    except Exception:
        # If we can't rollback, the connection might be bad
        # Try to close it instead of returning to pool
        try:
            conn.close()
            return  # Don't return bad connection to pool
        except Exception:
            pass
    try:
        connection_pool.putconn(conn)
    except Exception:
        # If we can't return to pool, close the connection
        try:
            conn.close()
        except Exception:
            pass


# --- Инициализация базы ---
def init_db():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        # Create users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                current_streak INTEGER DEFAULT 0,
                max_streak INTEGER DEFAULT 0,
                last_clean_day VARCHAR(10),
                review_time VARCHAR(5),
                timezone_offset INTEGER DEFAULT 3,
                created_at VARCHAR(50),
                name VARCHAR(100)
            )
        """)
        
        # Add timezone_offset column if it doesn't exist (for existing databases)
        cursor.execute("""
            DO $$ 
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='users' AND column_name='timezone_offset'
                ) THEN
                    ALTER TABLE users ADD COLUMN timezone_offset INTEGER DEFAULT 3;
                    UPDATE users SET timezone_offset = 3 WHERE timezone_offset IS NULL;
                END IF;
            END $$;
        """)

        # Add name column if it doesn't exist (for existing databases)
        cursor.execute("""
            DO $$ 
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='users' AND column_name='name'
                ) THEN
                    ALTER TABLE users ADD COLUMN name VARCHAR(100);
                END IF;
            END $$;
        """)

        # Add is_female column if it doesn't exist (for feminine endings in messages)
        cursor.execute("""
            DO $$ 
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='users' AND column_name='is_female'
                ) THEN
                    ALTER TABLE users ADD COLUMN is_female BOOLEAN;
                END IF;
            END $$;
        """)

        # Create events table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                datetime VARCHAR(50),
                text TEXT,
                analysis TEXT,
                analyzed INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        
        # Create index on telegram_id for faster lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);
        """)
        
        # Create index on user_id and datetime for events
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_user_datetime ON events(user_id, datetime);
        """)

        conn.commit()
    finally:
        return_connection(conn)


# --- Работа с пользователем ---
def get_user(tg_id):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE telegram_id = %s", (tg_id,))
        row = cursor.fetchone()
        if row:
            # row is a tuple: (id, telegram_id, current_streak, max_streak, last_clean_day, review_time, timezone_offset, created_at)
            return row
        return None
    finally:
        return_connection(conn)

def create_user(tg_id):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO users (telegram_id, last_clean_day, created_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (telegram_id) DO NOTHING
            """,
            (tg_id, date.today().isoformat(), datetime.now().isoformat())
        )
        conn.commit()
    finally:
        return_connection(conn)


# --- Работа с событиями ---
def add_event(user_id, text):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO events (user_id, datetime, text) VALUES (%s, %s, %s)",
            (user_id, datetime.now().isoformat(), text)
        )
        conn.commit()
    finally:
        return_connection(conn)

def get_today_events(user_id):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        today = date.today().isoformat()
        cursor.execute("""
            SELECT * FROM events
            WHERE user_id = %s AND datetime LIKE %s AND analyzed = 0
            ORDER BY datetime
        """, (user_id, f"{today}%"))
        rows = cursor.fetchall()
        # rows are already tuples
        return rows
    finally:
        return_connection(conn)

def save_analysis(event_id, analysis_text):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE events SET analysis = %s, analyzed = 1 WHERE id = %s",
            (analysis_text, event_id)
        )
        conn.commit()
    finally:
        return_connection(conn)


# --- Вечернее время для разбора ---
def set_review_time(user_id, time_str):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET review_time = %s WHERE id = %s",
            (time_str, user_id)
        )
        conn.commit()
    finally:
        return_connection(conn)

def get_users_with_review_time():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, telegram_id, review_time FROM users WHERE review_time IS NOT NULL"
        )
        rows = cursor.fetchall()
        # rows are already tuples
        return rows
    finally:
        return_connection(conn)

def get_all_users():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, telegram_id, timezone_offset FROM users")
        rows = cursor.fetchall()
        # rows are already tuples
        return rows
    finally:
        return_connection(conn)

def set_timezone(user_id, offset):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET timezone_offset = %s WHERE id = %s",
            (offset, user_id)
        )
        conn.commit()
    finally:
        return_connection(conn)

def set_user_name(user_id, name):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET name = %s WHERE id = %s",
            (name.strip()[:100], user_id)
        )
        conn.commit()
    finally:
        return_connection(conn)

def set_user_is_female(user_id, is_female):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET is_female = %s WHERE id = %s",
            (bool(is_female), user_id)
        )
        conn.commit()
    finally:
        return_connection(conn)

def get_users_with_review_time_and_tz():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, telegram_id, review_time, timezone_offset FROM users WHERE review_time IS NOT NULL"
        )
        rows = cursor.fetchall()
        # rows are already tuples
        return rows
    finally:
        return_connection(conn)
