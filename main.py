import asyncio
import re
from datetime import datetime
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from db import (
    init_db, create_user, get_user, add_event,
    get_today_events, save_analysis, set_review_time,
    get_users_with_review_time, conn
)

BOT_TOKEN = "8511739482:AAGvix92KkVx4mGRQVl0QvDo9xYHOYtlMvc"

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


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📌 Записать момент")],
            [KeyboardButton(text="⏰ Изменить время вечернего разбора")]
        ],
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
        "1️⃣ *Записать момент*\n"
        "   Нажми кнопку 📌 или используй команду /pogryz, чтобы написать, что произошло.\n\n"
        "2️⃣ *Вечерний разбор*\n"
        "   Я буду напоминать вечером:\n"
        "   - ✅ Да — ногти целы, покажу твою текущую серию дней без грызения.\n"
        "   - ❌ Нет — сразу разберём ситуацию и причины.\n\n"
        "3️⃣ *Статистика*\n"
        "   Показываю только текущую и максимальную серии дней без грызения.\n\n"
        "4️⃣ *Время вечернего разбора*\n"
    )

    if not user[5]:  # review_time
        await message.answer(
            welcome_text +
            "Давай установим удобное время для вечернего разбора. Напиши в формате ЧЧ:ММ, например 21:30",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )
        await state.set_state(TimeState.waiting_time)
    else:
        await message.answer(
            welcome_text +
            f"Напоминания настроены на {user[5]} 🕰\n\nЯ готов помочь отслеживать твою привычку 🙌",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )


# --- /pogryz ---
async def pogryz_start(message: Message, state: FSMContext):
    await message.answer("Опиши, что случилось в этот момент:")
    await state.set_state(PogryzState.waiting_text)

# --- /pogryz ---
async def save_pogryz(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напиши /start 🙌")
        return

    # Сохраняем событие, но не сбрасываем серию и не запускаем разбор
    add_event(user[0], message.text)
    await message.answer("Событие записано ✅", reply_markup=main_keyboard())
    await state.clear()



# --- /review ---
async def start_review(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напиши /start 🙌")
        return

    # Берём все события за сегодня
    events = get_today_events(user[0])
    if not events:
        await message.answer("Сегодня нет записанных моментов. Это хороший знак 💪")
        return

    # Начинаем разбор с первого события
    await state.update_data(events=events, index=0)
    first_event = events[0]
    await message.answer(
        f"Давай разберём все сегодняшние события:\n\n_{first_event[3]}_\n\n"
        "Что стало причиной? Какие чувства были?"
    )
    await state.set_state(ReviewState.waiting_analysis)


# --- Сохранение текста после разбора ---
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
        # --- После разбора всех событий сбрасываем серию ---
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
async def reminder_loop(bot: Bot, dp: Dispatcher):
    while True:
        now = datetime.now().strftime("%H:%M")
        users = get_users_with_review_time()
        for user_id, tg_id, review_time in users:
            if review_time == now:
                # Берём все события за сегодня
                events = get_today_events(user_id)
                if events:
                    # Отправляем напоминание о разборе
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






# --- Обработка кнопок ---
async def button_handler(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[1])
    user = get_user(callback.from_user.id)

    # Убираем клавиатуру сразу после нажатия
    await callback.message.edit_reply_markup(None)

    if callback.data.startswith("yes_"):
        # --- Сначала обновляем серию ---
        current_streak = (user[2] or 0) + 1
        max_streak = max(user[3] or 0, current_streak)

        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET current_streak = ?, max_streak = ?, last_clean_day = ? WHERE id = ?",
            (current_streak, max_streak, datetime.now().date().isoformat(), user[0])
        )
        conn.commit()

        # --- Теперь выводим обновлённую статистику ---
        await callback.message.answer(
            f"Молодец! Продолжай в том же духе 💪\n\n"
            f"Текущая серия дней без грызения: {current_streak}\n"
            f"Максимальная серия: {max_streak}"
        )
        await callback.answer()

    else:  # "Нет"
        await callback.message.answer("Опиши, что произошло и что стало причиной:")
        await state.set_state(CallbackState.waiting_text)
        await state.update_data(user_id=user_id)
        await callback.answer()


# --- Сохранение текста после "Нет" ---
async def save_callback_text(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("user_id")
    add_event(user_id, message.text)
    await state.clear()

    # --- Сразу запускаем разбор событий ---
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
    
    dp.message.register(keyboard_handler)

    asyncio.create_task(reminder_loop(bot, dp))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())