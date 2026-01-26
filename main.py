import asyncio
import re
import os
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage

from db import (
    init_db, create_user, get_user, add_event,
    get_today_events, save_analysis, set_review_time,
    get_users_with_review_time, conn
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))

init_db()

# ---------- FSM ----------
class PogryzState(StatesGroup):
    waiting_text = State()

class ReviewState(StatesGroup):
    waiting_analysis = State()

class TimeState(StatesGroup):
    waiting_time = State()

class CallbackState(StatesGroup):
    waiting_text = State()


# ---------- Keyboards ----------
def main_keyboard(is_admin=False):
    keyboard = [
        [KeyboardButton(text="📌 Записать момент")],
        [KeyboardButton(text="⏰ Изменить время вечернего разбора")],
        [KeyboardButton(text="🌍 Изменить часовой пояс")]
    ]

    if is_admin:
        keyboard.append([KeyboardButton(text="📊 Статистика бота")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True
    )


def russia_timezone_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Калининград (UTC+2)", callback_data="tz_2")],
        [InlineKeyboardButton(text="🇷🇺 Москва (UTC+3)", callback_data="tz_3")],
        [InlineKeyboardButton(text="🇷🇺 Самара (UTC+4)", callback_data="tz_4")],
        [InlineKeyboardButton(text="🇷🇺 Екатеринбург (UTC+5)", callback_data="tz_5")],
        [InlineKeyboardButton(text="🇷🇺 Омск (UTC+6)", callback_data="tz_6")],
        [InlineKeyboardButton(text="🇷🇺 Красноярск (UTC+7)", callback_data="tz_7")],
        [InlineKeyboardButton(text="🇷🇺 Иркутск (UTC+8)", callback_data="tz_8")],
        [InlineKeyboardButton(text="🇷🇺 Якутск (UTC+9)", callback_data="tz_9")],
        [InlineKeyboardButton(text="🇷🇺 Владивосток (UTC+10)", callback_data="tz_10")],
    ])


def checkin_keyboard(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👍 Отлично", callback_data=f"checkin_ok_{user_id}"),
            InlineKeyboardButton(text="😕 Не очень", callback_data=f"checkin_bad_{user_id}")
        ]
    ])


# ---------- /start ----------
async def start(message: Message, state: FSMContext):
    create_user(message.from_user.id)
    user = get_user(message.from_user.id)

    if user[6] is None:
        await message.answer(
            "Чтобы я присылал напоминания вовремя, выбери свой часовой пояс 🇷🇺",
            reply_markup=russia_timezone_keyboard()
        )
        return

    if not user[5]:
        await message.answer(
            "Напиши удобное время вечернего разбора в формате ЧЧ:ММ (например 21:30)",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID)
        )
        await state.set_state(TimeState.waiting_time)
        return

    await message.answer(
        "Я готов помочь отслеживать твою привычку 🙌",
        reply_markup=main_keyboard(message.from_user.id == ADMIN_ID)
    )


# ---------- Timezone callback ----------
async def timezone_callback(callback: CallbackQuery):
    tz = int(callback.data.split("_")[1])
    user = get_user(callback.from_user.id)

    cur = conn.cursor()
    cur.execute("UPDATE users SET timezone = ? WHERE id = ?", (tz, user[0]))
    conn.commit()

    await callback.message.edit_text(
        f"Часовой пояс UTC+{tz} сохранён 🕰\n\n"
        "Теперь можешь пользоваться ботом 👌"
    )
    await callback.answer()


# ---------- Pogryz ----------
async def pogryz_start(message: Message, state: FSMContext):
    await message.answer("Опиши, что случилось:")
    await state.set_state(PogryzState.waiting_text)

async def save_pogryz(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    add_event(user[0], message.text)
    await message.answer("Событие записано ✅", reply_markup=main_keyboard())
    await state.clear()


# ---------- Review ----------
async def start_review(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    events = get_today_events(user[0])

    if not events:
        await message.answer("Сегодня всё чисто 💪")
        return

    await state.update_data(events=events, index=0)
    await message.answer(f"{events[0][3]}")
    await state.set_state(ReviewState.waiting_analysis)

async def save_review_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    events = data["events"]
    index = data["index"]

    save_analysis(events[index][0], message.text)
    index += 1

    if index < len(events):
        await state.update_data(index=index)
        await message.answer(events[index][3])
    else:
        await message.answer("Разбор завершён 🙌")
        await state.clear()


# ---------- Set time ----------
async def save_time(message: Message, state: FSMContext):
    if not re.match(r"^\d{2}:\d{2}$", message.text):
        await message.answer("Формат ЧЧ:ММ")
        return

    user = get_user(message.from_user.id)
    set_review_time(user[0], message.text)
    await message.answer("Время сохранено 🕰")
    await state.clear()


# ---------- Keyboard handler (ВАЖНО) ----------
async def keyboard_handler(message: Message, state: FSMContext):
    if message.text == "📌 Записать момент":
        await pogryz_start(message, state)

    elif message.text == "⏰ Изменить время вечернего разбора":
        await message.answer("Введи новое время (ЧЧ:ММ)")
        await state.set_state(TimeState.waiting_time)

    elif message.text == "🌍 Изменить часовой пояс":
        await message.answer(
            "Выбери новый часовой пояс 🇷🇺",
            reply_markup=russia_timezone_keyboard()
        )

    elif message.text == "📊 Статистика бота":
        await admin_stats(message)


# ---------- Admin stats ----------
async def admin_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    users = cur.fetchone()[0]

    await message.answer(f"👤 Пользователей: {users}")


# ---------- Reminder loop ----------
async def reminder_loop(bot: Bot):
    while True:
        now_utc = datetime.utcnow()
        users = get_users_with_review_time()

        for user_id, tg_id, review_time in users:
            user = get_user(tg_id)
            tz = user[6] or 0
            now = (now_utc + timedelta(hours=tz)).strftime("%H:%M")

            if now == "13:00":
                await bot.send_message(
                    tg_id,
                    "Как твои ногти?",
                    reply_markup=checkin_keyboard(user_id)
                )

            if review_time == now:
                await bot.send_message(tg_id, "Время вечернего разбора /review")

        await asyncio.sleep(60)


# ---------- main ----------
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.register(start, Command("start"))
    dp.message.register(pogryz_start, Command("pogryz"))
    dp.message.register(start_review, Command("review"))
    dp.message.register(save_pogryz, PogryzState.waiting_text)
    dp.message.register(save_review_answer, ReviewState.waiting_analysis)
    dp.message.register(save_time, TimeState.waiting_time)
    dp.message.register(keyboard_handler)

    dp.callback_query.register(timezone_callback, lambda c: c.data.startswith("tz_"))

    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
