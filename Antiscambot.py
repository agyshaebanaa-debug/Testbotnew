import asyncio
import sqlite3
import os
import time
import random
import string
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
            ref_claimed INTEGER DEFAULT 0,
            shadowban INTEGER DEFAULT 0
        )
    ''')
    
    # Умная миграция: добавление новых колонок, если их нет в старой БД
    cursor.execute("PRAGMA table_info(users)")
    columns = [info[1] for info in cursor.fetchall()]
    if 'role' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
    if 'badges' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN badges TEXT DEFAULT ''")
    if 'ref_claimed' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN ref_claimed INTEGER DEFAULT 0")
    if 'shadowban' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN shadowban INTEGER DEFAULT 0")
    
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
            staff_id INTEGER,
            action TEXT,
            target_id INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Таблица безопасных комнат
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS safe_rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_code TEXT,
            creator_id INTEGER,
            partner_id INTEGER,
            status TEXT DEFAULT 'waiting',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

async def check_shadow_cheat(user_id):
    """
    Теневой детектор накрутки.
    Если пользователь совершает >= 5 сделок или отзывов за последние 4 часа, 
    его аккаунт помечается флагом shadowban=1.
    """
    conn = get_db()
    # Считаем сделки за 4 часа
    trades = conn.execute('''
        SELECT COUNT(*) FROM trades_p2p 
        WHERE (initiator_id=? OR partner_id=?) 
        AND created_at >= datetime('now', '-4 hours')
    ''', (user_id, user_id)).fetchone()[0]
    
    # Считаем отзывы за 4 часа
    reviews = conn.execute('''
        SELECT COUNT(*) FROM reviews 
        WHERE reviewer_id=? 
        AND created_at >= datetime('now', '-4 hours')
    ''', (user_id,)).fetchone()[0]
    
    if (trades + reviews) >= 5:
        conn.execute("UPDATE users SET shadowban = 1 WHERE tg_id = ?", (user_id,))
        conn.commit()
        conn.close()
        
        # Уведомляем админов тихо
        try:
            await bot.send_message(
                ADMIN_ID, 
                f"🕷 <b>Сработал Теневой Детектор Накрутки!</b>\nПользователь ID <code>{user_id}</code> совершил >= 5 подозрительных действий за 4 часа. Выдан теневой бан."
            )
        except: pass
        return True
    conn.close()
    return False

# ==========================================
# ФИЛЬТРЫ ДОСТУПА И ИНДЕКС РИСКА
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
        return user and user['role'] in ['admin', 'senior_mod', 'moderator', 'junior_mod']

class IsPrivate(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.chat.type == "private"

def calculate_risk_index(user):
    """Динамический расчет индекса риска сделки с пользователем."""
    if user['status'] == 'scammer': return 100.0
    if user['status'] == 'garant': return 0.0
    
    risk = 65.0 # Базовый риск ноунейма
    
    # Снижение риска
    if user['roblox_id'] != 'Нет': risk -= 20.0
    risk -= min(user['trades'] * 2.0, 40.0) # До -40% за успешные сделки
    
    # Повышение риска
    if user['shadowban'] == 1: risk += 50.0
    
    return max(0.0, min(100.0, risk))

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
    conn = get_db()
    user = conn.execute("SELECT trades, status, rating_sum, reviews_count, badges FROM users WHERE tg_id = ?", (user_id,)).fetchone()
    
    if not user:
        conn.close()
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
    percent = (trades % 10)
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
        [KeyboardButton(text="🎮 Привязать Roblox"), KeyboardButton(text="🚨 Подать жалобу")],
        [KeyboardButton(text="🕸 Реф. Паутина"), KeyboardButton(text="🔒 Безопасная комната")]
    ], resize_keyboard=True)
    
    if user_role in ['admin', 'senior_mod', 'moderator', 'junior_mod']:
        builder.keyboard.append([KeyboardButton(text="👑 Админ Панель")])
        
    return builder

def get_cancel_reply_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)

def get_admin_main_kb(is_superadmin=False):
    kb = [
        [InlineKeyboardButton(text="🎫 Заявки Roblox", callback_data="admin_tickets_roblox"),
         InlineKeyboardButton(text="🚨 Жалобы", callback_data="admin_tickets_reports")],
        [InlineKeyboardButton(text="🔍 Управление юзером", callback_data="admin_search_user"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📡 Трекер отвязанных", callback_data="admin_unbound_tracker")]
    ]
    if is_superadmin:
        kb.append([InlineKeyboardButton(text="🛡 Управление Персоналом", callback_data="admin_staff_manage")])
        kb.append([InlineKeyboardButton(text="📢 Глобальная Рассылка", callback_data="admin_broadcast")])
        kb.append([InlineKeyboardButton(text="💾 Скачать бэкап БД", callback_data="admin_backup_db"),
                   InlineKeyboardButton(text="⬆️ Загрузить БД", callback_data="admin_upload_db")])
        
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_user_manage_kb(target_id, current_status, current_role, is_superadmin=False):
    buttons = []
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
    
    waiting_for_referral = State()
    waiting_for_safe_room_code = State()
    
    # Админские стейты
    admin_waiting_user_search = State()
    admin_waiting_roblox_data = State()
    admin_waiting_broadcast_msg = State()
    admin_waiting_upload_db = State()
    
    # Стейты управления персоналом
    admin_waiting_add_mod = State()
    admin_waiting_mod_role = State()
    admin_waiting_rem_mod = State()
    
    target_ticket_id = None
    target_mod_id = None

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
        conn.execute("UPDATE users SET username = ? WHERE tg_id = ?", (message.from_user.username or "Без_Ника", message.from_user.id))
        conn.commit()
    conn.close()
    
    await message.answer(
        f"👋 Добро пожаловать в <b>ГАД (Global Antiscam Database)</b>, <b>{message.from_user.first_name}</b>!\n\n"
        f"🛡 Это официальная международная база данных репутации.\n"
        f"Здесь вы можете фиксировать сделки, использовать безопасные комнаты и искать верифицированных гарантов.\n\n"
        f"Используйте кнопки ниже 👇",
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
    await message.answer("✅ Действие отменено. Вы вернулись в главное меню.", reply_markup=get_main_reply_kb(role))

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
    risk = calculate_risk_index(user)
    
    status_text = "🟢 Обычный пользователь"
    if user['status'] == 'garant': status_text = "⚖️ Официальный Гарант ГАД"
    elif user['status'] == 'scammer': status_text = "☠️ В ЧЁРНОМ СПИСКЕ (СКАМ)"

    badges_list = user['badges'].split(',') if user['badges'] else []
    badges_display = "\n".join([f"🏅 {ACHIEVEMENTS.get(b, b)}" for b in badges_list if b in ACHIEVEMENTS])
    if not badges_display: badges_display = "<i>Пока нет достижений</i>"

    shadow_warning = "\n⚠️ <b>Теневой бан:</b> На аккаунте подозрительная активность" if user['shadowban'] else ""

    text = (
        f"👤 <b>Ваш Профиль ГАД:</b>\n"
        f"🔖 <b>ID:</b> <code>{user['tg_id']}</code>\n"
        f"🎮 <b>Roblox:</b> <code>{user['roblox_username']}</code> (ID: {user['roblox_id']})\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏆 <b>Титул:</b> {title}\n"
        f"⚖️ <b>Статус:</b> {status_text}\n"
        f"🤝 <b>Сделок:</b> {user['trades']}\n"
        f"⭐️ <b>Рейтинг:</b> {rating}\n"
        f"📉 <b>Индекс риска:</b> {risk:.1f}%\n"
        f"📊 <b>Прогресс до ранга:</b>\n<code>{progress}</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎖 <b>Ваши Достижения:</b>\n{badges_display}"
        f"{shadow_warning}"
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
    risk = calculate_risk_index(user)
    
    text = (
        f"🧾 <b>ОФИЦИАЛЬНЫЙ ЧЕК ТРЕЙДЕРА ГАД</b> 🧾\n\n"
        f"👤 <b>Пользователь:</b> @{user['username']} (ID: <code>{user['tg_id']}</code>)\n"
        f"🎮 <b>Roblox:</b> <code>{user['roblox_username']}</code>\n"
        f"✅ <b>Успешных сделок:</b> {user['trades']}\n"
        f"⭐️ <b>Рейтинг:</b> {rating}\n"
        f"📉 <b>Индекс риска:</b> {risk:.1f}%\n"
        f"⚖️ <b>Статус:</b> {user['status'].upper()}\n"
    )
    if badges_display:
        text += f"🎖 <b>Награды:</b> {badges_display}\n"
        
    text += f"\n<i>✅ Верифицировано ботом ГАД (Global Antiscam Database)</i>"
    await message.answer(text)

# ==========================================
# НОВИНКИ: РЕФЕРАЛЬНАЯ ПАУТИНА И БЕЗОПАСНЫЕ КОМНАТЫ
# ==========================================
@router.message(F.text == "🕸 Реф. Паутина", IsPrivate())
async def referral_web_start(message: Message, state: FSMContext):
    conn = get_db()
    user = conn.execute("SELECT ref_claimed FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    conn.close()
    
    if user['ref_claimed'] == 1:
        return await message.answer("❌ Вы уже активировали свою связь в реферальной паутине. Этот бонус одноразовый.")
        
    await message.answer("🕸 <b>Реферальная паутина</b>\n\nЗдесь вы можете указать ID человека, который пригласил вас в ГАД. Он получит <b>мощный одноразовый буст к рейтингу</b> в благодарность!\n\nВведите <b>Telegram ID</b> вашего пригласителя:", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_referral)

@router.message(AppStates.waiting_for_referral, IsPrivate())
async def referral_web_process(message: Message, state: FSMContext):
    target_id = message.text.strip()
    if not target_id.isdigit():
        return await message.answer("❌ Введите корректный числовой Telegram ID.")
        
    target_id = int(target_id)
    if target_id == message.from_user.id:
        return await message.answer("❌ Вы не можете указать самого себя.")
        
    conn = sqlite3.connect(DB_NAME)
    target = conn.execute("SELECT * FROM users WHERE tg_id = ?", (target_id,)).fetchone()
    
    if not target:
        conn.close()
        return await message.answer("❌ Пользователь с таким ID не найден в базе ГАД.")
        
    # Буст: добавляем вес одного 5.0 отзыва
    conn.execute("UPDATE users SET rating_sum = rating_sum + 5.0, reviews_count = reviews_count + 1 WHERE tg_id = ?", (target_id,))
    conn.execute("UPDATE users SET ref_claimed = 1 WHERE tg_id = ?", (message.from_user.id,))
    
    user_me = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    role = user_me[0] if user_me else 'user'
    
    conn.commit()
    conn.close()
    
    await message.answer("✅ Паутина сплетена! Ваш пригласитель получил буст рейтинга.", reply_markup=get_main_reply_kb(role))
    try:
        await bot.send_message(target_id, f"🕸 <b>Реферальная Паутина сработала!</b>\nПользователь ID <code>{message.from_user.id}</code> указал вас как пригласителя. Вы получили мощный буст к вашему рейтингу!")
    except: pass
    
    await state.clear()

@router.message(F.text == "🔒 Безопасная комната", IsPrivate())
async def safe_room_menu(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать комнату", callback_data="saferoom_create")],
        [InlineKeyboardButton(text="🔗 Войти по коду", callback_data="saferoom_join")]
    ])
    await message.answer("🔒 <b>Безопасные комнаты ГАД</b>\n\nЭто виртуальные изолированные сессии. Создайте комнату, передайте код партнеру. Когда вы оба в комнате, бот зафиксирует факт безопасной сделки.", reply_markup=kb)

@router.callback_query(F.data == "saferoom_create")
async def safe_room_create(call: CallbackQuery):
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    conn = sqlite3.connect(DB_NAME)
    conn.execute("INSERT INTO safe_rooms (room_code, creator_id) VALUES (?, ?)", (code, call.from_user.id))
    conn.commit()
    conn.close()
    
    await call.message.edit_text(f"✅ <b>Комната создана!</b>\n\nВаш секретный код: <code>{code}</code>\n\nПередайте его партнеру. Ждем его подключения...")
    await call.answer()

@router.callback_query(F.data == "saferoom_join")
async def safe_room_join_start(call: CallbackQuery, state: FSMContext):
    await call.message.delete()
    await bot.send_message(call.from_user.id, "🔗 Введите 6-значный код Безопасной Комнаты:", reply_markup=get_cancel_reply_kb())
    await state.set_state(AppStates.waiting_for_safe_room_code)
    await call.answer()

@router.message(AppStates.waiting_for_safe_room_code, IsPrivate())
async def safe_room_join_process(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    conn = sqlite3.connect(DB_NAME)
    room = conn.execute("SELECT * FROM safe_rooms WHERE room_code = ? AND status = 'waiting'", (code,)).fetchone()
    
    user = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    role = user[0] if user else 'user'
    
    if not room:
        conn.close()
        return await message.answer("❌ Комната не найдена или уже занята.", reply_markup=get_main_reply_kb(role))
        
    if room['creator_id'] == message.from_user.id:
        conn.close()
        return await message.answer("❌ Вы не можете войти в собственную комнату.", reply_markup=get_main_reply_kb(role))
        
    conn.execute("UPDATE safe_rooms SET partner_id = ?, status = 'active' WHERE id = ?", (message.from_user.id, room['id']))
    conn.commit()
    conn.close()
    
    await message.answer("🔒 <b>Вы успешно вошли в Безопасную Комнату!</b>\nТеперь ГАД фиксирует вашу сделку как официально начатую в безопасном режиме. Вы можете переходить к трейду в игре.", reply_markup=get_main_reply_kb(role))
    try:
        await bot.send_message(room['creator_id'], f"🔒 <b>Партнер вошел в комнату! (ID: {message.from_user.id})</b>\nГАД зафиксировал сессию. Проводите трейд безопасно.")
    except: pass
    
    await state.clear()

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

    # Проверка на теневой бан
    if await check_shadow_cheat(message.from_user.id):
        conn.close()
        return await message.answer("⚠️ Ваша активность подозрительна. Создание сделок временно приостановлено.")

    cursor = conn.cursor()
    cursor.execute("INSERT INTO trades_p2p (initiator_id, partner_id) VALUES (?, ?)", (message.from_user.id, partner['tg_id']))
    trade_id = cursor.lastrowid
    conn.commit()
    
    user_me = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    role = user_me['role'] if user_me else 'user'
    conn.close()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить сделку", callback_data=f"p2p_accept_{trade_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"p2p_decline_{trade_id}")]
    ])
    
    try:
        await bot.send_message(partner['tg_id'], f"🤝 <b>Новый запрос на сделку!</b>\nПользователь @{message.from_user.username} утверждает, что провел с вами успешный трейд.\nПодтверждаете?", reply_markup=kb)
        await message.answer("✅ Запрос отправлен партнеру. Ожидайте подтверждения.", reply_markup=get_main_reply_kb(role))
    except:
        await message.answer("❌ Не удалось отправить сообщение партнеру (бот заблокирован или юзер не запускал бота).", reply_markup=get_main_reply_kb(role))
    
    await state.clear()

@router.callback_query(F.data.startswith("p2p_accept_"))
async def p2p_accept(call: CallbackQuery):
    trade_id = int(call.data.split("_")[2])
    
    if await check_shadow_cheat(call.from_user.id):
        return await call.answer("⚠️ Вы заблокированы теневым детектором.", show_alert=True)
        
    conn = sqlite3.connect(DB_NAME)
    trade = conn.execute("SELECT * FROM trades_p2p WHERE id = ?", (trade_id,)).fetchone()
    
    if not trade or trade[3] != 'pending': 
        conn.close()
        return await call.answer("❌ Сделка уже обработана.", show_alert=True)
        
    # Проверка теневого бана обоих юзеров
    u1 = conn.execute("SELECT shadowban FROM users WHERE tg_id = ?", (trade[1],)).fetchone()
    u2 = conn.execute("SELECT shadowban FROM users WHERE tg_id = ?", (trade[2],)).fetchone()
    if (u1 and u1[0]) or (u2 and u2[0]):
        conn.close()
        return await call.answer("Сделка отклонена антифрод-системой ГАД.", show_alert=True)

    conn.execute("UPDATE users SET trades = trades + 1 WHERE tg_id IN (?, ?)", (trade[1], trade[2]))
    conn.execute("UPDATE trades_p2p SET status = 'accepted' WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()
    
    try:
        await call.message.edit_text("✅ Сделка подтверждена! Вам обоим начислена +1 сделка в статистику.")
    except TelegramBadRequest: pass
    
    try:
        await bot.send_message(trade[1], "🎉 Партнер подтвердил сделку! Вам начислена +1 сделка.")
    except: pass
    
    await check_achievements(trade[1])
    await check_achievements(trade[2])

@router.callback_query(F.data.startswith("p2p_decline_"))
async def p2p_decline(call: CallbackQuery):
    trade_id = int(call.data.split("_")[2])
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE trades_p2p SET status = 'declined' WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()
    try:
        await call.message.edit_text("❌ Вы отклонили сделку.")
    except TelegramBadRequest: pass

# ==========================================
# ОБРАБОТЧИКИ: ОТЗЫВЫ И РЕЙТИНГИ
# ==========================================
@router.message(F.text == "⭐ Оставить отзыв", IsPrivate())
async def review_start(message: Message, state: FSMContext):
    await message.answer("Введи @username или ID Гаранта, которому хочешь оставить отзыв:\n\n<i>Отзывы можно оставлять только официальным гарантам проекта ГАД.</i>", reply_markup=get_cancel_reply_kb())
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
    if await check_shadow_cheat(message.from_user.id):
        return await message.answer("⚠️ Вы заблокированы антифрод-системой ГАД. Отзыв не засчитан.")

    data = await state.get_data()
    conn = sqlite3.connect(DB_NAME)
    
    # Проверка: не в теневом ли бане юзер/гарант
    u1 = conn.execute("SELECT shadowban FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    if u1 and u1[0]:
        conn.close()
        return await message.answer("Ваш аккаунт в теневом бане, действия отклонены.")

    conn.execute("INSERT INTO reviews (reviewer_id, target_id, rating, review_text) VALUES (?, ?, ?, ?)",
                 (message.from_user.id, data['target_id'], data['stars'], message.text))
    conn.execute("UPDATE users SET rating_sum = rating_sum + ?, reviews_count = reviews_count + 1 WHERE tg_id = ?",
                 (data['stars'], data['target_id']))
    
    user = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    role = user[0] if user else 'user'
    
    conn.commit()
    conn.close()
    
    await message.answer("✅ Отзыв успешно опубликован! Спасибо за вклад в безопасность комьюнити ГАД.", reply_markup=get_main_reply_kb(role))
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
        
    text = "🛡 <b>Список Официальных Гарантов ГАД:</b>\n\n"
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
        
    text = "🚨 <b>Последние заблокированные ГАД (ЧС):</b>\n\n"
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
    text = "🏆 <b>ТОП-10 Трейдеров базы ГАД:</b>\n\n"
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
    role = user['role'] if user else 'user'
    conn.close()
        
    if not garants:
        await message.answer("😔 К сожалению, сейчас в базе нет доступных гарантов.", reply_markup=get_main_reply_kb(role))
        return await state.clear()
        
    notified = 0
    for g in garants:
        try:
            await bot.send_message(
                g['tg_id'], 
                f"🛎 <b>СРОЧНЫЙ ВЫЗОВ ГАРАНТА ГАД</b>\nОт: @{message.from_user.username}\n\n<b>Условия:</b> <i>{desc}</i>\n\n👉 Свяжитесь с клиентом в ЛС, если готовы взять заказ."
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
    
    await message.answer("✅ Видео отправлено модераторам ГАД на проверку. Мы привяжем аккаунт, как только посмотрим его!", reply_markup=get_main_reply_kb(role))
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
    
    await message.answer("✅ Ваша жалоба зафиксирована и передана в арбитраж ГАД. Спасибо за бдительность!", reply_markup=get_main_reply_kb(role))
    await state.clear()

# ==========================================
# ОБРАБОТЧИКИ: ПРОВЕРКА И ЗАЩИТА ГРУПП (GROUP SHIELD)
# ==========================================
@router.message(Command("check"))
@router.message(F.text == "🔍 Проверить")
async def check_user_start(message: Message, state: FSMContext):
    if message.chat.type != "private":
        parts = message.text.split()
        if len(parts) < 2:
            return await message.reply("Использование: <code>/check @username</code> или <code>/check ID</code>")
        target = parts[1].replace("@", "")
        return await send_check_result(message, target)
        
    await message.answer("🔍 Введите <b>@username</b> или <b>ID</b> пользователя для проверки по базе ГАД:", reply_markup=get_cancel_reply_kb())
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
        text = "❓ <b>Пользователь не найден в базе ГАД.</b>\nБудьте осторожны при сделках с ноунеймами."
        if state:
            role = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()[0]
            await message.answer(text, reply_markup=get_main_reply_kb(role))
            return await state.clear()
        else:
            return await message.reply(text)
    
    conn.close()
    rating = format_rating(user['rating_sum'], user['reviews_count'])
    title = get_user_title(user['trades'])
    risk = calculate_risk_index(user)
    
    text = (
        f"📋 <b>Досье пользователя ГАД:</b>\n"
        f"👤 @{user['username']} (<code>{user['tg_id']}</code>)\n"
        f"🎮 Roblox: <code>{user['roblox_username']}</code>\n"
        f"🏆 Титул: {title}\n"
        f"🤝 Успешных сделок: <b>{user['trades']}</b>\n"
        f"⭐️ Рейтинг: {rating}\n"
        f"📉 Индекс риска: <b>{risk:.1f}%</b>\n"
    )
    if user['status'] == 'scammer':
        text = "‼️ <b>ВНИМАНИЕ! СКАМЕР ИЗ ЧС ГАД! БЕЗ СДЕЛОК!</b> ‼️\n\n" + text
    elif user['status'] == 'garant':
        text = "⚖️ <b>ОФИЦИАЛЬНЫЙ ГАРАНТ ГАД</b> ⚖️\n\n" + text
        
    if state:
        conn = get_db()
        role = conn.execute("SELECT role FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()[0]
        conn.close()
        await message.answer(text, reply_markup=get_main_reply_kb(role))
        await state.clear()
    else:
        await message.reply(text)

@router.message(Command("ban"), IsModOrAdmin(), F.chat.type.in_({'group', 'supergroup'}))
async def group_ban_command(message: Message):
    if not message.reply_to_message:
        return await message.reply("Ответьте на сообщение пользователя командой /ban, чтобы забанить его в этой группе.")
    target_id = message.reply_to_message.from_user.id
    try:
        await bot.ban_chat_member(chat_id=message.chat.id, user_id=target_id)
        await message.reply("🔨 Пользователь успешно забанен администратором/модератором ГАД.")
    except Exception as e:
        await message.reply(f"❌ Не удалось забанить. Возможно, у меня нет прав. Ошибка: {e}")

@router.message(F.chat.type.in_({'group', 'supergroup'}))
async def group_shield_handler(message: Message):
    if not message.from_user: return
    
    conn = get_db()
    user = conn.execute("SELECT status, username FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    
    if not user:
        conn.execute("INSERT INTO users (tg_id, username) VALUES (?, ?)", (message.from_user.id, message.from_user.username or "Без_Ника"))
        conn.commit()
    elif user['username'] != message.from_user.username:
        conn.execute("UPDATE users SET username = ? WHERE tg_id = ?", (message.from_user.username, message.from_user.id))
        conn.commit()
        
    if user and user['status'] == 'scammer':
        try:
            await message.delete() 
            await bot.ban_chat_member(chat_id=message.chat.id, user_id=message.from_user.id)
            await message.answer(f"🚨 <b>СКАМЕР ОБНАРУЖЕН СИСТЕМОЙ ГАД И УСТРАНЕН!</b>\nУчастник @{message.from_user.username} числится в Черном Списке. Бот удалил его из группы.")
        except:
            await message.answer(f"⚠️ <b>ВНИМАНИЕ ЧАТУ!</b>\nЭтот участник (@{message.from_user.username}) находится в глобальной базе скамеров ГАД! Выдайте боту права администратора (Ban Users), чтобы он автоматически удалял таких людей.")
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
    
    role_name = user['role'].replace('_', ' ').title() if not is_super else "Super Admin"
    
    text = (
        f"👑 <b>GAD Enterprise Dashboard</b> [{role_name}]\n\n"
        f"🎫 Ожидают привязки Roblox: <b>{tickets_roblox}</b>\n"
        f"🚨 Неразобранные жалобы: <b>{tickets_reports}</b>\n\n"
        "Выберите действие ниже:"
    )
    await message.answer(text, reply_markup=get_admin_main_kb(is_super))

@router.callback_query(F.data == "admin_cancel", IsModOrAdmin())
async def admin_cancel_call(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.delete()
    except TelegramBadRequest: pass
    await admin_panel_start(call.message)
    await call.answer()

@router.callback_query(F.data == "admin_stats", IsModOrAdmin())
async def admin_stats_panel(call: CallbackQuery):
    conn = get_db()
    total_u = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_t = conn.execute("SELECT SUM(trades) FROM users").fetchone()[0] or 0
    total_s = conn.execute("SELECT COUNT(*) FROM users WHERE status='scammer'").fetchone()[0]
    garants = conn.execute("SELECT COUNT(*) FROM users WHERE status='garant'").fetchone()[0]
    mods = conn.execute("SELECT COUNT(*) FROM users WHERE role IN ('moderator', 'senior_mod', 'junior_mod')").fetchone()[0]
    
    is_super = conn.execute("SELECT role FROM users WHERE tg_id = ?", (call.from_user.id,)).fetchone()[0] == 'admin'
    conn.close()
        
    text = (
        f"📊 <b>Статистика GAD Bot:</b>\n\n"
        f"👥 Всего юзеров: <b>{total_u}</b>\n"
        f"🤝 Сделок (всего): <b>{total_t}</b>\n"
        f"🛡 Гарантов: <b>{garants}</b>\n"
        f"👮‍♂️ Персонала: <b>{mods}</b>\n"
        f"☠️ Скамеров в ЧС: <b>{total_s}</b>"
    )
    try:
        await call.message.edit_text(text, reply_markup=get_admin_main_kb(is_super))
    except TelegramBadRequest: pass
    await call.answer()

@router.callback_query(F.data == "admin_unbound_tracker", IsModOrAdmin())
async def unbound_tracker(call: CallbackQuery):
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM users WHERE roblox_id = 'Нет'").fetchone()[0]
    is_super = conn.execute("SELECT role FROM users WHERE tg_id = ?", (call.from_user.id,)).fetchone()[0] == 'admin'
    conn.close()
    
    text = f"📡 <b>Трекер отвязанных аккаунтов ГАД:</b>\n\nКоличество пользователей без привязанного Roblox аккаунта: <b>{count}</b>"
    
    try:
        await call.message.edit_text(text, reply_markup=get_admin_main_kb(is_super))
    except TelegramBadRequest: pass
    await call.answer()

# === УПРАВЛЕНИЕ ПЕРСОНАЛОМ (HR МОДУЛЬ) ===
@router.callback_query(F.data == "admin_staff_manage", IsSuperAdmin())
async def admin_staff_manage_panel(call: CallbackQuery, state: FSMContext):
    conn = get_db()
    mods = conn.execute("SELECT tg_id, username, role FROM users WHERE role IN ('moderator', 'senior_mod', 'junior_mod')").fetchall()
    conn.close()

    text = "🛡 <b>Управление Персоналом ГАД (Модераторы)</b>\n\n<b>Текущий состав:</b>\n"
    if mods:
        for mod in mods:
            role_p = mod['role'].replace('_', ' ').title()
            text += f"• @{mod['username']} (<code>{mod['tg_id']}</code>) — {role_p}\n"
    else:
        text += "<i>Нет назначенных модераторов.</i>\n"

    text += "\n👇 <i>Выберите действие:</i>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Назначить/Изменить модератора", callback_data="admin_add_mod_start")],
        [InlineKeyboardButton(text="➖ Разжаловать модератора", callback_data="admin_rem_mod_start")],
        [InlineKeyboardButton(text="◀️ Назад в Админку", callback_data="admin_cancel")]
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest: pass
    await call.answer()

@router.callback_query(F.data == "admin_add_mod_start", IsSuperAdmin())
async def add_mod_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("👤 Введите <b>@username</b> или <b>ID</b> пользователя, которого хотите назначить (или изменить ранг) МОДЕРАТОРОМ:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_staff_manage")]]))
    await state.set_state(AppStates.admin_waiting_add_mod)
    await call.answer()

@router.message(AppStates.admin_waiting_add_mod, IsSuperAdmin())
async def add_mod_process(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    conn = get_db()
    if target.isdigit():
        user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else:
        user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()
    conn.close()
        
    if not user:
        return await message.answer("❌ Пользователь не найден в базе.")
    if user['role'] == 'admin':
        return await message.answer("❌ Это Супер-Админ. Его нельзя понизить до модератора таким образом.")

    await state.update_data(target_mod_id=user['tg_id'])
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Junior Mod", callback_data="admrole_junior_mod")],
        [InlineKeyboardButton(text="🟡 Moderator", callback_data="admrole_moderator")],
        [InlineKeyboardButton(text="🔴 Senior Mod", callback_data="admrole_senior_mod")]
    ])
    
    await message.answer(f"Выберите класс модератора для пользователя @{user['username']}:", reply_markup=kb)
    await state.set_state(AppStates.admin_waiting_mod_role)

@router.callback_query(F.data.startswith("admrole_"), IsSuperAdmin())
async def set_mod_role(call: CallbackQuery, state: FSMContext):
    role = call.data.split("_", 1)[1]
    data = await state.get_data()
    target_id = data.get('target_mod_id')
    
    conn = get_db()
    conn.execute("UPDATE users SET role = ? WHERE tg_id = ?", (role, target_id))
    conn.commit()
    conn.close()
    
    log_audit(call.from_user.id, f"Promoted to {role}", target_id)
    await call.message.edit_text(f"✅ Пользователю назначен класс <b>{role.replace('_', ' ').title()}</b>!", reply_markup=get_admin_main_kb(True))
    
    try:
        await bot.send_message(target_id, f"🎉 <b>Вам выданы права: {role.replace('_', ' ').title()}!</b> Введите /start чтобы обновить меню.")
    except: pass
    await state.clear()

@router.callback_query(F.data == "admin_rem_mod_start", IsSuperAdmin())
async def rem_mod_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("📉 Введите <b>@username</b> или <b>ID</b> модератора для разжалования:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_staff_manage")]]))
    await state.set_state(AppStates.admin_waiting_rem_mod)
    await call.answer()

@router.message(AppStates.admin_waiting_rem_mod, IsSuperAdmin())
async def rem_mod_process(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    conn = get_db()
    if target.isdigit():
        user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else:
        user = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (target,)).fetchone()

    if not user or user['role'] not in ['moderator', 'senior_mod', 'junior_mod']:
        conn.close()
        return await message.answer("❌ Пользователь не найден или не является модератором.")

    conn.execute("UPDATE users SET role = 'user' WHERE tg_id = ?", (user['tg_id'],))
    conn.commit()
    conn.close()

    log_audit(message.from_user.id, "Demoted moderator", user['tg_id'])
    await message.answer(f"✅ Пользователь @{user['username']} разжалован до обычного юзера.", reply_markup=get_admin_main_kb(True))
    try:
        await bot.send_message(user['tg_id'], "📉 Вы были лишены прав модератора.")
    except: pass
    await state.clear()

# === ПОИСК И УПРАВЛЕНИЕ ЮЗЕРАМИ ===
@router.callback_query(F.data == "admin_search_user", IsModOrAdmin())
async def admin_search_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("🔍 <b>Админ Поиск ГАД</b>\nВведите @username или Telegram ID юзера:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_cancel")]]))
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
            f"Сделок: {user['trades']} | Рейтинг: {user['rating_sum']}/{user['reviews_count']}\n"
            f"Теневой бан: {'Да' if user['shadowban'] else 'Нет'}")
            
    await message.answer(text, reply_markup=get_admin_user_manage_kb(user['tg_id'], user['status'], user['role'], is_super))
    await state.clear()

@router.callback_query(F.data.startswith("admset_") | F.data.startswith("admadd_") | F.data.startswith("admclr_"), IsModOrAdmin())
async def admin_manage_user(call: CallbackQuery):
    parts = call.data.split("_")
    action_type = parts[0]
    action = parts[1]
    target_id = int(parts[2])
    
    conn = sqlite3.connect(DB_NAME)
    staff = conn.execute("SELECT role FROM users WHERE tg_id = ?", (call.from_user.id,)).fetchone()
    is_super = staff[0] == 'admin'
    
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
        conn.execute("UPDATE users SET trades = 0, rating_sum = 0, reviews_count = 0, badges = '', shadowban = 0 WHERE tg_id = ?", (target_id,))
        text = "🧹 Статистика, ачивки и теневые баны очищены."
        log_audit(call.from_user.id, "Cleared stats", target_id)
        
    conn.commit()
    conn.close()
    
    await call.answer(text, show_alert=True)
    try:
        await call.message.delete()
    except TelegramBadRequest: pass
    await admin_panel_start(call.message)

# === ТИКЕТЫ (ПРИВЯЗКА И ЖАЛОБЫ) ===
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
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except: pass
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
    
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except: pass
    
    if t_type == 'roblox_bind':
        await call.message.reply("Введите Ник и ID Roblox через пробел\n(Пример: <code>CoolNinja 1234567</code>):")
        await state.set_state(AppStates.admin_waiting_roblox_data)
    else:
        await call.message.reply("✅ Жалоба помечена как обработанная. Вы можете найти нарушителя через «Управление юзером» и выдать ЧС.")
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
            await bot.send_message(target_id, f"🎉 <b>Ваш аккаунт Roblox успешно верифицирован и привязан к ГАД!</b>\nНик: {rbx_nick}\nID: {rbx_id}")
        except: pass
        await state.clear()
    except Exception:
        await message.answer("❌ Ошибка формата. Нужно ввести ровно два слова: Ник ID")

# ==========================================
# СУПЕР-АДМИН ФУНКЦИИ (Рассылка, Бэкап, Загрузка БД)
# ==========================================
@router.callback_query(F.data == "admin_broadcast", IsSuperAdmin())
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("📢 Отправьте текст (можно с картинкой/видео), который нужно разослать ВСЕМ пользователям бота:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_cancel")]]))
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
            await asyncio.sleep(0.05) 
        except: pass
        
    await message.answer(f"✅ Рассылка завершена!\nУспешно доставлено: <b>{success}</b> пользователям.", reply_markup=get_admin_main_kb(True))
    log_audit(message.from_user.id, f"Broadcasted message to {success} users")
    await state.clear()

@router.callback_query(F.data == "admin_backup_db", IsSuperAdmin())
async def admin_backup_panel(call: CallbackQuery):
    try:
        await bot.send_document(call.from_user.id, FSInputFile(DB_NAME), caption=f"💾 Ручной бэкап БД ГАД от {datetime.now().strftime('%Y-%m-%d %H:%M')}.")
        log_audit(call.from_user.id, "Downloaded DB Backup")
        await call.answer("✅ Бэкап отправлен.")
    except Exception as e:
        await call.answer(f"❌ Ошибка: {e}", show_alert=True)

@router.callback_query(F.data == "admin_upload_db", IsSuperAdmin())
async def admin_upload_db_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("⬆️ <b>Внимание!</b> Это действие перезапишет текущую базу данных ГАД.\n\nОтправьте файл `.db` в этот чат (как документ):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_cancel")]]))
    await state.set_state(AppStates.admin_waiting_upload_db)
    await call.answer()

@router.message(AppStates.admin_waiting_upload_db, IsSuperAdmin(), F.document)
async def admin_upload_db_process(message: Message, state: FSMContext):
    if not message.document.file_name.endswith('.db'):
        return await message.answer("❌ Файл должен быть в формате .db!")
        
    msg = await message.answer("⏳ Скачивание и замена БД...")
    
    try:
        file = await bot.get_file(message.document.file_id)
        await bot.download_file(file.file_path, DB_NAME)
        
        log_audit(message.from_user.id, "Uploaded new DB file")
        await msg.edit_text("✅ <b>База данных успешно загружена и заменена!</b>\nВсе новые данные уже применены.", reply_markup=get_admin_main_kb(True))
    except Exception as e:
        await msg.edit_text(f"❌ Произошла ошибка при загрузке БД: {e}", reply_markup=get_admin_main_kb(True))
        
    await state.clear()

# ==========================================
# ФОНОВЫЕ ЗАДАЧИ И ЗАПУСК
# ==========================================
async def auto_backup():
    while True:
        await asyncio.sleep(3600) # Авто-бэкап каждый час (3600 сек)
        try:
            await bot.send_document(ADMIN_ID, FSInputFile(DB_NAME), caption=f"💾 АВТО-БЭКАП БД ГАД | {datetime.now().strftime('%H:%M')}")
        except: pass

async def main():
    dp.include_router(router)
    print("ГАД (Global Antiscam Database) Успешно запущен!")
    
    asyncio.create_task(auto_backup())
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
