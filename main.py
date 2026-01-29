import asyncio
import re
import os
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Update
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram import BaseMiddleware

from db import (
    init_db, create_user, get_user, add_event,
    get_today_events, save_analysis, set_review_time,
    get_users_with_review_time, get_all_users, set_timezone,
    get_users_with_review_time_and_tz, get_connection, return_connection,
    set_user_name, set_user_is_female
)

moscow_tz = timezone(timedelta(hours=3))
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))  # <-- сюда вставь свой telegram id

init_db()

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
def main_keyboard(is_admin=False):
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
    """Клавиатура настроек (время разбора, часовой пояс, назад)"""
    keyboard = [
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
        "Я Ваш помощник в борьбе с привычкой грызть ногти. "
        "Я помогу Вам отслеживать моменты, когда это происходит, "
        "и разбирать причины вместе с Вами. 💙\n\n"
        "**Как я работаю:**\n\n"
        "1️⃣ 📌 Записать момент — если что-то произошло, просто запишите это\n"
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

async def send_welcome_and_next(reply_target, user, state: FSMContext, is_admin: bool):
    """Отправить приветствие и следующий шаг (время или настройки). reply_target — message или callback.message."""
    name = get_display_name(user)
    welcome_text = welcome_text_with_name(name)
    if not user[5]:  # review_time не установлен
        await reply_target.answer(
            welcome_text +
            "**Начнём настройку:**\n\n"
            "Давайте установим удобное время для вечернего разбора. "
            "Напишите время в формате ЧЧ:ММ\n"
            "Например: 21:30",
            parse_mode="Markdown",
            reply_markup=main_keyboard(is_admin)
        )
        await state.set_state(TimeState.waiting_time)
    else:
        tz_offset = get_user_timezone(user)
        tz_name = next((tz["name"] for tz in RUSSIAN_TIMEZONES.values() if tz["offset"] == tz_offset), f"UTC+{tz_offset}")
        await reply_target.answer(
            welcome_text +
            f"**Ваши настройки:**\n"
            f"⏰ Время напоминаний: {user[5]}\n"
            f"🌍 Часовой пояс: {tz_name}\n\n"
            f"Всё готово, {name}! Я буду помогать Вам каждый день. 🙌💙",
            parse_mode="Markdown",
            reply_markup=main_keyboard(is_admin)
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
            "Как Вас зовут? Напишите своё имя — так мне будет удобнее обращаться к Вам.",
            reply_markup=ReplyKeyboardMarkup(keyboard=[], resize_keyboard=True)
        )
        await state.set_state(NameState.waiting_name)
        return

    # --- Если пол не указан — спрашиваем для правильных окончаний ---
    if len(user) > 9 and user[9] is None:
        await message.answer(
            f"{name}, укажите, пожалуйста, Ваш пол:",
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
            "Давайте установим удобное время для вечернего разбора. "
            "Напишите время в формате ЧЧ:ММ\n"
            "Например: 21:30",
            parse_mode="Markdown",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID)
        )
        await state.set_state(TimeState.waiting_time)
    else:
        tz_offset = get_user_timezone(user)
        tz_name = next((tz["name"] for tz in RUSSIAN_TIMEZONES.values() if tz["offset"] == tz_offset), f"UTC+{tz_offset}")
        await message.answer(
            welcome_text +
            f"**Ваши настройки:**\n"
            f"⏰ Время напоминаний: {user[5]}\n"
            f"🌍 Часовой пояс: {tz_name}\n\n"
            f"Всё готово, {name}! Я буду помогать Вам каждый день. 🙌💙",
            parse_mode="Markdown",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID)
        )


# --- Сохранение имени ---
async def save_name(message: Message, state: FSMContext):
    name = message.text.strip() if message.text else ""
    if not name or len(name) < 2:
        await message.answer("Напишите, пожалуйста, своё имя (хотя бы 2 буквы).")
        return
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напишите /start 🙌")
        await state.clear()
        return
    set_user_name(user[0], name[:100])
    user = get_user(message.from_user.id)  # обновлённые данные
    # Если пол ещё не указан — спрашиваем
    if len(user) > 9 and user[9] is None:
        await message.answer(
            f"{get_display_name(user)}, укажите, пожалуйста, Ваш пол:",
            reply_markup=gender_keyboard()
        )
        await state.set_state(GenderState.waiting)
        return
    await state.clear()
    await send_welcome_and_next(message, user, state, message.from_user.id == ADMIN_ID)


# --- Кнопка "Начать" ---
async def start_button_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()  # убираем "часики"
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
    await message.answer(
        "Расскажите, что произошло в этот момент: 📝\n\n"
        "Опишите ситуацию, свои чувства и мысли. Это поможет лучше понять причины."
    )
    await state.set_state(PogryzState.waiting_text)

async def save_pogryz(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напишите /start 🙌")
        return

    add_event(user[0], message.text)
    name = get_display_name(user)
    await message.answer(
        f"✅ Событие записано!\n\n"
        f"Спасибо, {name}, что поделились. Вечером мы сможем разобрать это вместе. 💙",
        reply_markup=main_keyboard(message.from_user.id == ADMIN_ID)
    )
    await state.clear()


# --- /review ---
async def start_review(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напишите /start 🙌")
        return

    events = get_today_events(user[0])
    name = get_display_name(user)
    if not events:
        await message.answer(
            f"🎉 Отлично, {name}! Сегодня нет записанных моментов!\n\n"
            "Это значит, что Вы справляетесь! Продолжайте в том же духе! 💪✨"
        )
        return

    await state.update_data(events=events, index=0)
    first_event = events[0]
    event_count = len(events)
    await message.answer(
        f"Давайте разберём все сегодняшние события 📋\n\n"
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
            f"🎉 Отлично, {name}! Вы разобрали все моменты дня!\n\n"
            "Это важный шаг к пониманию себя и своих триггеров. "
            "Каждый разбор делает Вас сильнее! 💪✨\n\n"
            "Продолжайте работать над собой, у Вас всё получается! 🌟"
        )
        await state.clear()


# --- /set_time ---
async def save_time(message: Message, state: FSMContext):
    time_text = message.text.strip()
    if not re.match(r"^\d{2}:\d{2}$", time_text):
        await message.answer(
            "❌ Неверный формат времени.\n\n"
            "Пожалуйста, используйте формат ЧЧ:ММ\n"
            "Например: 21:30"
        )
        return

    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напишите /start 🙌")
        return

    set_review_time(user[0], time_text)
    name = get_display_name(user)
    # Always prompt for timezone selection after setting review time (as per user request)
    # This ensures users set their timezone during initial setup
    await message.answer(
        f"✅ Отлично, {name}! Буду напоминать Вам каждый день в {time_text} 🕰\n\n"
        "Теперь выберите свой часовой пояс, чтобы напоминания приходили в правильное время:\n\n"
        "📍 Рекомендуется Москва (UTC+3)",
        reply_markup=timezone_keyboard()
    )
    await state.set_state(TimezoneState.waiting_selection)


# --- Reminder loop ---
async def reminder_loop(bot: Bot):
    while True:
        utc_now = datetime.now(timezone.utc)
        
        # Get all users with their timezones
        all_users = get_all_users()
        for user_id, tg_id, tz_offset in all_users:
            if tz_offset is None:
                continue  # Skip users without timezone set
            
            # Calculate user's local time
            user_tz = timezone(timedelta(hours=tz_offset))
            user_local_time = utc_now.astimezone(user_tz)
            now_str = user_local_time.strftime("%H:%M")
            
            # 1:00 PM check-in notification
            if now_str == "13:00":
                keyboard = checkin_keyboard(user_id)
                try:
                    user_row = get_user(tg_id)
                    name = get_display_name(user_row) if user_row else "друг"
                    await bot.send_message(
                        tg_id,
                        f"Привет, {name}! 👋 Как дела? Как Вы себя чувствуете?",
                        reply_markup=keyboard
                    )
                except Exception:
                    pass  # Skip if user blocked bot or other error
        
        # Evening review reminders
        users = get_users_with_review_time_and_tz()
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
                            "Давайте разберём их вместе! 💙\n\n"
                            "Используйте команду /review"
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
                            "Как дела? Целостны ли Ваши ногти сейчас? 💅",
                            reply_markup=keyboard
                        )
                except Exception:
                    pass  # Skip if user blocked bot or other error
        
        await asyncio.sleep(60)



# --- Обработка выбора пола (для окончаний в сообщениях) ---
async def gender_callback_handler(callback: CallbackQuery, state: FSMContext):
    if callback.data not in ("gender_yes", "gender_no"):
        return False
    user = get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Пользователь не найден")
        return True
    set_user_is_female(user[0], callback.data == "gender_yes")
    user = get_user(callback.from_user.id)
    try:
        await callback.message.edit_reply_markup(None)
    except Exception:
        pass
    await callback.answer()
    await send_welcome_and_next(callback.message, user, state, callback.from_user.id == ADMIN_ID)
    return True

# --- Кнопки Да/Нет и сохранение текста ---
async def button_handler(callback: CallbackQuery, state: FSMContext):
    # Handle gender selection (after name)
    if await gender_callback_handler(callback, state):
        return
    # Handle timezone selection
    if callback.data.startswith("tz_"):
        tz_key = callback.data[3:]  # Remove "tz_" prefix
        if tz_key in RUSSIAN_TIMEZONES:
            user = get_user(callback.from_user.id)
            if not user:
                await callback.answer("❌ Пользователь не найден")
                return
            
            tz_info = RUSSIAN_TIMEZONES[tz_key]
            set_timezone(user[0], tz_info["offset"])
            await callback.message.edit_reply_markup(None)
            name = get_display_name(user)
            await callback.message.answer(
                f"✅ Часовой пояс установлен: {tz_info['name']} (UTC+{tz_info['offset']}) 🌍\n\n"
                f"{name}, теперь все напоминания будут приходить по Вашему местному времени!",
                reply_markup=main_keyboard(callback.from_user.id == ADMIN_ID)
            )
            await state.clear()
            await callback.answer()
        return
    
    # Handle check-in buttons (Great! / Just a little nibbling)
    if callback.data.startswith("checkin_great_"):
        user_id = int(callback.data.split("_")[2])
        await callback.message.edit_reply_markup(None)
        user = get_user(callback.from_user.id)
        name = get_display_name(user) if user else "друг"
        await callback.message.answer(
            f"Это замечательно, {name}! 🎉\n\n"
            f"Вы {praise_word(user).lower()}, продолжайте в том же духе! Вы справляетесь отлично! 💪✨\n\n"
            "Помните: каждый день без грызения — это маленькая победа! 🌟"
        )
        await callback.answer()
        return
    
    if callback.data.startswith("checkin_nibbling_"):
        user_id = int(callback.data.split("_")[2])
        await callback.message.edit_reply_markup(None)
        await callback.message.answer(
            "Понимаю, такое бывает 😔\n\n"
            "Расскажите, пожалуйста, что произошло? Что Вы чувствовали в этот момент?"
        )
        await state.set_state(CheckinNibblingState.waiting_text)
        await state.update_data(user_id=user_id)
        await callback.answer()
        return
    
    # Handle evening review buttons (Да/Нет)
    if not (callback.data.startswith("yes_") or callback.data.startswith("no_")):
        await callback.answer("❌ Неизвестная команда")
        return
    
    user_id = int(callback.data.split("_")[1])
    user = get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Пользователь не найден")
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
                f"👍 Отлично, {name}! Вы уже отметили этот день без грызения.\n\n"
                f"📊 Ваша статистика без изменений:\n"
                f"• Текущая серия: {current_streak} {'день' if current_streak == 1 else 'дней' if current_streak < 5 else 'дней'} 🔥\n"
                f"• Максимальная серия: {max_streak} {'день' if max_streak == 1 else 'дней' if max_streak < 5 else 'дней'} ⭐"
            )
            await callback.answer()
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
            f"🎉 {praise_word(user)}, {name}! Продолжайте в том же духе! 💪\n\n"
            f"📊 Ваша статистика:\n"
            f"• Текущая серия дней без грызения: {current_streak} {'день' if current_streak == 1 else 'дней' if current_streak < 5 else 'дней'} 🔥\n"
            f"• Максимальная серия: {max_streak} {'день' if max_streak == 1 else 'дней' if max_streak < 5 else 'дней'} ⭐\n\n"
            f"Вы делаете отличную работу! Каждый день — это победа! 🌟"
        )
        await callback.answer()
    else:
        await callback.message.answer(
            "Понимаю, такое бывает 😔\n\n"
            "Расскажите, пожалуйста, что произошло и что стало причиной? "
            "Опишите ситуацию и свои чувства в этот момент."
        )
        await state.set_state(CallbackState.waiting_text)
        await state.update_data(user_id=user_id)
        await callback.answer()


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
            "Это значит, что Вы справляетесь! Продолжайте в том же духе! 💪✨"
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
        "Берегите себя! Всё будет хорошо! 💙✨"
    )
    await state.clear()


async def keyboard_handler(message: Message, state: FSMContext):
    if message.text == "📌 Записать момент":
        await pogryz_start(message, state)
    elif message.text == "⚙️ Настройки":
        await message.answer(
            "⚙️ Настройки\n\nВыберите, что хотите настроить:",
            reply_markup=settings_keyboard(message.from_user.id == ADMIN_ID)
        )
    elif message.text == "◀️ Назад":
        await message.answer(
            "◀️ Назад",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID)
        )
    elif message.text == "⏰ Изменить время вечернего разбора":
        await message.answer(
            "⏰ Настройка времени вечернего разбора\n\n"
            "Напишите новое время в формате ЧЧ:ММ\n"
            "Например: 21:30",
            reply_markup=settings_keyboard(message.from_user.id == ADMIN_ID)
        )
        await state.set_state(TimeState.waiting_time)
    elif message.text == "🌍 Изменить часовой пояс":
        user = get_user(message.from_user.id)
        if not user:
            await message.answer("Напишите /start 🙌")
            return
        await message.answer(
            "🌍 Настройка часового пояса\n\n"
            "Выберите свой часовой пояс, чтобы все напоминания приходили в правильное время:\n\n"
            "📍 Рекомендуется Москва (UTC+3)",
            reply_markup=timezone_keyboard()
        )
        await state.set_state(TimezoneState.waiting_selection)
    elif message.text == "📊 Статистика бота":
        await admin_stats(message)


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
        parse_mode="Markdown"
    )



# --- Рассылка актуального меню при старте бота ---
async def broadcast_keyboard_on_startup(bot: Bot):
    """При каждом деплое отправляет всем пользователям актуальное меню."""
    try:
        users = get_all_users()
        for user_id, tg_id, _ in users:
            try:
                is_admin = tg_id == ADMIN_ID
                await bot.send_message(
                    tg_id,
                    " ",
                    reply_markup=main_keyboard(is_admin=is_admin)
                )
                await asyncio.sleep(0.05)  # Небольшая пауза, чтобы не упереться в лимиты
            except Exception:
                pass  # Пользователь заблокировал бота или другая ошибка — пропускаем
    except Exception:
        pass  # Ошибка при получении пользователей — не падаем при старте


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


# --- main ---
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

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
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())