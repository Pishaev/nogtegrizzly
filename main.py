import asyncio
import re
import os
import signal
import hmac
import hashlib
import json
import urllib.parse
from datetime import datetime, timezone, timedelta, date
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Update
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest

from db import (
    init_db, create_user, get_user, add_event,
    get_today_events, save_analysis, set_review_time,
    get_users_with_review_time, get_all_users, set_timezone,
    get_users_with_review_time_and_tz, get_connection, return_connection,
    set_user_name, set_user_is_female,
    set_subscription_ends_at, set_trial_used, get_user_by_id,
    create_payment as db_create_payment, get_payment_by_yookassa_id, mark_payment_succeeded,
    set_payment_telegram_message
)

# Опциональный импорт close_pool (может отсутствовать в старых версиях db.py)
try:
    from db import close_pool
except ImportError:
    # Если функция еще не добавлена в db.py, создаем заглушку
    def close_pool():
        pass

# Опциональный импорт функций для отслеживания check-in уведомлений
try:
    from db import get_last_checkin_sent_date, set_last_checkin_sent_date
except ImportError:
    # Если функции еще не добавлены в db.py, создаем заглушки
    def get_last_checkin_sent_date(user_id):
        return None
    
    def set_last_checkin_sent_date(user_id, date_str):
        pass

# Опциональный импорт функций для отслеживания уведомлений об окончании подписки
try:
    from db import get_last_subscription_expiry_notified_date, set_last_subscription_expiry_notified_date
except ImportError:
    # Если функции еще не добавлены в db.py, создаем заглушки
    def get_last_subscription_expiry_notified_date(user_id):
        return None
    
    def set_last_subscription_expiry_notified_date(user_id, date_str):
        pass

moscow_tz = timezone(timedelta(hours=3))

# --- Переменные окружения (задать в Railway: Variables) ---
# Обязательные:
BOT_TOKEN = os.environ.get("BOT_TOKEN")                    # Токен бота от @BotFather
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))            # Ваш Telegram ID (число)
DATABASE_URL = os.environ.get("DATABASE_URL")               # PostgreSQL (Railway создаёт сам)

# YooKassa (подписка 199 ₽/мес): вставить Shop ID и Секретный ключ из личного кабинета ЮKassa
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID")      # Идентификатор магазина (shopId)
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY") # Секретный ключ (Secret key)

# Необязательные:
# YOOKASSA_RETURN_URL — куда вернуть пользователя после оплаты (по умолчанию https://t.me/)
# Порт для вебхука ЮKassa берётся из PORT (Railway подставляет сам) — ничего указывать не нужно

# Бесплатный режим: весь функционал доступен всем, кнопка подписки остаётся (на будущее)
FREE_MODE = True

# Инициализация БД при старте (с обработкой ошибок)
try:
    init_db()
except Exception as e:
    # Если не удалось подключиться при старте, попробуем позже при первом обращении
    print(f"Warning: Could not initialize database at startup: {e}")
    print("Database will be initialized on first use.")

# Helper function to get timezone offset from user tuple
# Handles both new schema (timezone_offset at index 6) and old schema (at index 7 if added)
def get_user_timezone(user):
    """Get timezone offset from user tuple, handling both old and new schemas"""
    if len(user) > 7:
        # New schema: timezone_offset is at index 6, created_at at 7
        return user[6] if user[6] is not None else 3
    elif len(user) > 6:
        # Old schema with added column: timezone_offset might be at index 7
        # Check both positions
        if user[6] is not None and isinstance(user[6], int):
            return user[6]
        elif len(user) > 7 and user[7] is not None and isinstance(user[7], int):
            return user[7]
    return 3  # Default to Moscow

# User tuple: id, telegram_id, current_streak, max_streak, last_clean_day, review_time, timezone_offset, created_at, name, is_female (if columns exist)
def get_user_name(user):
    """Get name from user tuple. Name at index 8 after ALTER ADD name."""
    if len(user) > 8 and user[8]:
        return user[8].strip()
    return None

def get_user_is_female(user):
    """True if user is female (for feminine endings in messages)."""
    if len(user) > 9 and user[9] is not None:
        return bool(user[9])
    return False

def get_display_name(user):
    """Имя для обращения в сообщениях: имя пользователя или «друг»."""
    name = get_user_name(user)
    return name if name else "друг"

def praise_word(user):
    """«Молодец» или «Умница» в зависимости от пола пользователя."""
    return "Умница" if get_user_is_female(user) else "Молодец"

# --- Безопасный ответ на callback (игнорирует ошибки "query is too old") ---
async def safe_callback_answer(callback: CallbackQuery, text: str = None, show_alert: bool = False):
    """Безопасно отвечает на callback, игнорируя ошибки 'query is too old'."""
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest as e:
        # Игнорируем ошибки "query is too old" - это нормально для старых callback'ов
        if "query is too old" in str(e).lower() or "query id is invalid" in str(e).lower():
            pass  # Тихо игнорируем
        else:
            raise  # Пробрасываем другие ошибки

# --- Подписка (user tuple: ... index 10 = subscription_ends_at, 11 = trial_used) ---
def get_subscription_ends_at(user):
    """Дата окончания подписки (YYYY-MM-DD) или None."""
    if len(user) > 10 and user[10]:
        return user[10]
    return None

def get_trial_used(user):
    """Использован ли пробный период."""
    if len(user) > 11 and user[11] is not None:
        return bool(user[11])
    return False

def has_active_subscription(user):
    """Подписка активна (включая пробный период): сегодня <= subscription_ends_at.
    В FREE_MODE всегда True — весь функционал доступен всем."""
    if FREE_MODE:
        return True
    end = get_subscription_ends_at(user)
    if not end:
        return False
    try:
        end_date = date.fromisoformat(end)
        return date.today() <= end_date
    except (ValueError, TypeError):
        return False

# --- FSM States ---
class PogryzState(StatesGroup):
    waiting_text = State()

class ReviewState(StatesGroup):
    waiting_analysis = State()

class TimeState(StatesGroup):
    waiting_time = State()

class CallbackState(StatesGroup):
    waiting_text = State()

class CheckinNibblingState(StatesGroup):
    waiting_text = State()

class TimezoneState(StatesGroup):
    waiting_selection = State()

class NameState(StatesGroup):
    waiting_name = State()

class GenderState(StatesGroup):
    waiting = State()


# --- Основная клавиатура ---
def main_keyboard(is_admin=False, has_subscription=True):
    """Если нет подписки и не админ — только кнопка «Подписка». Иначе полное меню."""
    if not is_admin and not has_subscription:
        keyboard = [[KeyboardButton(text="💳 Подписка")]]
    else:
        keyboard = [
            [KeyboardButton(text="📌 Записать момент")],
            [KeyboardButton(text="⚙️ Настройки")]
        ]
        if is_admin:
            keyboard.append([KeyboardButton(text="📊 Статистика бота")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False
    )


def settings_keyboard(is_admin=False):
    """Клавиатура настроек (подписка, время разбора, часовой пояс, назад)"""
    keyboard = [
        [KeyboardButton(text="💳 Подписка")],
        [KeyboardButton(text="⏰ Изменить время вечернего разбора")],
        [KeyboardButton(text="🌍 Изменить часовой пояс")],
        [KeyboardButton(text="◀️ Назад")]
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False
    )

# --- Russian timezones ---
RUSSIAN_TIMEZONES = {
    "kaliningrad": {"name": "Калининград", "offset": 2},
    "moscow": {"name": "Москва", "offset": 3},
    "samara": {"name": "Самара", "offset": 4},
    "yekaterinburg": {"name": "Екатеринбург", "offset": 5},
    "omsk": {"name": "Омск", "offset": 6},
    "krasnoyarsk": {"name": "Красноярск", "offset": 7},
    "irkutsk": {"name": "Иркутск", "offset": 8},
    "yakutsk": {"name": "Якутск", "offset": 9},
    "vladivostok": {"name": "Владивосток", "offset": 10},
    "magadan": {"name": "Магадан", "offset": 11}
}

def timezone_keyboard():
    """Create keyboard with 10 Russian timezones, Moscow first as suggested"""
    buttons = []
    # Moscow first (suggested)
    buttons.append([InlineKeyboardButton(
        text=f"📍 {RUSSIAN_TIMEZONES['moscow']['name']} (UTC+{RUSSIAN_TIMEZONES['moscow']['offset']})",
        callback_data=f"tz_moscow"
    )])
    
    # Other timezones in two columns
    other_tz = [k for k in RUSSIAN_TIMEZONES.keys() if k != "moscow"]
    for i in range(0, len(other_tz), 2):
        row = []
        row.append(InlineKeyboardButton(
            text=f"{RUSSIAN_TIMEZONES[other_tz[i]]['name']} (UTC+{RUSSIAN_TIMEZONES[other_tz[i]]['offset']})",
            callback_data=f"tz_{other_tz[i]}"
        ))
        if i + 1 < len(other_tz):
            row.append(InlineKeyboardButton(
                text=f"{RUSSIAN_TIMEZONES[other_tz[i+1]]['name']} (UTC+{RUSSIAN_TIMEZONES[other_tz[i+1]]['offset']})",
                callback_data=f"tz_{other_tz[i+1]}"
            ))
        buttons.append(row)
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)



# --- /start ---
def welcome_text_with_name(name):
    return (
        f"Привет, {name}! 👋\n\n"
        "Я твой помощник в борьбе с привычкой грызть ногти. "
        "Я помогу тебе отслеживать моменты, когда это происходит, "
        "и разбирать причины вместе с тобой. 💙\n\n"
        "**Как я работаю:**\n\n"
        "1️⃣ 📌 Записать момент — если что-то произошло, просто запиши это\n"
        "2️⃣ 🌙 Вечерний разбор — я буду напоминать вечером для анализа дня\n"
        "3️⃣ ⚙️ Настройки — время напоминаний и часовой пояс\n\n"
        "Вместе мы справимся! 💪✨\n"
    )

def gender_keyboard():
    """Клавиатура выбора пола после ввода имени (кнопки исчезают после выбора)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Женский", callback_data="gender_yes"),
            InlineKeyboardButton(text="Мужской", callback_data="gender_no")
        ]
    ])

# --- Подписка: кнопки оплаты и пробного периода ---
SUBSCRIPTION_PRICE_RUB = 199
TRIAL_DAYS = 3

def subscription_keyboard(user):
    """Клавиатура подписки: оплата 199 ₽ и пробный период (если ещё не использован)."""
    buttons = [[InlineKeyboardButton(text=f"Оформить подписку — {SUBSCRIPTION_PRICE_RUB} ₽/мес", callback_data="sub_pay")]]
    if not get_trial_used(user):
        buttons.append([InlineKeyboardButton(text="Попробовать 3 дня бесплатно", callback_data="sub_trial")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def paywall_message():
    return (
        "⏳ Подписка не активна.\n\n"
        "Чтобы пользоваться ботом (записывать моменты, вечерний разбор и напоминания), "
        "оформите подписку или начните с пробного периода."
    )

async def send_paywall(target, user, is_admin: bool):
    """target: message или callback.message. Показать оплату и/или пробный период.
    Кнопка «Попробовать бесплатно» показывается только если пробный период ещё не использован."""
    user = get_user(user[1]) or user  # свежие данные из БД (trial_used и т.д.)
    text = paywall_message()
    kb = subscription_keyboard(user)
    await target.answer(text, reply_markup=kb)

async def send_welcome_and_next(reply_target, user, state: FSMContext, is_admin: bool):
    """Отправить приветствие и следующий шаг (время или настройки). reply_target — message или callback.message."""
    name = get_display_name(user)
    welcome_text = welcome_text_with_name(name)
    if not user[5]:  # review_time не установлен
        await reply_target.answer(
            welcome_text +
            "**Начнём настройку:**\n\n"
            "Давай установим удобное время для вечернего разбора. "
            "Напиши время в формате ЧЧ:ММ\n"
            "Например: 21:30",
            parse_mode="Markdown",
            reply_markup=main_keyboard(is_admin, has_active_subscription(user))
        )
        await state.set_state(TimeState.waiting_time)
    else:
        tz_offset = get_user_timezone(user)
        tz_name = next((tz["name"] for tz in RUSSIAN_TIMEZONES.values() if tz["offset"] == tz_offset), f"UTC+{tz_offset}")
        await reply_target.answer(
            welcome_text +
            f"**Твои настройки:**\n"
            f"⏰ Время напоминаний: {user[5]}\n"
            f"🌍 Часовой пояс: {tz_name}\n\n"
            f"Всё готово, {name}! Я буду помогать тебе каждый день. 🙌💙",
            parse_mode="Markdown",
            reply_markup=main_keyboard(is_admin, has_active_subscription(user))
        )
        await state.clear()


async def start(message: Message, state: FSMContext):
    create_user(message.from_user.id)
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Что-то пошло не так. Попробуйте ещё раз /start 🙌")
        return

    name = get_user_name(user)

    # --- Если имя не указано — просим ввести ---
    if not name:
        await message.answer(
            "👋 Добро пожаловать!\n\n"
            "Как тебя зовут? Напиши своё имя — так мне будет удобнее обращаться к тебе.",
            reply_markup=ReplyKeyboardMarkup(keyboard=[], resize_keyboard=True)
        )
        await state.set_state(NameState.waiting_name)
        return

    # --- Если пол не указан — спрашиваем для правильных окончаний ---
    if len(user) > 9 and user[9] is None:
        await message.answer(
            f"{name}, укажи, пожалуйста, свой пол:",
            reply_markup=gender_keyboard()
        )
        await state.set_state(GenderState.waiting)
        return

    welcome_text = welcome_text_with_name(name)

    # --- Если review_time ещё не установлен ---
    if not user[5]:  # review_time
        await message.answer(
            welcome_text +
            "**Начнём настройку:**\n\n"
            "Давай установим удобное время для вечернего разбора. "
            "Напиши время в формате ЧЧ:ММ\n"
            "Например: 21:30",
            parse_mode="Markdown",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID, has_active_subscription(user))
        )
        await state.set_state(TimeState.waiting_time)
    else:
        tz_offset = get_user_timezone(user)
        tz_name = next((tz["name"] for tz in RUSSIAN_TIMEZONES.values() if tz["offset"] == tz_offset), f"UTC+{tz_offset}")
        await message.answer(
            welcome_text +
            f"**Твои настройки:**\n"
            f"⏰ Время напоминаний: {user[5]}\n"
            f"🌍 Часовой пояс: {tz_name}\n\n"
            f"Всё готово, {name}! Я буду помогать тебе каждый день. 🙌💙",
            parse_mode="Markdown",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID, has_active_subscription(user))
        )


# --- Сохранение имени ---
async def save_name(message: Message, state: FSMContext):
    name = message.text.strip() if message.text else ""
    if not name or len(name) < 2:
        await message.answer("Напиши, пожалуйста, своё имя (хотя бы 2 буквы).")
        return
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напиши /start 🙌")
        await state.clear()
        return
    set_user_name(user[0], name[:100])
    user = get_user(message.from_user.id)  # обновлённые данные
    # Если пол ещё не указан — спрашиваем
    if len(user) > 9 and user[9] is None:
        await message.answer(
            f"{get_display_name(user)}, укажи, пожалуйста, свой пол:",
            reply_markup=gender_keyboard()
        )
        await state.set_state(GenderState.waiting)
        return
    await state.clear()
    await send_welcome_and_next(message, user, state, message.from_user.id == ADMIN_ID)


# --- Кнопка "Начать" ---
async def start_button_handler(callback: CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)  # убираем "часики"
    await callback.message.delete()  # удаляем приветственное сообщение с кнопкой
    # Создаём fake Message для передачи в start
    fake_msg = Message(
        message_id=callback.message.message_id,
        from_user=callback.from_user,
        chat=callback.message.chat,
        date=callback.message.date,
        text="/start",
    )
    await start(fake_msg, state)

def checkin_keyboard(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Отлично! 👍", callback_data=f"checkin_great_{user_id}"),
            InlineKeyboardButton(text="Чуть-чуть погрыз 😔", callback_data=f"checkin_nibbling_{user_id}")
        ]
    ])

# --- /pogryz ---
async def pogryz_start(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напиши /start 🙌")
        return
    if message.from_user.id != ADMIN_ID and not has_active_subscription(user):
        await send_paywall(message, user, message.from_user.id == ADMIN_ID)
        await message.answer("\u200b", reply_markup=main_keyboard(False, False))
        return
    await message.answer(
        "Расскажи, что произошло в этот момент: 📝\n\n"
        "Опиши ситуацию, свои чувства и мысли. Это поможет лучше понять причины."
    )
    await state.set_state(PogryzState.waiting_text)

async def save_pogryz(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напиши /start 🙌")
        return

    add_event(user[0], message.text)
    name = get_display_name(user)
    await message.answer(
        f"✅ Событие записано!\n\n"
        f"Спасибо, {name}, что поделил{'ся' if not get_user_is_female(user) else 'ась'}. Вечером мы сможем разобрать это вместе. 💙",
        reply_markup=main_keyboard(message.from_user.id == ADMIN_ID, has_active_subscription(user))
    )
    await state.clear()


# --- /review ---
async def start_review(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напиши /start 🙌")
        return
    if message.from_user.id != ADMIN_ID and not has_active_subscription(user):
        await send_paywall(message, user, message.from_user.id == ADMIN_ID)
        await message.answer("\u200b", reply_markup=main_keyboard(False, False))
        return

    events = get_today_events(user[0])
    name = get_display_name(user)
    if not events:
        await message.answer(
            f"🎉 Отлично, {name}! Сегодня нет записанных моментов!\n\n"
            "Это значит, что ты справляешься! Продолжай в том же духе! 💪✨",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID, has_active_subscription(user))
        )
        return

    await state.update_data(events=events, index=0)
    first_event = events[0]
    event_count = len(events)
    await message.answer(
        f"Давай разберём все сегодняшние события 📋\n\n"
        f"Всего событий сегодня: {event_count}\n\n"
        f"**Событие 1 из {event_count}:**\n_{first_event[3]}_\n\n"
        "Что стало причиной? Какие чувства и мысли были в этот момент? 🤔"
    )
    await state.set_state(ReviewState.waiting_analysis)


async def save_review_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    index = data.get("index", 0)
    events = data.get("events", [])

    user = get_user(message.from_user.id)
    save_analysis(events[index][0], message.text)

    index += 1
    if index < len(events):
        await state.update_data(index=index)
        next_event = events[index]
        await message.answer(
            f"**Событие {index + 1} из {len(events)}:**\n\n"
            f"_{next_event[3]}_\n\n"
            "Что стало причиной? Какие чувства и мысли были в этот момент? 🤔"
        )
    else:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET current_streak = 0 WHERE id = %s",
                (user[0],)
            )
            conn.commit()
        finally:
            return_connection(conn)

        name = get_display_name(user)
        await message.answer(
            f"🎉 Отлично, {name}! Ты разобрал{'а' if get_user_is_female(user) else ''} все моменты дня!\n\n"
            "Это важный шаг к пониманию себя и своих триггеров. "
            "Каждый разбор делает тебя сильнее! 💪✨\n\n"
            "Продолжай работать над собой, у тебя всё получается! 🌟",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID, has_active_subscription(user))
        )
        await state.clear()


# --- /set_time ---
async def save_time(message: Message, state: FSMContext):
    time_text = message.text.strip()
    if not re.match(r"^\d{2}:\d{2}$", time_text):
        await message.answer(
            "❌ Неверный формат времени.\n\n"
            "Пожалуйста, используйте формат ЧЧ:ММ\n"
            "Например: 21:30",
            reply_markup=settings_keyboard(message.from_user.id == ADMIN_ID)
        )
        return

    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напиши /start 🙌")
        return

    set_review_time(user[0], time_text)
    name = get_display_name(user)
    # Always prompt for timezone selection after setting review time (as per user request)
    # This ensures users set their timezone during initial setup
    await message.answer(
        f"✅ Отлично, {name}! Буду напоминать тебе каждый день в {time_text} 🕰\n\n"
        "Теперь выбери свой часовой пояс, чтобы напоминания приходили в правильное время:\n\n"
        "📍 Рекомендуется Москва (UTC+3)",
        reply_markup=timezone_keyboard()
    )
    await state.set_state(TimezoneState.waiting_selection)


# --- Reminder loop ---
async def reminder_loop(bot: Bot):
    while True:
        try:
            utc_now = datetime.now(timezone.utc)
            
            # Get all users with their timezones
            try:
                all_users = get_all_users()
            except Exception as e:
                # Если ошибка соединения с БД, ждем и пробуем снова
                print(f"Database connection error in reminder_loop: {e}")
                await asyncio.sleep(60)  # Ждем минуту перед следующей попыткой
                continue
            
            today_str = date.today().isoformat()
            
            for user_id, tg_id, tz_offset in all_users:
                if tz_offset is None:
                    continue  # Skip users without timezone set
                
                # Calculate user's local time
                user_tz = timezone(timedelta(hours=tz_offset))
                user_local_time = utc_now.astimezone(user_tz)
                now_str = user_local_time.strftime("%H:%M")
                current_hour = user_local_time.hour
                current_minute = user_local_time.minute
                
                # Уведомление об окончании подписки (10:00 утра) — отключено в FREE_MODE, на будущее
                # if current_hour == 10 and current_minute == 0:
                #     user_row = get_user(tg_id)
                #     if user_row:
                #         sub_end = get_subscription_ends_at(user_row)
                #         if sub_end:
                #             try:
                #                 end_date = date.fromisoformat(sub_end)
                #                 if end_date == date.today():
                #                     last_notified = get_last_subscription_expiry_notified_date(user_id)
                #                     if last_notified != today_str:
                #                         name = get_display_name(user_row)
                #                         is_trial = get_trial_used(user_row)
                #                         trial_text = "пробный период" if is_trial else "подписка"
                #                         try:
                #                             await bot.send_message(
                #                                 tg_id,
                #                                 f"📢 {name}, сегодня заканчивается твой {trial_text}! 📅\n\n"
                #                                 "Чтобы продолжить пользоваться ботом (записывать моменты, "
                #                                 "вечерний разбор и напоминания), оформи подписку. 💙",
                #                                 reply_markup=subscription_keyboard(user_row)
                #                             )
                #                             set_last_subscription_expiry_notified_date(user_id, today_str)
                #                         except Exception:
                #                             pass
                #             except (ValueError, TypeError):
                #                 pass
                
                # 1:00 PM check-in notification (13:00-13:01)
                # Проверяем диапазон, чтобы не пропустить уведомление
                if current_hour == 13 and current_minute == 0:
                    # Проверяем, что уведомление еще не было отправлено сегодня
                    last_sent = get_last_checkin_sent_date(user_id)
                    if last_sent == today_str:
                        continue  # Уже отправлено сегодня
                    
                    # Проверяем подписку
                    user_row = get_user(tg_id)
                    if not user_row:
                        continue
                    
                    # Админы всегда получают уведомления, остальные - только с активной подпиской
                    if tg_id != ADMIN_ID and not has_active_subscription(user_row):
                        continue
                    
                    keyboard = checkin_keyboard(user_id)
                    try:
                        name = get_display_name(user_row)
                        await bot.send_message(
                            tg_id,
                            f"Привет, {name}! 👋 Как дела? Как ты себя чувствуешь?",
                            reply_markup=keyboard
                        )
                        # Отмечаем, что уведомление отправлено сегодня
                        set_last_checkin_sent_date(user_id, today_str)
                    except Exception:
                        pass  # Skip if user blocked bot or other error
            
            # Evening review reminders
            try:
                users = get_users_with_review_time_and_tz()
            except Exception as e:
                # Если ошибка соединения с БД, пропускаем вечерние напоминания в этой итерации
                print(f"Database connection error getting review users: {e}")
                users = []
            
            for user_id, tg_id, review_time, tz_offset in users:
                if tz_offset is None:
                    continue  # Skip users without timezone set
                
                # Calculate user's local time
                user_tz = timezone(timedelta(hours=tz_offset))
                user_local_time = utc_now.astimezone(user_tz)
                now_str = user_local_time.strftime("%H:%M")
                
                if review_time == now_str:
                    events = get_today_events(user_id)
                    try:
                        u = get_user(tg_id)
                        name = get_display_name(u) if u else "друг"
                        if events:
                            await bot.send_message(
                                tg_id,
                                f"🌙 Добрый вечер, {name}! Время вечернего разбора!\n\n"
                                "У Вас есть записанные события за сегодня. "
                                "Давай разберём их вместе! 💙\n\n"
                                "Используй команду /review"
                            )
                        else:
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [
                                    InlineKeyboardButton(text="✅ Да, целы", callback_data=f"yes_{user_id}"),
                                    InlineKeyboardButton(text="❌ Нет, погрыз", callback_data=f"no_{user_id}")
                                ]
                            ])
                            await bot.send_message(
                                tg_id,
                                f"🌙 Добрый вечер, {name}!\n\n"
                                "Как дела? Целостны ли твои ногти сейчас? 💅",
                                reply_markup=keyboard
                            )
                    except Exception:
                        pass  # Skip if user blocked bot or other error
            
            await asyncio.sleep(60)
        except Exception as e:
            # Обработка любых других неожиданных ошибок
            print(f"Unexpected error in reminder_loop: {e}")
            await asyncio.sleep(60)  # Ждем минуту перед следующей попыткой



# --- Обработка выбора пола (для окончаний в сообщениях) ---
async def gender_callback_handler(callback: CallbackQuery, state: FSMContext):
    if callback.data not in ("gender_yes", "gender_no"):
        return False
    user = get_user(callback.from_user.id)
    if not user:
        await safe_callback_answer(callback, "❌ Пользователь не найден")
        return True
    set_user_is_female(user[0], callback.data == "gender_yes")
    user = get_user(callback.from_user.id)
    try:
        await callback.message.edit_reply_markup(None)
    except Exception:
        pass
    await safe_callback_answer(callback)
    await send_welcome_and_next(callback.message, user, state, callback.from_user.id == ADMIN_ID)
    return True

# --- Подписка: пробный период и оплата ---
async def subscription_callback_handler(callback: CallbackQuery, state: FSMContext):
    if callback.data == "sub_trial":
        user = get_user(callback.from_user.id)
        if not user:
            await safe_callback_answer(callback, "❌ Пользователь не найден")
            return True
        if get_trial_used(user):
            await safe_callback_answer(callback, "Пробный период уже использован.", show_alert=True)
            return True
        end_date = date.today() + timedelta(days=TRIAL_DAYS)
        set_subscription_ends_at(user[0], end_date.isoformat())
        set_trial_used(user[0], True)
        user = get_user(callback.from_user.id)  # перечитать из БД после обновления подписки
        try:
            await callback.message.delete()
        except Exception:
            pass
        name = get_display_name(user)
        await callback.message.answer(
            f"✅ Пробный период активирован, {name}!\n\n"
            f"У тебя {TRIAL_DAYS} дня бесплатного доступа. "
            f"Подписка действует до {end_date.strftime('%d.%m.%Y')}.\n\n"
            "Можешь пользоваться всеми функциями бота. 💙",
            reply_markup=main_keyboard(callback.from_user.id == ADMIN_ID, has_active_subscription(user))
        )
        await safe_callback_answer(callback)
        return True
    if callback.data == "sub_pay":
        user = get_user(callback.from_user.id)
        if not user:
            await safe_callback_answer(callback, "❌ Пользователь не найден")
            return True
        if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
            await safe_callback_answer(callback, "Оплата временно недоступна. Напиши в поддержку.", show_alert=True)
            return True
        try:
            from yookassa import Configuration, Payment
            Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
            amount_rub = str(SUBSCRIPTION_PRICE_RUB)
            return_url = os.environ.get("YOOKASSA_RETURN_URL", "https://t.me/nogtegrizzly_bot")
            payment = Payment.create({
                "amount": {"value": amount_rub, "currency": "RUB"},
                "capture": True,  # списать сразу, без ручного подтверждения в личном кабинете
                "confirmation": {"type": "redirect", "return_url": return_url},
                "description": "Подписка на 1 месяц",
                "metadata": {"user_id": str(user[0])},
            })
            pay_id = payment.id
            url = payment.confirmation.confirmation_url if payment.confirmation else None
            if not url:
                await safe_callback_answer(callback, "Ошибка создания платежа.", show_alert=True)
                return True
            db_create_payment(user[0], pay_id, SUBSCRIPTION_PRICE_RUB)
            try:
                await callback.message.delete()
            except Exception:
                pass
            name = get_display_name(user)
            sent_msg = await callback.message.answer(
                f"Оплата подписки — {SUBSCRIPTION_PRICE_RUB} ₽/мес\n\n"
                f"{name}, перейдите по ссылке и оплатите:\n{url}\n\n"
                "После успешной оплаты подписка продлится автоматически. 💙",
                reply_markup=main_keyboard(callback.from_user.id == ADMIN_ID, has_active_subscription(user))
            )
            set_payment_telegram_message(pay_id, sent_msg.message_id)
        except Exception as e:
            await safe_callback_answer(callback, "Ошибка при создании платежа. Попробуйте позже.", show_alert=True)
            return True
        await safe_callback_answer(callback)
        return True
    return False

# --- Кнопки Да/Нет и сохранение текста ---
async def button_handler(callback: CallbackQuery, state: FSMContext):
    # Handle gender selection (after name)
    if await gender_callback_handler(callback, state):
        return
    # Handle subscription (trial / pay)
    if await subscription_callback_handler(callback, state):
        return
    # Handle timezone selection
    if callback.data.startswith("tz_"):
        tz_key = callback.data[3:]  # Remove "tz_" prefix
        if tz_key in RUSSIAN_TIMEZONES:
            user = get_user(callback.from_user.id)
            if not user:
                await safe_callback_answer(callback, "❌ Пользователь не найден")
                return
            
            tz_info = RUSSIAN_TIMEZONES[tz_key]
            set_timezone(user[0], tz_info["offset"])
            user = get_user(callback.from_user.id)  # обновить данные
            
            # Для новых пользователей без подписки — автоматически активируем триал
            if callback.from_user.id != ADMIN_ID and not has_active_subscription(user) and not get_trial_used(user):
                set_subscription_ends_at(user[0], (date.today() + timedelta(days=TRIAL_DAYS)).isoformat())
                set_trial_used(user[0], True)
                user = get_user(callback.from_user.id)
                trial_activated = True
            else:
                trial_activated = False
            
            await callback.message.edit_reply_markup(None)
            name = get_display_name(user)
            if trial_activated:
                end_date = date.today() + timedelta(days=TRIAL_DAYS)
                await callback.message.answer(
                    f"✅ Часовой пояс установлен: {tz_info['name']} (UTC+{tz_info['offset']}) 🌍\n\n"
                    f"🎁 Для тебя активирован пробный период {TRIAL_DAYS} дней! "
                    f"Подписка действует до {end_date.strftime('%d.%m.%Y')}.\n\n"
                    f"{name}, теперь ты можешь пользоваться всеми функциями бота! 🙌💙",
                    reply_markup=main_keyboard(callback.from_user.id == ADMIN_ID, has_active_subscription(user))
                )
            else:
                await callback.message.answer(
                    f"✅ Часовой пояс установлен: {tz_info['name']} (UTC+{tz_info['offset']}) 🌍\n\n"
                    f"{name}, теперь все напоминания будут приходить по твоему местному времени!",
                    reply_markup=main_keyboard(callback.from_user.id == ADMIN_ID, has_active_subscription(user))
                )
            await state.clear()
            await safe_callback_answer(callback)
        return

    # Handle check-in buttons (Great! / Just a little nibbling)
    if callback.data.startswith("checkin_great_"):
        user_id = int(callback.data.split("_")[2])
        await callback.message.edit_reply_markup(None)
        user = get_user(callback.from_user.id)
        name = get_display_name(user) if user else "друг"
        await callback.message.answer(
            f"Это замечательно, {name}! 🎉\n\n"
            f"Ты {praise_word(user).lower()}, продолжай в том же духе! Ты справляешься отлично! 💪✨\n\n"
            "Помни: каждый день без грызения — это маленькая победа! 🌟",
            reply_markup=main_keyboard(callback.from_user.id == ADMIN_ID, has_active_subscription(user))
        )
        await safe_callback_answer(callback)
        return

    if callback.data.startswith("checkin_nibbling_"):
        user_id = int(callback.data.split("_")[2])
        await callback.message.edit_reply_markup(None)
        u = get_user(callback.from_user.id)
        feel = "чувствовала" if (u and get_user_is_female(u)) else "чувствовал"
        await callback.message.answer(
            "Понимаю, такое бывает 😔\n\n"
            f"Расскажи, пожалуйста, что произошло? Что ты {feel} в этот момент?"
        )
        await state.set_state(CheckinNibblingState.waiting_text)
        await state.update_data(user_id=user_id)
        await safe_callback_answer(callback)
        return
    
    # Handle evening review buttons (Да/Нет)
    if not (callback.data.startswith("yes_") or callback.data.startswith("no_")):
        await safe_callback_answer(callback, "❌ Неизвестная команда")
        return
    
    user_id = int(callback.data.split("_")[1])
    user = get_user(callback.from_user.id)
    if not user:
        await safe_callback_answer(callback, "❌ Пользователь не найден")
        return
    if callback.from_user.id != ADMIN_ID and not has_active_subscription(user):
        await callback.message.edit_reply_markup(None)
        await send_paywall(callback.message, user, False)
        await callback.message.answer("\u200b", reply_markup=main_keyboard(False, False))
        await safe_callback_answer(callback)
        return

    await callback.message.edit_reply_markup(None)

    if callback.data.startswith("yes_"):
        today = datetime.now().date().isoformat()
        last_clean = user[4]  # last_clean_day — уже считали этот день?
        if last_clean == today:
            # Уже начислен +1 за сегодня (например, ответили «Да» на первом напоминании, потом сменили время)
            name = get_display_name(user)
            current_streak = user[2] or 0
            max_streak = user[3] or 0
            await callback.message.answer(
                f"👍 Отлично, {name}! Ты уже отметил{'а' if get_user_is_female(user) else ''} этот день без грызения.\n\n"
                f"📊 Твоя статистика без изменений:\n"
                f"• Текущая серия: {current_streak} {'день' if current_streak == 1 else 'дней' if current_streak < 5 else 'дней'} 🔥\n"
                f"• Максимальная серия: {max_streak} {'день' if max_streak == 1 else 'дней' if max_streak < 5 else 'дней'} ⭐",
                reply_markup=main_keyboard(callback.from_user.id == ADMIN_ID, has_active_subscription(user))
            )
            await safe_callback_answer(callback)
            return
        current_streak = (user[2] or 0) + 1
        max_streak = max(user[3] or 0, current_streak)
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET current_streak = %s, max_streak = %s, last_clean_day = %s WHERE id = %s",
                (current_streak, max_streak, today, user[0])
            )
            conn.commit()
        finally:
            return_connection(conn)
        name = get_display_name(user)
        await callback.message.answer(
            f"🎉 {praise_word(user)}, {name}! Продолжай в том же духе! 💪\n\n"
            f"📊 Твоя статистика:\n"
            f"• Текущая серия дней без грызения: {current_streak} {'день' if current_streak == 1 else 'дней' if current_streak < 5 else 'дней'} 🔥\n"
            f"• Максимальная серия: {max_streak} {'день' if max_streak == 1 else 'дней' if max_streak < 5 else 'дней'} ⭐\n\n"
            f"Ты делаешь отличную работу! Каждый день — это победа! 🌟",
            reply_markup=main_keyboard(callback.from_user.id == ADMIN_ID, has_active_subscription(user))
        )
        await safe_callback_answer(callback)
    else:
        await callback.message.answer(
            "Понимаю, такое бывает 😔\n\n"
            "Расскажи, пожалуйста, что произошло и что стало причиной? "
            "Опиши ситуацию и свои чувства в этот момент."
        )
        await state.set_state(CallbackState.waiting_text)
        await state.update_data(user_id=user_id)
        await safe_callback_answer(callback)


async def save_callback_text(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("user_id")
    add_event(user_id, message.text)
    user = get_user(message.from_user.id)
    events = get_today_events(user[0])
    await state.clear()
    name = get_display_name(user)
    if not events:
        await message.answer(
            f"🎉 Отлично, {name}! Сегодня нет записанных моментов!\n\n"
            "Это значит, что ты справляешься! Продолжай в том же духе! 💪✨",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID, has_active_subscription(user))
        )
        return
    await state.update_data(events=events, index=0)
    first_event = events[0]
    event_count = len(events)
    await message.answer(
        f"Давайте разберём сегодняшние события 📋\n\n"
        f"Всего событий: {event_count}\n\n"
        f"**Событие 1 из {event_count}:**\n_{first_event[3]}_\n\n"
        "Что стало причиной? Какие чувства и мысли были в этот момент? 🤔"
    )
    await state.set_state(ReviewState.waiting_analysis)


async def save_checkin_nibbling(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("user_id")
    user = get_user(message.from_user.id)
    if not user or user[0] != user_id:
        await message.answer(
            "❌ Произошла ошибка. Пожалуйста, попробуй ещё раз или напиши /start"
        )
        await state.clear()
        return
    # Log the message for evening review
    add_event(user_id, f"[Дневной чек-ин] {message.text}")
    name = get_display_name(user)
    await message.answer(
        f"Спасибо, {name}, что поделились! 🙏\n\n"
        "Я сохранил это для вечернего разбора. Вечером мы сможем разобрать, что произошло и почему.\n\n"
        "Береги себя! Всё будет хорошо! 💙✨",
        reply_markup=main_keyboard(message.from_user.id == ADMIN_ID, has_active_subscription(user))
    )
    await state.clear()


async def keyboard_handler(message: Message, state: FSMContext):
    if message.text == "📌 Записать момент":
        user = get_user(message.from_user.id)
        if not user:
            await message.answer("Напиши /start 🙌")
            return
        if message.from_user.id != ADMIN_ID and not has_active_subscription(user):
            await send_paywall(message, user, message.from_user.id == ADMIN_ID)
            await message.answer("\u200b", reply_markup=main_keyboard(False, False))
            return
        await pogryz_start(message, state)
    elif message.text == "💳 Подписка":
        user = get_user(message.from_user.id)
        if not user:
            await message.answer("Напиши /start 🙌")
            return
        if has_active_subscription(user):
            end_str = get_subscription_ends_at(user)
            try:
                end_date = date.fromisoformat(end_str)
                end_fmt = end_date.strftime("%d.%m.%Y")
            except (ValueError, TypeError):
                end_fmt = end_str or "—"
            await message.answer(
                f"✅ Подписка активна до {end_fmt}.\n\n"
                "Можете продлить в любой момент:",
                reply_markup=subscription_keyboard(user)
            )
        else:
            await send_paywall(message, user, message.from_user.id == ADMIN_ID)
            await message.answer("\u200b", reply_markup=main_keyboard(message.from_user.id == ADMIN_ID, False))
    elif message.text == "⚙️ Настройки":
        user = get_user(message.from_user.id)
        if user and message.from_user.id != ADMIN_ID and not has_active_subscription(user):
            await send_paywall(message, user, False)
            await message.answer("\u200b", reply_markup=main_keyboard(False, False))
            return
        await message.answer(
            "⚙️ Настройки\n\nВыберите, что хотите настроить:",
            reply_markup=settings_keyboard(message.from_user.id == ADMIN_ID)
        )
    elif message.text == "◀️ Назад":
        user = get_user(message.from_user.id)
        has_sub = has_active_subscription(user) if user else True
        await message.answer(
            "◀️ Назад",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID, has_sub)
        )
    elif message.text == "⏰ Изменить время вечернего разбора":
        await message.answer(
            "⏰ Настройка времени вечернего разбора\n\n"
            "Напиши новое время в формате ЧЧ:ММ\n"
            "Например: 21:30",
            reply_markup=settings_keyboard(message.from_user.id == ADMIN_ID)
        )
        await state.set_state(TimeState.waiting_time)
    elif message.text == "🌍 Изменить часовой пояс":
        user = get_user(message.from_user.id)
        if not user:
            await message.answer("Напиши /start 🙌")
            return
        await message.answer(
            "🌍 Настройка часового пояса\n\n"
            "Выбери свой часовой пояс, чтобы все напоминания приходили в правильное время:\n\n"
            "📍 Рекомендуется Москва (UTC+3)",
            reply_markup=timezone_keyboard()
        )
        await state.set_state(TimezoneState.waiting_selection)
    elif message.text == "📊 Статистика бота":
        await admin_stats(message)
    else:
        # Любое другое сообщение — подсказка: сначала кнопка, потом текст
        user = get_user(message.from_user.id)
        has_sub = has_active_subscription(user) if user else True
        await message.answer(
            "Чтобы записать момент, сначала нажми кнопку «📌 Записать момент», а затем напиши, что произошло.",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID, has_sub)
        )


async def admin_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    conn = get_connection()
    try:
        cur = conn.cursor()

        # Всего пользователей
        cur.execute("SELECT COUNT(*) FROM users")
        users_count = cur.fetchone()[0]

        # Новые сегодня
        today = datetime.now().date().isoformat()
        cur.execute(
            "SELECT COUNT(*) FROM users WHERE created_at LIKE %s",
            (f"{today}%",)
        )
        new_today = cur.fetchone()[0]

        # Всего событий
        cur.execute("SELECT COUNT(*) FROM events")
        events_count = cur.fetchone()[0]

        # Активные сегодня
        cur.execute("""
            SELECT COUNT(DISTINCT user_id)
            FROM events
            WHERE datetime LIKE %s
        """, (f"{today}%",))
        active_today = cur.fetchone()[0]
    finally:
        return_connection(conn)

    await message.answer(
        "📊 *Статистика бота*\n\n"
        f"👤 Всего пользователей: {users_count}\n"
        f"🆕 Новых сегодня: {new_today}\n"
        f"📝 Всего событий: {events_count}\n"
        f"🔥 Активных сегодня: {active_today}",
        parse_mode="Markdown",
        reply_markup=main_keyboard(is_admin=True)
    )



# --- Рассылка актуального меню при старте бота ---
async def broadcast_keyboard_on_startup(bot: Bot):
    """При каждом деплое отправляет всем пользователям актуальное меню."""
    try:
        users = get_all_users()
        for user_id, tg_id, _ in users:
            try:
                is_admin = tg_id == ADMIN_ID
                user_row = get_user(tg_id)
                has_sub = has_active_subscription(user_row) if user_row else False
                await bot.send_message(
                    tg_id,
                    " ",
                    reply_markup=main_keyboard(is_admin=is_admin, has_subscription=has_sub)
                )
                await asyncio.sleep(0.05)  # Небольшая пауза, чтобы не упереться в лимиты
            except Exception:
                pass  # Пользователь заблокировал бота или другая ошибка — пропускаем
    except Exception:
        pass  # Ошибка при получении пользователей — не падаем при старте


# --- YooKassa webhook (подписка после оплаты) ---
BOT_FOR_WEBHOOK = None  # устанавливается в main() для отправки сообщения пользователю

async def yookassa_webhook(request):
    """Обработчик POST от YooKassa: payment.succeeded -> продлить подписку."""
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Bad JSON")
    event = body.get("event")
    obj = body.get("object") or {}
    payment_id_yookassa = obj.get("id")
    if event != "payment.succeeded" or not payment_id_yookassa:
        return web.Response(status=200, text="OK")
    row = get_payment_by_yookassa_id(payment_id_yookassa)
    if not row:
        return web.Response(status=200, text="OK")
    our_id, user_id, _, status, telegram_message_id = row
    if status == "succeeded":
        return web.Response(status=200, text="OK")
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        return web.Response(status=200, text="OK")
    try:
        from yookassa import Configuration, Payment
        Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
        pay = Payment.find_one(payment_id_yookassa)
        if not pay or str(getattr(pay, "status", "")) != "succeeded":
            return web.Response(status=200, text="OK")
    except Exception:
        return web.Response(status=200, text="OK")
    mark_payment_succeeded(our_id)
    user_row = get_user_by_id(user_id)
    if not user_row:
        return web.Response(status=200, text="OK")
    today = date.today()
    end_str = get_subscription_ends_at(user_row) if len(user_row) > 10 else None
    if end_str:
        try:
            end_date = date.fromisoformat(end_str)
            start = end_date if end_date >= today else today
        except (ValueError, TypeError):
            start = today
    else:
        start = today
    new_end = start + timedelta(days=30)
    set_subscription_ends_at(user_id, new_end.isoformat())
    telegram_id = user_row[1]
    if BOT_FOR_WEBHOOK:
        try:
            if telegram_message_id:
                try:
                    await BOT_FOR_WEBHOOK.delete_message(chat_id=telegram_id, message_id=telegram_message_id)
                except Exception:
                    pass
            name = get_display_name(user_row)
            await BOT_FOR_WEBHOOK.send_message(
                telegram_id,
                f"✅ Оплата прошла успешно, {name}!\n\n"
                f"Подписка продлена до {new_end.strftime('%d.%m.%Y')}. Спасибо! 💙"
            )
        except Exception:
            pass
    return web.Response(status=200, text="OK")


# --- Защита от дублирования сообщений (один update обрабатываем один раз) ---
PROCESSED_UPDATE_IDS = set()
MAX_PROCESSED_IDS = 5000


class DeduplicationMiddleware(BaseMiddleware):
    """Пропускает уже обработанные update_id — убирает двойные ответы."""

    async def __call__(self, handler, event, data):
        if isinstance(event, Update):
            uid = event.update_id
            if uid in PROCESSED_UPDATE_IDS:
                return  # Уже обрабатывали — не отвечаем повторно
            PROCESSED_UPDATE_IDS.add(uid)
            if len(PROCESSED_UPDATE_IDS) > MAX_PROCESSED_IDS:
                PROCESSED_UPDATE_IDS.clear()
        return await handler(event, data)


# --- Проверка авторизации Telegram Web App ---
def verify_telegram_webapp_data(init_data: str) -> dict:
    """Проверяет и парсит initData от Telegram Web App."""
    try:
        # Парсим initData
        parsed_data = urllib.parse.parse_qs(init_data)
        
        # Извлекаем hash
        received_hash = parsed_data.get('hash', [None])[0]
        if not received_hash:
            return None
        
        # Удаляем hash из данных для проверки
        data_check_string = []
        for key in sorted(parsed_data.keys()):
            if key != 'hash':
                data_check_string.append(f"{key}={parsed_data[key][0]}")
        
        data_check_string = '\n'.join(data_check_string)
        
        # Вычисляем секретный ключ
        secret_key = hmac.new(
            "WebAppData".encode(),
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()
        
        # Вычисляем hash
        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()
        
        # Проверяем hash
        if calculated_hash != received_hash:
            return None
        
        # Извлекаем user данные
        user_str = parsed_data.get('user', [None])[0]
        if not user_str:
            return None
        
        user_data = json.loads(user_str)
        return user_data
    except Exception as e:
        print(f"Ошибка проверки initData: {e}")
        return None


# --- API endpoints для мини-приложения ---
async def api_user_handler(request):
    """API endpoint для получения данных пользователя."""
    print("API /api/user запрос получен")
    try:
        data = await request.json()
        init_data = data.get('initData', '')
        
        if not init_data:
            print("API /api/user: нет initData")
            return web.Response(status=401, text=json.dumps({"error": "No initData"}))
        
        # Проверяем авторизацию
        user_data = verify_telegram_webapp_data(init_data)
        if not user_data:
            print("API /api/user: неверный initData")
            return web.Response(status=401, text=json.dumps({"error": "Invalid auth"}))
        
        telegram_id = user_data.get('id')
        if not telegram_id:
            return web.Response(status=401, text=json.dumps({"error": "No user ID"}))
        
        # Получаем данные пользователя из БД
        user = get_user(telegram_id)
        if not user:
            print(f"API /api/user: пользователь не найден telegram_id={telegram_id}")
            return web.Response(status=404, text=json.dumps({"error": "User not found"}))
        
        # Формируем ответ (created_at — дата регистрации в боте, для календаря)
        created_at = user[7] if len(user) > 7 and user[7] else None
        response_data = {
            "name": get_user_name(user) or "друг",
            "current_streak": user[2] or 0,
            "max_streak": user[3] or 0,
            "created_at": created_at[:10] if created_at and len(created_at) >= 10 else None,
        }
        
        print(f"API /api/user: OK telegram_id={telegram_id}")
        return web.Response(
            status=200,
            text=json.dumps(response_data),
            content_type='application/json'
        )
    except Exception as e:
        print(f"Ошибка API user: {e}")
        return web.Response(status=500, text=json.dumps({"error": str(e)}))


async def api_events_handler(request):
    """API endpoint для получения событий и данных для графика."""
    try:
        data = await request.json()
        init_data = data.get('initData', '')
        
        if not init_data:
            return web.Response(status=401, text=json.dumps({"error": "No initData"}))
        
        # Проверяем авторизацию
        user_data = verify_telegram_webapp_data(init_data)
        if not user_data:
            return web.Response(status=401, text=json.dumps({"error": "Invalid auth"}))
        
        telegram_id = user_data.get('id')
        if not telegram_id:
            return web.Response(status=401, text=json.dumps({"error": "No user ID"}))
        
        # Получаем данные пользователя из БД
        user = get_user(telegram_id)
        if not user:
            return web.Response(status=404, text=json.dumps({"error": "User not found"}))
        
        # Получаем все события пользователя
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT datetime, text FROM events
                WHERE user_id = %s
                ORDER BY datetime DESC
                LIMIT 100
            """, (user[0],))
            events = cur.fetchall()
        finally:
            return_connection(conn)
        
        # Формируем данные для графика (последние 30 дней)
        chart_data = []
        today = date.today()
        for i in range(30):
            day = today - timedelta(days=29 - i)
            day_str = day.isoformat()
            
            # Подсчитываем события за этот день
            count = sum(1 for event in events if event[0] and event[0].startswith(day_str))
            chart_data.append({
                "date": day_str,
                "value": count
            })
        
        # События хранятся в UTC (сервер). Отдаём datetime с суффиксом Z,
        # чтобы в браузере new Date() парсил как UTC и getHours() давал локальный час.
        def as_utc_iso(dt_str):
            if not dt_str:
                return dt_str
            s = (dt_str.strip() or "").rstrip("Zz")
            if not s:
                return dt_str
            # Уже указана таймзона (например +03:00 или -05:00)
            if "+" in s[-6:] or (len(s) >= 6 and s[-6] in "+-" and ":" in s[-3:]):
                return dt_str
            return s + "Z"

        response_data = {
            "events": [{"datetime": as_utc_iso(e[0]), "text": e[1]} for e in events],
            "chartData": chart_data
        }
        
        return web.Response(
            status=200,
            text=json.dumps(response_data),
            content_type='application/json'
        )
    except Exception as e:
        print(f"Ошибка API events: {e}")
        return web.Response(status=500, text=json.dumps({"error": str(e)}))


# --- CORS для мини-приложения (запросы с Vercel на Railway) ---
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        return web.Response(
            status=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "86400",
            },
        )
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


# --- Webhook-сервер для ЮKassa ---
# После оплаты ЮKassa шлёт запрос на наш сервер — подписка продлевается автоматически.
# В личном кабинете ЮKassa: Настройки → HTTP-уведомления → URL: https://ВАШ-ДОМЕН.railway.app/webhook/yookassa
async def start_webhook_server(port: int):
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_post("/webhook/yookassa", yookassa_webhook)
    app.router.add_post("/api/user", api_user_handler)
    app.router.add_post("/api/events", api_events_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    try:
        while True:
            await asyncio.sleep(86400)  # 1 день — задача может быть отменена при остановке бота
    except asyncio.CancelledError:
        pass


# --- main ---
async def main():
    global BOT_FOR_WEBHOOK
    bot = Bot(token=BOT_TOKEN)
    BOT_FOR_WEBHOOK = bot
    dp = Dispatcher(storage=MemoryStorage())

    # Webhook для ЮKassa: слушаем на PORT (Railway подставляет сам) или 8080 локально
    port = os.environ.get("PORT") or os.environ.get("WEBHOOK_PORT") or "8080"
    try:
        asyncio.create_task(start_webhook_server(int(port)))
    except Exception:
        pass

    # Сначала ставим защиту от дублей
    dp.update.outer_middleware(DeduplicationMiddleware())

    dp.message.register(start, Command("start"))
    dp.message.register(pogryz_start, Command("pogryz"))
    dp.message.register(save_pogryz, PogryzState.waiting_text)
    dp.message.register(start_review, Command("review"))
    dp.message.register(save_review_answer, ReviewState.waiting_analysis)
    dp.message.register(save_name, NameState.waiting_name)
    dp.message.register(save_time, TimeState.waiting_time)
    dp.message.register(save_callback_text, CallbackState.waiting_text)
    dp.message.register(save_checkin_nibbling, CheckinNibblingState.waiting_text)

    dp.callback_query.register(button_handler)
    dp.callback_query.register(start_button_handler, lambda c: c.data == "start_bot")

    dp.message.register(keyboard_handler)

    # При старте отправляем всем пользователям актуальное меню (после деплоя не нужен /start)
    asyncio.create_task(broadcast_keyboard_on_startup(bot))

    asyncio.create_task(reminder_loop(bot))
    
    try:
        await dp.start_polling(bot)
    finally:
        # Корректно закрываем пул соединений при остановке
        try:
            close_pool()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Обработка Ctrl+C
        try:
            close_pool()
        except Exception:
            pass
    except Exception:
        # Обработка других исключений
        try:
            close_pool()
        except Exception:
            pass
        raise