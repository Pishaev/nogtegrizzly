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
        [KeyboardButton(text="⏰ Изменить время вечернего разбора")]
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

def checkin_keyboard(db_user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👍 Отлично", callback_data=f"checkin_ok_{db_user_id}"),
            InlineKeyboardButton(text="😕 Не очень", callback_data=f"checkin_bad_{db_user_id}")
        ]
    ])

# ---------- /start ----------
async def start(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        create_user(message.from_user.id)
        user = get_user(message.from_user.id)

    welcome_text = (
        "Привет! 👋\n\n"
        "Я помогаю отслеживать привычку грызть ногти и разбирать причины.\n\n"
        "📌 Записывай моменты\n"
        "🕰 Получай напоминания\n"
        "📊 Следи за серией без грызения\n"
    )

    # нет таймзоны
    if user[6] is None:
        await message.answer(
            "Чтобы я присылал напоминания вовремя, выбери свой часовой пояс 🇷🇺",
            reply_markup=russia_timezone_keyboard()
        )
        return

    # нет времени разбора
    if not user[5]:
        await message.answer(
            welcome_text + "\nНапиши время вечернего разбора (ЧЧ:ММ)",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID)
        )
        await state.set_state(TimeState.waiting_time)
        return

    await message.answer(
        welcome_text +
        f"\nНапоминания настроены на {user[5]} 🕰",
        reply_markup=main_keyboard(message.from_user.id == ADMIN_ID)
    )

# ---------- Timezone ----------
async def timezone_callback(callback: CallbackQuery):
    tz = int(callback.data.split("_")[1])
    user = get_user(callback.from_user.id)
    if not user:
        await callback.answer()
        return

    cur = conn.cursor()
    cur.execute("UPDATE users SET timezone = ? WHERE id = ?", (tz, user[0]))
    conn.commit()

    await callback.message.edit_text(
        f"Часовой пояс UTC+{tz} сохранён 🕰\n\nТеперь задай время вечернего разбора."
    )
    await callback.answer()

# ---------- Pogryz ----------
async def pogryz_start(message: Message, state: FSMContext):
    await message.answer("Опиши, что произошло:")
    await state.set_state(PogryzState.waiting_text)

async def save_pogryz(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        return

    add_event(user[0], message.text)
    await message.answer("Событие записано ✅", reply_markup=main_keyboard(message.from_user.id == ADMIN_ID))
    await state.clear()

# ---------- Review ----------
async def start_review(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    events = get_today_events(user[0])

    if not events:
        await message.answer("Сегодня событий нет 💪")
        return

    await state.update_data(events=events, index=0)
    await message.answer(
        f"_{events[0][3]}_\n\nЧто стало причиной?",
        parse_mode="Markdown"
    )
    await state.set_state(ReviewState.waiting_analysis)

async def save_review_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    index = data["index"]
    events = data["events"]

    save_analysis(events[index][0], message.text)
    index += 1

    if index < len(events):
        await state.update_data(index=index)
        await message.answer(f"_{events[index][3]}_", parse_mode="Markdown")
    else:
        cur = conn.cursor()
        cur.execute("UPDATE users SET current_streak = 0 WHERE id = ?", (events[0][1],))
        conn.commit()
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

# ---------- Reminder loop ----------
async def reminder_loop(bot: Bot):
    while True:
        now_utc = datetime.utcnow()
        users = get_users_with_review_time()

        for db_user_id, tg_id, review_time in users:
            user = get_user(tg_id)
            if not user:
                continue

            tz = user[6] or 0
            user_now = now_utc + timedelta(hours=tz)
            now_str = user_now.strftime("%H:%M")

            # чек-ин ровно один раз
            if now_str == "13:00" and user_now.second < 5:
                await bot.send_message(
                    tg_id,
                    "Как твои ногти сейчас?",
                    reply_markup=checkin_keyboard(db_user_id)
                )

            if review_time == now_str:
                events = get_today_events(db_user_id)
                if events:
                    await bot.send_message(tg_id, "Время вечернего разбора /review")
                else:
                    await bot.send_message(
                        tg_id,
                        "Целостны ли ногти?",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [
                                InlineKeyboardButton(text="Да", callback_data=f"yes_{db_user_id}"),
                                InlineKeyboardButton(text="Нет", callback_data=f"no_{db_user_id}")
                            ]
                        ])
                    )

        await asyncio.sleep(60)

# ---------- Callbacks ----------
async def button_handler(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")

    if callback.data.startswith("checkin_ok"):
        await callback.message.edit_reply_markup(None)
        await callback.message.answer("Круто 💪")
        await callback.answer()
        return

    if callback.data.startswith("checkin_bad"):
        await callback.message.edit_reply_markup(None)
        await callback.message.answer("Напиши коротко, что произошло.")
        await state.set_state(CallbackState.waiting_text)
        await state.update_data(user_id=int(parts[-1]))
        await callback.answer()
        return

    if callback.data.startswith("yes"):
        user = get_user(callback.from_user.id)
        cur = conn.cursor()
        current = (user[2] or 0) + 1
        cur.execute(
            "UPDATE users SET current_streak = ?, max_streak = MAX(max_streak, ?) WHERE id = ?",
            (current, current, user[0])
        )
        conn.commit()
        await callback.message.answer(f"Серия: {current} 🔥")
        await callback.answer()

    if callback.data.startswith("no"):
        await callback.message.answer("Опиши, что произошло:")
        await state.set_state(CallbackState.waiting_text)
        await state.update_data(user_id=int(parts[-1]))
        await callback.answer()

# ---------- Admin ----------
async def admin_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    users = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM events")
    events = cur.fetchone()[0]

    await message.answer(
        f"📊 Статистика\n\n👤 Пользователей: {users}\n📝 Событий: {events}"
    )

# ---------- Main ----------
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.register(start, Command("start"))
    dp.message.register(pogryz_start, Command("pogryz"))
    dp.message.register(save_pogryz, PogryzState.waiting_text)
    dp.message.register(start_review, Command("review"))
    dp.message.register(save_review_answer, ReviewState.waiting_analysis)
    dp.message.register(save_time, TimeState.waiting_time)
    dp.message.register(admin_stats, Command("stats"))

    dp.callback_query.register(timezone_callback, lambda c: c.data.startswith("tz_"))
    dp.callback_query.register(button_handler)

    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
