import asyncio
import sqlite3
import os
import time
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, BaseFilter, StateFilter, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, ChatPermissions,
    ForumTopic
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

# ==========================================
# НАСТРОЙКИ БОТА
# ==========================================
BOT_TOKEN = "8849560433:AAGneHz6CP09oIJl-C6sO9iIsYAy2YQtoLE" # <-- Твой токен
ADMIN_ID = 5341904332 # <-- Твой Telegram ID

# ID Супергруппы для Безопасных Комнат.
TRADE_ROOMS_GROUP_ID = -1003863551255 

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
    
    # Основная таблица пользователей
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
            badges TEXT DEFAULT '',
            referrer_id INTEGER DEFAULT 0,
            ref_bonus_received BOOLEAN DEFAULT 0,
            suspected_boost BOOLEAN DEFAULT 0,
            is_hidden BOOLEAN DEFAULT 0
        )
    ''')
    
    cursor.execute("PRAGMA table_info(users)")
    columns = [info[1] for info in cursor.fetchall()]
    
    migrations = {
        'role': "ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'",
        'badges': "ALTER TABLE users ADD COLUMN badges TEXT DEFAULT ''",
        'referrer_id': "ALTER TABLE users ADD COLUMN referrer_id INTEGER DEFAULT 0",
        'ref_bonus_received': "ALTER TABLE users ADD COLUMN ref_bonus_received BOOLEAN DEFAULT 0",
        'suspected_boost': "ALTER TABLE users ADD COLUMN suspected_boost BOOLEAN DEFAULT 0",
        'is_hidden': "ALTER TABLE users ADD COLUMN is_hidden BOOLEAN DEFAULT 0"
    }
    for col, query in migrations.items():
        if col not in columns:
            cursor.execute(query)
            
    # Таблицы для системы Безопасных Комнат
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trade_rooms (
            thread_id INTEGER PRIMARY KEY,
            garant_id INTEGER,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS room_participants (
            thread_id INTEGER,
            user_id INTEGER,
            UNIQUE(thread_id, user_id)
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
            target_id INTEGER DEFAULT 0,
            type TEXT,
            content TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute("PRAGMA table_info(tickets)")
    t_columns = [info[1] for info in cursor.fetchall()]
    if 'target_id' not in t_columns:
        cursor.execute("ALTER TABLE tickets ADD COLUMN target_id INTEGER DEFAULT 0")

    # Таблица логов аудита
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER,
            action TEXT,
            target_id INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Выдаем права Супер-Админа владельцу
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

class IsSuperAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id == ADMIN_ID

class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if message.from_user.id == ADMIN_ID: return True
        conn = get_db()
        user = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
        conn.close()
        return user and user['role'] == 'admin'

class IsSeniorOrAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if message.from_user.id == ADMIN_ID: return True
        conn = get_db()
        user = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
        conn.close()
        return user and user['role'] in ['admin', 'senior_mod']

class IsStaff(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if message.from_user.id == ADMIN_ID: return True
        conn = get_db()
        user = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
        conn.close()
        return user and user['role'] in ['admin', 'senior_mod', 'junior_mod']

class IsPrivate(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.chat.type == "private"

ACHIEVEMENTS = {
    "first_trade": "🤝 Первая Кровь",
    "ten_trades": "💼 Делец (10 сделок)",
    "fifty_trades": "🛡 Железный Трейдер (50 сделок)",
    "flawless": "🌟 Безупречный (Рейтинг 5.0)",
    "garant": "⚖️ Официальный Гарант",
    "web_master": "🕷 Мастер Паутины"
}

def calculate_risk_score(user_id: int, is_premium: bool, suspected_boost: bool) -> int:
    score = 10  # Базовый риск 10%
    if user_id > 6500000000: score += 15
    if is_premium: score -= 15
    if suspected_boost: score += 40
    
    conn = get_db()
    pending_reports = conn.execute("SELECT COUNT(*) FROM tickets WHERE target_id = ? AND type = 'report' AND status = 'pending'", (user_id,)).fetchone()[0]
    conn.close()
    
    score += (pending_reports * 30)
    
    if score < 0: return 0
    if score > 100: return 100
    return score

async def check_anti_boost(u1: int, u2: int):
    """Теневой детектор накрутки (Сделки и Отзывы)"""
    conn = get_db()
    recent_trades = conn.execute('''
        SELECT COUNT(*) FROM trades_p2p 
        WHERE ((initiator_id=? AND partner_id=?) OR (initiator_id=? AND partner_id=?)) 
        AND status='accepted' AND created_at >= datetime('now', '-4 hours')
    ''', (u1, u2, u2, u1)).fetchone()[0]
    
    recent_reviews = conn.execute('''
        SELECT COUNT(*) FROM reviews
        WHERE ((reviewer_id=? AND target_id=?) OR (reviewer_id=? AND target_id=?))
        AND created_at >= datetime('now', '-4 hours')
    ''', (u1, u2, u2, u1)).fetchone()[0]
    
    if (recent_trades + recent_reviews) >= 4: 
        conn.execute("UPDATE users SET suspected_boost = 1 WHERE tg_id IN (?, ?)", (u1, u2))
        conn.commit()
        try:
            await bot.send_message(ADMIN_ID, f"⚠️ <b>[Anti-Boost] Подозрение на накрутку!</b>\nЮзеры <code>{u1}</code> и <code>{u2}</code> провели 5+ действий (сделки/отзывы) за 4 часа. Флаг выставлен, индекс риска повышен.")
        except: pass
    conn.close()

async def check_referral_bonus(user_id):
    conn = get_db()
    user = conn.execute("SELECT referrer_id FROM users WHERE tg_id = ?", (user_id,)).fetchone()
    
    if not user or user['referrer_id'] == 0:
        conn.close()
        return
        
    referrer_id = user['referrer_id']
    referrer = conn.execute("SELECT ref_bonus_received, badges FROM users WHERE tg_id = ?", (referrer_id,)).fetchone()
    
    if not referrer or referrer['ref_bonus_received']:
        conn.close()
        return
        
    active_refs = conn.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ? AND trades >= 2 AND roblox_id != 'Нет'", (referrer_id,)).fetchone()[0]
    
    if active_refs >= 3:
        current_badges = referrer['badges'].split(',') if referrer['badges'] else []
        if "web_master" not in current_badges:
            current_badges.append("web_master")
            
        new_badges_str = ",".join(current_badges)
        conn.execute("UPDATE users SET badges = ?, ref_bonus_received = 1 WHERE tg_id = ?", (new_badges_str, referrer_id))
        conn.commit()
        
        try:
            await bot.send_message(
                referrer_id, 
                "🕷 <b>СЕТЬ СПЛЕТЕНА!</b> 🕷\n\n"
                "Трое твоих приглашенных друзей привязали Roblox и провели сделки.\n"
                "🎁 <b>Награда получена:</b>\n"
                "• Уникальный титул: <b>Мастер Паутины</b>"
            )
        except: pass
    conn.close()

async def check_achievements(user_id):
    conn = get_db()
    user = conn.execute("SELECT trades, status, rating_sum, reviews_count, badges FROM users WHERE tg_id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        return
        
    current_badges = user['badges'].split(',') if user['badges'] else []
    new_badges = []
    
    if user['trades'] >= 1 and "first_trade" not in current_badges: new_badges.append("first_trade")
    if user['trades'] >= 10 and "ten_trades" not in current_badges: new_badges.append("ten_trades")
    if user['trades'] >= 50 and "fifty_trades" not in current_badges: new_badges.append("fifty_trades")
        
    avg = user['rating_sum'] / user['reviews_count'] if user['reviews_count'] > 0 else 0
    if avg >= 4.9 and user['reviews_count'] >= 10 and "flawless" not in current_badges:
        new_badges.append("flawless")
        
    if user['status'] == 'garant' and "garant" not in current_badges:
        new_badges.append("garant")

    if new_badges:
        updated_badges = current_badges + new_badges
        badges_str = ",".join(updated_badges)
        conn.execute("UPDATE users SET badges = ? WHERE tg_id = ?", (badges_str, user_id))
        conn.commit()
        for badge in new_badges:
            try:
                await bot.send_message(user_id, f"🏆 <b>Новое достижение: {ACHIEVEMENTS[badge]}</b>!")
            except: pass
            
    conn.close()
    await check_referral_bonus(user_id)

def format_rating(rating_sum, reviews_count):
    if reviews_count == 0: return "Нет оценок 🤷‍♂️"
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

def get_main_reply_kb(user_role):
    builder = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🤝 Подтвердить сделку")],
        [KeyboardButton(text="⭐ Оставить отзыв"), KeyboardButton(text="🧾 Создать чек")],
        [KeyboardButton(text="🛡 Гаранты"), KeyboardButton(text="⚠️ Скамеры"), KeyboardButton(text="🏆 Топ")],
        [KeyboardButton(text="🔍 Проверить"), KeyboardButton(text="🛎 Вызвать Гаранта")],
        [KeyboardButton(text="🎮 Привязать Roblox"), KeyboardButton(text="🚨 Подать жалобу")]
    ], resize_keyboard=True)
    
    if user_role in ['admin', 'senior_mod', 'junior_mod']:
        builder.keyboard.append([KeyboardButton(text="👑 Админ Панель")])
        
    return builder

def get_cancel_reply_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)

def get_admin_main_kb(role):
    kb = [
        [InlineKeyboardButton(text="🎫 Заявки Roblox", callback_data="admin_tickets_roblox"),
         InlineKeyboardButton(text="🚨 Жалобы", callback_data="admin_tickets_reports")]
    ]
    
    if role in ['admin', 'senior_mod']:
        kb.append([InlineKeyboardButton(text="🔍 Управление юзером", callback_data="admin_search_user"),
                   InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")])
                   
    if role == 'admin':
        kb.append([InlineKeyboardButton(text="🛡 Управление Персоналом", callback_data="admin_staff_manage")])
        kb.append([InlineKeyboardButton(text="📢 Глобальная Рассылка", callback_data="admin_broadcast")])
        kb.append([InlineKeyboardButton(text="💾 Бэкап БД", callback_data="admin_backup_db"),
                   InlineKeyboardButton(text="📤 Загрузить БД", callback_data="admin_upload_db")])
        
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_user_manage_kb(target_id, current_status, current_role, is_hidden):
    buttons = []
    if current_status != 'scammer': buttons.append([InlineKeyboardButton(text="☠️ В ЧС (Скам)", callback_data=f"admset_scammer_{target_id}")])
    if current_status != 'garant': buttons.append([InlineKeyboardButton(text="⚖️ Выдать Гаранта", callback_data=f"admset_garant_{target_id}")])
    if current_status != 'user': buttons.append([InlineKeyboardButton(text="👤 Обычный юзер", callback_data=f"admset_user_{target_id}")])
        
    buttons.append([
        InlineKeyboardButton(text="➕ 1 Сделка", callback_data=f"admadd_trade_{target_id}"),
        InlineKeyboardButton(text="🧹 Обнулить", callback_data=f"admclr_stats_{target_id}")
    ])
    
    hidden_text = "👁 Показать в топах" if is_hidden else "👻 Скрыть из топов"
    buttons.append([InlineKeyboardButton(text=hidden_text, callback_data=f"admhide_{target_id}")])
    
    buttons.append([InlineKeyboardButton(text="◀️ Назад в Админку", callback_data="admin_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

class AppStates(StatesGroup):
    waiting_for_p2p_partner = State()
    waiting_for_review_target = State()
    waiting_for_review_stars = State()
    waiting_for_review_text = State()
    waiting_for_check_target = State()
    
    waiting_for_specific_garant = State()
    waiting_for_garant_details = State()
    
    waiting_for_bind_video = State()
    waiting_for_report_target = State()
    waiting_for_report_proofs = State()
    
    admin_waiting_user_search = State()
    admin_waiting_roblox_data = State()
    admin_waiting_broadcast_msg = State()
    admin_waiting_add_mod = State()
    admin_waiting_rem_mod = State()
    admin_waiting_db_upload = State()

@router.message(CommandStart(), IsPrivate())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    args = message.text.split()
    referrer_id = 0
    
    if len(args) > 1 and args[1].startswith('ref_'):
        try:
            referrer_id = int(args[1].split('_')[1])
            if referrer_id == message.from_user.id: referrer_id = 0
        except: pass

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    
    if not user:
        conn.execute("INSERT INTO users (tg_id, username, referrer_id) VALUES (?, ?, ?)", 
                     (message.from_user.id, message.from_user.username or "Без_Ника", referrer_id))
        conn.commit()
        role = "user"
    else:
        role = user['role']
        conn.execute("UPDATE users SET username = ? WHERE tg_id = ?", (message.from_user.username or "Без_Ника", message.from_user.id))
        conn.commit()
    conn.close()
    
    await message.answer(
        f"👋 Добро пожаловать, <b>{message.from_user.first_name}</b>!\n\n"
        f"🛡 <b>ГАД (Global Antiscam Database)</b> — официальная база данных репутации и защита сделок.\n\n"
        f"Используйте меню ниже 👇",
        reply_markup=get_main_reply_kb(role)
    )

@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена", IsPrivate())
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    conn = get_db()
    user = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    conn.close()
    role = user['role'] if user else 'user'
    await message.answer("✅ Действие отменено.", reply_markup=get_main_reply_kb(role))

@router.message(F.text == "👤 Профиль", IsPrivate())
async def show_profile(message: Message):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    conn.close()
    
    if not user: return await message.answer("Вас нет в базе. Напишите /start")
        
    title = get_user_title(user['trades'])
    rating = format_rating(user['rating_sum'], user['reviews_count'])
    
    status_text = "🟢 Обычный пользователь"
    if user['status'] == 'garant': status_text = "⚖️ Официальный Гарант"
    elif user['status'] == 'scammer': status_text = "☠️ В ЧЁРНОМ СПИСКЕ (СКАМ)"

    badges_list = user['badges'].split(',') if user['badges'] else []
    badges_display = "\n".join([f"🏅 {ACHIEVEMENTS.get(b, b)}" for b in badges_list if b in ACHIEVEMENTS])
    if not badges_display: badges_display = "<i>Нет достижений</i>"
    
    is_premium = getattr(message.from_user, 'is_premium', False)
    risk_score = calculate_risk_score(user['tg_id'], is_premium, user['suspected_boost'])
    
    risk_emoji = "🟢" if risk_score < 30 else "🟡" if risk_score < 70 else "🔴"
    
    ref_link = f"https://t.me/{(await bot.get_me()).username}?start=ref_{user['tg_id']}"
    hidden_status = "\n👻 <i>Теневой профиль (скрыт из топов)</i>" if user['is_hidden'] else ""

    text = (
        f"👤 <b>Профиль в ГАД:</b>\n"
        f"🔖 <b>ID:</b> <code>{user['tg_id']}</code>\n"
        f"🎮 <b>Roblox:</b> <code>{user['roblox_username']}</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏆 <b>Титул:</b> {title}\n"
        f"⚖️ <b>Статус:</b> {status_text}{hidden_status}\n"
        f"🤝 <b>Сделок:</b> {user['trades']}\n"
        f"⭐️ <b>Рейтинг:</b> {rating}\n"
        f"⚠️ <b>Индекс Риска:</b> {risk_emoji} <b>{risk_score}%</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎖 <b>Достижения:</b>\n{badges_display}\n\n"
        f"🕸 <b>Реферальная паутина:</b>\n"
        f"<i>Пригласи 3 друзей (привязка + 2 сделки) и получи уникальный статус.</i>\n"
        f"Твоя ссылка: <code>{ref_link}</code>"
    )
    await message.answer(text)

@router.message(F.text == "🧾 Создать чек", IsPrivate())
async def create_receipt(message: Message):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    conn.close()
    
    rating = format_rating(user['rating_sum'], user['reviews_count'])
    badges_list = user['badges'].split(',') if user['badges'] else []
    badges_display = " | ".join([ACHIEVEMENTS.get(b, '').split()[0] for b in badges_list if b in ACHIEVEMENTS])
    
    is_premium = getattr(message.from_user, 'is_premium', False)
    risk_score = calculate_risk_score(user['tg_id'], is_premium, user['suspected_boost'])
    
    text = (
        f"🧾 <b>ЧЕК ТРЕЙДЕРА ГАД</b> 🧾\n\n"
        f"👤 <b>Пользователь:</b> @{user['username']} (ID: <code>{user['tg_id']}</code>)\n"
        f"🎮 <b>Roblox:</b> <code>{user['roblox_username']}</code>\n"
        f"✅ <b>Успешных сделок:</b> {user['trades']}\n"
        f"⭐️ <b>Рейтинг:</b> {rating}\n"
        f"⚖️ <b>Статус:</b> {user['status'].upper()}\n"
        f"⚠️ <b>Индекс риска:</b> {risk_score}%\n"
    )
    if badges_display: text += f"🎖 <b>Награды:</b> {badges_display}\n"
    text += f"\n<i>✅ Верифицировано Global Antiscam Database</i>"
    await message.answer(text)

@router.message(F.text == "🤝 Подтвердить сделку", IsPrivate())
async def p2p_trade_start(message: Message, state: FSMContext):
    await message.answer("🤝 Введите <b>@username</b> или <b>ID</b> человека, которому хотите подтвердить и засчитать сделку (например, Гаранту):", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_p2p_partner)

@router.message(AppStates.waiting_for_p2p_partner, IsPrivate())
async def p2p_trade_process(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    conn = get_db()
    if target.isdigit(): partner = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else: partner = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()
        
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
    
    role = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()['role']
    conn.close()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять репутацию", callback_data=f"p2p_accept_{trade_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"p2p_decline_{trade_id}")]
    ])
    
    try:
        await bot.send_message(partner['tg_id'], f"🤝 <b>Новый запрос на подтверждение сделки!</b>\nПользователь @{message.from_user.username} хочет засчитать вам <b>+1 сделку</b> в профиль.\nПринять?", reply_markup=kb)
        await message.answer("✅ Запрос отправлен. Ожидайте подтверждения от получателя.", reply_markup=get_main_reply_kb(role))
    except:
        await message.answer("❌ Партнер заблокировал бота или ни разу его не запускал.", reply_markup=get_main_reply_kb(role))
    
    await state.clear()

@router.callback_query(F.data.startswith("p2p_accept_"))
async def p2p_accept(call: CallbackQuery):
    trade_id = int(call.data.split("_")[2])
    conn = sqlite3.connect(DB_NAME)
    trade = conn.execute("SELECT * FROM trades_p2p WHERE id = ?", (trade_id,)).fetchone()
    
    if not trade or trade[3] != 'pending': 
        conn.close()
        return await call.answer("❌ Запрос уже обработан.", show_alert=True)
        
    u1, u2 = trade[1], trade[2]
    conn.close()
    
    await check_anti_boost(u1, u2)
    
    conn = sqlite3.connect(DB_NAME)
    # Начисляем +1 только тому, КОМУ кинули запрос (партнеру u2)
    conn.execute("UPDATE users SET trades = trades + 1 WHERE tg_id = ?", (u2,))
    conn.execute("UPDATE trades_p2p SET status = 'accepted' WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()
    
    try: await call.message.edit_text("✅ Сделка подтверждена! Вам начислена +1 сделка в профиль.")
    except: pass
    try: await bot.send_message(u1, f"🎉 Пользователь принял ваш запрос! Ему начислена +1 сделка.")
    except: pass
    
    await check_achievements(u2)

@router.callback_query(F.data.startswith("p2p_decline_"))
async def p2p_decline(call: CallbackQuery):
    trade_id = int(call.data.split("_")[2])
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE trades_p2p SET status = 'declined' WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()
    try: await call.message.edit_text("❌ Вы отклонили запрос.")
    except: pass

@router.message(F.text == "⭐ Оставить отзыв", IsPrivate())
async def review_start(message: Message, state: FSMContext):
    await message.answer("Введи @username или ID Гаранта для отзыва:", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_review_target)

@router.message(AppStates.waiting_for_review_target, IsPrivate())
async def review_target(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    conn = get_db()
    if target.isdigit(): user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else: user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()
    conn.close()
        
    if not user: return await message.answer("❌ Пользователь не найден.")
    if user['status'] != 'garant': return await message.answer("❌ Отзывы можно оставлять только Гарантам.")
    if user['tg_id'] == message.from_user.id: return await message.answer("❌ Нельзя оставить себе.")
        
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
    if "⭐" not in message.text: return await message.answer("Пожалуйста, используйте кнопки ниже.")
    stars = int(message.text.split()[0])
    await state.update_data(stars=stars)
    await message.answer("Короткий комментарий (или '-'):", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_review_text)

@router.message(AppStates.waiting_for_review_text, IsPrivate())
async def review_text(message: Message, state: FSMContext):
    data = await state.get_data()
    await check_anti_boost(message.from_user.id, data['target_id'])
    
    conn = sqlite3.connect(DB_NAME)
    conn.execute("INSERT INTO reviews (reviewer_id, target_id, rating, review_text) VALUES (?, ?, ?, ?)",
                 (message.from_user.id, data['target_id'], data['stars'], message.text))
    conn.execute("UPDATE users SET rating_sum = rating_sum + ?, reviews_count = reviews_count + 1 WHERE tg_id = ?",
                 (data['stars'], data['target_id']))
    
    role = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()['role']
    conn.commit()
    conn.close()
    
    await message.answer("✅ Отзыв опубликован!", reply_markup=get_main_reply_kb(role))
    try: await bot.send_message(data['target_id'], f"🌟 <b>Новый отзыв!</b>\nОценка: {data['stars']} ⭐\nТекст: <i>{message.text}</i>")
    except: pass
    
    await check_achievements(data['target_id'])
    await state.clear()

@router.message(F.text == "🛡 Гаранты", IsPrivate())
async def show_garants(message: Message):
    conn = get_db()
    # Скрытых пользователей не показываем
    garants = conn.execute("SELECT username, trades, rating_sum, reviews_count FROM users WHERE status = 'garant' AND is_hidden = 0 ORDER BY trades DESC LIMIT 15").fetchall()
    conn.close()
    
    if not garants: return await message.answer("😔 Пока в базе нет гарантов.")
        
    text = "🛡 <b>Топ Гарантов ГАД:</b>\n\n"
    for i, g in enumerate(garants, 1):
        text += f"{i}. <b>@{g['username']}</b> | Сделок: {g['trades']} | {format_rating(g['rating_sum'], g['reviews_count'])}\n"
    await message.answer(text)

@router.message(F.text == "⚠️ Скамеры", IsPrivate())
async def show_scammers(message: Message):
    conn = get_db()
    scammers = conn.execute("SELECT username, tg_id FROM users WHERE status = 'scammer' ORDER BY join_date DESC LIMIT 15").fetchall()
    conn.close()
        
    if not scammers: return await message.answer("🎉 База скамеров пуста.")
        
    text = "🚨 <b>Последние заблокированные:</b>\n\n"
    for s in scammers: text += f"☠️ @{s['username']} (<code>{s['tg_id']}</code>)\n"
    await message.answer(text)

@router.message(F.text == "🏆 Топ", IsPrivate())
async def show_top(message: Message):
    conn = get_db()
    top_users = conn.execute("SELECT username, trades, status FROM users WHERE status != 'scammer' AND is_hidden = 0 ORDER BY trades DESC LIMIT 10").fetchall()
    conn.close()
        
    if not top_users: return await message.answer("Рейтинг пуст.")
        
    medals = ["🥇", "🥈", "🥉", "4.", "5.", "6.", "7.", "8.", "9.", "10."]
    text = "🏆 <b>ТОП-10 Трейдеров ГАД:</b>\n\n"
    for i, u in enumerate(top_users):
        text += f"{medals[i]} {'⚖️' if u['status'] == 'garant' else '🟢'} <b>@{u['username']}</b> — {u['trades']} сделок\n"
    await message.answer(text)

@router.message(F.text == "🛎 Вызвать Гаранта", IsPrivate())
async def call_garant_start(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Всем свободным Гарантам", callback_data="call_garant_any")],
        [InlineKeyboardButton(text="🎯 Конкретному Гаранту", callback_data="call_garant_specific")]
    ])
    await message.answer("🛎 Как вы хотите отправить вызов?", reply_markup=kb)

@router.callback_query(F.data == "call_garant_specific")
async def call_garant_specific_req(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("🎯 Введите <b>@username</b> или <b>ID</b> нужного Гаранта:")
    await state.set_state(AppStates.waiting_for_specific_garant)
    await call.answer()

@router.message(AppStates.waiting_for_specific_garant, IsPrivate())
async def call_garant_specific_process(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    conn = get_db()
    if target.isdigit(): garant = conn.execute("SELECT tg_id, status FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else: garant = conn.execute("SELECT tg_id, status FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()
    conn.close()

    if not garant or garant['status'] != 'garant':
        return await message.answer("❌ Гарант не найден. Убедитесь, что ник верный и у него есть официальный статус.")
        
    await state.update_data(target_garant=garant['tg_id'])
    await message.answer("📝 Теперь опишите условия сделки (Что даете вы, что должен дать партнер, сколько людей участвует):", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_garant_details)

@router.callback_query(F.data == "call_garant_any")
async def call_garant_any_req(call: CallbackQuery, state: FSMContext):
    await state.update_data(target_garant=None)
    await call.message.edit_text("📝 Опишите условия сделки (Что даете вы, что должен дать партнер, сколько людей участвует):")
    msg = await call.message.answer("Ожидаю ввод...", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_garant_details)
    await call.answer()

@router.message(AppStates.waiting_for_garant_details, IsPrivate())
async def call_garant_process(message: Message, state: FSMContext):
    data = await state.get_data()
    desc = message.text
    target_garant_id = data.get('target_garant')
    
    conn = get_db()
    if target_garant_id:
        garants = conn.execute("SELECT tg_id FROM users WHERE tg_id = ?", (target_garant_id,)).fetchall()
    else:
        garants = conn.execute("SELECT tg_id FROM users WHERE status = 'garant'").fetchall()
        
    role = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()['role']
    conn.close()
        
    if not garants:
        await message.answer("😔 Нет доступных гарантов.", reply_markup=get_main_reply_kb(role))
        return await state.clear()
        
    # Формируем кнопку принятия заказа (только ID создателя заявки)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Взять заказ (Создать комнату)", callback_data=f"take_order_{message.from_user.id}")]
    ])
        
    notified = 0
    for g in garants:
        try:
            await bot.send_message(
                g['tg_id'], 
                f"🛎 <b>ВЫЗОВ ГАРАНТА</b>\nОт: @{message.from_user.username}\n\n<b>Условия:</b> <i>{desc}</i>",
                reply_markup=kb
            )
            notified += 1
        except: pass
        
    target_text = "выбранному гаранту" if target_garant_id else f"{notified} гарантам"
    await message.answer(f"✅ Заявка отправлена {target_text}! Ожидайте создания Безопасной Комнаты.", reply_markup=get_main_reply_kb(role))
    await state.clear()

@router.callback_query(F.data.startswith("take_order_"))
async def garant_take_order(call: CallbackQuery):
    client_a = int(call.data.split("_")[2])
    
    if TRADE_ROOMS_GROUP_ID == 0:
        return await call.answer("❌ Админ не настроил TRADE_ROOMS_GROUP_ID. Функция недоступна.", show_alert=True)
        
    try:
        topic: ForumTopic = await bot.create_forum_topic(
            chat_id=TRADE_ROOMS_GROUP_ID,
            name=f"🤝 Сделка #{client_a}"
        )
        
        chat_id_str = str(TRADE_ROOMS_GROUP_ID).replace("-100", "")
        topic_url = f"https://t.me/c/{chat_id_str}/{topic.message_thread_id}"
        
        # Регистрируем комнату и участников в БД
        conn = sqlite3.connect(DB_NAME)
        conn.execute("INSERT INTO trade_rooms (thread_id, garant_id) VALUES (?, ?)", (topic.message_thread_id, call.from_user.id))
        conn.execute("INSERT INTO room_participants (thread_id, user_id) VALUES (?, ?)", (topic.message_thread_id, client_a))
        conn.commit()
        conn.close()
        
        msg = f"🛡 <b>БЕЗОПАСНАЯ КОМНАТА СОЗДАНА</b> 🛡\n\nГарант @{call.from_user.username} взял вашу сделку.\nПерейдите в закрытую комнату:\n👉 {topic_url}"
        await bot.send_message(client_a, msg)
        
        try:
            await call.message.edit_reply_markup(reply_markup=None)
            await call.message.reply(f"✅ Комната успешно создана.\n👉 {topic_url}\n\nВнутри темы напишите <code>/add @username</code> чтобы добавить участников (до 5 чел).\nПо завершению напишите <code>/closeroom</code>.")
        except: pass
        
        await bot.send_message(
            chat_id=TRADE_ROOMS_GROUP_ID,
            message_thread_id=topic.message_thread_id,
            text=f"⚖️ <b>Сделка начата</b>\nГарант: {call.from_user.mention_html()}\nИнициатор: <a href='tg://user?id={client_a}'>Участник</a>\n\n<i>Гарант может добавлять других участников командой /add @username\nВсе посторонние сообщения удаляются!</i>"
        )
        
    except TelegramBadRequest as e:
        await call.answer(f"❌ Ошибка создания комнаты: {e}", show_alert=True)
    except Exception as e:
        await call.answer(f"❌ Ошибка: {e}", show_alert=True)

@router.message(Command("add"))
async def add_user_to_room(message: Message):
    if message.chat.id != TRADE_ROOMS_GROUP_ID or not message.is_topic_message: return
        
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Формат: <code>/add @username</code> или <code>/add ID</code>")
        
    target = args[1].replace("@", "")
    thread_id = message.message_thread_id
    
    conn = get_db()
    room = conn.execute("SELECT garant_id FROM trade_rooms WHERE thread_id = ? AND status = 'active'", (thread_id,)).fetchone()
    
    if not room:
        conn.close()
        return
        
    if room['garant_id'] != message.from_user.id:
        role_check = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
        if not role_check or role_check['role'] not in ['admin', 'senior_mod']:
            conn.close()
            return await message.reply("❌ Только гарант сделки может добавлять людей.")

    if target.isdigit(): new_user = conn.execute("SELECT tg_id, username FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else: new_user = conn.execute("SELECT tg_id, username FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()
    
    if not new_user:
        conn.close()
        return await message.reply("❌ Пользователь не найден в базе ГАД. Пусть нажмет /start в боте.")
        
    count = conn.execute("SELECT COUNT(*) FROM room_participants WHERE thread_id = ?", (thread_id,)).fetchone()[0]
    if count >= 5:
        conn.close()
        return await message.reply("❌ Достигнут лимит (5 участников на комнату).")
        
    try:
        conn.execute("INSERT INTO room_participants (thread_id, user_id) VALUES (?, ?)", (thread_id, new_user['tg_id']))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return await message.reply("⚠️ Пользователь уже в комнате.")
        
    conn.close()
    
    chat_id_str = str(TRADE_ROOMS_GROUP_ID).replace("-100", "")
    topic_url = f"https://t.me/c/{chat_id_str}/{thread_id}"
    
    try:
        await bot.send_message(new_user['tg_id'], f"🛡 <b>ПРИГЛАШЕНИЕ В СДЕЛКУ</b>\nГарант добавил вас в безопасную комнату:\n👉 {topic_url}")
        await message.reply(f"✅ Пользователь @{new_user['username']} добавлен в вайтлист комнаты и получил ссылку.")
    except:
        await message.reply(f"✅ Пользователь @{new_user['username']} добавлен в вайтлист, но <b>сообщение не отправлено</b> (бот заблокирован). Скиньте ему ссылку вручную:\n{topic_url}")

@router.message(Command("closeroom"))
async def close_trade_room(message: Message):
    if message.chat.id != TRADE_ROOMS_GROUP_ID or not message.is_topic_message: return
        
    thread_id = message.message_thread_id
    conn = get_db()
    room = conn.execute("SELECT garant_id FROM trade_rooms WHERE thread_id = ? AND status = 'active'", (thread_id,)).fetchone()
    
    if not room:
        conn.close()
        return await message.reply("❌ Эта комната не найдена или уже закрыта.")
        
    role_check = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    if room['garant_id'] != message.from_user.id and (not role_check or role_check['role'] not in ['admin', 'senior_mod']):
        conn.close()
        return await message.reply("❌ Только гарант или админ может закрыть комнату.")

    # Раздаем плюсики к сделкам
    participants = conn.execute("SELECT user_id FROM room_participants WHERE thread_id = ?", (thread_id,)).fetchall()
    users_to_reward = [p['user_id'] for p in participants]
    if room['garant_id'] not in users_to_reward: users_to_reward.append(room['garant_id'])
    
    conn.execute("UPDATE trade_rooms SET status = 'closed' WHERE thread_id = ?", (thread_id,))
    
    for uid in users_to_reward:
        conn.execute("UPDATE users SET trades = trades + 1 WHERE tg_id = ?", (uid,))
        if uid == room['garant_id']:
            msg_text = "✅ Безопасная комната закрыта! Вам начислена +1 сделка в профиль."
        else:
            msg_text = "✅ Сделка через гаранта успешно завершена! Вам начислена +1 сделка в профиль.\n\n⭐️ Желательно оставить отзыв гаранту через кнопку «⭐ Оставить отзыв» в главном меню!"
        try: await bot.send_message(uid, msg_text)
        except: pass

    conn.commit()
    conn.close()
    
    for uid in users_to_reward:
        await check_achievements(uid)

    await message.reply("🔒 <b>Сделка завершена. Статистика обновлена.</b>\nКомната будет закрыта и удалена через 5 секунд.")
    await asyncio.sleep(5)
    try:
        await bot.delete_forum_topic(chat_id=message.chat.id, message_thread_id=thread_id)
        log_audit(message.from_user.id, f"Closed Trade Room {thread_id}")
    except: pass

@router.message(F.chat.id == TRADE_ROOMS_GROUP_ID)
async def room_whitelist_protection(message: Message):
    """Слушает сообщения в Супергруппе и удаляет сообщения посторонних (Вышибала)"""
    if not message.is_topic_message or not message.from_user: return
        
    thread_id = message.message_thread_id
    user_id = message.from_user.id
    
    conn = get_db()
    room = conn.execute("SELECT garant_id FROM trade_rooms WHERE thread_id = ? AND status = 'active'", (thread_id,)).fetchone()
    
    if room:
        is_participant = conn.execute("SELECT 1 FROM room_participants WHERE thread_id = ? AND user_id = ?", (thread_id, user_id)).fetchone()
        role = conn.execute("SELECT role FROM users WHERE tg_id = ?", (user_id,)).fetchone()
        user_role = role['role'] if role else 'user'
        
        if not is_participant and user_id != room['garant_id'] and user_role not in ['admin', 'senior_mod']:
            try: await message.delete()
            except: pass
    conn.close()

@router.message(F.text == "🎮 Привязать Roblox", IsPrivate())
async def bind_roblox_start(message: Message, state: FSMContext):
    await message.answer("📹 Запишите короткое видео, где видно ваш Telegram профиль и переход в аккаунт Roblox, затем отправьте его сюда.", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_bind_video)

@router.message(AppStates.waiting_for_bind_video, IsPrivate())
async def bind_roblox_process(message: Message, state: FSMContext):
    if not message.video: return await message.answer("Пожалуйста, отправьте ВИДЕО.")
        
    conn = sqlite3.connect(DB_NAME)
    conn.execute("INSERT INTO tickets (user_id, type, content) VALUES (?, ?, ?)", (message.from_user.id, 'roblox_bind', message.video.file_id))
    role = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()['role']
    conn.commit()
    conn.close()
    
    await message.answer("✅ Заявка отправлена модераторам!", reply_markup=get_main_reply_kb(role))
    await state.clear()

@router.message(F.text == "🚨 Подать жалобу", IsPrivate())
async def report_user_start(message: Message, state: FSMContext):
    await message.answer("🚨 Введите @username нарушителя:", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_report_target)

@router.message(AppStates.waiting_for_report_target, IsPrivate())
async def report_user_target(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    conn = get_db()
    if target.isdigit(): user = conn.execute("SELECT tg_id FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else: user = conn.execute("SELECT tg_id FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()
    conn.close()
    
    if not user:
        return await message.answer("❌ Пользователь не найден. Убедитесь, что ник верный.")
        
    await state.update_data(target=message.text, target_id=user['tg_id'])
    await message.answer("Отправьте доказательства (Скриншоты, чеки, ссылки на видео):")
    await state.set_state(AppStates.waiting_for_report_proofs)

@router.message(AppStates.waiting_for_report_proofs, IsPrivate())
async def report_user_process(message: Message, state: FSMContext):
    data = await state.get_data()
    content = f"Нарушитель: {data['target']}\nПруфы: {message.text}"
    
    conn = sqlite3.connect(DB_NAME)
    conn.execute("INSERT INTO tickets (user_id, target_id, type, content) VALUES (?, ?, ?, ?)", (message.from_user.id, data['target_id'], 'report', content))
    role = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()['role']
    conn.commit()
    conn.close()
    
    await message.answer("✅ Жалоба передана в арбитраж.", reply_markup=get_main_reply_kb(role))
    await state.clear()

@router.message(Command("check"))
@router.message(F.text == "🔍 Проверить")
async def check_user_start(message: Message, state: FSMContext):
    if message.chat.type != "private":
        parts = message.text.split()
        if len(parts) < 2: return await message.reply("Формат: <code>/check @username</code>")
        return await send_check_result(message, parts[1].replace("@", ""))
        
    await message.answer("🔍 Введите <b>@username</b> или <b>ID</b>:", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_check_target)

@router.message(AppStates.waiting_for_check_target, IsPrivate())
async def check_user_process(message: Message, state: FSMContext):
    await send_check_result(message, message.text.replace("@", ""), state)

async def send_check_result(message: Message, target: str, state: FSMContext = None):
    conn = get_db()
    if target.isdigit(): user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else: user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()
        
    if not user:
        conn.close()
        text = "❓ <b>Не найден в БД ГАД.</b> Будьте осторожны."
        if state:
            role = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()['role']
            await message.answer(text, reply_markup=get_main_reply_kb(role))
            return await state.clear()
        return await message.reply(text)
    
    is_super = False
    if state:
        staff = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
        is_super = staff['role'] == 'admin'
        
    conn.close()
    
    risk_score = calculate_risk_score(user['tg_id'], False, user['suspected_boost'])
    risk_emoji = "🟢" if risk_score < 30 else "🟡" if risk_score < 70 else "🔴"
    
    text = (
        f"📋 <b>Досье ГАД:</b>\n"
        f"👤 @{user['username']} (<code>{user['tg_id']}</code>)\n"
        f"🎮 Roblox: <code>{user['roblox_username']}</code>\n"
        f"🏆 Титул: {get_user_title(user['trades'])}\n"
        f"🤝 Сделок: <b>{user['trades']}</b>\n"
        f"⭐️ Рейтинг: {format_rating(user['rating_sum'], user['reviews_count'])}\n"
        f"⚠️ Риск: {risk_emoji} <b>{risk_score}%</b>\n"
    )
    if user['status'] == 'scammer': text = "‼️ <b>СКАМЕР (В ЧС)! БЕЗ СДЕЛОК!</b> ‼️\n\n" + text
    elif user['status'] == 'garant': text = "⚖️ <b>ОФИЦИАЛЬНЫЙ ГАРАНТ</b> ⚖️\n\n" + text
        
    if user['is_hidden'] and is_super:
        text += "\n👻 <i>Внимание: Этот пользователь СКРЫТ ИЗ ТОПОВ (Теневой режим)</i>"
        
    if state:
        conn = get_db()
        role = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()['role']
        conn.close()
        await message.answer(text, reply_markup=get_main_reply_kb(role))
        await state.clear()
    else:
        await message.reply(text)

@router.message(F.text == "👑 Админ Панель", IsStaff())
async def admin_panel_start(message: Message):
    conn = get_db()
    user = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    role = user['role']
    
    tickets_roblox = conn.execute("SELECT COUNT(*) FROM tickets WHERE type='roblox_bind' AND status='pending'").fetchone()[0]
    tickets_reports = conn.execute("SELECT COUNT(*) FROM tickets WHERE type='report' AND status='pending'").fetchone()[0]
    conn.close()
    
    role_names = {'admin': 'Супер-Админ', 'senior_mod': 'Ст. Модератор', 'junior_mod': 'Мл. Модератор'}
    
    text = (
        f"👑 <b>Панель ГАД</b> [{role_names[role]}]\n\n"
        f"🎫 Ожидают привязки: <b>{tickets_roblox}</b>\n"
        f"🚨 Жалобы: <b>{tickets_reports}</b>\n"
    )
    await message.answer(text, reply_markup=get_admin_main_kb(role))

@router.callback_query(F.data == "admin_cancel", IsStaff())
async def admin_cancel_call(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await call.message.delete()
    except: pass
    await admin_panel_start(call.message)
    await call.answer()

@router.callback_query(F.data == "admin_stats", IsSeniorOrAdmin())
async def admin_stats_panel(call: CallbackQuery):
    conn = get_db()
    total_u = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_t = conn.execute("SELECT SUM(trades) FROM users").fetchone()[0] or 0
    total_s = conn.execute("SELECT COUNT(*) FROM users WHERE status='scammer'").fetchone()[0]
    garants = conn.execute("SELECT COUNT(*) FROM users WHERE status='garant'").fetchone()[0]
    
    role = conn.execute("SELECT role FROM users WHERE tg_id = ?", (call.from_user.id,)).fetchone()['role']
    conn.close()
        
    text = (
        f"📊 <b>Статистика ГАД:</b>\n\n"
        f"👥 Юзеров: <b>{total_u}</b>\n"
        f"🤝 Сделок: <b>{total_t}</b>\n"
        f"🛡 Гарантов: <b>{garants}</b>\n"
        f"☠️ Скамеров: <b>{total_s}</b>"
    )
    try: await call.message.edit_text(text, reply_markup=get_admin_main_kb(role))
    except: pass
    await call.answer()

@router.callback_query(F.data == "admin_search_user", IsSeniorOrAdmin())
async def admin_search_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("🔍 Введите @username или ID юзера:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_cancel")]]))
    await state.set_state(AppStates.admin_waiting_user_search)
    await call.answer()

@router.message(AppStates.admin_waiting_user_search, IsSeniorOrAdmin())
async def admin_search_process(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    conn = get_db()
    if target.isdigit(): user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else: user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()
        
    role = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()['role']
    conn.close()
            
    if not user:
        return await message.answer("❌ Юзер не найден.", reply_markup=get_admin_main_kb(role))
        
    text = (f"👤 Юзер: @{user['username']} (ID: <code>{user['tg_id']}</code>)\n"
            f"Статус: <b>{user['status'].upper()}</b> | Роль: <b>{user['role'].upper()}</b>\n"
            f"Сделок: {user['trades']}\n")
    if user['is_hidden']: text += "👻 <i>Скрыт из топов</i>\n"
            
    await message.answer(text, reply_markup=get_admin_user_manage_kb(user['tg_id'], user['status'], user['role'], user['is_hidden']))
    await state.clear()

@router.callback_query(F.data.startswith("admset_") | F.data.startswith("admadd_") | F.data.startswith("admclr_") | F.data.startswith("admhide_"), IsSeniorOrAdmin())
async def admin_manage_user(call: CallbackQuery):
    parts = call.data.split("_")
    action_type = parts[0]
    target_id = int(parts[2] if len(parts) > 2 else parts[1])
    action = parts[1] if action_type != "admhide" else None
    
    conn = sqlite3.connect(DB_NAME)
    
    if action_type == "admset":
        conn.execute("UPDATE users SET status = ? WHERE tg_id = ?", (action, target_id))
        text = f"✅ Статус изменен на {action}."
        if action == 'garant': await check_achievements(target_id)
            
    elif action_type == "admadd":
        conn.execute("UPDATE users SET trades = trades + 1 WHERE tg_id = ?", (target_id,))
        text = "✅ Начислена 1 сделка."
        await check_achievements(target_id)
        
    elif action_type == "admclr":
        conn.execute("UPDATE users SET trades = 0, rating_sum = 0, reviews_count = 0, badges = '' WHERE tg_id = ?", (target_id,))
        text = "🧹 Статистика очищена."
        
    elif action_type == "admhide":
        current_hide = conn.execute("SELECT is_hidden FROM users WHERE tg_id = ?", (target_id,)).fetchone()[0]
        new_hide = 0 if current_hide else 1
        conn.execute("UPDATE users SET is_hidden = ? WHERE tg_id = ?", (new_hide, target_id))
        text = "👻 Пользователь скрыт из топов." if new_hide else "👁 Пользователь возвращен в топы."
        
    conn.commit()
    conn.close()
    
    await call.answer(text, show_alert=True)
    try: await call.message.delete()
    except: pass
    await admin_panel_start(call.message)

@router.callback_query(F.data.startswith("admin_tickets_"), IsStaff())
async def admin_view_tickets(call: CallbackQuery, state: FSMContext):
    t_type = call.data.split("_")[2]
    db_type = 'roblox_bind' if t_type == 'roblox' else 'report'
    
    conn = get_db()
    ticket = conn.execute("SELECT * FROM tickets WHERE type=? AND status='pending' LIMIT 1", (db_type,)).fetchone()
    conn.close()
    
    if not ticket: return await call.answer("✅ Тикетов нет.", show_alert=True)
        
    await state.update_data(ticket_id=ticket['id'], user_id=ticket['user_id'])
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm_t_accept_{ticket['id']}_{db_type}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_t_decline_{ticket['id']}")]
    ])
    
    if db_type == 'roblox_bind':
        await bot.send_video(call.from_user.id, ticket['content'], caption=f"🎫 <b>Привязка Roblox</b>\nTelegram ID: {ticket['user_id']}", reply_markup=kb)
    else:
        await bot.send_message(call.from_user.id, f"🚨 <b>Жалоба</b>\nОт ID: {ticket['user_id']}\nНа ID: {ticket['target_id']}\n\n{ticket['content']}", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("adm_t_decline_"), IsStaff())
async def admin_ticket_decline(call: CallbackQuery):
    t_id = int(call.data.split("_")[3])
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE tickets SET status='closed' WHERE id=?", (t_id,))
    conn.commit()
    conn.close()
    try: await call.message.edit_reply_markup(reply_markup=None)
    except: pass
    await call.message.reply("❌ Тикет отклонен.")
    await call.answer()

@router.callback_query(F.data.startswith("adm_t_accept_"), IsStaff())
async def admin_ticket_accept(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    t_id = int(parts[3])
    t_type = parts[4]
    
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE tickets SET status='closed' WHERE id=?", (t_id,))
    conn.commit()
    conn.close()
    
    try: await call.message.edit_reply_markup(reply_markup=None)
    except: pass
    
    if t_type == 'roblox_bind':
        await call.message.reply("Введите Ник и ID Roblox через пробел\n(Пример: <code>CoolNinja 1234567</code>):")
        await state.set_state(AppStates.admin_waiting_roblox_data)
    else:
        await call.message.reply("✅ Жалоба обработана.")
        await state.clear()
        
    await call.answer()

@router.message(AppStates.admin_waiting_roblox_data, IsStaff())
async def admin_save_roblox_data(message: Message, state: FSMContext):
    try:
        rbx_nick, rbx_id = message.text.split()
        data = await state.get_data()
        target_id = data.get('user_id')
        
        conn = get_db()
        blacklist_check = conn.execute("SELECT tg_id, status FROM users WHERE (roblox_username = ? OR roblox_id = ?) AND status = 'scammer'", (rbx_nick, rbx_id)).fetchone()
        
        if blacklist_check:
            conn.execute("UPDATE users SET status = 'scammer' WHERE tg_id = ?", (target_id,))
            conn.commit()
            conn.close()
            alert_msg = f"🚨 <b>АЛЕРТ СИСТЕМЫ: ПОПЫТКА ОБХОДА БАНА!</b>\nПользователь <code>{target_id}</code> попытался привязать Roblox <b>{rbx_nick}</b>, который ранее был в ЧС (связан с {blacklist_check['tg_id']}).\nНовый аккаунт автоматически ЗАБЛОКИРОВАН."
            await message.answer(alert_msg)
            try: await bot.send_message(ADMIN_ID, alert_msg)
            except: pass
            return await state.clear()

        conn.execute("UPDATE users SET roblox_username = ?, roblox_id = ? WHERE tg_id = ?", (rbx_nick, rbx_id, target_id))
        conn.commit()
        role = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()['role']
        conn.close()
        
        await message.answer(f"✅ Данные успешно привязаны!", reply_markup=get_admin_main_kb(role))
        try: await bot.send_message(target_id, f"🎉 <b>Roblox успешно привязан!</b>\nНик: {rbx_nick}\nID: {rbx_id}")
        except: pass
        
        await check_referral_bonus(target_id)
        await state.clear()
        
    except Exception as e:
        await message.answer("❌ Ошибка формата. Нужно ввести ровно два слова: Ник ID")

@router.callback_query(F.data == "admin_staff_manage", IsAdmin())
async def admin_staff_manage_panel(call: CallbackQuery, state: FSMContext):
    conn = get_db()
    mods = conn.execute("SELECT tg_id, username, role FROM users WHERE role IN ('senior_mod', 'junior_mod')").fetchall()
    conn.close()

    text = "🛡 <b>Персонал ГАД</b>\n\n"
    if mods:
        for mod in mods: text += f"• @{mod['username']} ({mod['role']})\n"
    else: text += "<i>Нет модераторов.</i>\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Назначить Старшего Мод.", callback_data="admin_add_sen_mod")],
        [InlineKeyboardButton(text="➕ Назначить Младшего Мод.", callback_data="admin_add_jun_mod")],
        [InlineKeyboardButton(text="➖ Разжаловать", callback_data="admin_rem_mod_start")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_cancel")]
    ])
    try: await call.message.edit_text(text, reply_markup=kb)
    except: pass
    await call.answer()

@router.callback_query(F.data.in_({"admin_add_sen_mod", "admin_add_jun_mod"}), IsAdmin())
async def add_mod_start(call: CallbackQuery, state: FSMContext):
    role = "senior_mod" if "sen" in call.data else "junior_mod"
    await state.update_data(mod_role=role)
    await call.message.edit_text("👤 Введите <b>@username</b> или <b>ID</b>:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_staff_manage")]]))
    await state.set_state(AppStates.admin_waiting_add_mod)
    await call.answer()

@router.message(AppStates.admin_waiting_add_mod, IsAdmin())
async def add_mod_process(message: Message, state: FSMContext):
    data = await state.get_data()
    role = data['mod_role']
    target = message.text.replace("@", "")
    conn = sqlite3.connect(DB_NAME)
    
    if target.isdigit(): user = conn.execute("SELECT tg_id FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else: user = conn.execute("SELECT tg_id FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()

    if not user:
        conn.close()
        return await message.answer("❌ Не найден.")

    conn.execute("UPDATE users SET role = ? WHERE tg_id = ?", (role, user[0]))
    conn.commit()
    conn.close()

    await message.answer(f"✅ Назначен на {role}!", reply_markup=get_admin_main_kb('admin'))
    try: await bot.send_message(user[0], "🎉 Вы назначены модератором. Напишите /start")
    except: pass
    await state.clear()

@router.callback_query(F.data == "admin_rem_mod_start", IsAdmin())
async def rem_mod_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("📉 Введите <b>@username</b> или <b>ID</b> для разжалования:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_staff_manage")]]))
    await state.set_state(AppStates.admin_waiting_rem_mod)
    await call.answer()

@router.message(AppStates.admin_waiting_rem_mod, IsAdmin())
async def rem_mod_process(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    conn = sqlite3.connect(DB_NAME)
    
    if target.isdigit(): user = conn.execute("SELECT tg_id, role FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else: user = conn.execute("SELECT tg_id, role FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()

    if not user or user[1] not in ['senior_mod', 'junior_mod']:
        conn.close()
        return await message.answer("❌ Не найден или не модератор.")

    conn.execute("UPDATE users SET role = 'user' WHERE tg_id = ?", (user[0],))
    conn.commit()
    conn.close()

    await message.answer(f"✅ Разжалован.", reply_markup=get_admin_main_kb('admin'))
    await state.clear()

@router.callback_query(F.data == "admin_backup_db", IsAdmin())
async def admin_backup_panel(call: CallbackQuery):
    try:
        await bot.send_document(call.from_user.id, FSInputFile(DB_NAME), caption=f"💾 Бэкап БД ГАД от {datetime.now().strftime('%Y-%m-%d %H:%M')}.")
        await call.answer("✅ Бэкап отправлен.")
    except Exception as e:
        await call.answer(f"❌ Ошибка: {e}", show_alert=True)

@router.callback_query(F.data == "admin_upload_db", IsAdmin())
async def admin_upload_db_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("📤 Отправьте файл <code>antiscam_pro.db</code> сюда. \n⚠️ Внимание! Это ПОЛНОСТЬЮ перезапишет текущую базу.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_cancel")]]))
    await state.set_state(AppStates.admin_waiting_db_upload)
    await call.answer()

@router.message(AppStates.admin_waiting_db_upload, IsAdmin())
async def admin_upload_db_process(message: Message, state: FSMContext):
    if not message.document or not message.document.file_name.endswith('.db'):
        return await message.answer("❌ Пожалуйста, отправьте валидный файл .db!")
        
    await message.answer("⏳ Скачивание и замена БД...")
    
    temp_file = "temp_db_upload.db"
    file = await bot.get_file(message.document.file_id)
    await bot.download_file(file.file_path, temp_file)
    
    try:
        test_conn = sqlite3.connect(temp_file)
        test_conn.execute("SELECT 1 FROM users LIMIT 1")
        test_conn.close()
        
        os.replace(temp_file, DB_NAME)
        await message.answer("✅ База данных успешно обновлена! Изменения вступили в силу.", reply_markup=get_admin_main_kb('admin'))
        
    except Exception as e:
        if os.path.exists(temp_file): os.remove(temp_file)
        await message.answer(f"❌ Ошибка проверки новой БД: {e}\nОригинальная база не тронута.", reply_markup=get_admin_main_kb('admin'))
        
    await state.clear()

@router.callback_query(F.data == "admin_broadcast", IsAdmin())
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("📢 Отправьте сообщение для рассылки ВСЕМ пользователям:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_cancel")]]))
    await state.set_state(AppStates.admin_waiting_broadcast_msg)
    await call.answer()

@router.message(AppStates.admin_waiting_broadcast_msg, IsAdmin())
async def admin_broadcast_send(message: Message, state: FSMContext):
    await message.answer("⏳ Начинаю рассылку...")
    conn = get_db()
    users = conn.execute("SELECT tg_id FROM users").fetchall()
    conn.close()
    
    success = 0
    for u in users:
        try:
            await message.copy_to(chat_id=u['tg_id'])
            success += 1
            await asyncio.sleep(0.05) 
        except: pass
        
    await message.answer(f"✅ Рассылка завершена! Доставлено: <b>{success}</b>", reply_markup=get_admin_main_kb('admin'))
    await state.clear()

async def main():
    dp.include_router(router)
    print("ГАД (Global Antiscam Database) запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
