import asyncio
import re
import os
from datetime import datetime
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from datetime import datetime, timezone, timedelta

from db import (
    init_db, create_user, get_user, add_event,
    get_today_events, save_analysis, set_review_time,
    get_users_with_review_time, conn
)

moscow_tz = timezone(timedelta(hours=3))
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))  # <-- сюда вставь свой telegram id

init_db()

# --- FSM States ---
class PogryzState(StatesGroup):
    waiting_text = State()

class ReviewState(StatesGroup):
    waiting_analysis = State()

class TimeState(StatesGroup):
    waiting_time = State()

class CallbackState(StatesGroup):
    waiting_text = State()


# --- Основная клавиатура ---
def main_keyboard(is_admin=False):
    keyboard = [
        [KeyboardButton(text="📌 Записать момент")],
        [KeyboardButton(text="⏰ Изменить время вечернего разбора")]
    ]

    if is_admin:
        keyboard.append([KeyboardButton(text="📊 Статистика бота")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False
    )



# --- /start ---
async def start(message: Message, state: FSMContext):
    create_user(message.from_user.id)
    user = get_user(message.from_user.id)

    welcome_text = (
        "Привет! 👋\n\n"
        "Я бот, который помогает следить за привычкой грызть ногти и разбирать причины, когда это происходит.\n\n"
        "Вот как со мной работать:\n\n"
        "1️⃣ Записать момент — нажми кнопку 📌 или используй команду /pogryz\n"
        "2️⃣ Вечерний разбор — я буду напоминать вечером, целостны ли ногти\n"
        "3️⃣ Статистика — покажу текущую и максимальную серии дней без грызения\n"
        "4️⃣ Время вечернего разбора — настроим удобное время 🕰\n"
    )

    # --- Если пользователь впервые, показываем кнопку Начать ---
    if not user:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Начать", callback_data="start_bot")]
        ])
        await message.answer(
            "Добро пожаловать! Нажми кнопку ниже, чтобы начать:",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID)
        )
        return

    # --- Если review_time ещё не установлен ---
    if not user[5]:  # review_time
        await message.answer(
            welcome_text +
            "Давай установим удобное время для вечернего разбора. Напиши в формате ЧЧ:ММ, например 21:30",
            parse_mode="Markdown",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID)
        )
        await state.set_state(TimeState.waiting_time)
    else:
        await message.answer(
            welcome_text +
            f"Напоминания настроены на {user[5]} 🕰\n\nЯ готов помочь отслеживать твою привычку 🙌",
            parse_mode="Markdown",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID)
        )


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


# --- /pogryz ---
async def pogryz_start(message: Message, state: FSMContext):
    await message.answer("Опиши, что случилось в этот момент:")
    await state.set_state(PogryzState.waiting_text)

async def save_pogryz(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напиши /start 🙌")
        return

    add_event(user[0], message.text)
    await message.answer("Событие записано ✅", reply_markup=main_keyboard(message.from_user.id == ADMIN_ID))
    await state.clear()


# --- /review ---
async def start_review(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напиши /start 🙌")
        return

    events = get_today_events(user[0])
    if not events:
        await message.answer("Сегодня нет записанных моментов. Это хороший знак 💪")
        return

    await state.update_data(events=events, index=0)
    first_event = events[0]
    await message.answer(
        f"Давай разберём все сегодняшние события:\n\n_{first_event[3]}_\n\n"
        "Что стало причиной? Какие чувства были?"
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
            f"Следующий момент:\n\n_{next_event[3]}_\n\nЧто стало причиной? Какие чувства были?"
        )
    else:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET current_streak = 0 WHERE id = ?",
            (user[0],)
        )
        conn.commit()

        await message.answer("Отлично! Ты разобрал все моменты дня 🙌")
        await state.clear()


# --- /set_time ---
async def save_time(message: Message, state: FSMContext):
    time_text = message.text.strip()
    if not re.match(r"^\d{2}:\d{2}$", time_text):
        await message.answer("Формат неверный. Пример: 21:30")
        return

    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напиши /start 🙌")
        return

    set_review_time(user[0], time_text)
    await message.answer(f"Отлично! Буду напоминать каждый день в {time_text} 🕰")
    await state.clear()


# --- Reminder loop ---
async def reminder_loop(bot: Bot):
    while True:
        now = datetime.now(moscow_tz)
        now_str = now.strftime("%H:%M")
        users = get_users_with_review_time()
        for user_id, tg_id, review_time in users:
            if review_time == now_str:
                events = get_today_events(user_id)
                if events:
                    await bot.send_message(
                        tg_id,
                        "Время вечернего разбора! Давай разберём все события /review"
                    )
                else:
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(text="Да", callback_data=f"yes_{user_id}"),
                            InlineKeyboardButton(text="Нет", callback_data=f"no_{user_id}")
                        ]
                    ])
                    await bot.send_message(
                        tg_id,
                        "Целостны ли твои ногти сейчас?",
                        reply_markup=keyboard
                    )
        await asyncio.sleep(60)



# --- Кнопки Да/Нет и сохранение текста ---
async def button_handler(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[1])
    user = get_user(callback.from_user.id)
    await callback.message.edit_reply_markup(None)

    if callback.data.startswith("yes_"):
        current_streak = (user[2] or 0) + 1
        max_streak = max(user[3] or 0, current_streak)
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET current_streak = ?, max_streak = ?, last_clean_day = ? WHERE id = ?",
            (current_streak, max_streak, datetime.now().date().isoformat(), user[0])
        )
        conn.commit()
        await callback.message.answer(
            f"Молодец! Продолжай в том же духе 💪\n\n"
            f"Текущая серия дней без грызения: {current_streak}\n"
            f"Максимальная серия: {max_streak}"
        )
        await callback.answer()
    else:
        await callback.message.answer("Опиши, что произошло и что стало причиной:")
        await state.set_state(CallbackState.waiting_text)
        await state.update_data(user_id=user_id)
        await callback.answer()


async def save_callback_text(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("user_id")
    add_event(user_id, message.text)
    await state.clear()
    user = get_user(message.from_user.id)
    events = get_today_events(user[0])
    if not events:
        await message.answer("Сегодня нет записанных моментов. Это хороший знак 💪")
        return
    await state.update_data(events=events, index=0)
    first_event = events[0]
    await message.answer(
        f"Давай разберём сегодняшний момент:\n\n_{first_event[3]}_\n\n"
        "Что стало причиной? Какие чувства были?"
    )
    await state.set_state(ReviewState.waiting_analysis)


async def keyboard_handler(message: Message, state: FSMContext):
    if message.text == "📌 Записать момент":
        await pogryz_start(message, state)
    elif message.text == "⏰ Изменить время вечернего разбора":
        await message.answer(
            "Напиши новое время в формате ЧЧ:ММ, например 21:30",
        )
        await state.set_state(TimeState.waiting_time)
    elif message.text == "📊 Статистика бота":
        await admin_stats(message)


async def admin_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM users")
    users_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM events")
    events_count = cur.fetchone()[0]

    today = datetime.now().date().isoformat()
    cur.execute("""
        SELECT COUNT(DISTINCT user_id)
        FROM events
        WHERE datetime LIKE ?
    """, (f"{today}%",))
    active_today = cur.fetchone()[0]

    await message.answer(
        "📊 *Статистика бота*\n\n"
        f"👤 Пользователей: {users_count}\n"
        f"📝 Всего событий: {events_count}\n"
        f"🔥 Активных сегодня: {active_today}",
        parse_mode="Markdown"
    )


# --- main ---
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.register(start, Command("start"))
    dp.message.register(pogryz_start, Command("pogryz"))
    dp.message.register(save_pogryz, PogryzState.waiting_text)
    dp.message.register(start_review, Command("review"))
    dp.message.register(save_review_answer, ReviewState.waiting_analysis)
    dp.message.register(save_time, TimeState.waiting_time)
    dp.message.register(save_callback_text, CallbackState.waiting_text)

    dp.callback_query.register(button_handler)
    dp.callback_query.register(start_button_handler, lambda c: c.data == "start_bot")

    dp.message.register(keyboard_handler)

    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
