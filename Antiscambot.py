import asyncio
import sqlite3
import os
import time
from datetime import datetime
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, BaseFilter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# ==========================================
# НАСТРОЙКИ БОТА
# ==========================================
BOT_TOKEN = "8792564218:AAHo3taU03G4FGAtIovL6mdSNXRA72QrtE0" # <-- Вставь сюда токен
ADMIN_ID = 5341904332 # <-- Вставь сюда свой Telegram ID (числами)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

# ==========================================
# БАЗА ДАННЫХ И МИГРАЦИИ
# ==========================================
DB_NAME = "antiscam_pro.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            username TEXT,
            roblox_username TEXT DEFAULT 'Не привязан',
            roblox_id TEXT DEFAULT 'Нет',
            status TEXT DEFAULT 'user',
            trades INTEGER DEFAULT 0,
            rating_sum REAL DEFAULT 0.0,
            reviews_count INTEGER DEFAULT 0,
            join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица логов P2P сделок
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades_p2p (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            initiator_id INTEGER,
            partner_id INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица отзывов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reviewer_id INTEGER,
            target_id INTEGER,
            rating INTEGER,
            review_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица тикетов (привязки и жалобы)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            content TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица логов аудита
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            action TEXT,
            target_id INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Вспомогательная функция для БД
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

# ==========================================
# ФИЛЬТРЫ И ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id == ADMIN_ID

class IsPrivate(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.chat.type == "private"

def format_rating(rating_sum, reviews_count):
    if reviews_count == 0:
        return "Нет оценок 🤷‍♂️"
    avg = rating_sum / reviews_count
    stars_count = round(avg)
    stars = "⭐" * stars_count + "🌑" * (5 - stars_count)
    return f"{avg:.1f} {stars} ({reviews_count} отз.)"

def get_user_title(trades):
    if trades < 5: return "🌱 Новичок"
    elif trades < 15: return "⚔️ Опытный"
    elif trades < 50: return "🛡 Мастер"
    elif trades < 100: return "👑 Гранд-Мастер"
    else: return "🔥 Легенда"

def get_progress_bar(trades):
    if trades >= 100: return "[██████████] 100%"
    percent = trades % 10  # Для визуализации прогресса к следующему десятку
    filled = percent
    empty = 10 - filled
    return f"[{'█' * filled}{'░' * empty}] {percent * 10}% до некст ранга"

# ==========================================
# КЛАВИАТУРЫ
# ==========================================
def get_main_reply_kb(user_id):
    builder = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🤝 Создать сделку")],
        [KeyboardButton(text="⭐ Оставить отзыв"), KeyboardButton(text="🧾 Создать чек")],
        [KeyboardButton(text="🛡 Гаранты"), KeyboardButton(text="⚠️ Скамеры"), KeyboardButton(text="🏆 Топ")],
        [KeyboardButton(text="🔍 Проверить"), KeyboardButton(text="🛎 Вызвать Гаранта")],
        [KeyboardButton(text="🎮 Привязать Roblox"), KeyboardButton(text="🚨 Подать жалобу")]
    ], resize_keyboard=True)
    
    if user_id == ADMIN_ID:
        builder.keyboard.append([KeyboardButton(text="👑 Админ Панель")])
        
    return builder

def get_cancel_reply_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)

def get_admin_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎫 Заявки Roblox", callback_data="admin_tickets_roblox"),
         InlineKeyboardButton(text="🚨 Жалобы", callback_data="admin_tickets_reports")],
        [InlineKeyboardButton(text="🔍 Поиск Юзера", callback_data="admin_search_user"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="💾 Скачать БД", callback_data="admin_backup_db")]
    ])

def get_admin_user_manage_kb(target_id, current_status):
    buttons = []
    if current_status != 'scammer':
        buttons.append([InlineKeyboardButton(text="☠️ В ЧС (Скам)", callback_data=f"admset_scammer_{target_id}")])
    if current_status != 'garant':
        buttons.append([InlineKeyboardButton(text="👑 Сделать Гарантом", callback_data=f"admset_garant_{target_id}")])
    if current_status != 'user':
        buttons.append([InlineKeyboardButton(text="👤 Снять статусы", callback_data=f"admset_user_{target_id}")])
        
    buttons.append([
        InlineKeyboardButton(text="➕ 1 Сделка", callback_data=f"admadd_trade_{target_id}"),
        InlineKeyboardButton(text="🧹 Обнулить", callback_data=f"admclr_stats_{target_id}")
    ])
    buttons.append([InlineKeyboardButton(text="◀️ Назад в Админку", callback_data="admin_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==========================================
# МАШИНА СОСТОЯНИЙ (FSM)
# ==========================================
class P2PTrade(StatesGroup):
    waiting_for_partner = State()

class LeaveReview(StatesGroup):
    waiting_for_target = State()
    waiting_for_stars = State()
    waiting_for_text = State()

class CheckUser(StatesGroup):
    waiting_for_target = State()

class CallGarant(StatesGroup):
    waiting_for_details = State()

class BindRoblox(StatesGroup):
    waiting_for_video = State()

class ReportUser(StatesGroup):
    waiting_for_target = State()
    waiting_for_proofs = State()

class AdminPanelStates(StatesGroup):
    waiting_for_user_search = State()
    waiting_for_roblox_data = State()
    target_ticket_id = None

# ==========================================
# ОБРАБОТЧИКИ: БАЗОВЫЕ (СТАРТ, ПРОФИЛЬ)
# ==========================================
@router.message(Command("start"), IsPrivate())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    if not user:
        conn.execute("INSERT INTO users (tg_id, username) VALUES (?, ?)", 
                     (message.from_user.id, message.from_user.username))
        conn.commit()
    conn.close()
    
    await message.answer(
        f"👋 Добро пожаловать, <b>{message.from_user.first_name}</b>!\n\n"
        f"🛡 <b>AntiScam Pro</b> — это передовая база данных трейдеров и гарантов.\n"
        f"Используй меню ниже для навигации.",
        reply_markup=get_main_reply_kb(message.from_user.id)
    )

@router.message(F.text == "❌ Отмена", IsPrivate())
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=get_main_reply_kb(message.from_user.id))

@router.message(F.text == "👤 Профиль", IsPrivate())
async def show_profile(message: Message):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    conn.close()
    
    if not user:
        return await message.answer("Вас нет в базе. Напишите /start")
        
    title = get_user_title(user['trades'])
    rating = format_rating(user['rating_sum'], user['reviews_count'])
    progress = get_progress_bar(user['trades'])
    
    status_text = "🟢 Обычный пользователь"
    if user['status'] == 'garant': status_text = "👑 Официальный Гарант"
    elif user['status'] == 'scammer': status_text = "☠️ В ЧЁРНОМ СПИСКЕ (СКАМ)"

    text = (
        f"👤 <b>Ваш Профиль:</b>\n\n"
        f"🔖 <b>ID:</b> <code>{user['tg_id']}</code>\n"
        f"🎮 <b>Roblox:</b> <code>{user['roblox_username']}</code> (ID: {user['roblox_id']})\n\n"
        f"🏆 <b>Титул:</b> {title}\n"
        f"⚖️ <b>Статус:</b> {status_text}\n"
        f"🤝 <b>Сделок:</b> {user['trades']}\n"
        f"📊 <b>Прогресс:</b>\n<code>{progress}</code>\n\n"
        f"⭐️ <b>Рейтинг:</b> {rating}"
    )
    await message.answer(text)

# ==========================================
# ОБРАБОТЧИКИ: P2P СДЕЛКИ
# ==========================================
@router.message(F.text == "🤝 Создать сделку", IsPrivate())
async def p2p_trade_start(message: Message, state: FSMContext):
    await message.answer("🤝 Введите <b>@username</b> или <b>ID</b> человека, с которым вы успешно провели сделку (он должен подтвердить):", reply_markup=get_cancel_reply_kb())
    await state.set_state(P2PTrade.waiting_for_partner)

@router.message(P2PTrade.waiting_for_partner, IsPrivate())
async def p2p_trade_process(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    conn = get_db()
    if target.isdigit():
        partner = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else:
        partner = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()
        
    if not partner:
        conn.close()
        return await message.answer("❌ Пользователь не найден в базе бота. Попросите его запустить бота.")
        
    if partner['tg_id'] == message.from_user.id:
        conn.close()
        return await message.answer("❌ Нельзя провести сделку с самим собой.")

    # Записываем в БД ожидающую сделку
    cursor = conn.cursor()
    cursor.execute("INSERT INTO trades_p2p (initiator_id, partner_id) VALUES (?, ?)", (message.from_user.id, partner['tg_id']))
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Отправляем партнеру
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить сделку", callback_data=f"p2p_accept_{trade_id}")],
        [InlineKeyboardButton(text="❌ Это ошибка", callback_data=f"p2p_decline_{trade_id}")]
    ])
    
    try:
        await bot.send_message(partner['tg_id'], f"🤝 Пользователь @{message.from_user.username} утверждает, что провел с вами успешную сделку.\nПодтверждаете?", reply_markup=kb)
        await message.answer("✅ Запрос отправлен партнеру. Ожидайте подтверждения.", reply_markup=get_main_reply_kb(message.from_user.id))
    except:
        await message.answer("❌ Не удалось отправить сообщение партнеру (возможно он заблокировал бота).", reply_markup=get_main_reply_kb(message.from_user.id))
    
    await state.clear()

@router.callback_query(F.data.startswith("p2p_accept_"))
async def p2p_accept(call: CallbackQuery):
    trade_id = int(call.data.split("_")[2])
    conn = sqlite3.connect(DB_NAME)
    trade = conn.execute("SELECT * FROM trades_p2p WHERE id = ?", (trade_id,)).fetchone()
    
    if not trade or trade[3] != 'pending': # status index is 3
        conn.close()
        return await call.answer("❌ Эта сделка уже обработана.", show_alert=True)
        
    # Добавляем сделки обоим
    conn.execute("UPDATE users SET trades = trades + 1 WHERE tg_id IN (?, ?)", (trade[1], trade[2]))
    conn.execute("UPDATE trades_p2p SET status = 'accepted' WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()
    
    await call.message.edit_text("✅ Сделка подтверждена! Вам обоим начислена +1 сделка в статистику.")
    try:
        await bot.send_message(trade[1], "🎉 Партнер подтвердил сделку! Вам начислена +1 сделка.")
    except: pass

@router.callback_query(F.data.startswith("p2p_decline_"))
async def p2p_decline(call: CallbackQuery):
    trade_id = int(call.data.split("_")[2])
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE trades_p2p SET status = 'declined' WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()
    await call.message.edit_text("❌ Вы отклонили сделку.")

# ==========================================
# ОБРАБОТЧИКИ: ОТЗЫВЫ
# ==========================================
@router.message(F.text == "⭐ Оставить отзыв", IsPrivate())
async def review_start(message: Message, state: FSMContext):
    await message.answer("Введи @username или ID Гаранта/Трейдера, которому хочешь оставить отзыв:", reply_markup=get_cancel_reply_kb())
    await state.set_state(LeaveReview.waiting_for_target)

@router.message(LeaveReview.waiting_for_target, IsPrivate())
async def review_target(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    conn = get_db()
    if target.isdigit():
        user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else:
        user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()
    conn.close()
        
    if not user:
        return await message.answer("❌ Пользователь не найден.")
    if user['tg_id'] == message.from_user.id:
        return await message.answer("❌ Нельзя оставить отзыв самому себе.")
        
    await state.update_data(target_id=user['tg_id'])
    
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="1 ⭐"), KeyboardButton(text="2 ⭐"), KeyboardButton(text="3 ⭐")],
        [KeyboardButton(text="4 ⭐"), KeyboardButton(text="5 ⭐")],
        [KeyboardButton(text="❌ Отмена")]
    ], resize_keyboard=True)
    
    await message.answer(f"Какую оценку поставите пользователю @{user['username']}?", reply_markup=kb)
    await state.set_state(LeaveReview.waiting_for_stars)

@router.message(LeaveReview.waiting_for_stars, IsPrivate())
async def review_stars(message: Message, state: FSMContext):
    if "⭐" not in message.text:
        return await message.answer("Пожалуйста, используйте кнопки ниже.")
        
    stars = int(message.text.split()[0])
    await state.update_data(stars=stars)
    await message.answer("Напишите короткий текстовый отзыв:", reply_markup=get_cancel_reply_kb())
    await state.set_state(LeaveReview.waiting_for_text)

@router.message(LeaveReview.waiting_for_text, IsPrivate())
async def review_text(message: Message, state: FSMContext):
    data = await state.get_data()
    conn = sqlite3.connect(DB_NAME)
    
    # Сохраняем отзыв
    conn.execute("INSERT INTO reviews (reviewer_id, target_id, rating, review_text) VALUES (?, ?, ?, ?)",
                 (message.from_user.id, data['target_id'], data['stars'], message.text))
    # Обновляем стату
    conn.execute("UPDATE users SET rating_sum = rating_sum + ?, reviews_count = reviews_count + 1 WHERE tg_id = ?",
                 (data['stars'], data['target_id']))
    conn.commit()
    conn.close()
    
    await message.answer("✅ Отзыв успешно опубликован!", reply_markup=get_main_reply_kb(message.from_user.id))
    try:
        await bot.send_message(data['target_id'], f"🌟 <b>Вам оставили новый отзыв!</b>\nОценка: {data['stars']} ⭐\nОтзыв: <i>{message.text}</i>")
    except: pass
    await state.clear()

# ==========================================
# ОБРАБОТЧИКИ: КНОПКИ МЕНЮ (ЧЕКИ, СПИСКИ, ПОИСК)
# ==========================================
@router.message(F.text == "🧾 Создать чек", IsPrivate())
async def create_receipt(message: Message):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    conn.close()
    
    rating = format_rating(user['rating_sum'], user['reviews_count'])
    text = (
        f"🧾 <b>ОФИЦИАЛЬНЫЙ ЧЕК ТРЕЙДЕРА</b> 🧾\n\n"
        f"👤 <b>Пользователь:</b> @{user['username']} (ID: <code>{user['tg_id']}</code>)\n"
        f"🎮 <b>Roblox:</b> <code>{user['roblox_username']}</code>\n"
        f"✅ <b>Успешных сделок:</b> {user['trades']}\n"
        f"⭐️ <b>Рейтинг:</b> {rating}\n"
        f"⚖️ <b>Статус:</b> {user['status'].upper()}\n\n"
        f"<i>Верифицировано ботом AntiScam Pro 🛡</i>"
    )
    await message.answer(text)

@router.message(F.text == "🛡 Гаранты", IsPrivate())
async def show_garants(message: Message):
    conn = get_db()
    garants = conn.execute("SELECT username, trades, rating_sum, reviews_count FROM users WHERE status = 'garant' ORDER BY trades DESC LIMIT 15").fetchall()
    conn.close()
    
    if not garants:
        return await message.answer("😔 Пока в базе нет официальных гарантов.")
        
    text = "🛡 <b>Список Официальных Гарантов:</b>\n\n"
    for i, g in enumerate(garants, 1):
        rating = format_rating(g['rating_sum'], g['reviews_count'])
        text += f"{i}. <b>@{g['username']}</b> | Сделок: {g['trades']} | {rating}\n"
        
    await message.answer(text)

@router.message(F.text == "⚠️ Скамеры", IsPrivate())
async def show_scammers(message: Message):
    conn = get_db()
    scammers = conn.execute("SELECT username, tg_id FROM users WHERE status = 'scammer' ORDER BY join_date DESC LIMIT 15").fetchall()
    conn.close()
        
    if not scammers:
        return await message.answer("🎉 База скамеров пока пуста! Так держать.")
        
    text = "🚨 <b>Последние заблокированные (ЧС):</b>\n\n"
    for i, s in enumerate(scammers, 1):
        text += f"☠️ @{s['username']} (<code>{s['tg_id']}</code>)\n"
        
    await message.answer(text)

@router.message(F.text == "🏆 Топ", IsPrivate())
async def show_top(message: Message):
    conn = get_db()
    top_users = conn.execute("SELECT username, trades, status FROM users WHERE status != 'scammer' ORDER BY trades DESC LIMIT 10").fetchall()
    conn.close()
        
    if not top_users:
        return await message.answer("Рейтинг пока пуст.")
        
    medals = ["🥇", "🥈", "🥉"]
    text = "🏆 <b>ТОП-10 Трейдеров базы:</b>\n\n"
    for i, u in enumerate(top_users):
        medal = medals[i] if i < 3 else f"{i+1}."
        status_emoji = "👑" if u['status'] == 'garant' else "🟢"
        text += f"{medal} {status_emoji} <b>@{u['username']}</b> — {u['trades']} сделок\n"
        
    await message.answer(text)

@router.message(F.text == "🔍 Проверить", IsPrivate())
async def check_user_start(message: Message, state: FSMContext):
    await message.answer("🔍 Введите <b>@username</b> или <b>ID</b> пользователя для проверки:", reply_markup=get_cancel_reply_kb())
    await state.set_state(CheckUser.waiting_for_target)

@router.message(CheckUser.waiting_for_target, IsPrivate())
async def check_user_process(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    conn = get_db()
    if target.isdigit():
        user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else:
        user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()
    conn.close()
    
    if not user:
        await message.answer("❓ Пользователь не найден в базе.", reply_markup=get_main_reply_kb(message.from_user.id))
        return await state.clear()
        
    rating = format_rating(user['rating_sum'], user['reviews_count'])
    title = get_user_title(user['trades'])
    
    text = (
        f"📋 <b>Досье пользователя:</b>\n"
        f"👤 @{user['username']} (<code>{user['tg_id']}</code>)\n"
        f"🎮 Roblox: <code>{user['roblox_username']}</code>\n"
        f"🏆 Титул: {title}\n"
        f"🤝 Успешных сделок: <b>{user['trades']}</b>\n"
        f"⭐️ Рейтинг: {rating}\n"
    )
    if user['status'] == 'scammer':
        text = "‼️ <b>СКАМЕР ИЗ ЧС! БЕЗ СДЕЛОК!</b> ‼️\n\n" + text
    elif user['status'] == 'garant':
        text = "👑 <b>ОФИЦИАЛЬНЫЙ ГАРАНТ</b> 👑\n\n" + text
        
    await message.answer(text, reply_markup=get_main_reply_kb(message.from_user.id))
    await state.clear()

@router.message(F.text == "🛎 Вызвать Гаранта", IsPrivate())
async def call_garant_start(message: Message, state: FSMContext):
    await message.answer("🛎 Напишите условия сделки, и я отправлю их всем свободным Гарантам.\n\n<i>Пример: Я даю 1000 робуксов, мне дают пета в Adopt Me.</i>", reply_markup=get_cancel_reply_kb())
    await state.set_state(CallGarant.waiting_for_details)

@router.message(CallGarant.waiting_for_details, IsPrivate())
async def call_garant_process(message: Message, state: FSMContext):
    desc = message.text
    conn = get_db()
    garants = conn.execute("SELECT tg_id FROM users WHERE status = 'garant'").fetchall()
    conn.close()
        
    if not garants:
        await message.answer("😔 К сожалению, сейчас в базе нет доступных гарантов.", reply_markup=get_main_reply_kb(message.from_user.id))
        return await state.clear()
        
    notified = 0
    for g in garants:
        try:
            await bot.send_message(
                g['tg_id'], 
                f"🛎 <b>ВЫЗОВ ГАРАНТА</b>\nОт: @{message.from_user.username}\n\n<b>Условия:</b> <i>{desc}</i>\n\n👉 Свяжитесь с клиентом в ЛС, если готовы помочь."
            )
            notified += 1
        except: pass
        
    await message.answer(f"✅ Заявка отправлена {notified} гарантам! Ожидайте сообщения в ЛС.", reply_markup=get_main_reply_kb(message.from_user.id))
    await state.clear()

# ==========================================
# ОБРАБОТЧИКИ: ПРИВЯЗКА И ЖАЛОБЫ (ТИКЕТЫ)
# ==========================================
@router.message(F.text == "🎮 Привязать Roblox", IsPrivate())
async def bind_roblox_start(message: Message, state: FSMContext):
    await message.answer("📹 Чтобы привязать аккаунт, запишите короткое видео, где видно ваш Telegram профиль и переход в аккаунт Roblox, после чего отправьте его сюда.", reply_markup=get_cancel_reply_kb())
    await state.set_state(BindRoblox.waiting_for_video)

@router.message(BindRoblox.waiting_for_video, IsPrivate())
async def bind_roblox_process(message: Message, state: FSMContext):
    if not message.video:
        return await message.answer("Пожалуйста, отправьте именно ВИДЕО.")
        
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO tickets (user_id, type, content) VALUES (?, ?, ?)", (message.from_user.id, 'roblox_bind', message.video.file_id))
    conn.commit()
    conn.close()
    
    await message.answer("✅ Заявка отправлена администраторам на проверку!", reply_markup=get_main_reply_kb(message.from_user.id))
    try:
        await bot.send_message(ADMIN_ID, f"🔔 <b>Новая заявка на привязку Roblox!</b>\nОт: @{message.from_user.username}\nПроверьте в Админ-Панели.")
    except: pass
    await state.clear()

@router.message(F.text == "🚨 Подать жалобу", IsPrivate())
async def report_user_start(message: Message, state: FSMContext):
    await message.answer("🚨 Введите @username нарушителя:", reply_markup=get_cancel_reply_kb())
    await state.set_state(ReportUser.waiting_for_target)

@router.message(ReportUser.waiting_for_target, IsPrivate())
async def report_user_target(message: Message, state: FSMContext):
    await state.update_data(target=message.text)
    await message.answer("Теперь отправьте доказательства (текст с ссылками на видео/скрины):")
    await state.set_state(ReportUser.waiting_for_proofs)

@router.message(ReportUser.waiting_for_proofs, IsPrivate())
async def report_user_process(message: Message, state: FSMContext):
    data = await state.get_data()
    content = f"Нарушитель: {data['target']}\nПруфы: {message.text}"
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO tickets (user_id, type, content) VALUES (?, ?, ?)", (message.from_user.id, 'report', content))
    conn.commit()
    conn.close()
    
    await message.answer("✅ Жалоба зарегистрирована. Администраторы рассмотрят её в ближайшее время.", reply_markup=get_main_reply_kb(message.from_user.id))
    try:
        await bot.send_message(ADMIN_ID, f"🔔 <b>Новая жалоба!</b>\nОт: @{message.from_user.username}\nНа: {data['target']}")
    except: pass
    await state.clear()

# ==========================================
# ОБРАБОТЧИКИ: АДМИН-ПАНЕЛЬ
# ==========================================
@router.message(F.text == "👑 Админ Панель", IsAdmin())
async def admin_panel_start(message: Message):
    conn = get_db()
    tickets_roblox = conn.execute("SELECT COUNT(*) FROM tickets WHERE type='roblox_bind' AND status='pending'").fetchone()[0]
    tickets_reports = conn.execute("SELECT COUNT(*) FROM tickets WHERE type='report' AND status='pending'").fetchone()[0]
    conn.close()
    
    text = (
        "👑 <b>Enterprise Dashboard</b>\n\n"
        f"🎫 Ожидают привязки Roblox: <b>{tickets_roblox}</b>\n"
        f"🚨 Неразобранные жалобы: <b>{tickets_reports}</b>\n\n"
        "Выберите действие ниже:"
    )
    await message.answer(text, reply_markup=get_admin_main_kb())

@router.callback_query(F.data == "admin_cancel", IsAdmin())
async def admin_cancel_call(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await admin_panel_start(call.message)
    await call.answer()

@router.callback_query(F.data == "admin_stats", IsAdmin())
async def admin_stats_panel(call: CallbackQuery):
    conn = get_db()
    total_u = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_t = conn.execute("SELECT SUM(trades) FROM users").fetchone()[0] or 0
    total_s = conn.execute("SELECT COUNT(*) FROM users WHERE status='scammer'").fetchone()[0]
    garants = conn.execute("SELECT COUNT(*) FROM users WHERE status='garant'").fetchone()[0]
    conn.close()
        
    text = (
        f"📊 <b>Статистика Enterprise Bot:</b>\n\n"
        f"👥 Всего юзеров: <b>{total_u}</b>\n"
        f"🤝 Успешных сделок (всего): <b>{total_t}</b>\n"
        f"🛡 Гарантов: <b>{garants}</b>\n"
        f"☠️ Скамеров в ЧС: <b>{total_s}</b>"
    )
    await call.message.edit_text(text, reply_markup=get_admin_main_kb())
    await call.answer()

@router.callback_query(F.data == "admin_backup_db", IsAdmin())
async def admin_backup_panel(call: CallbackQuery):
    try:
        await bot.send_document(ADMIN_ID, FSInputFile(DB_NAME), caption="💾 Ручной бэкап БД.")
        await call.answer("✅ Бэкап отправлен в ЛС.")
    except Exception as e:
        await call.answer(f"❌ Ошибка: {e}", show_alert=True)

# ПОИСК ЮЗЕРА АДМИНОМ
@router.callback_query(F.data == "admin_search_user", IsAdmin())
async def admin_search_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("🔍 <b>Админ Поиск</b>\nВведите @username или Telegram ID юзера:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_cancel")]]))
    await state.set_state(AdminPanelStates.waiting_for_user_search)
    await call.answer()

@router.message(AdminPanelStates.waiting_for_user_search, IsAdmin())
async def admin_search_process(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    conn = get_db()
    if target.isdigit():
        user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else:
        user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()
    conn.close()
            
    if not user:
        return await message.answer("❌ Юзер не найден.", reply_markup=get_admin_main_kb())
        
    text = (f"👤 Юзер: @{user['username']} (ID: <code>{user['tg_id']}</code>)\n"
            f"Статус: {user['status'].upper()}\nСделок: {user['trades']}")
            
    await message.answer(text, reply_markup=get_admin_user_manage_kb(user['tg_id'], user['status']))
    await state.clear()

# ИЗМЕНЕНИЕ СТАТУСОВ АДМИНОМ
@router.callback_query(F.data.startswith("admset_") | F.data.startswith("admadd_") | F.data.startswith("admclr_"), IsAdmin())
async def admin_manage_user(call: CallbackQuery):
    action, target_id = call.data.split("_")[1], int(call.data.split("_")[2])
    conn = sqlite3.connect(DB_NAME)
    
    if action == "scammer":
        conn.execute("UPDATE users SET status = 'scammer' WHERE tg_id = ?", (target_id,))
        text = "☠️ Юзер добавлен в ЧС!"
    elif action == "garant":
        conn.execute("UPDATE users SET status = 'garant' WHERE tg_id = ?", (target_id,))
        text = "👑 Юзер теперь Гарант!"
    elif action == "user":
        conn.execute("UPDATE users SET status = 'user' WHERE tg_id = ?", (target_id,))
        text = "👤 Статус сброшен на Обычный."
    elif action == "trade":
        conn.execute("UPDATE users SET trades = trades + 1 WHERE tg_id = ?", (target_id,))
        text = "✅ Начислена 1 сделка."
    elif action == "stats":
        conn.execute("UPDATE users SET trades = 0, rating_sum = 0, reviews_count = 0 WHERE tg_id = ?", (target_id,))
        text = "🧹 Вся статистика очищена."
        
    conn.commit()
    conn.close()
    await call.answer(text, show_alert=True)
    await admin_panel_start(call.message)

# ТИКЕТЫ: ПРОСМОТР
@router.callback_query(F.data.startswith("admin_tickets_"), IsAdmin())
async def admin_view_tickets(call: CallbackQuery, state: FSMContext):
    t_type = call.data.split("_")[2]
    db_type = 'roblox_bind' if t_type == 'roblox' else 'report'
    
    conn = get_db()
    ticket = conn.execute("SELECT * FROM tickets WHERE type=? AND status='pending' LIMIT 1", (db_type,)).fetchone()
    conn.close()
    
    if not ticket:
        return await call.answer("✅ Новых тикетов в этой категории нет.", show_alert=True)
        
    await state.set_state(AdminPanelStates.target_ticket_id)
    await state.update_data(ticket_id=ticket['id'], user_id=ticket['user_id'])
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить / Ответить", callback_data=f"adm_t_accept_{ticket['id']}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_t_decline_{ticket['id']}")]
    ])
    
    if db_type == 'roblox_bind':
        await bot.send_video(ADMIN_ID, ticket['content'], caption=f"🎫 <b>Привязка Roblox</b>\nОт ID: {ticket['user_id']}", reply_markup=kb)
    else:
        await bot.send_message(ADMIN_ID, f"🚨 <b>Жалоба</b>\nОт ID: {ticket['user_id']}\n\n{ticket['content']}", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("adm_t_decline_"), IsAdmin())
async def admin_ticket_decline(call: CallbackQuery):
    t_id = int(call.data.split("_")[3])
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE tickets SET status='closed' WHERE id=?", (t_id,))
    conn.commit()
    conn.close()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("❌ Тикет закрыт.")

@router.callback_query(F.data.startswith("adm_t_accept_"), IsAdmin())
async def admin_ticket_accept(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    t_id = data.get('ticket_id')
    user_id = data.get('user_id')
    
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE tickets SET status='closed' WHERE id=?", (t_id,))
    conn.commit()
    conn.close()
    
    await call.message.edit_reply_markup(reply_markup=None)
    # Если это привязка роблокса, просим ввести ник
    await call.message.answer("Введите Ник и ID Roblox через пробел (Пример: Masha2010 1234567):")
    await state.set_state(AdminPanelStates.waiting_for_roblox_data)

@router.message(AdminPanelStates.waiting_for_roblox_data, IsAdmin())
async def admin_save_roblox_data(message: Message, state: FSMContext):
    try:
        rbx_nick, rbx_id = message.text.split()
        data = await state.get_data()
        target_id = data.get('user_id')
        
        conn = sqlite3.connect(DB_NAME)
        conn.execute("UPDATE users SET roblox_username = ?, roblox_id = ? WHERE tg_id = ?", (rbx_nick, rbx_id, target_id))
        conn.commit()
        conn.close()
        
        await message.answer("✅ Данные привязаны!", reply_markup=get_admin_main_kb())
        try:
            await bot.send_message(target_id, f"🎉 <b>Roblox успешно привязан!</b>\nНик: {rbx_nick}\nID: {rbx_id}")
        except: pass
        await state.clear()
    except Exception:
        await message.answer("❌ Ошибка формата. Нужно: Ник ID")

# ==========================================
# ЗАЩИТА ГРУПП (GROUP SHIELD)
# ==========================================
@router.message(~IsPrivate())
async def group_shield_handler(message: Message):
    if not message.from_user: return
    
    conn = get_db()
    user = conn.execute("SELECT status FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    conn.close()
    
    if user and user['status'] == 'scammer':
        try:
            await message.delete()
            # Можно добавить await message.chat.ban(message.from_user.id) для полного бана
        except: pass # Бот не имеет прав администратора в группе

# ==========================================
# ЗАПУСК БОТА И АВТО-БЭКАПЫ
# ==========================================
async def auto_backup():
    while True:
        await asyncio.sleep(3600) # Каждый час
        try:
            await bot.send_document(ADMIN_ID, FSInputFile(DB_NAME), caption="💾 Автоматический ежечасный бэкап БД.")
        except: pass

async def main():
    dp.include_router(router)
    print("Бот успешно запущен!")
    # Запускаем авто-бэкап как фоновую задачу
    asyncio.create_task(auto_backup())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
