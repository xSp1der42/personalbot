import asyncio
import os
import re
from datetime import datetime, timedelta
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

# === ЗАГРУЗКА НАСТРОЕК ИЗ .env ===
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

if not BOT_TOKEN or not CHANNEL_ID:
    raise ValueError("❌ ОШИБКА: Заполни BOT_TOKEN и CHANNEL_ID в файле .env!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# === БАЗА ДАННЫХ ===
async def init_db():
    async with aiosqlite.connect('secretary.db') as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS tasks (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER,
                            task_text TEXT,
                            deadline DATE,
                            status TEXT DEFAULT 'pending'
                        )''')
        await db.commit()

# === ПРОВЕРКА ПОДПИСКИ ===
async def check_sub(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        # Пользователь подписан, если он member, admin или creator
        return member.status in ["member", "administrator", "creator"]
    except TelegramBadRequest:
        # Если бот не админ в канале или канал не существует, вернет False
        return False

def get_sub_keyboard():
    # Убираем @ для генерации ссылки
    channel_url = CHANNEL_ID.replace("@", "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=f"https://t.me/{channel_url}")],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")]
    ])

# Промежуточный слой (Middleware) для проверки подписки
@router.message()
async def force_sub_middleware(message: Message, handler):
    # Пропускаем команду /start, она обрабатывается отдельно
    if message.text and message.text.startswith('/start'):
        return await handler(message)
        
    if not await check_sub(message.from_user.id):
        await message.answer(
            "🛑 Секретарь работает только для своих.\n"
            f"Подпишись на канал {CHANNEL_ID}, чтобы получить доступ.",
            reply_markup=get_sub_keyboard()
        )
        return
    return await handler(message)

# === ХЭНДЛЕРЫ ===

@router.message(Command("start"))
async def cmd_start(message: Message):
    if not await check_sub(message.from_user.id):
        await message.answer(
            "Привет! Я твой личный секретарь. Напоминания, задачи, дайджесты.\n"
            f"Но сначала подпишись на {CHANNEL_ID}.",
            reply_markup=get_sub_keyboard()
        )
        return
        
    async with aiosqlite.connect('secretary.db') as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (message.from_user.id,))
        await db.commit()
        
    await message.answer(
        "✅ Доступ открыт! \n\n"
        "Напиши мне любую задачу, например:\n"
        "• <code>Купить молоко</code> (без даты — улетит в инбокс)\n"
        "• <code>Сделать отчет завтра</code> (автоматически поставит дедлайн на завтра)\n\n"
        "<b>Команды:</b>\n"
        "/plan - план на сегодня",
        parse_mode="HTML"
    )

@router.callback_query(F.data == "check_sub")
async def callback_check_sub(callback: CallbackQuery):
    if await check_sub(callback.from_user.id):
        await callback.message.edit_text("✅ Отлично! Подписка подтверждена. Напиши мне задачу!")
    else:
        await callback.answer("❌ Ты еще не подписался!", show_alert=True)

# ПАРСЕР И БЫСТРОЕ ДОБАВЛЕНИЕ ЗАДАЧ
@router.message(F.text & ~F.text.startswith('/'))
async def add_task(message: Message):
    text = message.text
    deadline = None
    today = datetime.now().date()
    
    # Примитивный парсинг текста без ИИ
    if re.search(r'\bзавтра\b', text.lower()):
        deadline = today + timedelta(days=1)
        text = re.sub(r'\bзавтра\b', '', text, flags=re.IGNORECASE).strip()
    elif re.search(r'\bсегодня\b', text.lower()):
        deadline = today
        text = re.sub(r'\bсегодня\b', '', text, flags=re.IGNORECASE).strip()
    
    # Сохраняем в БД
    async with aiosqlite.connect('secretary.db') as db:
        cursor = await db.execute(
            "INSERT INTO tasks (user_id, task_text, deadline) VALUES (?, ?, ?)",
            (message.from_user.id, text, deadline)
        )
        task_id = cursor.lastrowid
        await db.commit()

    # Клавиатура под задачей
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сделано", callback_data=f"done_{task_id}"),
         InlineKeyboardButton(text="➡️ На завтра", callback_data=f"tmrw_{task_id}")]
    ])
    
    date_str = deadline.strftime("%d.%m.%Y") if deadline else "Без срока (Инбокс)"
    await message.answer(
        f"📝 <b>Задача добавлена:</b> {text}\n📅 <b>Срок:</b> {date_str}", 
        parse_mode="HTML", 
        reply_markup=kb
    )

# ОБРАБОТКА КНОПОК ЗАДАЧИ
@router.callback_query(F.data.startswith("done_"))
async def complete_task(callback: CallbackQuery):
    task_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect('secretary.db') as db:
        await db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
        await db.commit()
    
    # Зачеркиваем текст задачи
    await callback.message.edit_text(
        f"<s>{callback.message.html_text}</s>\n\n✅ <b>Выполнено!</b>", 
        parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("tmrw_"))
async def postpone_task(callback: CallbackQuery):
    task_id = int(callback.data.split("_")[1])
    tmrw = (datetime.now() + timedelta(days=1)).date()
    
    async with aiosqlite.connect('secretary.db') as db:
        await db.execute("UPDATE tasks SET deadline = ? WHERE id = ?", (tmrw, task_id))
        await db.commit()
        
    await callback.message.edit_text(
        f"{callback.message.html_text}\n\n➡️ <i>Перенесено на завтра</i>", 
        parse_mode="HTML"
    )

# ПЛАН НА СЕГОДНЯ
@router.message(Command("plan"))
async def show_plan(message: Message):
    today = datetime.now().date()
    async with aiosqlite.connect('secretary.db') as db:
        cursor = await db.execute(
            "SELECT id, task_text FROM tasks WHERE user_id = ? AND deadline = ? AND status = 'pending'",
            (message.from_user.id, today)
        )
        tasks = await cursor.fetchall()
        
    if not tasks:
        await message.answer("На сегодня задач нет! Отдыхай. 🍻")
        return
        
    msg = "📋 <b>Твой план на сегодня:</b>\n\n"
    for i, task in enumerate(tasks, 1):
        msg += f"{i}. {task[1]}\n"
        
    await message.answer(msg, parse_mode="HTML")

# === ЗАПУСК ===
async def main():
    await init_db()
    print("🚀 Бот-секретарь успешно запущен!")
    # Удаляем вебхуки на всякий случай, чтобы polling работал нормально
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")