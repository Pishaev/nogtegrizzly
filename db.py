
import sqlite3
from datetime import datetime, date

# --- Подключение к базе ---
conn = sqlite3.connect("bot.db")
cursor = conn.cursor()

# --- Инициализация базы ---
def init_db():
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            telegram_id INTEGER UNIQUE,
            current_streak INTEGER DEFAULT 0,
            max_streak INTEGER DEFAULT 0,
            last_clean_day TEXT,
            review_time TEXT,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            datetime TEXT,
            text TEXT,
            analysis TEXT,
            analyzed INTEGER DEFAULT 0
        )
    """)

    conn.commit()


# --- Работа с пользователем ---
def get_user(tg_id):
    cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_id,))
    return cursor.fetchone()

def create_user(tg_id):
    cursor.execute(
        """
        INSERT OR IGNORE INTO users (telegram_id, last_clean_day, created_at)
        VALUES (?, ?, ?)
        """,
        (tg_id, date.today().isoformat(), datetime.now().isoformat())
    )
    conn.commit()


# --- Работа с событиями ---
def add_event(user_id, text):
    cursor.execute(
        "INSERT INTO events (user_id, datetime, text) VALUES (?, ?, ?)",
        (user_id, datetime.now().isoformat(), text)
    )
    conn.commit()

def get_today_events(user_id):
    today = date.today().isoformat()
    cursor.execute("""
        SELECT * FROM events
        WHERE user_id = ? AND datetime LIKE ? AND analyzed = 0
    """, (user_id, f"{today}%"))
    return cursor.fetchall()

def save_analysis(event_id, analysis_text):
    cursor.execute(
        "UPDATE events SET analysis = ?, analyzed = 1 WHERE id = ?",
        (analysis_text, event_id)
    )
    conn.commit()


# --- Вечернее время для разбора ---
def set_review_time(user_id, time_str):
    cursor.execute(
        "UPDATE users SET review_time = ? WHERE id = ?",
        (time_str, user_id)
    )
    conn.commit()

def get_users_with_review_time():
    cursor.execute(
        "SELECT id, telegram_id, review_time FROM users WHERE review_time IS NOT NULL"
    )
    return cursor.fetchall()

def get_all_users():
    cursor.execute("SELECT id, telegram_id FROM users")
    return cursor.fetchall()
