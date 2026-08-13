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
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, ChatPermissions
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

# ==========================================
# НАСТРОЙКИ БОТА
# ==========================================
BOT_TOKEN = "8792564218:AAHo3taU03G4FGAtIovL6mdSNXRA72QrtE0" # <-- Вставь сюда токен
ADMIN_ID = 5341904332 # <-- Вставь сюда свой Telegram ID (числами)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

# ==========================================
# БАЗА ДАННЫХ И УМНЫЕ МИГРАЦИИ
# ==========================================
DB_NAME = "antiscam_pro.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # 1. Основная таблица пользователей (с новыми полями role и badges)
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
            join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            role TEXT DEFAULT 'user',
            badges TEXT DEFAULT ''
        )
    ''')
    
    # Умная миграция: добавление новых колонок, если их нет в старой БД
    cursor.execute("PRAGMA table_info(users)")
    columns = [info[1] for info in cursor.fetchall()]
    if 'role' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
    if 'badges' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN badges TEXT DEFAULT ''")
    
    # 2. Таблица логов P2P сделок
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades_p2p (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            initiator_id INTEGER,
            partner_id INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 3. Таблица отзывов
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
    
    # 4. Таблица тикетов (привязки и жалобы)
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
    
    # 5. Таблица логов аудита (Для безопасности: кто из админов/модеров что делал)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER,
            action TEXT,
            target_id INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Выдаем права Супер-Админа владельцу при старте
    cursor.execute("SELECT * FROM users WHERE tg_id = ?", (ADMIN_ID,))
    if cursor.fetchone():
        cursor.execute("UPDATE users SET role = 'admin' WHERE tg_id = ?", (ADMIN_ID,))
    else:
        cursor.execute("INSERT INTO users (tg_id, username, role) VALUES (?, ?, ?)", (ADMIN_ID, "Owner", "admin"))
        
    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def log_audit(staff_id, action, target_id=0):
    conn = get_db()
    conn.execute("INSERT INTO audit_log (staff_id, action, target_id) VALUES (?, ?, ?)", (staff_id, action, target_id))
    conn.commit()
    conn.close()

# ==========================================
# ФИЛЬТРЫ ДОСТУПА (АДМИНЫ И МОДЕРАТОРЫ)
# ==========================================
class IsSuperAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id == ADMIN_ID

class IsModOrAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if message.from_user.id == ADMIN_ID:
            return True
        conn = get_db()
        user = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
        conn.close()
        return user and user['role'] in ['admin', 'moderator']

class IsPrivate(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.chat.type == "private"

# ==========================================
# СИСТЕМА ДОСТИЖЕНИЙ И ФОРМАТИРОВАНИЕ
# ==========================================
ACHIEVEMENTS = {
    "first_trade": "🤝 Первая Кровь",
    "ten_trades": "💼 Делец (10 сделок)",
    "fifty_trades": "🛡 Железный Трейдер (50 сделок)",
    "flawless": "🌟 Безупречный (Рейтинг 5.0 при 10+ отзывах)",
    "garant": "⚖️ Официальный Гарант"
}

async def check_achievements(user_id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    user = conn.execute("SELECT trades, status, rating_sum, reviews_count, badges FROM users WHERE tg_id = ?", (user_id,)).fetchone()
    
    if not user:
        return
        
    current_badges = user['badges'].split(',') if user['badges'] else []
    new_badges = []
    
    if user['trades'] >= 1 and "first_trade" not in current_badges:
        new_badges.append("first_trade")
    if user['trades'] >= 10 and "ten_trades" not in current_badges:
        new_badges.append("ten_trades")
    if user['trades'] >= 50 and "fifty_trades" not in current_badges:
        new_badges.append("fifty_trades")
        
    avg = user['rating_sum'] / user['reviews_count'] if user['reviews_count'] > 0 else 0
    if avg == 5.0 and user['reviews_count'] >= 10 and "flawless" not in current_badges:
        new_badges.append("flawless")
        
    if user['status'] == 'garant' and "garant" not in current_badges:
        new_badges.append("garant")

    if new_badges:
        updated_badges = current_badges + new_badges
        badges_str = ",".join(updated_badges)
        conn.execute("UPDATE users SET badges = ? WHERE tg_id = ?", (badges_str, user_id))
        conn.commit()
        # Уведомляем пользователя о новых наградах
        for badge in new_badges:
            try:
                await bot.send_message(user_id, f"🏆 <b>Новое достижение разблокировано!</b>\nВы получили бейдж: <b>{ACHIEVEMENTS[badge]}</b>\n\n<i>Теперь он будет отображаться в вашем профиле и чеках!</i>")
            except: pass
            
    conn.close()

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
    if trades >= 100: return "[██████████] MAX"
    percent = trades % 10
    return f"[{'█' * percent}{'░' * (10 - percent)}] {percent * 10}% до некст ранга"

# ==========================================
# КЛАВИАТУРЫ
# ==========================================
def get_main_reply_kb(user_role):
    builder = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🤝 Создать сделку")],
        [KeyboardButton(text="⭐ Оставить отзыв"), KeyboardButton(text="🧾 Создать чек")],
        [KeyboardButton(text="🛡 Гаранты"), KeyboardButton(text="⚠️ Скамеры"), KeyboardButton(text="🏆 Топ")],
        [KeyboardButton(text="🔍 Проверить"), KeyboardButton(text="🛎 Вызвать Гаранта")],
        [KeyboardButton(text="🎮 Привязать Roblox"), KeyboardButton(text="🚨 Подать жалобу")]
    ], resize_keyboard=True)
    
    if user_role in ['admin', 'moderator']:
        builder.keyboard.append([KeyboardButton(text="👑 Админ Панель")])
        
    return builder

def get_cancel_reply_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)

def get_admin_main_kb(is_superadmin=False):
    kb = [
        [InlineKeyboardButton(text="🎫 Заявки Roblox", callback_data="admin_tickets_roblox"),
         InlineKeyboardButton(text="🚨 Жалобы", callback_data="admin_tickets_reports")],
        [InlineKeyboardButton(text="🔍 Управление юзером", callback_data="admin_search_user"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")]
    ]
    if is_superadmin:
        kb.append([InlineKeyboardButton(text="🛡 Управление Персоналом", callback_data="admin_staff_manage")])
        kb.append([InlineKeyboardButton(text="📢 Глобальная Рассылка", callback_data="admin_broadcast")])
        kb.append([InlineKeyboardButton(text="💾 Скачать бэкап БД", callback_data="admin_backup_db")])
        
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_user_manage_kb(target_id, current_status, current_role, is_superadmin=False):
    buttons = []
    # Модераторы могут выдавать ЧС, сбрасывать статусы и накручивать сделки.
    if current_status != 'scammer':
        buttons.append([InlineKeyboardButton(text="☠️ В ЧС (Скам)", callback_data=f"admset_scammer_{target_id}")])
    if current_status != 'garant':
        buttons.append([InlineKeyboardButton(text="⚖️ Выдать Гаранта", callback_data=f"admset_garant_{target_id}")])
    if current_status != 'user':
        buttons.append([InlineKeyboardButton(text="👤 Обычный юзер", callback_data=f"admset_user_{target_id}")])
        
    buttons.append([
        InlineKeyboardButton(text="➕ 1 Сделка", callback_data=f"admadd_trade_{target_id}"),
        InlineKeyboardButton(text="🧹 Обнулить", callback_data=f"admclr_stats_{target_id}")
    ])
    
    # Только супер-админ может менять РОЛИ (нанимать модеров)
    if is_superadmin:
        if current_role == 'user':
            buttons.append([InlineKeyboardButton(text="👔 Назначить Модератором", callback_data=f"admrole_moderator_{target_id}")])
        elif current_role == 'moderator':
            buttons.append([InlineKeyboardButton(text="📉 Разжаловать Модератора", callback_data=f"admrole_user_{target_id}")])
            
    buttons.append([InlineKeyboardButton(text="◀️ Назад в Админку", callback_data="admin_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==========================================
# МАШИНА СОСТОЯНИЙ (FSM)
# ==========================================
class AppStates(StatesGroup):
    waiting_for_p2p_partner = State()
    waiting_for_review_target = State()
    waiting_for_review_stars = State()
    waiting_for_review_text = State()
    waiting_for_check_target = State()
    waiting_for_garant_details = State()
    waiting_for_bind_video = State()
    waiting_for_report_target = State()
    waiting_for_report_proofs = State()
    
    # Админские стейты
    admin_waiting_user_search = State()
    admin_waiting_roblox_data = State()
    admin_waiting_broadcast_msg = State()
    admin_waiting_staff_search = State()
    target_ticket_id = None

# ==========================================
# ОБРАБОТЧИКИ: БАЗОВЫЕ (СТАРТ, ОТМЕНА, ПРОФИЛЬ)
# ==========================================
@router.message(Command("start"), IsPrivate())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    if not user:
        conn.execute("INSERT INTO users (tg_id, username) VALUES (?, ?)", 
                     (message.from_user.id, message.from_user.username or "Без_Ника"))
        conn.commit()
        role = "user"
    else:
        role = user['role']
        # Обновляем юзернейм на случай, если он поменялся
        conn.execute("UPDATE users SET username = ? WHERE tg_id = ?", (message.from_user.username or "Без_Ника", message.from_user.id))
        conn.commit()
    conn.close()
    
    await message.answer(
        f"👋 Добро пожаловать, <b>{message.from_user.first_name}</b>!\n\n"
        f"🛡 <b>AntiScam Enterprise</b> — официальная база данных репутации.\n"
        f"Здесь вы можете фиксировать свои сделки, искать гарантов и проверять продавцов.\n\n"
        f"Используйте кнопки ниже 👇",
        reply_markup=get_main_reply_kb(role)
    )

@router.message(F.text == "❌ Отмена", IsPrivate())
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    conn = get_db()
    user = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    conn.close()
    role = user['role'] if user else 'user'
    await message.answer("Действие отменено.", reply_markup=get_main_reply_kb(role))

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
    if user['status'] == 'garant': status_text = "⚖️ Официальный Гарант"
    elif user['status'] == 'scammer': status_text = "☠️ В ЧЁРНОМ СПИСКЕ (СКАМ)"

    badges_list = user['badges'].split(',') if user['badges'] else []
    badges_display = "\n".join([f"🏅 {ACHIEVEMENTS.get(b, b)}" for b in badges_list if b in ACHIEVEMENTS])
    if not badges_display: badges_display = "<i>Пока нет достижений</i>"

    text = (
        f"👤 <b>Ваш Профиль:</b>\n"
        f"🔖 <b>ID:</b> <code>{user['tg_id']}</code>\n"
        f"🎮 <b>Roblox:</b> <code>{user['roblox_username']}</code> (ID: {user['roblox_id']})\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏆 <b>Титул:</b> {title}\n"
        f"⚖️ <b>Статус:</b> {status_text}\n"
        f"🤝 <b>Сделок:</b> {user['trades']}\n"
        f"⭐️ <b>Рейтинг:</b> {rating}\n"
        f"📊 <b>Прогресс до ранга:</b>\n<code>{progress}</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎖 <b>Ваши Достижения:</b>\n{badges_display}"
    )
    await message.answer(text)

@router.message(F.text == "🧾 Создать чек", IsPrivate())
async def create_receipt(message: Message):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    conn.close()
    
    rating = format_rating(user['rating_sum'], user['reviews_count'])
    badges_list = user['badges'].split(',') if user['badges'] else []
    badges_display = " | ".join([ACHIEVEMENTS.get(b, '').split()[0] for b in badges_list if b in ACHIEVEMENTS]) # Только эмодзи
    
    text = (
        f"🧾 <b>ОФИЦИАЛЬНЫЙ ЧЕК ТРЕЙДЕРА</b> 🧾\n\n"
        f"👤 <b>Пользователь:</b> @{user['username']} (ID: <code>{user['tg_id']}</code>)\n"
        f"🎮 <b>Roblox:</b> <code>{user['roblox_username']}</code>\n"
        f"✅ <b>Успешных сделок:</b> {user['trades']}\n"
        f"⭐️ <b>Рейтинг:</b> {rating}\n"
        f"⚖️ <b>Статус:</b> {user['status'].upper()}\n"
    )
    if badges_display:
        text += f"🎖 <b>Награды:</b> {badges_display}\n"
        
    text += f"\n<i>✅ Верифицировано ботом AntiScam Enterprise</i>"
    await message.answer(text)

# ==========================================
# ОБРАБОТЧИКИ: P2P СДЕЛКИ
# ==========================================
@router.message(F.text == "🤝 Создать сделку", IsPrivate())
async def p2p_trade_start(message: Message, state: FSMContext):
    await message.answer("🤝 Введите <b>@username</b> или <b>ID</b> человека, с которым вы успешно провели сделку.\n\nЕму придет запрос. Как только он нажмет «Подтвердить», вам обоим засчитается сделка!", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_p2p_partner)

@router.message(AppStates.waiting_for_p2p_partner, IsPrivate())
async def p2p_trade_process(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    conn = get_db()
    if target.isdigit():
        partner = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else:
        partner = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()
        
    if not partner:
        conn.close()
        return await message.answer("❌ Пользователь не найден. Попросите его запустить бота.")
    if partner['tg_id'] == message.from_user.id:
        conn.close()
        return await message.answer("❌ Нельзя провести сделку с самим собой.")

    cursor = conn.cursor()
    cursor.execute("INSERT INTO trades_p2p (initiator_id, partner_id) VALUES (?, ?)", (message.from_user.id, partner['tg_id']))
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить сделку", callback_data=f"p2p_accept_{trade_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"p2p_decline_{trade_id}")]
    ])
    
    try:
        await bot.send_message(partner['tg_id'], f"🤝 <b>Новый запрос на сделку!</b>\nПользователь @{message.from_user.username} утверждает, что провел с вами успешный трейд.\nПодтверждаете?", reply_markup=kb)
        await message.answer("✅ Запрос отправлен партнеру. Ожидайте подтверждения.", reply_markup=get_main_reply_kb('user'))
    except:
        await message.answer("❌ Не удалось отправить сообщение партнеру (бот заблокирован).", reply_markup=get_main_reply_kb('user'))
    
    await state.clear()

@router.callback_query(F.data.startswith("p2p_accept_"))
async def p2p_accept(call: CallbackQuery):
    trade_id = int(call.data.split("_")[2])
    conn = sqlite3.connect(DB_NAME)
    trade = conn.execute("SELECT * FROM trades_p2p WHERE id = ?", (trade_id,)).fetchone()
    
    if not trade or trade[3] != 'pending': 
        conn.close()
        return await call.answer("❌ Сделка уже обработана.", show_alert=True)
        
    conn.execute("UPDATE users SET trades = trades + 1 WHERE tg_id IN (?, ?)", (trade[1], trade[2]))
    conn.execute("UPDATE trades_p2p SET status = 'accepted' WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()
    
    await call.message.edit_text("✅ Сделка подтверждена! Вам обоим начислена +1 сделка в статистику.")
    try:
        await bot.send_message(trade[1], "🎉 Партнер подтвердил сделку! Вам начислена +1 сделка.")
    except: pass
    
    # Проверка на выдачу ачивок
    await check_achievements(trade[1])
    await check_achievements(trade[2])

@router.callback_query(F.data.startswith("p2p_decline_"))
async def p2p_decline(call: CallbackQuery):
    trade_id = int(call.data.split("_")[2])
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE trades_p2p SET status = 'declined' WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()
    await call.message.edit_text("❌ Вы отклонили сделку.")

# ==========================================
# ОБРАБОТЧИКИ: ОТЗЫВЫ И РЕЙТИНГИ
# ==========================================
@router.message(F.text == "⭐ Оставить отзыв", IsPrivate())
async def review_start(message: Message, state: FSMContext):
    await message.answer("Введи @username или ID Гаранта, которому хочешь оставить отзыв:\n\n<i>Отзывы можно оставлять только официальным гарантам проекта.</i>", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_review_target)

@router.message(AppStates.waiting_for_review_target, IsPrivate())
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
    if user['status'] != 'garant':
        return await message.answer("❌ Этот пользователь не является официальным Гарантом. Отзывы можно оставлять только им.")
    if user['tg_id'] == message.from_user.id:
        return await message.answer("❌ Нельзя оставить отзыв самому себе.")
        
    await state.update_data(target_id=user['tg_id'])
    
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="1 ⭐"), KeyboardButton(text="2 ⭐"), KeyboardButton(text="3 ⭐")],
        [KeyboardButton(text="4 ⭐"), KeyboardButton(text="5 ⭐")],
        [KeyboardButton(text="❌ Отмена")]
    ], resize_keyboard=True)
    
    await message.answer(f"Какую оценку поставите гаранту @{user['username']}?", reply_markup=kb)
    await state.set_state(AppStates.waiting_for_review_stars)

@router.message(AppStates.waiting_for_review_stars, IsPrivate())
async def review_stars(message: Message, state: FSMContext):
    if "⭐" not in message.text:
        return await message.answer("Пожалуйста, используйте кнопки ниже.")
        
    stars = int(message.text.split()[0])
    await state.update_data(stars=stars)
    await message.answer("Напишите короткий текстовый комментарий к сделке (или отправьте '-' если нет текста):", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_review_text)

@router.message(AppStates.waiting_for_review_text, IsPrivate())
async def review_text(message: Message, state: FSMContext):
    data = await state.get_data()
    conn = sqlite3.connect(DB_NAME)
    
    conn.execute("INSERT INTO reviews (reviewer_id, target_id, rating, review_text) VALUES (?, ?, ?, ?)",
                 (message.from_user.id, data['target_id'], data['stars'], message.text))
    conn.execute("UPDATE users SET rating_sum = rating_sum + ?, reviews_count = reviews_count + 1 WHERE tg_id = ?",
                 (data['stars'], data['target_id']))
    
    # Получаем роль пользователя для правильной клавиатуры
    user = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    role = user[0] if user else 'user'
    
    conn.commit()
    conn.close()
    
    await message.answer("✅ Отзыв успешно опубликован! Спасибо за вклад в безопасность комьюнити.", reply_markup=get_main_reply_kb(role))
    try:
        await bot.send_message(data['target_id'], f"🌟 <b>Вам оставили новый отзыв!</b>\nОценка: {data['stars']} ⭐\nКомментарий: <i>{message.text}</i>")
    except: pass
    
    await check_achievements(data['target_id'])
    await state.clear()

# ==========================================
# ОБРАБОТЧИКИ: СПИСКИ, ТОПЫ, ВЫЗОВ
# ==========================================
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
        
    medals = ["🥇", "🥈", "🥉", "4.", "5.", "6.", "7.", "8.", "9.", "10."]
    text = "🏆 <b>ТОП-10 Трейдеров базы:</b>\n\n"
    for i, u in enumerate(top_users):
        medal = medals[i]
        status_emoji = "⚖️" if u['status'] == 'garant' else "🟢"
        text += f"{medal} {status_emoji} <b>@{u['username']}</b> — {u['trades']} сделок\n"
        
    await message.answer(text)

@router.message(F.text == "🛎 Вызвать Гаранта", IsPrivate())
async def call_garant_start(message: Message, state: FSMContext):
    await message.answer("🛎 Напишите условия сделки, и я отправлю их всем свободным Гарантам.\n\n<i>Пример: Я даю 1000 робуксов, мне дают пета в Adopt Me.</i>", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_garant_details)

@router.message(AppStates.waiting_for_garant_details, IsPrivate())
async def call_garant_process(message: Message, state: FSMContext):
    desc = message.text
    conn = get_db()
    garants = conn.execute("SELECT tg_id FROM users WHERE status = 'garant'").fetchall()
    
    user = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    role = user[0] if user else 'user'
    conn.close()
        
    if not garants:
        await message.answer("😔 К сожалению, сейчас в базе нет доступных гарантов.", reply_markup=get_main_reply_kb(role))
        return await state.clear()
        
    notified = 0
    for g in garants:
        try:
            await bot.send_message(
                g['tg_id'], 
                f"🛎 <b>СРОЧНЫЙ ВЫЗОВ ГАРАНТА</b>\nОт: @{message.from_user.username}\n\n<b>Условия:</b> <i>{desc}</i>\n\n👉 Свяжитесь с клиентом в ЛС, если готовы взять заказ."
            )
            notified += 1
        except: pass
        
    await message.answer(f"✅ Заявка разлетелась {notified} гарантам! Ожидайте сообщения в ЛС.", reply_markup=get_main_reply_kb(role))
    await state.clear()

# ==========================================
# ОБРАБОТЧИКИ: ПРИВЯЗКА И ЖАЛОБЫ
# ==========================================
@router.message(F.text == "🎮 Привязать Roblox", IsPrivate())
async def bind_roblox_start(message: Message, state: FSMContext):
    await message.answer("📹 Чтобы привязать аккаунт, запишите короткое видео, где видно ваш Telegram профиль и переход в аккаунт Roblox, после чего отправьте его сюда.", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_bind_video)

@router.message(AppStates.waiting_for_bind_video, IsPrivate())
async def bind_roblox_process(message: Message, state: FSMContext):
    if not message.video:
        return await message.answer("Пожалуйста, отправьте именно ВИДЕО файл.")
        
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO tickets (user_id, type, content) VALUES (?, ?, ?)", (message.from_user.id, 'roblox_bind', message.video.file_id))
    conn.commit()
    
    user = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    role = user[0] if user else 'user'
    conn.close()
    
    await message.answer("✅ Видео отправлено модераторам на проверку. Мы привяжем аккаунт, как только посмотрим его!", reply_markup=get_main_reply_kb(role))
    await state.clear()

@router.message(F.text == "🚨 Подать жалобу", IsPrivate())
async def report_user_start(message: Message, state: FSMContext):
    await message.answer("🚨 Введите @username нарушителя:", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_report_target)

@router.message(AppStates.waiting_for_report_target, IsPrivate())
async def report_user_target(message: Message, state: FSMContext):
    await state.update_data(target=message.text)
    await message.answer("Теперь отправьте доказательства (Скриншоты переписки / ссылку на видео / чек перевода):")
    await state.set_state(AppStates.waiting_for_report_proofs)

@router.message(AppStates.waiting_for_report_proofs, IsPrivate())
async def report_user_process(message: Message, state: FSMContext):
    data = await state.get_data()
    content = f"Нарушитель: {data['target']}\nПруфы: {message.text}"
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO tickets (user_id, type, content) VALUES (?, ?, ?)", (message.from_user.id, 'report', content))
    conn.commit()
    
    user = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    role = user[0] if user else 'user'
    conn.close()
    
    await message.answer("✅ Ваша жалоба зафиксирована и передана в арбитраж. Спасибо за бдительность!", reply_markup=get_main_reply_kb(role))
    await state.clear()

# ==========================================
# ОБРАБОТЧИКИ: ЗАЩИТА ГРУПП (GROUP SHIELD) И КОМАНДЫ
# ==========================================
@router.message(Command("check"))
@router.message(F.text == "🔍 Проверить")
async def check_user_start(message: Message, state: FSMContext):
    if message.chat.type != "private":
        # Если вызвано в группе через /check @username
        parts = message.text.split()
        if len(parts) < 2:
            return await message.reply("Использование: <code>/check @username</code> или <code>/check ID</code>")
        target = parts[1].replace("@", "")
        return await send_check_result(message, target)
        
    await message.answer("🔍 Введите <b>@username</b> или <b>ID</b> пользователя для проверки:", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_check_target)

@router.message(AppStates.waiting_for_check_target, IsPrivate())
async def check_user_process(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    await send_check_result(message, target, state)

async def send_check_result(message: Message, target: str, state: FSMContext = None):
    conn = get_db()
    if target.isdigit():
        user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else:
        user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()
        
    if not user:
        conn.close()
        text = "❓ <b>Пользователь не найден в базе.</b>\nБудьте осторожны при сделках с ноунеймами."
        if state:
            role = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()[0]
            await message.answer(text, reply_markup=get_main_reply_kb(role))
            return await state.clear()
        else:
            return await message.reply(text)
    
    conn.close()
    
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
        text = "‼️ <b>ВНИМАНИЕ! СКАМЕР ИЗ ЧС! БЕЗ СДЕЛОК!</b> ‼️\n\n" + text
    elif user['status'] == 'garant':
        text = "⚖️ <b>ОФИЦИАЛЬНЫЙ ГАРАНТ</b> ⚖️\n\n" + text
        
    if state:
        conn = get_db()
        role = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()[0]
        conn.close()
        await message.answer(text, reply_markup=get_main_reply_kb(role))
        await state.clear()
    else:
        await message.reply(text)

# АВТО-ЗАЩИТА В ЧАТАХ (Удаление спама от скамеров)
@router.message(F.chat.type.in_({'group', 'supergroup'}))
async def group_shield_handler(message: Message):
    if not message.from_user: return
    
    # 1. Записываем юзера в базу, если его нет (пассивный сбор)
    conn = get_db()
    user = conn.execute("SELECT status, username FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    
    if not user:
        conn.execute("INSERT INTO users (tg_id, username) VALUES (?, ?)", (message.from_user.id, message.from_user.username or "Без_Ника"))
        conn.commit()
    elif user['username'] != message.from_user.username:
        conn.execute("UPDATE users SET username = ? WHERE tg_id = ?", (message.from_user.username, message.from_user.id))
        conn.commit()
        
    # 2. Если это скамер, караем
    if user and user['status'] == 'scammer':
        try:
            await message.delete() # Удаляем его сообщение
            # Пытаемся забанить, если бот - админ
            await message.chat.ban_sender_chat(message.from_user.id) 
            await message.chat.restrict(message.from_user.id, ChatPermissions(can_send_messages=False))
            await message.answer(f"🚨 <b>СКАМЕР ОБНАРУЖЕН И УСТРАНЕН!</b>\nУчастник @{message.from_user.username} числится в Черном Списке.")
        except:
            # Если у бота нет прав удалять/банить, просто предупреждаем чат
            await message.answer(f"⚠️ <b>ВНИМАНИЕ ЧАТУ!</b>\nЭтот участник (@{message.from_user.username}) находится в глобальной базе скамеров! Выдайте боту права администратора для авто-бана.")
    conn.close()

# ==========================================
# ОБРАБОТЧИКИ: АДМИН-ПАНЕЛЬ (STAFF & ADMIN)
# ==========================================
@router.message(F.text == "👑 Админ Панель", IsModOrAdmin())
async def admin_panel_start(message: Message):
    conn = get_db()
    user = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    is_super = user['role'] == 'admin'
    
    tickets_roblox = conn.execute("SELECT COUNT(*) FROM tickets WHERE type='roblox_bind' AND status='pending'").fetchone()[0]
    tickets_reports = conn.execute("SELECT COUNT(*) FROM tickets WHERE type='report' AND status='pending'").fetchone()[0]
    conn.close()
    
    role_name = "Super Admin" if is_super else "Moderator"
    
    text = (
        f"👑 <b>Enterprise Dashboard</b> [{role_name}]\n\n"
        f"🎫 Ожидают привязки Roblox: <b>{tickets_roblox}</b>\n"
        f"🚨 Неразобранные жалобы: <b>{tickets_reports}</b>\n\n"
        "Выберите действие ниже:"
    )
    await message.answer(text, reply_markup=get_admin_main_kb(is_super))

@router.callback_query(F.data == "admin_cancel", IsModOrAdmin())
async def admin_cancel_call(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await admin_panel_start(call.message)
    await call.answer()

@router.callback_query(F.data == "admin_stats", IsModOrAdmin())
async def admin_stats_panel(call: CallbackQuery):
    conn = get_db()
    total_u = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_t = conn.execute("SELECT SUM(trades) FROM users").fetchone()[0] or 0
    total_s = conn.execute("SELECT COUNT(*) FROM users WHERE status='scammer'").fetchone()[0]
    garants = conn.execute("SELECT COUNT(*) FROM users WHERE status='garant'").fetchone()[0]
    mods = conn.execute("SELECT COUNT(*) FROM users WHERE role='moderator'").fetchone()[0]
    
    is_super = conn.execute("SELECT role FROM users WHERE tg_id = ?", (call.from_user.id,)).fetchone()[0] == 'admin'
    conn.close()
        
    text = (
        f"📊 <b>Статистика Enterprise Bot:</b>\n\n"
        f"👥 Всего юзеров: <b>{total_u}</b>\n"
        f"🤝 Сделок (всего): <b>{total_t}</b>\n"
        f"🛡 Гарантов: <b>{garants}</b>\n"
        f"👮‍♂️ Модераторов: <b>{mods}</b>\n"
        f"☠️ Скамеров в ЧС: <b>{total_s}</b>"
    )
    await call.message.edit_text(text, reply_markup=get_admin_main_kb(is_super))
    await call.answer()

# ПОИСК ЮЗЕРА АДМИНОМ / МОДЕРОМ
@router.callback_query(F.data == "admin_search_user", IsModOrAdmin())
async def admin_search_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("🔍 <b>Админ Поиск</b>\nВведите @username или Telegram ID юзера:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_cancel")]]))
    await state.set_state(AppStates.admin_waiting_user_search)
    await call.answer()

@router.message(AppStates.admin_waiting_user_search, IsModOrAdmin())
async def admin_search_process(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    conn = get_db()
    if target.isdigit():
        user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else:
        user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()
        
    staff = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    is_super = staff['role'] == 'admin'
    conn.close()
            
    if not user:
        return await message.answer("❌ Юзер не найден в БД.", reply_markup=get_admin_main_kb(is_super))
        
    text = (f"👤 Юзер: @{user['username']} (ID: <code>{user['tg_id']}</code>)\n"
            f"Статус: <b>{user['status'].upper()}</b> | Роль: <b>{user['role'].upper()}</b>\n"
            f"Сделок: {user['trades']} | Рейтинг: {user['rating_sum']}/{user['reviews_count']}")
            
    await message.answer(text, reply_markup=get_admin_user_manage_kb(user['tg_id'], user['status'], user['role'], is_super))
    await state.clear()

# ИЗМЕНЕНИЕ СТАТУСОВ АДМИНОМ/МОДЕРОМ
@router.callback_query(F.data.startswith("admset_") | F.data.startswith("admadd_") | F.data.startswith("admclr_") | F.data.startswith("admrole_"), IsModOrAdmin())
async def admin_manage_user(call: CallbackQuery):
    parts = call.data.split("_")
    action_type = parts[0]
    action = parts[1]
    target_id = int(parts[2])
    
    conn = sqlite3.connect(DB_NAME)
    staff = conn.execute("SELECT role FROM users WHERE tg_id = ?", (call.from_user.id,)).fetchone()
    is_super = staff[0] == 'admin'
    
    if action_type == "admrole" and not is_super:
        conn.close()
        return await call.answer("⛔ У вас нет прав изменять роли персонала.", show_alert=True)
    
    if action_type == "admset":
        conn.execute("UPDATE users SET status = ? WHERE tg_id = ?", (action, target_id))
        text = f"✅ Статус изменен на {action}."
        log_audit(call.from_user.id, f"Set status {action}", target_id)
        if action == 'garant': await check_achievements(target_id)
            
    elif action_type == "admadd":
        conn.execute("UPDATE users SET trades = trades + 1 WHERE tg_id = ?", (target_id,))
        text = "✅ Начислена 1 сделка."
        log_audit(call.from_user.id, "Added 1 trade", target_id)
        await check_achievements(target_id)
        
    elif action_type == "admclr":
        conn.execute("UPDATE users SET trades = 0, rating_sum = 0, reviews_count = 0, badges = '' WHERE tg_id = ?", (target_id,))
        text = "🧹 Статистика и ачивки очищены."
        log_audit(call.from_user.id, "Cleared stats", target_id)
        
    elif action_type == "admrole":
        conn.execute("UPDATE users SET role = ? WHERE tg_id = ?", (action, target_id))
        text = f"👔 Роль изменена на {action}."
        log_audit(call.from_user.id, f"Changed role to {action}", target_id)
        
    conn.commit()
    conn.close()
    
    await call.answer(text, show_alert=True)
    await call.message.delete()
    await admin_panel_start(call.message)

# ТИКЕТЫ: ПРОСМОТР И ОБРАБОТКА
@router.callback_query(F.data.startswith("admin_tickets_"), IsModOrAdmin())
async def admin_view_tickets(call: CallbackQuery, state: FSMContext):
    t_type = call.data.split("_")[2]
    db_type = 'roblox_bind' if t_type == 'roblox' else 'report'
    
    conn = get_db()
    ticket = conn.execute("SELECT * FROM tickets WHERE type=? AND status='pending' LIMIT 1", (db_type,)).fetchone()
    conn.close()
    
    if not ticket:
        return await call.answer("✅ Новых тикетов в этой категории нет.", show_alert=True)
        
    await state.set_state(AppStates.target_ticket_id)
    await state.update_data(ticket_id=ticket['id'], user_id=ticket['user_id'])
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить / Обработать", callback_data=f"adm_t_accept_{ticket['id']}_{db_type}")],
        [InlineKeyboardButton(text="❌ Отклонить / Закрыть", callback_data=f"adm_t_decline_{ticket['id']}")]
    ])
    
    if db_type == 'roblox_bind':
        await bot.send_video(call.from_user.id, ticket['content'], caption=f"🎫 <b>Привязка Roblox</b>\nОт Telegram ID: {ticket['user_id']}", reply_markup=kb)
    else:
        await bot.send_message(call.from_user.id, f"🚨 <b>Жалоба</b>\nОт Telegram ID: {ticket['user_id']}\n\n{ticket['content']}", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("adm_t_decline_"), IsModOrAdmin())
async def admin_ticket_decline(call: CallbackQuery):
    t_id = int(call.data.split("_")[3])
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE tickets SET status='closed' WHERE id=?", (t_id,))
    conn.commit()
    conn.close()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.reply("❌ Тикет закрыт (отклонен).")
    await call.answer()

@router.callback_query(F.data.startswith("adm_t_accept_"), IsModOrAdmin())
async def admin_ticket_accept(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    t_id = int(parts[3])
    t_type = parts[4]
    
    data = await state.get_data()
    if data.get('ticket_id') != t_id:
        return await call.answer("Ошибка стейта. Попробуйте снова.", show_alert=True)
        
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE tickets SET status='closed' WHERE id=?", (t_id,))
    conn.commit()
    conn.close()
    
    await call.message.edit_reply_markup(reply_markup=None)
    
    if t_type == 'roblox_bind':
        await call.message.reply("Введите Ник и ID Roblox через пробел\n(Пример: <code>CoolNinja 1234567</code>):")
        await state.set_state(AppStates.admin_waiting_roblox_data)
    else:
        await call.message.reply("✅ Жалоба помечена как обработанная. Теперь вы можете найти нарушителя через «Управление юзером» и выдать ЧС.")
        await state.clear()
        
    await call.answer()

@router.message(AppStates.admin_waiting_roblox_data, IsModOrAdmin())
async def admin_save_roblox_data(message: Message, state: FSMContext):
    try:
        rbx_nick, rbx_id = message.text.split()
        data = await state.get_data()
        target_id = data.get('user_id')
        
        conn = sqlite3.connect(DB_NAME)
        conn.execute("UPDATE users SET roblox_username = ?, roblox_id = ? WHERE tg_id = ?", (rbx_nick, rbx_id, target_id))
        conn.commit()
        
        staff = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
        is_super = staff[0] == 'admin'
        conn.close()
        
        await message.answer(f"✅ Данные успешно привязаны пользователю {target_id}!", reply_markup=get_admin_main_kb(is_super))
        try:
            await bot.send_message(target_id, f"🎉 <b>Ваш аккаунт Roblox успешно верифицирован и привязан!</b>\nНик: {rbx_nick}\nID: {rbx_id}")
        except: pass
        await state.clear()
    except Exception:
        await message.answer("❌ Ошибка формата. Нужно ввести ровно два слова: Ник ID")

# ==========================================
# СУПЕР-АДМИН ФУНКЦИИ (Рассылка, Бэкап)
# ==========================================
@router.callback_query(F.data == "admin_broadcast", IsSuperAdmin())
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("📢 Отправьте текст (с картинкой/видео или без), который нужно разослать ВСЕМ пользователям бота:\n\nДля отмены нажмите кнопку ниже.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_cancel")]]))
    await state.set_state(AppStates.admin_waiting_broadcast_msg)
    await call.answer()

@router.message(AppStates.admin_waiting_broadcast_msg, IsSuperAdmin())
async def admin_broadcast_send(message: Message, state: FSMContext):
    await message.answer("⏳ Начинаю рассылку. Это может занять некоторое время...")
    conn = get_db()
    users = conn.execute("SELECT tg_id FROM users").fetchall()
    conn.close()
    
    success = 0
    for u in users:
        try:
            await message.copy_to(chat_id=u['tg_id'])
            success += 1
            await asyncio.sleep(0.05) # Защита от спам-блока Телеграма
        except: pass
        
    await message.answer(f"✅ Рассылка завершена!\nУспешно доставлено: <b>{success}</b> пользователям.", reply_markup=get_admin_main_kb(True))
    log_audit(message.from_user.id, f"Broadcasted message to {success} users")
    await state.clear()

@router.callback_query(F.data == "admin_backup_db", IsSuperAdmin())
async def admin_backup_panel(call: CallbackQuery):
    try:
        await bot.send_document(call.from_user.id, FSInputFile(DB_NAME), caption=f"💾 Ручной бэкап БД от {datetime.now().strftime('%Y-%m-%d %H:%M')}.")
        log_audit(call.from_user.id, "Downloaded DB Backup")
        await call.answer("✅ Бэкап отправлен.")
    except Exception as e:
        await call.answer(f"❌ Ошибка: {e}", show_alert=True)

# ==========================================
# ФОНОВЫЕ ЗАДАЧИ
# ==========================================
async def auto_backup():
    while True:
        await asyncio.sleep(3600) # Каждый час (3600 сек)
        try:
            await bot.send_document(ADMIN_ID, FSInputFile(DB_NAME), caption=f"💾 АВТО-БЭКАП БД | {datetime.now().strftime('%H:%M')}")
        except: pass

async def main():
    dp.include_router(router)
    print("AntiScam Enterprise v2 Успешно запущен!")
    
    # Запускаем авто-бэкап как фоновую задачу
    asyncio.create_task(auto_backup())
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
