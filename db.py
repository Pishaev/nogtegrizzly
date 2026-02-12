
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
_db_initialized = False


def _reset_pool_on_connection_error():
    """Закрывает пул при ошибке соединения (SSL/EOF), чтобы следующие запросы создали новые соединения."""
    global connection_pool
    if connection_pool is not None:
        try:
            connection_pool.close()
        except Exception:
            pass
        connection_pool = None


def get_connection(timeout=60, _retry_after_fail=False):
    """Get a connection from the pool. _retry_after_fail — только для внутренней повторной попытки."""
    global connection_pool, _db_initialized
    
    DATABASE_URL = os.environ.get("DATABASE_URL")
    if not DATABASE_URL:
        raise ValueError(
            "DATABASE_URL environment variable is not set. "
            "Please set it in your Railway environment variables."
        )
    
    if connection_pool is None:
        connection_pool = ConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            timeout=60,
            reconnect_timeout=5,
            max_waiting=10,
            max_idle=120,   # 2 мин — меньше шанс получить «мёртвое» соединение (SSL EOF)
            max_lifetime=600,  # 10 мин
        )
    
    if not _db_initialized:
        try:
            temp_conn = connection_pool.getconn(timeout=30)
            try:
                _init_db_with_connection(temp_conn)
                _db_initialized = True
            finally:
                return_connection(temp_conn)
        except Exception:
            pass
    
    return _get_connection_checked(timeout, allow_retry=not _retry_after_fail)


def _get_connection_checked(timeout, allow_retry=True):
    """Проверка соединения (SELECT 1). При сбое — одна повторная попытка через новый пул."""
    conn = connection_pool.getconn(timeout=timeout)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        return conn
    except Exception:
        try:
            if not conn.closed:
                conn.close()
        except Exception:
            pass
        _reset_pool_on_connection_error()
        if allow_retry:
            return get_connection(timeout=timeout, _retry_after_fail=True)
        raise


def return_connection(conn):
    """Return connection to the pool. При ошибке (SSL/EOF) закрывает соединение и сбрасывает пул."""
    if conn is None:
        return
    
    try:
        if conn.closed:
            return
        if conn.info.transaction_status != 0:
            conn.rollback()
    except Exception:
        # Соединение битое (SSL error, unexpected eof) — закрываем и сбрасываем весь пул
        try:
            if not conn.closed:
                conn.close()
        except Exception:
            pass
        _reset_pool_on_connection_error()
        return
    
    try:
        if conn.closed:
            return
        connection_pool.putconn(conn)
    except Exception:
        try:
            if not conn.closed:
                conn.close()
        except Exception:
            pass
        _reset_pool_on_connection_error()

def close_pool():
    """Закрывает пул соединений при остановке приложения."""
    global connection_pool
    if connection_pool:
        try:
            connection_pool.close()
        except Exception:
            pass
        connection_pool = None


# --- Инициализация базы ---
def _init_db_with_connection(conn):
    """Внутренняя функция инициализации БД с уже полученным соединением."""
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

    # Subscription: end date (inclusive), trial used once
    cursor.execute("""
        DO $$ 
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='users' AND column_name='subscription_ends_at'
            ) THEN
                ALTER TABLE users ADD COLUMN subscription_ends_at VARCHAR(10);
            END IF;
        END $$;
    """)
    cursor.execute("""
        DO $$ 
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='users' AND column_name='trial_used'
            ) THEN
                ALTER TABLE users ADD COLUMN trial_used BOOLEAN DEFAULT FALSE;
            END IF;
        END $$;
    """)
    
    # Add last_checkin_sent_date column to track when check-in notification was sent
    cursor.execute("""
        DO $$ 
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='users' AND column_name='last_checkin_sent_date'
            ) THEN
                ALTER TABLE users ADD COLUMN last_checkin_sent_date VARCHAR(10);
            END IF;
        END $$;
    """)

    # Payments table for YooKassa: link payment_id -> user_id (webhook)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            yookassa_payment_id VARCHAR(100) UNIQUE NOT NULL,
            amount_rub INTEGER NOT NULL,
            status VARCHAR(20) DEFAULT 'pending',
            created_at VARCHAR(50) NOT NULL
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_payments_yookassa_id ON payments(yookassa_payment_id);
    """)
    cursor.execute("""
        DO $$ 
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='payments' AND column_name='telegram_message_id'
            ) THEN
                ALTER TABLE payments ADD COLUMN telegram_message_id INTEGER;
            END IF;
        END $$;
    """)
    cursor.execute("""
        DO $$ 
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='payments' AND column_name='amount_cents'
            ) THEN
                ALTER TABLE payments RENAME COLUMN amount_cents TO amount_rub;
            END IF;
        END $$;
    """)
    cursor.execute("""
        UPDATE payments SET amount_rub = amount_rub / 100 WHERE amount_rub > 1000
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

def init_db():
    """Инициализация базы данных с повторными попытками при ошибках."""
    global _db_initialized
    max_retries = 3
    retry_delay = 2  # секунды
    
    for attempt in range(max_retries):
        try:
            conn = get_connection(timeout=30)  # Уменьшаем timeout для init_db
            try:
                _init_db_with_connection(conn)
                _db_initialized = True
                return
            finally:
                return_connection(conn)
        except Exception as e:
            if attempt == max_retries - 1:
                # Последняя попытка - пробрасываем ошибку
                raise
            # Ждем перед следующей попыткой
            import time
            time.sleep(retry_delay)
            continue


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


# --- Подписка ---
def set_subscription_ends_at(user_id, date_str):
    """date_str: YYYY-MM-DD, subscription active until end of this day (inclusive)."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET subscription_ends_at = %s WHERE id = %s",
            (date_str, user_id)
        )
        conn.commit()
    finally:
        return_connection(conn)

def set_trial_used(user_id, used=True):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET trial_used = %s WHERE id = %s",
            (bool(used), user_id)
        )
        conn.commit()
    finally:
        return_connection(conn)

def get_user_by_id(user_id):
    """Get user row by internal id (for webhook)."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return cursor.fetchone()
    finally:
        return_connection(conn)


# --- Платежи YooKassa (для вебхука) ---
def create_payment(user_id, yookassa_payment_id, amount_rub):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO payments (user_id, yookassa_payment_id, amount_rub, status, created_at)
               VALUES (%s, %s, %s, 'pending', %s)""",
            (user_id, yookassa_payment_id, amount_rub, datetime.now().isoformat())
        )
        conn.commit()
    finally:
        return_connection(conn)

def get_payment_by_yookassa_id(yookassa_payment_id):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, user_id, yookassa_payment_id, status, telegram_message_id FROM payments WHERE yookassa_payment_id = %s",
            (yookassa_payment_id,)
        )
        return cursor.fetchone()
    finally:
        return_connection(conn)

def set_payment_telegram_message(yookassa_payment_id, message_id):
    """Сохранить message_id сообщения со ссылкой на оплату (чтобы удалить после успеха)."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE payments SET telegram_message_id = %s WHERE yookassa_payment_id = %s",
            (message_id, yookassa_payment_id)
        )
        conn.commit()
    finally:
        return_connection(conn)

def mark_payment_succeeded(payment_id):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE payments SET status = 'succeeded' WHERE id = %s",
            (payment_id,)
        )
        conn.commit()
    finally:
        return_connection(conn)

def get_last_checkin_sent_date(user_id):
    """Получить дату последнего отправленного check-in уведомления (YYYY-MM-DD или None)."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT last_checkin_sent_date FROM users WHERE id = %s",
            (user_id,)
        )
        row = cursor.fetchone()
        return row[0] if row and row[0] else None
    finally:
        return_connection(conn)

def set_last_checkin_sent_date(user_id, date_str):
    """Установить дату последнего отправленного check-in уведомления (YYYY-MM-DD)."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET last_checkin_sent_date = %s WHERE id = %s",
            (date_str, user_id)
        )
        conn.commit()
    finally:
        return_connection(conn)
