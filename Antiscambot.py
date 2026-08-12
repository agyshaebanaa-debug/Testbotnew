import asyncio
import sqlite3
import logging
import os
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated, FSInputFile
from aiogram.filters import CommandStart, Command, BaseFilter, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest

# ================= НАСТРОЙКИ БОТА =================
BOT_TOKEN = "8792564218:AAHo3taU03G4FGAtIovL6mdSNXRA72QrtE0"
ADMIN_ID = 5341904332  # ЗАМЕНИ НА СВОЙ ID! (Узнать можно в @getmyid_bot)

# Инициализация
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ================= БАЗА ДАННЫХ =================
def init_db():
    conn = sqlite3.connect("antiscam_pro.db")
    cursor = conn.cursor()
    # Таблица пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            tg_username TEXT,
            roblox_username TEXT DEFAULT 'Не привязан',
            roblox_id TEXT DEFAULT 'Не привязан',
            trades INTEGER DEFAULT 0,
            scams INTEGER DEFAULT 0,
            status TEXT DEFAULT 'user', -- user, garant, scammer, admin
            join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Таблица чатов (где состоит бот)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def get_db_connection():
    conn = sqlite3.connect("antiscam_pro.db")
    conn.row_factory = sqlite3.Row
    return conn

def upgrade_db():
    """Автоматическая миграция: добавление новых колонок в старую БД"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Получаем все текущие колонки в таблице users
    cursor.execute("PRAGMA table_info(users)")
    columns = [row['name'] for row in cursor.fetchall()]
    
    # Если в старой базе нет новой колонки для уровней верификации - создаем ее, не теряя старые данные
    if 'verification_level' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN verification_level TEXT DEFAULT 'none'")
        
    conn.commit()
    conn.close()

# ================= FSM СОСТОЯНИЯ И ФИЛЬТРЫ =================
class RobloxLink(StatesGroup):
    waiting_for_video = State()

class ReportUser(StatesGroup):
    waiting_for_target_id = State()
    waiting_for_proofs = State()

class CallGarant(StatesGroup):
    waiting_for_details = State()

class AdminPanelStates(StatesGroup):
    waiting_for_roblox_data = State()
    waiting_for_user_search = State()
    waiting_for_broadcast_msg = State()
    waiting_for_db_upload = State() # Новое состояние для загрузки бэкапа
    target_user_id = None # Временное хранение

class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id == ADMIN_ID

class IsGroup(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.chat.type in ["group", "supergroup"]

class IsPrivate(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.chat.type == "private"

# ================= КЛАВИАТУРЫ =================
def get_main_menu(user_id: int):
    keyboard = [
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="my_profile")],
        [
            InlineKeyboardButton(text="🔗 Привязать Roblox", callback_data="link_roblox"),
            InlineKeyboardButton(text="🚨 Подать жалобу", callback_data="report_scam")
        ],
        [
            InlineKeyboardButton(text="🛡 Гаранты", callback_data="list_garants"),
            InlineKeyboardButton(text="⚠️ База Скамеров", callback_data="list_scammers"),
            InlineKeyboardButton(text="🏆 Топ", callback_data="top_traders")
        ],
        [
            InlineKeyboardButton(text="🔍 Проверить пользователя", callback_data="check_user_menu"),
            InlineKeyboardButton(text="🛎 Вызвать Гаранта", callback_data="call_garant")
        ]
    ]
    if user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton(text="👑 Админ Панель", callback_data="admin_main")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_back_button(target="back_to_main"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=target)]
    ])

def get_admin_main_kb():
    keyboard = [
        [InlineKeyboardButton(text="🔍 Управление пользователем", callback_data="admin_search_user")],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
            InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")
        ],
        [
            InlineKeyboardButton(text="💾 Скачать БД", callback_data="admin_backup_db"),
            InlineKeyboardButton(text="📥 Загрузить БД", callback_data="admin_upload_db")
        ],
        [InlineKeyboardButton(text="🔙 В меню бота", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_admin_user_manage_kb(target_id: int, current_status: str):
    keyboard = [
        [
            InlineKeyboardButton(text="🟢 Сделать Юзером", callback_data=f"admset_user_{target_id}"),
            InlineKeyboardButton(text="👑 Дать Гаранта", callback_data=f"admset_garant_{target_id}")
        ],
        [InlineKeyboardButton(text="🚨 В ЧС (СКАМЕР)", callback_data=f"admset_scammer_{target_id}")],
        [
            InlineKeyboardButton(text="➕ Сделки (+10)", callback_data=f"admadd_trades_{target_id}_10"),
            InlineKeyboardButton(text="➖ Сбросить Скам", callback_data=f"admclr_scam_{target_id}")
        ],
        [InlineKeyboardButton(text="🔙 В Админку", callback_data="admin_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ================= ХЕЛПЕРЫ =================
def generate_trust_bar(trades: int, scams: int) -> str:
    if scams > 0: return "🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥 [0% СКАМ]"
    if trades == 0: return "⬜️⬜️⬜️⬜️⬜️⬜️⬜️⬜️⬜️⬜️ [0% НОВИЧОК]"
    
    score = min(10, (trades // 5) + 1)
    bar = "🟩" * score + "⬜️" * (10 - score)
    percent = min(100, trades * 2 + 50) # Плавный рост до 100%
    return f"{bar} [{percent}%]"

# ================= ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ =================
@router.message(CommandStart(), IsPrivate())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    conn = get_db_connection()
    cursor = conn.cursor()
    user = cursor.execute("SELECT * FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    
    if not user:
        cursor.execute("INSERT INTO users (tg_id, tg_username) VALUES (?, ?)", 
                       (message.from_user.id, message.from_user.username))
        conn.commit()
    elif user['tg_username'] != message.from_user.username:
        # Обновляем юзернейм если сменился
        cursor.execute("UPDATE users SET tg_username = ? WHERE tg_id = ?", (message.from_user.username, message.from_user.id))
        conn.commit()
    conn.close()

    text = (
        "🛡 <b>AntiScam & Garant Network</b>\n\n"
        "Главная независимая база репутации трейдеров Roblox.\n"
        "Проверяйте пользователей перед сделкой, находите гарантов и повышайте свой рейтинг доверия!\n\n"
        "👇 <i>Выберите действие ниже:</i>"
    )
    await message.answer(text, reply_markup=get_main_menu(message.from_user.id))

@router.callback_query(F.data == "back_to_main")
async def back_to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    text = "🛡 <b>AntiScam & Garant Network</b>\n\nВыберите действие:"
    await call.message.edit_text(text, reply_markup=get_main_menu(call.from_user.id))
    await call.answer()

@router.callback_query(F.data == "my_profile")
async def show_profile(call: CallbackQuery):
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (call.from_user.id,)).fetchone()
    conn.close()

    status_dict = {"user": "🟢 Трейдер", "garant": "👑 Оф. Гарант", "scammer": "🔴 СКАМЕР В ЧС", "admin": "👨‍💻 Создатель"}
    trust_bar = generate_trust_bar(user['trades'], user['scams'])
    
    text = (
        f"👤 <b>Профиль Трейдера</b>\n"
        f"├ <b>ID:</b> <code>{user['tg_id']}</code>\n"
        f"├ <b>Username:</b> @{user['tg_username']}\n"
        f"└ <b>Статус:</b> {status_dict.get(user['status'], 'Неизвестно')}\n\n"
        f"🎮 <b>Roblox Данные:</b>\n"
        f"├ <b>Ник:</b> <code>{user['roblox_username']}</code>\n"
        f"└ <b>ID:</b> <code>{user['roblox_id']}</code>\n\n"
        f"📊 <b>Репутация:</b>\n"
        f"{trust_bar}\n"
        f"├ ✅ Успешных сделок: <b>{user['trades']}</b>\n"
        f"└ 🚨 Жалоб на скам: <b>{user['scams']}</b>"
    )
    await call.message.edit_text(text, reply_markup=get_back_button())
    await call.answer()

# ================= ПРИВЯЗКА ROBLOX =================
@router.callback_query(F.data == "link_roblox")
async def start_link_roblox(call: CallbackQuery, state: FSMContext):
    text = (
        "🔗 <b>Привязка аккаунта Roblox</b>\n\n"
        "Отправь <b>видео</b>, где ты заходишь в свой профиль Roblox (чтобы мы точно видели твой никнейм).\n"
        "Модераторы проверят его и привяжут аккаунт."
    )
    await call.message.edit_text(text, reply_markup=get_back_button())
    await state.set_state(RobloxLink.waiting_for_video)
    await call.answer()

@router.message(RobloxLink.waiting_for_video, F.video | F.animation | F.document, IsPrivate())
async def process_video(message: Message, state: FSMContext):
    await message.answer("✅ <b>Видео отправлено!</b> Ожидайте проверки.", reply_markup=get_back_button("back_to_main"))
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"adm_apprv_rbx_{message.from_user.id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_rej_rbx_{message.from_user.id}")]
    ])
    admin_text = f"🚨 <b>Новая заявка на привязку Roblox!</b>\nОт: @{message.from_user.username} (<code>{message.from_user.id}</code>)"
    await bot.send_message(ADMIN_ID, admin_text)
    await bot.copy_message(chat_id=ADMIN_ID, from_chat_id=message.chat.id, message_id=message.message_id, reply_markup=kb)
    await state.clear()

# ================= СИСТЕМА ЖАЛОБ (РЕПОРТОВ) =================
@router.callback_query(F.data == "report_scam")
async def report_scam_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("🚨 <b>Подача жалобы</b>\n\nВведите <b>Telegram ID</b> скамера (только цифры):", reply_markup=get_back_button())
    await state.set_state(ReportUser.waiting_for_target_id)
    await call.answer()

@router.message(ReportUser.waiting_for_target_id, IsPrivate())
async def report_scam_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("❌ Нужно ввести числовой ID. Попробуйте еще раз:")
    
    await state.update_data(target_id=message.text)
    await message.answer("Теперь отправьте <b>все доказательства одним сообщением</b> (текст + фото/видео чеков, переписки):")
    await state.set_state(ReportUser.waiting_for_proofs)

@router.message(ReportUser.waiting_for_proofs, IsPrivate())
async def report_scam_proofs(message: Message, state: FSMContext):
    data = await state.get_data()
    target_id = data['target_id']
    
    await message.answer("✅ <b>Жалоба успешно отправлена модераторам!</b>", reply_markup=get_back_button("back_to_main"))
    
    admin_text = f"🚨 <b>НОВЫЙ РЕПОРТ НА СКАМ!</b>\nЖалоба на ID: <code>{target_id}</code>\nОт: @{message.from_user.username} (<code>{message.from_user.id}</code>)\n\nДоказательства ниже:"
    await bot.send_message(ADMIN_ID, admin_text)
    await bot.copy_message(chat_id=ADMIN_ID, from_chat_id=message.chat.id, message_id=message.message_id)
    await state.clear()

# ================= СПИСКИ И ТОПЫ =================
@router.callback_query(F.data.in_({"list_garants", "list_scammers", "top_traders"}))
async def show_lists(call: CallbackQuery):
    conn = get_db_connection()
    if call.data == "list_garants":
        users = conn.execute("SELECT tg_username, trades FROM users WHERE status = 'garant' ORDER BY trades DESC").fetchall()
        title, empty_msg = "🛡 <b>Официальные Гаранты:</b>", "Гарантов пока нет."
        format_str = "👑 @{u} — {t} сделок"
    elif call.data == "list_scammers":
        users = conn.execute("SELECT tg_username, tg_id, roblox_username FROM users WHERE status = 'scammer' ORDER BY scams DESC LIMIT 30").fetchall()
        title, empty_msg = "🚨 <b>База СКАМЕРОВ:</b>\n<i>Никаких сделок с ними!</i>", "База чиста."
        format_str = "🔴 @{u} (<code>{i}</code>) | RBX: {r}"
    else: # top_traders
        users = conn.execute("SELECT tg_username, trades FROM users WHERE status IN ('user', 'garant') AND trades > 0 ORDER BY trades DESC LIMIT 10").fetchall()
        title, empty_msg = "🏆 <b>Топ-10 Трейдеров:</b>", "Топ пока пуст."
        format_str = "⭐ @{u} — {t} сделок"
    conn.close()

    text = f"{title}\n\n"
    if not users:
        text += empty_msg
    else:
        for idx, u in enumerate(users, 1):
            if call.data == "list_scammers":
                text += f"{idx}. {format_str.format(u=u['tg_username'], i=u['tg_id'], r=u['roblox_username'])}\n"
            else:
                text += f"{idx}. {format_str.format(u=u['tg_username'], t=u['trades'])}\n"

    await call.message.edit_text(text, reply_markup=get_back_button())
    await call.answer()

# ================= ПОИСК ПОЛЬЗОВАТЕЛЯ =================
@router.callback_query(F.data == "check_user_menu")
async def ask_check_id(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("🔍 <b>Проверка пользователя</b>\n\nОтправьте мне <b>Telegram ID</b> (только цифры) человека, которого хотите проверить:", reply_markup=get_back_button())
    await state.set_state(AdminPanelStates.waiting_for_user_search) # Используем тот же стейт, но без прав админа
    await call.answer()

# ================= ВЫЗОВ ГАРАНТА (НОВАЯ ФУНКЦИЯ) =================
@router.callback_query(F.data == "call_garant")
async def call_garant_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("🛎 <b>Вызов Гаранта на сделку</b>\n\nОпишите детально условия сделки:\n<i>(Например: Я отдаю Dragon в Blox Fruits, мне отдают 1000 Robux)</i>\n\nОтправьте текст одним сообщением, и мы разошлем его всем онлайн-гарантам.", reply_markup=get_back_button())
    await state.set_state(CallGarant.waiting_for_details)
    await call.answer()

@router.message(CallGarant.waiting_for_details, IsPrivate())
async def call_garant_send(message: Message, state: FSMContext):
    conn = get_db_connection()
    garants = conn.execute("SELECT tg_id FROM users WHERE status = 'garant'").fetchall()
    conn.close()
    
    if not garants:
        return await message.answer("❌ К сожалению, сейчас нет зарегистрированных гарантов.", reply_markup=get_back_button("back_to_main"))
        
    notified = 0
    for garant in garants:
        try:
            msg = f"🛎 <b>ВЫЗОВ ГАРАНТА!</b>\nОт: @{message.from_user.username} (<code>{message.from_user.id}</code>)\n\n<b>Условия сделки:</b>\n{message.text}\n\n<i>Свяжитесь с пользователем в ЛС, если готовы взять сделку.</i>"
            await bot.send_message(garant['tg_id'], msg)
            notified += 1
        except Exception:
            pass
            
    await message.answer(f"✅ <b>Заявка отправлена!</b>\nУведомлено гарантов: <b>{notified}</b>.\nОжидайте, пока кто-то из них напишет вам в Личные Сообщения.", reply_markup=get_back_button("back_to_main"))
    await state.clear()

# ================= 👑 АДМИН-ПАНЕЛЬ (СУПЕР-ФУНКЦИОНАЛ) =================
@router.callback_query(F.data == "admin_main")
async def admin_main(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return await call.answer("Отказано в доступе.", show_alert=True)
    await state.clear()
    await call.message.edit_text("👑 <b>Панель Управления Ботом</b>", reply_markup=get_admin_main_kb())
    await call.answer()

@router.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    conn = get_db_connection()
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_scammers = conn.execute("SELECT COUNT(*) FROM users WHERE status = 'scammer'").fetchone()[0]
    total_groups = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    conn.close()
    
    text = (
        "📊 <b>Статистика Системы:</b>\n\n"
        f"👥 Всего юзеров: <b>{total_users}</b>\n"
        f"🚨 В ЧС (Скамеры): <b>{total_scammers}</b>\n"
        f"🛡 Защищаемых чатов: <b>{total_groups}</b>"
    )
    await call.message.edit_text(text, reply_markup=get_back_button("admin_main"))
    await call.answer()

@router.callback_query(F.data == "admin_backup_db")
async def admin_backup_db(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    try:
        await call.message.answer_document(FSInputFile("antiscam_pro.db"), caption="💾 <b>Ручной бэкап базы данных.</b>\n<i>Храните этот файл в надежном месте.</i>")
    except Exception as e:
        await call.message.answer(f"❌ Ошибка отправки: {e}")
    await call.answer()

@router.callback_query(F.data == "admin_upload_db")
async def admin_upload_db_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    await call.message.edit_text("📥 <b>Загрузка старого бэкапа БД</b>\n\nОтправьте мне файл <code>antiscam_pro.db</code>. Он полностью <b>ЗАМЕНИТ</b> текущую базу!\n\n<i>Система автоматически добавит новые колонки, если это база от старой версии (Механизм миграции активен).</i>", reply_markup=get_back_button("admin_main"))
    await state.set_state(AdminPanelStates.waiting_for_db_upload)
    await call.answer()

@router.message(AdminPanelStates.waiting_for_db_upload, F.document, IsPrivate())
async def handle_db_upload(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    
    file_id = message.document.file_id
    file = await bot.get_file(file_id)
    
    # Скачиваем файл с серверов ТГ и заменяем текущий
    await bot.download_file(file.file_path, "antiscam_pro.db")
    
    # Прогоняем миграцию (если структура старая, невидимо добавятся новые колонки)
    upgrade_db()
    
    await message.answer("✅ <b>База данных успешно загружена!</b>\nСтарая структура адаптирована под текущую версию бота.", reply_markup=get_back_button("admin_main"))
    await state.clear()

@router.callback_query(F.data == "admin_search_user")
async def admin_search_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    await call.message.edit_text("🔍 <b>Управление Юзером</b>\nВведите TG ID пользователя:", reply_markup=get_back_button("admin_main"))
    await state.set_state(AdminPanelStates.waiting_for_user_search)
    await call.answer()

@router.message(AdminPanelStates.waiting_for_user_search, IsPrivate())
async def execute_user_search(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("❌ ID должен быть числом.")
    target_id = int(message.text)
    
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (target_id,)).fetchone()
    conn.close()
    
    if not user:
        return await message.answer(f"Пользователь <code>{target_id}</code> не найден в базе.", reply_markup=get_back_button("admin_main" if message.from_user.id == ADMIN_ID else "back_to_main"))
    
    status_dict = {"user": "🟢 Трейдер", "garant": "👑 Гарант", "scammer": "🔴 СКАМЕР", "admin": "👨‍💻 Админ"}
    text = (
        f"📋 <b>Досье:</b> <code>{user['tg_id']}</code>\n"
        f"👤 <b>Ник:</b> @{user['tg_username']}\n"
        f"🔰 <b>Статус:</b> {status_dict.get(user['status'])}\n"
        f"🎮 <b>RBX:</b> {user['roblox_username']} ({user['roblox_id']})\n"
        f"✅ Сделок: {user['trades']} | 🚨 Жалоб: {user['scams']}"
    )
    
    if user['status'] == 'scammer':
        text = "‼️ <b>ВНИМАНИЕ! ПОЛЬЗОВАТЕЛЬ В БАЗЕ СКАМЕРОВ!</b> ‼️\n\n" + text

    # Если искал обычный юзер, показываем досье с кнопкой назад в меню. Если админ - панель управления.
    if message.from_user.id == ADMIN_ID:
        AdminPanelStates.target_user_id = target_id
        await message.answer(text, reply_markup=get_admin_user_manage_kb(target_id, user['status']))
    else:
        await message.answer(text, reply_markup=get_back_button())
    await state.clear()

@router.callback_query(F.data.startswith("admset_") | F.data.startswith("admadd_") | F.data.startswith("admclr_"))
async def admin_edit_user(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    action_parts = call.data.split("_")
    action_type = action_parts[0]
    
    target_id = int(action_parts[2]) if action_type != "admadd" else int(action_parts[2])
    conn = get_db_connection()
    
    if action_type == "admset":
        new_status = action_parts[1]
        conn.execute("UPDATE users SET status = ? WHERE tg_id = ?", (new_status, target_id))
        await call.answer(f"✅ Статус изменен на {new_status}")
        
    elif action_type == "admadd": # admadd_trades_ID_AMOUNT
        amount = int(action_parts[3])
        conn.execute("UPDATE users SET trades = trades + ? WHERE tg_id = ?", (amount, target_id))
        await call.answer(f"✅ Добавлено {amount} сделок")
        
    elif action_type == "admclr": # admclr_scam_ID
        conn.execute("UPDATE users SET scams = 0, status = 'user' WHERE tg_id = ?", (target_id,))
        await call.answer("✅ Скамы обнулены")
        
    conn.commit()
    conn.close()
    
    # Обновляем сообщение (симуляция повторного поиска)
    class FakeMessage: text = str(target_id); from_user = call.from_user
    await execute_user_search(FakeMessage(), FSMContext(storage=dp.storage, key=call.message.chat.id))
    await call.message.delete()

@router.callback_query(F.data.startswith("adm_apprv_rbx_") | F.data.startswith("adm_rej_rbx_"))
async def review_roblox_link(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    target_id = int(call.data.split("_")[3])
    
    if call.data.startswith("adm_rej"):
        await bot.send_message(target_id, "❌ <b>Заявка на привязку Roblox отклонена.</b>\nВидео не соответствует требованиям.")
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.reply(f"❌ Заявка {target_id} отклонена.")
        return await call.answer()
        
    AdminPanelStates.target_user_id = target_id
    await call.message.reply("✅ Принято. Отправь мне: <b>Ник ID</b>\n<i>Пример: myname 123456</i>")
    await state.set_state(AdminPanelStates.waiting_for_roblox_data)
    await call.answer()

@router.message(AdminPanelStates.waiting_for_roblox_data, IsPrivate())
async def save_roblox_data(message: Message, state: FSMContext):
    try:
        rbx_nick, rbx_id = message.text.split()
        target_id = AdminPanelStates.target_user_id
        
        conn = get_db_connection()
        conn.execute("UPDATE users SET roblox_username = ?, roblox_id = ? WHERE tg_id = ?", (rbx_nick, rbx_id, target_id))
        conn.commit()
        conn.close()
        
        await message.answer(f"✅ Данные привязаны к {target_id}!")
        await bot.send_message(target_id, f"🎉 <b>Ваш Roblox успешно привязан!</b>\nНик: {rbx_nick}\nID: {rbx_id}")
        await state.clear()
    except Exception:
        await message.answer("❌ Ошибка формата. Нужно: Ник ID")

# ================= ИНТЕГРАЦИЯ В ГРУППЫ (GROUP SHIELD) =================

@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added_to_group(event: ChatMemberUpdated):
    if event.chat.type in ["group", "supergroup"]:
        conn = get_db_connection()
        conn.execute("INSERT OR IGNORE INTO groups (chat_id, title) VALUES (?, ?)", (event.chat.id, event.chat.title))
        conn.commit()
        conn.close()
        text = (
            "🛡 <b>AntiScam Shield активирован!</b>\n"
            "Я буду защищать чат от скамеров из глобальной базы.\n"
            "Выдайте мне права <b>Администратора (Удаление сообщений)</b>, чтобы я мог работать!\n\n"
            "<i>Команда для проверки:</i> <code>/check @username</code>"
        )
        await bot.send_message(event.chat.id, text)

@router.message(Command("check"), IsGroup())
async def group_check_command(message: Message):
    # Команда /check @username или /check ID в группе
    args = message.text.split()
    if len(args) < 2:
        return await message.reply("Укажите юзернейм или ID: <code>/check @username</code>")
    
    target = args[1].replace("@", "")
    conn = get_db_connection()
    if target.isdigit():
        user = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(target),)).fetchone()
    else:
        user = conn.execute("SELECT * FROM users WHERE tg_username = ? COLLATE NOCASE", (target,)).fetchone()
    conn.close()

    if not user:
        return await message.reply(f"❓ Пользователь <b>{target}</b> не найден в нашей базе.")
        
    status_dict = {"user": "🟢 Трейдер", "garant": "👑 Гарант", "scammer": "🔴 СКАМЕР", "admin": "👨‍💻 Создатель"}
    text = (
        f"📋 <b>Сводка на {user['tg_username']}:</b>\n"
        f"<b>Статус:</b> {status_dict.get(user['status'])}\n"
        f"<b>Сделок:</b> {user['trades']} | <b>Жалоб:</b> {user['scams']}\n"
        f"<b>Надежность:</b> {generate_trust_bar(user['trades'], user['scams'])}"
    )
    if user['status'] == 'scammer':
        text = "‼️ <b>ЭТОТ ЧЕЛОВЕК НАХОДИТСЯ В ЧС! ВОЗДЕРЖИТЕСЬ ОТ СДЕЛОК!</b> ‼️\n\n" + text
        
    await message.reply(text)

@router.message(IsGroup())
async def group_message_monitor(message: Message):
    # Автоматически проверяем каждое сообщение в группе на наличие отправителя в ЧС
    conn = get_db_connection()
    user = conn.execute("SELECT status FROM users WHERE tg_id = ?", (message.from_user.id,)).fetchone()
    conn.close()
    
    if user and user['status'] == 'scammer':
        try:
            await message.delete() # Пытаемся удалить сообщение скамера
            warn_msg = await message.answer(f"⚠️ Сообщение от @{message.from_user.username} удалено. <b>Пользователь находится в глобальной БАЗЕ СКАМЕРОВ!</b>")
            await asyncio.sleep(10)
            await warn_msg.delete() # Удаляем варнинг через 10 сек чтобы не засорять чат
        except TelegramBadRequest:
            pass # Если у бота нет прав на удаление, он просто молчит

async def auto_backup_loop():
    """Фоновая задача: скидывать БД админу каждый час"""
    while True:
        await asyncio.sleep(3600) # 3600 секунд = 1 час
        try:
            if os.path.exists("antiscam_pro.db"):
                await bot.send_document(ADMIN_ID, FSInputFile("antiscam_pro.db"), caption="🕒 <b>Автоматический ежечасный бэкап БД.</b>\n(Создано фоновым процессом безопасности)")
        except Exception as e:
            logging.error(f"Ошибка авто-бэкапа: {e}")

# ================= ЗАПУСК БОТА =================
async def main():
    init_db()
    upgrade_db() # Применяем миграции при запуске
    
    # Запускаем фоновый бэкап асинхронно
    asyncio.create_task(auto_backup_loop())
    
    print("🚀 AntiScam Pro Bot успешно запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
