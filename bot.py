import asyncio
import os
import re
from datetime import datetime, timedelta
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from dotenv import load_dotenv
from typing import Any, Awaitable, Callable, Dict
from aiohttp import web

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

if not BOT_TOKEN or not CHANNEL_ID or not ADMIN_ID:
    raise ValueError("❌ Заполни BOT_TOKEN, CHANNEL_ID и ADMIN_ID в .env!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# === БАЗА ДАННЫХ ===
async def init_db():
    async with aiosqlite.connect('secretary.db') as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
                            user_id INTEGER PRIMARY KEY,
                            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS tasks (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER,
                            task_text TEXT,
                            deadline DATE,
                            status TEXT DEFAULT 'pending')''')
        await db.execute('''CREATE TABLE IF NOT EXISTS notes (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER,
                            note_text TEXT)''')
        await db.commit()

# === ПРОВЕРКА ПОДПИСКИ ===
async def check_sub(user_id: int) -> bool:
    # Админу подписка не обязательна (или он и так владелец канала)
    if user_id == ADMIN_ID: return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except TelegramBadRequest:
        return False

def get_sub_keyboard():
    channel_url = CHANNEL_ID.replace("@", "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=f"https://t.me/{channel_url}")],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")]
    ])

class ForceSubMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]], event: Message, data: Dict[str, Any]) -> Any:
        if event.text and event.text.startswith(('/start', '/help')):
            return await handler(event, data)
        if not await check_sub(event.from_user.id):
            await event.answer(f"🛑 Секретарь работает только для своих.\nПодпишись на канал {CHANNEL_ID}.", reply_markup=get_sub_keyboard())
            return
        return await handler(event, data)

router.message.middleware(ForceSubMiddleware())
dp.include_router(router)

# ==========================================
# 📋 КОМАНДЫ ПОЛЬЗОВАТЕЛЯ
# ==========================================

@router.message(Command("start", "help"))
async def cmd_start(message: Message):
    async with aiosqlite.connect('secretary.db') as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (message.from_user.id,))
        await db.commit()
        
    text = (
        "👔 <b>Твой Личный Секретарь!</b>\n\n"
        "Напиши мне задачу текстом:\n"
        "• <code>Купить хлеб</code> — улетит в Инбокс\n"
        "• <code>Позвонить маме завтра</code> — поставит срок на завтра\n\n"
        "<b>Команды:</b>\n"
        "/plan - план на сегодня\n"
        "/inbox - задачи без срока\n"
        "/overdue - просроченные задачи\n"
        "/note [текст] - сохранить заметку\n"
        "/notes - все заметки\n"
        "/stat - моя статистика\n"
        "/focus [минуты] - запустить таймер"
    )
    if message.from_user.id == ADMIN_ID:
        text += "\n\n👑 <b>Админка:</b> /admin_help"
        
    await message.answer(text, parse_mode="HTML")

@router.message(Command("plan"))
async def show_plan(message: Message):
    today = datetime.now().date()
    async with aiosqlite.connect('secretary.db') as db:
        cursor = await db.execute("SELECT id, task_text FROM tasks WHERE user_id = ? AND deadline = ? AND status = 'pending'", (message.from_user.id, today))
        tasks = await cursor.fetchall()
        
    if not tasks: return await message.answer("На сегодня задач нет! Отдыхай. 🍻")
    
    msg = "📋 <b>План на сегодня:</b>\n\n"
    for i, t in enumerate(tasks, 1): msg += f"{i}. {t[1]}\n"
    await message.answer(msg, parse_mode="HTML")

@router.message(Command("inbox"))
async def show_inbox(message: Message):
    async with aiosqlite.connect('secretary.db') as db:
        cursor = await db.execute("SELECT id, task_text FROM tasks WHERE user_id = ? AND deadline IS NULL AND status = 'pending'", (message.from_user.id,))
        tasks = await cursor.fetchall()
        
    if not tasks: return await message.answer("Инбокс пуст. Молодец! 🧹")
    msg = "📥 <b>Неразобранные задачи:</b>\n\n"
    for i, t in enumerate(tasks, 1): msg += f"{i}. {t[1]}\n"
    await message.answer(msg, parse_mode="HTML")

@router.message(Command("overdue"))
async def show_overdue(message: Message):
    today = datetime.now().date()
    async with aiosqlite.connect('secretary.db') as db:
        cursor = await db.execute("SELECT id, task_text, deadline FROM tasks WHERE user_id = ? AND deadline < ? AND status = 'pending'", (message.from_user.id, today))
        tasks = await cursor.fetchall()
        
    if not tasks: return await message.answer("Просроченных задач нет! 🔥")
    msg = "🚨 <b>ПРОСРОЧЕНО:</b>\n\n"
    for i, t in enumerate(tasks, 1): msg += f"{i}. {t[1]} (был {t[2]})\n"
    await message.answer(msg, parse_mode="HTML")

@router.message(Command("stat"))
async def show_stats(message: Message):
    async with aiosqlite.connect('secretary.db') as db:
        cursor = await db.execute("SELECT status, COUNT(*) FROM tasks WHERE user_id = ? GROUP BY status", (message.from_user.id,))
        rows = await cursor.fetchall()
        
    stats = {'pending': 0, 'done': 0}
    for row in rows: stats[row[0]] = row[1]
    
    await message.answer(
        f"📊 <b>Твоя статистика:</b>\n\n"
        f"✅ Выполнено задач: {stats['done']}\n"
        f"⏳ В ожидании: {stats['pending']}",
        parse_mode="HTML"
    )

@router.message(Command("note"))
async def add_note(message: Message, command: CommandObject):
    if not command.args: return await message.answer("Напиши текст заметки. Пример: `/note Идея для видео...`", parse_mode="Markdown")
    async with aiosqlite.connect('secretary.db') as db:
        await db.execute("INSERT INTO notes (user_id, note_text) VALUES (?, ?)", (message.from_user.id, command.args))
        await db.commit()
    await message.answer("📝 Заметка сохранена!")

@router.message(Command("notes"))
async def show_notes(message: Message):
    async with aiosqlite.connect('secretary.db') as db:
        cursor = await db.execute("SELECT note_text FROM notes WHERE user_id = ?", (message.from_user.id,))
        notes = await cursor.fetchall()
    if not notes: return await message.answer("У тебя пока нет заметок.")
    msg = "📒 <b>Твои заметки:</b>\n\n"
    for i, n in enumerate(notes, 1): msg += f"🔹 {n[0]}\n\n"
    await message.answer(msg, parse_mode="HTML")

@router.message(Command("focus"))
async def pomodoro(message: Message, command: CommandObject):
    minutes = int(command.args) if command.args and command.args.isdigit() else 25
    await message.answer(f"🍅 Таймер на {minutes} минут запущен. Работай, не отвлекайся!")
    await asyncio.sleep(minutes * 60)
    await message.answer(f"🔔 Прошло {minutes} минут! Сделай перерыв.")

# ==========================================
# 👑 КОМАНДЫ АДМИНА
# ==========================================

@router.message(Command("admin_help"))
async def admin_help(message: Message):
    if message.from_user.id != ADMIN_ID: return
    text = (
        "👑 <b>Панель Администратора</b>\n\n"
        "/admin_stat - Общая статистика бота\n"
        "/admin_users - Топ юзеров по активности\n"
        "/admin_broadcast [текст] - Рассылка всем\n"
        "/admin_backup - Скачать базу данных (Важно делать периодически!)"
    )
    await message.answer(text, parse_mode="HTML")

@router.message(Command("admin_stat"))
async def admin_stat(message: Message):
    if message.from_user.id != ADMIN_ID: return
    async with aiosqlite.connect('secretary.db') as db:
        u_cursor = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await u_cursor.fetchone())[0]
        
        t_cursor = await db.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
        tasks = await t_cursor.fetchall()
        t_stats = {'pending': 0, 'done': 0}
        for t in tasks: t_stats[t[0]] = t[1]
        
    await message.answer(
        f"📈 <b>Глобальная статистика:</b>\n\n"
        f"👥 Всего юзеров: {total_users}\n"
        f"✅ Решено задач (всеми): {t_stats['done']}\n"
        f"⏳ Висит задач: {t_stats['pending']}",
        parse_mode="HTML"
    )

@router.message(Command("admin_users"))
async def admin_top(message: Message):
    if message.from_user.id != ADMIN_ID: return
    async with aiosqlite.connect('secretary.db') as db:
        # Топ 10 пользователей по количеству добавленных задач
        cursor = await db.execute("SELECT user_id, COUNT(*) as c FROM tasks GROUP BY user_id ORDER BY c DESC LIMIT 10")
        top_users = await cursor.fetchall()
        
    msg = "🏆 <b>Топ юзеров (по кол-ву задач):</b>\n\n"
    for i, user in enumerate(top_users, 1):
        msg += f"{i}. ID <code>{user[0]}</code> — {user[1]} задач\n"
    await message.answer(msg, parse_mode="HTML")

@router.message(Command("admin_broadcast"))
async def admin_broadcast(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("Введи текст рассылки: `/admin_broadcast Привет всем!`")
    
    async with aiosqlite.connect('secretary.db') as db:
        cursor = await db.execute("SELECT user_id FROM users")
        users = await cursor.fetchall()
    
    success, fail = 0, 0
    await message.answer("🚀 Начинаю рассылку...")
    
    for user in users:
        try:
            await bot.send_message(user[0], f"📢 <b>Сообщение от админа:</b>\n\n{command.args}", parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.05) # Лимиты телеграма
        except TelegramForbiddenError:
            # Юзер заблокировал бота
            fail += 1
        except Exception:
            fail += 1
            
    await message.answer(f"✅ Рассылка завершена!\nУспешно: {success}\nОшибок (заблокировали): {fail}")

@router.message(Command("admin_backup"))
async def admin_backup(message: Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        doc = FSInputFile("secretary.db")
        await message.answer_document(doc, caption="💽 Твоя база данных. Храни в надежном месте!")
    except Exception as e:
        await message.answer(f"Ошибка при выгрузке базы: {e}")

# ==========================================
# 🧠 ОБРАБОТКА ТЕКСТА (Создание задач)
# ==========================================

@router.callback_query(F.data == "check_sub")
async def callback_check_sub(callback: CallbackQuery):
    if await check_sub(callback.from_user.id):
        await callback.message.edit_text("✅ Подписка подтверждена. Напиши мне задачу!")
    else:
        await callback.answer("❌ Ты еще не подписался!", show_alert=True)

@router.message(F.text & ~F.text.startswith('/'))
async def add_task(message: Message):
    text = message.text
    deadline = None
    today = datetime.now().date()
    
    if re.search(r'\bзавтра\b', text.lower()):
        deadline = today + timedelta(days=1)
        text = re.sub(r'\bзавтра\b', '', text, flags=re.IGNORECASE).strip()
    elif re.search(r'\bсегодня\b', text.lower()):
        deadline = today
        text = re.sub(r'\bсегодня\b', '', text, flags=re.IGNORECASE).strip()
    
    async with aiosqlite.connect('secretary.db') as db:
        cursor = await db.execute(
            "INSERT INTO tasks (user_id, task_text, deadline) VALUES (?, ?, ?)",
            (message.from_user.id, text, deadline)
        )
        task_id = cursor.lastrowid
        await db.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сделано", callback_data=f"done_{task_id}"),
         InlineKeyboardButton(text="➡️ На завтра", callback_data=f"tmrw_{task_id}")]
    ])
    
    date_str = deadline.strftime("%d.%m.%Y") if deadline else "Без срока (Инбокс 📥)"
    await message.answer(f"📝 <b>Задача:</b> {text}\n📅 <b>Срок:</b> {date_str}", parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("done_"))
async def complete_task(callback: CallbackQuery):
    task_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect('secretary.db') as db:
        await db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
        await db.commit()
    await callback.message.edit_text(f"<s>{callback.message.html_text}</s>\n\n✅ <b>Выполнено!</b>", parse_mode="HTML")

@router.callback_query(F.data.startswith("tmrw_"))
async def postpone_task(callback: CallbackQuery):
    task_id = int(callback.data.split("_")[1])
    tmrw = (datetime.now() + timedelta(days=1)).date()
    async with aiosqlite.connect('secretary.db') as db:
        await db.execute("UPDATE tasks SET deadline = ? WHERE id = ?", (tmrw, task_id))
        await db.commit()
    await callback.message.edit_text(f"{callback.message.html_text}\n\n➡️ <i>Перенесено на завтра</i>", parse_mode="HTML")


# ==========================================
# 🚀 ЗАПУСК ДЛЯ RENDER
# ==========================================

async def start_bot():
    await init_db()
    print("🚀 Бот-секретарь успешно запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

async def health_check(request):
    return web.Response(text="Бот живой и работает!")

async def on_startup(app):
    asyncio.create_task(start_bot())

if __name__ == "__main__":
    app = web.Application()
    app.router.add_get('/', health_check)
    app.on_startup.append(on_startup)
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)