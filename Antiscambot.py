import asyncio
import sqlite3
import logging
import os
from datetime import datetime
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated, FSInputFile,
    BotCommand, BotCommandScopeDefault
)
from aiogram.filters import CommandStart, Command, BaseFilter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest

# ================= НАСТРОЙКИ БОТА =================
BOT_TOKEN = "8792564218:AAHo3taU03G4FGAtIovL6mdSNXRA72QrtE0"
ADMIN_ID = 5341904332  # ЗАМЕНИ НА СВОЙ ID!

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ================= БАЗА ДАННЫХ (ENTERPRISE STRUCTURE) =================
# Эта структура идеально читается в DB Browser for SQLite
def init_db():
    with sqlite3.connect("antiscam_pro.db") as conn:
        cursor = conn.cursor()
        
        # 1. Таблица Пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                tg_username TEXT,
                roblox_username TEXT DEFAULT 'Не привязан',
                roblox_id TEXT DEFAULT 'Не привязан',
                trades INTEGER DEFAULT 0,
                scams INTEGER DEFAULT 0,
                status TEXT DEFAULT 'user', -- user, garant, scammer, admin
                rating_sum INTEGER DEFAULT 0,
                reviews_count INTEGER DEFAULT 0,
                join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 2. Таблица Логирования Сделок (P2P) - НОВОЕ!
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                initiator_id INTEGER,
                partner_id INTEGER,
                description TEXT,
                status TEXT DEFAULT 'pending', -- pending, completed, cancelled
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 3. Таблица Отзывов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reviewer_id INTEGER,
                target_id INTEGER,
                trade_id INTEGER, -- Привязка к конкретной сделке
                rating INTEGER,
                review_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 4. Таблица Тикетов (Жалобы и Привязки)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT, 
                data TEXT, 
                proof_msg_id INTEGER, 
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 5. Таблица Аудита (Действия Админов) - НОВОЕ!
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

def get_db():
    """Возвращает подключение как словарь (Row) для удобства доступа к колонкам"""
    conn = sqlite3.connect("antiscam_pro.db")
    conn.row_factory = sqlite3.Row
    return conn

def log_admin_action(admin_id: int, action: str, target_id: int):
    """Скрытно записывает действия админа в базу для безопасности"""
    with get_db() as conn:
        conn.execute("INSERT INTO audit_log (admin_id, action, target_id) VALUES (?, ?, ?)", 
                     (admin_id, action, target_id))
        conn.commit()

# ================= FSM СОСТОЯНИЯ =================
class RobloxLink(StatesGroup):
    waiting_for_video = State()

class ReportUser(StatesGroup):
    waiting_for_target = State()
    waiting_for_proofs = State()

class LogTrade(StatesGroup):
    waiting_for_partner = State()
    waiting_for_description = State()

class LeaveReview(StatesGroup):
    waiting_for_stars = State()
    waiting_for_text = State()
    trade_id = None
    target_id = None

class AdminPanelStates(StatesGroup):
    waiting_for_user_search = State()
    waiting_for_roblox_data = State()
    target_ticket_id = None

# ================= ФИЛЬТРЫ =================
class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id == ADMIN_ID

class IsGroup(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.chat.type in ["group", "supergroup"]

class IsPrivate(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.chat.type == "private"

# ================= ХЕЛПЕРЫ =================
def get_user_title(trades: int, scams: int) -> str:
    if scams > 0: return "🏴‍☠️ Изгой (СКАМЕР)"
    if trades == 0: return "🌱 Новичок"
    if trades < 5: return "⚔️ Начинающий"
    if trades < 20: return "🛡 Опытный Трейдер"
    if trades < 50: return "🌟 Мастер Трейда"
    if trades < 100: return "💎 Гранд-Мастер"
    return "👑 Легенда Роблокса"

def generate_trust_bar(trades: int, scams: int) -> str:
    if scams > 0: return "🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥 [В ЧС]"
    if trades == 0: return "⬜️⬜️⬜️⬜️⬜️⬜️⬜️⬜️⬜️⬜️ [0%]"
    score = min(10, (trades // 3) + 1)
    bar = "🟩" * score + "⬜️" * (10 - score)
    percent = min(100, trades * 3 + 40)
    return f"{bar} [{percent}%]"

def format_rating(rating_sum: int, reviews_count: int) -> str:
    if not reviews_count or reviews_count == 0: 
        return "0.0 ⚪️⚪️⚪️⚪️⚪️ (0 отзывов)"
    avg = rating_sum / reviews_count
    stars_count = round(avg)
    stars = "⭐" * stars_count + "⚪️" * (5 - stars_count)
    return f"{avg:.1f} {stars} ({reviews_count} отзывов)"

# ================= КЛАВИАТУРЫ =================
def get_main_reply_kb(user_id: int, is_eligible_for_garant=False):
    kb = [
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="🤝 Зафиксировать сделку")],
        [KeyboardButton(text="🔗 Привязать Roblox"), KeyboardButton(text="🧾 Создать чек")],
        [KeyboardButton(text="🚨 Подать жалобу"), KeyboardButton(text="🛎 Вызвать Гаранта")],
        [KeyboardButton(text="🛡 Гаранты"), KeyboardButton(text="⚠️ Скамеры"), KeyboardButton(text="🏆 Топ")],
        [KeyboardButton(text="🔍 Проверить")]
    ]
    if is_eligible_for_garant:
        kb.insert(0, [KeyboardButton(text="🌟 Подать заявку на Гаранта")])
    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="👑 Админ Панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, input_field_placeholder="Выберите действие...")

def get_cancel_reply_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)

def get_p2p_trade_kb(trade_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить сделку", callback_data=f"trade_accept_{trade_id}"),
            InlineKeyboardButton(text="❌ Это ошибка / Отказ", callback_data=f"trade_decline_{trade_id}")
        ]
    ])

def get_rating_kb(trade_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1 ⭐", callback_data=f"setrate_{trade_id}_1"),
            InlineKeyboardButton(text="2 ⭐", callback_data=f"setrate_{trade_id}_2"),
            InlineKeyboardButton(text="3 ⭐", callback_data=f"setrate_{trade_id}_3")
        ],
        [
            InlineKeyboardButton(text="4 ⭐", callback_data=f"setrate_{trade_id}_4"),
            InlineKeyboardButton(text="5 ⭐", callback_data=f"setrate_{trade_id}_5")
        ]
    ])

def get_admin_main_kb():
    with get_db() as conn:
        rbx_t = conn.execute("SELECT COUNT(*) FROM tickets WHERE type='roblox' AND status='pending'").fetchone()[0]
        rep_t = conn.execute("SELECT COUNT(*) FROM tickets WHERE type='report' AND status='pending'").fetchone()[0]
        garant_t = conn.execute("SELECT COUNT(*) FROM tickets WHERE type='garant_app' AND status='pending'").fetchone()[0]

    keyboard = [
        [InlineKeyboardButton(text="🔍 Поиск юзера", callback_data="admin_search_user")],
        [
            InlineKeyboardButton(text=f"🎫 Роблокс ({rbx_t})", callback_data="admin_tickets_roblox"),
            InlineKeyboardButton(text=f"🚨 Жалобы ({rep_t})", callback_data="admin_tickets_report")
        ],
        [InlineKeyboardButton(text=f"🌟 Заявки в Гаранты ({garant_t})", callback_data="admin_tickets_garant_app")],
        [
            InlineKeyboardButton(text="📊 Статистика БД", callback_data="admin_stats"),
            InlineKeyboardButton(text="💾 Скачать БД", callback_data="admin_backup_db")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_admin_user_manage_kb(target_id: int, current_status: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 Сделать Юзером", callback_data=f"admset_user_{target_id}"),
            InlineKeyboardButton(text="👑 Дать Гаранта", callback_data=f"admset_garant_{target_id}")
        ],
        [InlineKeyboardButton(text="🚨 В ЧС (СКАМЕР)", callback_data=f"admset_scammer_{target_id}")],
        [
            InlineKeyboardButton(text="➕ Сделки (+1)", callback_data=f"admadd_trades_{target_id}_1"),
            InlineKeyboardButton(text="➕ Сделки (+10)", callback_data=f"admadd_trades_{target_id}_10")
        ],
        [InlineKeyboardButton(text="➖ Сбросить Скам", callback_data=f"admclr_scam_{target_id}")]
    ])

# ================= ОСНОВНЫЕ ОБРАБОТЧИКИ =================
async def setup_bot_commands():
    commands = [
        BotCommand(command="start", description="Перезапустить бота"),
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="check", description="Проверить пользователя")
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

@router.message(CommandStart(), IsPrivate())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
        if not user:
            conn.execute("INSERT INTO users (tg_id, tg_username) VALUES (?, ?)", 
                         (message.from_user.id, message.from_user.username))
        elif user['tg_username'] != message.from_user.username:
            conn.execute("UPDATE users SET tg_username = ? WHERE tg_id = ?", 
                         (message.from_user.username, message.from_user.id))
        conn.commit()

    text = (
        "🛡 <b>AntiScam Enterprise</b>\n\n"
        "Самая надежная база трейдеров и гарантов.\n"
        "Проводите безопасные сделки, фиксируйте их в боте и стройте свою репутацию!\n\n"
        "👇 <i>Используйте кнопки меню ниже:</i>"
    )
    await message.answer(text, reply_markup=get_main_reply_kb(message.from_user.id))

@router.message(F.text == "❌ Отмена", IsPrivate())
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=get_main_reply_kb(message.from_user.id))

@router.message(F.text == "👤 Мой профиль", IsPrivate())
@router.message(Command("profile"), IsPrivate())
async def show_profile(message: Message, state: FSMContext):
    await state.clear()
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()

    status_dict = {"user": "🟢 Трейдер", "garant": "👑 Оф. Гарант", "scammer": "🔴 СКАМЕР (ЧС)", "admin": "👨‍💻 Администратор"}
    trust_bar = generate_trust_bar(user['trades'], user['scams'])
    title = get_user_title(user['trades'], user['scams'])
    rating_str = format_rating(user['rating_sum'], user['reviews_count'])
    
    is_eligible = (user['trades'] >= 15 and user['reviews_count'] >= 5 and 
                   (user['rating_sum'] / user['reviews_count']) >= 4.5 and 
                   user['scams'] == 0 and user['status'] == 'user')
    
    text = (
        f"👤 <b>Досье Трейдера</b>\n"
        f"├ <b>ID:</b> <code>{user['tg_id']}</code>\n"
        f"├ <b>Титул:</b> {title}\n"
        f"└ <b>Статус:</b> {status_dict.get(user['status'], 'Неизвестно')}\n\n"
        f"🎮 <b>Roblox Данные:</b>\n"
        f"├ <b>Ник:</b> <code>{user['roblox_username']}</code>\n"
        f"└ <b>ID:</b> <code>{user['roblox_id']}</code>\n\n"
        f"📊 <b>Репутация:</b>\n"
        f"├ Рейтинг: <b>{rating_str}</b>\n"
        f"{trust_bar}\n"
        f"├ ✅ Успешных сделок: <b>{user['trades']}</b>\n"
        f"└ 🚨 Жалоб на скам: <b>{user['scams']}</b>"
    )
    await message.answer(text, reply_markup=get_main_reply_kb(message.from_user.id, is_eligible))

# ================= P2P ФИКСАЦИЯ СДЕЛОК (НОВАЯ ФУНКЦИЯ) =================
@router.message(F.text == "🤝 Зафиксировать сделку", IsPrivate())
async def log_trade_start(message: Message, state: FSMContext):
    text = (
        "🤝 <b>Фиксация Сделки (P2P)</b>\n\n"
        "Чтобы получить <b>+1 к успешным сделкам</b> и возможность обменяться отзывами, "
        "вы должны зафиксировать сделку с партнером.\n\n"
        "👉 Введите <b>@username</b> или <b>ID</b> человека, с которым вы провели сделку:"
    )
    await message.answer(text, reply_markup=get_cancel_reply_kb())
    await state.set_state(LogTrade.waiting_for_partner)

@router.message(LogTrade.waiting_for_partner, IsPrivate())
async def log_trade_partner(message: Message, state: FSMContext):
    target = message.text.replace("@", "")
    with get_db() as conn:
        if target.isdigit():
            partner = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
        else:
            partner = conn.execute("SELECT * FROM users WHERE tg_username = ? COLLATE NOCASE", (target,)).fetchone()

    if not partner:
        return await message.answer("❌ Пользователь не найден в базе бота. Попросите его запустить бота: @ваш_бот", reply_markup=get_cancel_reply_kb())
    if partner['tg_id'] == message.from_user.id:
        return await message.answer("❌ Вы не можете зафиксировать сделку с самим собой.")
    if partner['status'] == 'scammer':
        return await message.answer("⚠️ <b>ВНИМАНИЕ! Этот пользователь в ЧЕРНОМ СПИСКЕ!</b> Сделки с ним запрещены.", reply_markup=get_main_reply_kb(message.from_user.id))

    await state.update_data(partner_id=partner['tg_id'], partner_username=partner['tg_username'])
    await message.answer("📝 Кратко опишите, чем вы обменялись (например: <i>Я дал Frost Dragon, он дал 2000 Robux</i>):")
    await state.set_state(LogTrade.waiting_for_description)

@router.message(LogTrade.waiting_for_description, IsPrivate())
async def log_trade_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    partner_id = data['partner_id']
    desc = message.text

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO trade_logs (initiator_id, partner_id, description) VALUES (?, ?, ?)",
            (message.from_user.id, partner_id, desc)
        )
        trade_id = cursor.lastrowid
        conn.commit()

    await message.answer("⏳ <b>Запрос отправлен партнеру!</b>\nЕсли он подтвердит, вы оба получите +1 к сделкам.", reply_markup=get_main_reply_kb(message.from_user.id))
    
    # Отправляем запрос партнеру
    try:
        confirm_text = (
            f"🤝 <b>ЗАПРОС НА ПОДТВЕРЖДЕНИЕ СДЕЛКИ</b>\n\n"
            f"Трейдер @{message.from_user.username} утверждает, что провел с вами сделку.\n"
            f"<b>Описание:</b> <i>{desc}</i>\n\n"
            f"Подтверждаете ли вы это?"
        )
        await bot.send_message(partner_id, confirm_text, reply_markup=get_p2p_trade_kb(trade_id))
    except:
        pass
    await state.clear()

@router.callback_query(F.data.startswith("trade_"), IsPrivate())
async def process_p2p_trade(call: CallbackQuery, state: FSMContext):
    action = call.data.split("_")[1] # accept / decline
    trade_id = int(call.data.split("_")[2])

    with get_db() as conn:
        trade = conn.execute("SELECT * FROM trade_logs WHERE id = ?", (trade_id,)).fetchone()
        if not trade or trade['status'] != 'pending':
            return await call.answer("Сделка уже обработана.", show_alert=True)
            
        if action == "accept":
            # Меняем статус и даем +1 сделку обоим
            conn.execute("UPDATE trade_logs SET status = 'completed' WHERE id = ?", (trade_id,))
            conn.execute("UPDATE users SET trades = trades + 1 WHERE tg_id IN (?, ?)", (trade['initiator_id'], trade['partner_id']))
            conn.commit()
            
            await call.message.edit_text("✅ <b>Сделка успешно подтверждена!</b> Вам начислен +1 трейд.")
            
            # Предлагаем оставить отзыв инициатору
            try:
                await bot.send_message(
                    trade['initiator_id'], 
                    f"✅ Партнер подтвердил сделку! Вы получили +1 трейд.\n\n👇 Оцените работу партнера:",
                    reply_markup=get_rating_kb(trade_id)
                )
            except: pass
            
            # Предлагаем оставить отзыв партнеру
            await call.message.answer(
                "👇 Оцените, как прошла сделка с вашей стороны:", 
                reply_markup=get_rating_kb(trade_id)
            )

        elif action == "decline":
            conn.execute("UPDATE trade_logs SET status = 'cancelled' WHERE id = ?", (trade_id,))
            conn.commit()
            await call.message.edit_text("❌ Запрос на сделку отклонен.")
            try:
                await bot.send_message(trade['initiator_id'], "❌ Партнер отклонил вашу заявку на фиксацию сделки.")
            except: pass
            
    await call.answer()

# ================= ОТЗЫВЫ ПОСЛЕ СДЕЛКИ =================
@router.callback_query(F.data.startswith("setrate_"), IsPrivate())
async def review_stars_selected(call: CallbackQuery, state: FSMContext):
    trade_id = int(call.data.split("_")[1])
    stars = int(call.data.split("_")[2])
    
    with get_db() as conn:
        trade = conn.execute("SELECT initiator_id, partner_id FROM trade_logs WHERE id = ?", (trade_id,)).fetchone()
    
    if not trade: return await call.answer("Ошибка сделки.", show_alert=True)
    
    target_id = trade['partner_id'] if call.from_user.id == trade['initiator_id'] else trade['initiator_id']
    
    await state.update_data(trade_id=trade_id, target_id=target_id, stars=stars)
    await call.message.edit_text(f"⭐️ <b>Вы поставили {stars} из 5.</b>")
    await call.message.answer("📝 Напишите короткий текстовый отзыв (или отправьте '-' если не хотите):", reply_markup=get_cancel_reply_kb())
    await state.set_state(LeaveReview.waiting_for_text)

@router.message(LeaveReview.waiting_for_text, IsPrivate())
async def review_save(message: Message, state: FSMContext):
    data = await state.get_data()
    
    with get_db() as conn:
        # Проверяем, не оставлял ли он уже отзыв к этой сделке
        exists = conn.execute("SELECT id FROM reviews WHERE reviewer_id = ? AND trade_id = ?", 
                              (message.from_user.id, data['trade_id'])).fetchone()
        if exists:
            await state.clear()
            return await message.answer("❌ Вы уже оставили отзыв к этой сделке.", reply_markup=get_main_reply_kb(message.from_user.id))
            
        conn.execute(
            "INSERT INTO reviews (reviewer_id, target_id, trade_id, rating, review_text) VALUES (?, ?, ?, ?, ?)",
            (message.from_user.id, data['target_id'], data['trade_id'], data['stars'], message.text)
        )
        conn.execute(
            "UPDATE users SET rating_sum = rating_sum + ?, reviews_count = reviews_count + 1 WHERE tg_id = ?",
            (data['stars'], data['target_id'])
        )
        conn.commit()

    await message.answer("✅ Отзыв сохранен! Спасибо.", reply_markup=get_main_reply_kb(message.from_user.id))
    
    try:
        await bot.send_message(data['target_id'], f"🌟 <b>Вам оставили новый отзыв!</b>\nОценка: {data['stars']} ⭐\nОтзыв: {message.text}")
    except: pass
    await state.clear()

# ================= ПРИВЯЗКА И ЖАЛОБЫ =================
@router.message(F.text == "🌟 Подать заявку на Гаранта", IsPrivate())
async def apply_garant(message: Message):
    with get_db() as conn:
        conn.execute("INSERT INTO tickets (user_id, type) VALUES (?, 'garant_app')", (message.from_user.id,))
        conn.commit()
    await message.answer("✅ <b>Заявка на Гаранта отправлена!</b> Администратор изучит вашу статистику и примет решение.", reply_markup=get_main_reply_kb(message.from_user.id))
    await bot.send_message(ADMIN_ID, f"🌟 <b>НОВАЯ ЗАЯВКА В ГАРАНТЫ!</b>\nОт: @{message.from_user.username}")

@router.message(F.text == "🔗 Привязать Roblox", IsPrivate())
async def start_link_roblox(message: Message, state: FSMContext):
    await message.answer("🔗 Отправьте <b>видео</b>, где вы заходите в свой профиль Roblox (нужно видеть ваш ник):", reply_markup=get_cancel_reply_kb())
    await state.set_state(RobloxLink.waiting_for_video)

@router.message(RobloxLink.waiting_for_video, F.video | F.animation | F.document, IsPrivate())
async def process_video(message: Message, state: FSMContext):
    with get_db() as conn:
        conn.execute("INSERT INTO tickets (user_id, type, proof_msg_id) VALUES (?, 'roblox', ?)",
                     (message.from_user.id, message.message_id))
        conn.commit()
    await message.answer("✅ Видео отправлено модераторам.", reply_markup=get_main_reply_kb(message.from_user.id))
    await state.clear()

@router.message(F.text == "🚨 Подать жалобу", IsPrivate())
async def report_scam_start(message: Message, state: FSMContext):
    await message.answer("🚨 Введите <b>Telegram ID</b> или <b>@username</b> скамера:", reply_markup=get_cancel_reply_kb())
    await state.set_state(ReportUser.waiting_for_target)

@router.message(ReportUser.waiting_for_target, IsPrivate())
async def report_scam_id(message: Message, state: FSMContext):
    await state.update_data(target_data=message.text.replace("@", ""))
    await message.answer("Теперь отправьте <b>все доказательства (фото/видео + текст) одним сообщением</b>:")
    await state.set_state(ReportUser.waiting_for_proofs)

@router.message(ReportUser.waiting_for_proofs, IsPrivate())
async def report_scam_proofs(message: Message, state: FSMContext):
    data = await state.get_data()
    with get_db() as conn:
        conn.execute("INSERT INTO tickets (user_id, type, data, proof_msg_id) VALUES (?, 'report', ?, ?)",
                     (message.from_user.id, data['target_data'], message.message_id))
        conn.commit()
    await message.answer("✅ Жалоба отправлена в Арбитраж.", reply_markup=get_main_reply_kb(message.from_user.id))
    await state.clear()

# ================= АДМИН ПАНЕЛЬ =================
@router.message(F.text == "👑 Админ Панель", IsAdmin())
async def admin_main(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("👑 <b>Enterprise Dashboard</b>", reply_markup=get_admin_main_kb())

@router.callback_query(F.data.startswith("admin_tickets_"), IsAdmin())
async def view_tickets(call: CallbackQuery):
    ticket_type = call.data.split("_")[2] # roblox, report, garant_app
    
    with get_db() as conn:
        ticket = conn.execute("SELECT * FROM tickets WHERE type=? AND status='pending' LIMIT 1", (ticket_type,)).fetchone()
    
    if not ticket:
        return await call.answer("🎉 Тикетов в этой категории нет!", show_alert=True)
        
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"ticket_appr_{ticket['id']}_{ticket_type}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ticket_rej_{ticket['id']}_{ticket_type}")
    ]])

    text = f"🎫 <b>Тикет #{ticket['id']} ({ticket_type.upper()})</b>\nОт ID: <code>{ticket['user_id']}</code>\n"
    if ticket['data']: text += f"Цель: {ticket['data']}\n"

    await call.message.edit_text(text, reply_markup=kb)
    
    if ticket['proof_msg_id']:
        try:
            await bot.copy_message(chat_id=ADMIN_ID, from_chat_id=ticket['user_id'], message_id=ticket['proof_msg_id'])
        except:
            await bot.send_message(ADMIN_ID, "❌ Ошибка загрузки медиа.")
    await call.answer()

@router.callback_query(F.data.startswith("ticket_"), IsAdmin())
async def resolve_ticket(call: CallbackQuery, state: FSMContext):
    action, t_id, t_type = call.data.split("_")[1], int(call.data.split("_")[2]), call.data.split("_")[3]
    
    with get_db() as conn:
        conn.execute("UPDATE tickets SET status = ? WHERE id = ?", ('resolved' if action == 'appr' else 'rejected', t_id))
        ticket = conn.execute("SELECT user_id FROM tickets WHERE id = ?", (t_id,)).fetchone()
        conn.commit()
    
    user_id = ticket['user_id']
    
    if action == "rej":
        await bot.send_message(user_id, f"❌ Ваша заявка ({t_type}) отклонена.")
        await call.message.edit_text(f"✅ Тикет #{t_id} отклонен.", reply_markup=get_admin_main_kb())
        return await call.answer()
        
    if t_type == "roblox":
        AdminPanelStates.target_ticket_id = user_id
        await call.message.edit_text("✅ Введите Ник и ID для пользователя в формате:\n<code>MyNick 123456</code>")
        await state.set_state(AdminPanelStates.waiting_for_roblox_data)
    elif t_type == "report":
        await call.message.edit_text(f"✅ Репорт одобрен. Найдите скамера через поиск и забаньте.", reply_markup=get_admin_main_kb())
    elif t_type == "garant_app":
        with get_db() as conn:
            conn.execute("UPDATE users SET status = 'garant' WHERE tg_id = ?", (user_id,))
            conn.commit()
        log_admin_action(call.from_user.id, "approved_garant", user_id)
        await bot.send_message(user_id, "🎉 <b>ПОЗДРАВЛЯЕМ!</b> Ваша заявка одобрена. Вы стали Официальным Гарантом!")
        await call.message.edit_text(f"✅ Пользователю {user_id} выдан статус Гаранта.", reply_markup=get_admin_main_kb())

    await call.answer()

@router.message(AdminPanelStates.waiting_for_roblox_data, IsAdmin())
async def save_roblox_data(message: Message, state: FSMContext):
    try:
        rbx_nick, rbx_id = message.text.split()
        target_id = AdminPanelStates.target_ticket_id
        with get_db() as conn:
            conn.execute("UPDATE users SET roblox_username = ?, roblox_id = ? WHERE tg_id = ?", (rbx_nick, rbx_id, target_id))
            conn.commit()
        log_admin_action(message.from_user.id, "linked_roblox", target_id)
        
        await message.answer("✅ Данные привязаны!", reply_markup=get_admin_main_kb())
        await bot.send_message(target_id, f"🎉 <b>Roblox привязан!</b>\nНик: {rbx_nick}\nID: {rbx_id}")
        await state.clear()
    except Exception:
        await message.answer("❌ Ошибка формата. Нужно: Ник ID")

@router.callback_query(F.data.startswith("admset_") | F.data.startswith("admadd_") | F.data.startswith("admclr_"), IsAdmin())
async def admin_edit_user(call: CallbackQuery):
    parts = call.data.split("_")
    action_type, target_id = parts[0], int(parts[2])
    
    with get_db() as conn:
        if action_type == "admset":
            conn.execute("UPDATE users SET status = ? WHERE tg_id = ?", (parts[1], target_id))
            log_admin_action(call.from_user.id, f"set_status_{parts[1]}", target_id)
        elif action_type == "admadd":
            conn.execute("UPDATE users SET trades = trades + ? WHERE tg_id = ?", (int(parts[3]), target_id))
            log_admin_action(call.from_user.id, f"add_trades_{parts[3]}", target_id)
        elif action_type == "admclr":
            conn.execute("UPDATE users SET scams = 0, status = 'user' WHERE tg_id = ?", (target_id,))
            log_admin_action(call.from_user.id, "cleared_scams", target_id)
        conn.commit()
        
    await call.answer("✅ База обновлена!", show_alert=True)
    await call.message.delete()

# ================= GROUP SHIELD (АВТОМОДЕРАЦИЯ) =================
@router.message(Command("check"), IsGroup())
async def group_check_command(message: Message):
    args = message.text.split()
    if len(args) < 2: return await message.reply("Использование: <code>/check @username</code>")
    target = args[1].replace("@", "")
    
    with get_db() as conn:
        if target.isdigit():
            user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
        else:
            user = conn.execute("SELECT * FROM users WHERE tg_username = ? COLLATE NOCASE", (target,)).fetchone()

    if not user: return await message.reply("❓ Неизвестный пользователь.")
    
    text = (f"📋 <b>Досье на {user['tg_username']}:</b>\n"
            f"Сделок: {user['trades']} | Рейтинг: {format_rating(user['rating_sum'], user['reviews_count'])}\n")
    if user['status'] == 'scammer': text = "‼️ <b>СКАМЕР ИЗ ЧС! БЕЗ СДЕЛОК!</b> ‼️\n" + text
    await message.reply(text)

@router.message(IsGroup())
async def group_message_monitor(message: Message):
    with get_db() as conn:
        user = conn.execute("SELECT status FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    
    if user and user['status'] == 'scammer':
        try:
            await message.delete()
            warn = await message.answer(f"⚠️ Сообщение от @{message.from_user.username} удалено. <b>СКАМЕР В БАЗЕ!</b>")
            await asyncio.sleep(10)
            await warn.delete()
        except TelegramBadRequest: pass

# ================= АВТОБЭКАПЫ =================
async def auto_backup_loop():
    while True:
        await asyncio.sleep(3600) # Каждый час
        try:
            if os.path.exists("antiscam_pro.db"):
                await bot.send_document(ADMIN_ID, FSInputFile("antiscam_pro.db"), caption="🕒 Авто-бэкап БД.")
        except: pass

async def main():
    init_db()
    await setup_bot_commands()
    asyncio.create_task(auto_backup_loop())
    print("🚀 AntiScam Enterprise Bot запущен (v3.0)!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
