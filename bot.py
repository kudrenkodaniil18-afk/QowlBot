import asyncio
import aiosqlite
import logging
from datetime import datetime, timedelta
from io import BytesIO
import json

import matplotlib.pyplot as plt
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InputMediaPhoto, InputMediaVideo, BufferedInputFile
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter

# ================= CONFIG =================
TOKEN = "8638375796:AAGlo9QbXA_viI8VxDJ5QF2zWe6im9BgF0o"
CHANNEL_ID = "@qowlc"
ADMINS = {6411263772}
DB = "bot.db"
BANNER = "https://i.ibb.co/qFgBSgF5/your-image.jpg"

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ================= FSM =================
class BroadcastState(StatesGroup):
    waiting_header = State()
    waiting_text = State()
    waiting_media = State()

class AdminSearchState(StatesGroup):
    waiting_user_id = State()
    waiting_ban_id = State()
    waiting_unban_id = State()

# ================= DATABASE =================
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            ref_by INTEGER,
            refs INTEGER DEFAULT 0,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            display_name TEXT
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY,
            banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS user_achievements (
            user_id INTEGER,
            achievement TEXT,
            awarded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, achievement)
        )
        """)
        await db.commit()

async def add_user(user_id, username, ref_by=None):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        if await cur.fetchone():
            return
        if ref_by == user_id:
            ref_by = None
        username = username or "no_name"
        ref_by = int(ref_by) if ref_by else None
        display_name = username if username != "no_name" else "Пользователь"
        await db.execute(
            "INSERT INTO users (user_id, username, ref_by, refs, display_name) VALUES (?, ?, ?, 0, ?)",
            (user_id, username, ref_by, display_name)
        )
        if ref_by:
            await db.execute("UPDATE users SET refs = refs + 1 WHERE user_id=?", (ref_by,))
            await check_and_grant_refs_achievement(ref_by)
        await db.commit()
        await grant_achievement(user_id, "Появление в боте", notify=True)
        if ref_by:
            try:
                await bot.send_message(ref_by, f"🎉 Новый реферал: {user_id}")
            except:
                pass

async def is_banned(user_id):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT 1 FROM banned_users WHERE user_id=?", (user_id,))
        return await cur.fetchone() is not None

async def ban_user(user_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)", (user_id,))
        await db.commit()

async def unban_user(user_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM banned_users WHERE user_id=?", (user_id,))
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return await cur.fetchone()

async def get_username(user_id):
    if not user_id: return "нет"
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT username FROM users WHERE user_id=?", (user_id,))
        r = await cur.fetchone()
        return r[0] if r and r[0] else "нет"

async def get_all_users():
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id FROM users")
        return await cur.fetchall()

def is_admin(user_id):
    return user_id in ADMINS

async def is_subscribed(user_id):
    try:
        m = await bot.get_chat_member(CHANNEL_ID, user_id)
        return m.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.warning(f"Ошибка проверки подписки для {user_id}: {e}")
        return False

# ================= ACHIEVEMENTS =================
async def has_achievement(user_id, ach):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT 1 FROM user_achievements WHERE user_id=? AND achievement=?", (user_id, ach))
        return await cur.fetchone() is not None

async def grant_achievement(user_id, ach, notify=True):
    if await has_achievement(user_id, ach):
        return False
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO user_achievements (user_id, achievement) VALUES (?, ?)", (user_id, ach))
        await db.commit()
    if notify:
        try:
            await bot.send_message(user_id, f"🏆 Достижение получено: {ach}")
        except:
            pass
    return True

async def get_user_achievements(user_id):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT achievement FROM user_achievements WHERE user_id=?", (user_id,))
        rows = await cur.fetchall()
        return [row[0] for row in rows]

async def check_and_grant_refs_achievement(user_id):
    user = await get_user(user_id)
    if user and user[3] >= 10:
        await grant_achievement(user_id, "10 рефералов", notify=True)

# ================= GAME ACCESS =================
async def is_game_available(user_id):
    if await is_banned(user_id): return False
    subscribed = await is_subscribed(user_id)
    user = await get_user(user_id)
    if not user: return False
    return subscribed and user[3] >= 10

# ================= PROFILE TEXT =================
async def profile_text(user):
    user_id, username, ref_by, refs, joined_at, display_name = user
    inviter = await get_username(ref_by)
    bot_username = (await bot.me()).username
    ref_link = f"https://t.me/{bot_username}?start={user_id}"
    sub_icon = "🟢" if await is_subscribed(user_id) else "🔴"
    game_status = "✅ Доступ открыт" if await is_game_available(user_id) else "🔒 Требуется: 10 рефералов и подписка"
    return (
        f"🎮 <b>QOWL</b>\n━━━━━━━━━━━━━━\n\n"
        f"👤 ID: <code>{user_id}</code>\nNick: @{username}\nИмя: {display_name or username}\n\n"
        f"👥 Рефералы: {refs}/10\n🧑 Пригласил: @{inviter}\n\n"
        f"🔗 Реф. ссылка:\n{ref_link}\n\n📢 Подписка: {sub_icon}\n🎮 Доступ к игре: {game_status}\n━━━━━━━━━━━━━━"
    )

# ================= KEYBOARDS =================
def profile_kb(user_id=None):
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Рефералы", callback_data="refs")
    kb.button(text="🏆 Топ", callback_data="top")
    kb.button(text="📢 ТГК", url=f"https://t.me/{CHANNEL_ID.replace('@','')}")
    kb.button(text="📌 Подписка", callback_data="sub")
    kb.button(text="🎮 Игра", callback_data="game")
    if is_admin(user_id):
        kb.button(text="⚙ Админ", callback_data="admin_panel")
    kb.adjust(2,2,1)
    return kb.as_markup()

def back_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅ Назад", callback_data="back")
    return kb.as_markup()

def admin_main_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Рассылка", callback_data="admin_bc")
    kb.button(text="🔍 Поиск", callback_data="admin_search")
    kb.button(text="📊 Графики", callback_data="admin_stats")
    kb.button(text="🚫 Бан", callback_data="admin_ban")
    kb.button(text="✅ Разбан", callback_data="admin_unban")
    kb.button(text="⬅ Назад", callback_data="back")
    kb.adjust(1)
    return kb.as_markup()

def media_skip_done_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Пропустить медиа", callback_data="skip_media")
    kb.button(text="✅ Готово", callback_data="done_media")
    kb.adjust(2)
    return kb.as_markup()

def admin_cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="admin_cancel")
    return kb.as_markup()

# ================= CALLBACKS =================
async def get_refs_list(user_id):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT username, joined_at FROM users WHERE ref_by=?", (user_id,))
        return await cur.fetchall()

@dp.callback_query(F.data == "back")
async def back_to_profile(call: CallbackQuery):
    if await is_banned(call.from_user.id):
        await call.answer("Вы забанены", show_alert=True)
        return
    user = await get_user(call.from_user.id)
    try:
        await call.message.edit_caption(caption=await profile_text(user), reply_markup=profile_kb(call.from_user.id), parse_mode="HTML")
    except: pass
    await call.answer()

@dp.callback_query(F.data == "refs")
async def show_refs(call: CallbackQuery):
    if await is_banned(call.from_user.id):
        await call.answer("Вы забанены", show_alert=True)
        return
    refs = await get_refs_list(call.from_user.id)
    if not refs:
        text = "📭 У вас пока нет рефералов."
    else:
        lines = [f"@{name[0]}" if name[0] != "no_name" else "Аноним" for name in refs[:10]]
        text = "👥 Ваши рефералы:\n" + "\n".join(lines)
    await call.message.edit_caption(caption=text, reply_markup=back_kb(), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "top")
async def show_top(call: CallbackQuery):
    if await is_banned(call.from_user.id):
        await call.answer("Вы забанены", show_alert=True)
        return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, refs FROM users ORDER BY refs DESC LIMIT 10")
        top = await cur.fetchall()
    if not top:
        text = "🏆 Топ рефералов пока пуст."
    else:
        lines = [f"{i}. @{await get_username(uid)} - {refs} реф." for i, (uid, refs) in enumerate(top, 1)]
        text = "🏆 Топ-10 рефералов:\n" + "\n".join(lines)
    await call.message.edit_caption(caption=text, reply_markup=back_kb(), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "sub")
async def check_subscription(call: CallbackQuery):
    if await is_banned(call.from_user.id):
        await call.answer("Вы забанены", show_alert=True)
        return
    user_id = call.from_user.id
    subscribed = await is_subscribed(user_id)
    if subscribed:
        await grant_achievement(user_id, "Подписка на канал")
        alert_text = "✅ Вы подписаны!"
    else:
        alert_text = f"❌ Не подписаны. Подпишитесь: https://t.me/{CHANNEL_ID.replace('@','')}"
    await call.answer(alert_text, show_alert=True)
    user = await get_user(user_id)
    new_caption = await profile_text(user)
    if call.message.caption != new_caption:
        try:
            await call.message.edit_caption(caption=new_caption, reply_markup=profile_kb(user_id), parse_mode="HTML")
        except: pass

@dp.callback_query(F.data == "game")
async def game_info(call: CallbackQuery):
    if await is_banned(call.from_user.id):
        await call.answer("Вы забанены", show_alert=True)
        return
    user_id = call.from_user.id
    available = await is_game_available(user_id)
    if available:
        await call.answer("✅ Доступ к игре открыт!", show_alert=True)
    else:
        user = await get_user(user_id)
        refs = user[3] if user else 0
        sub_status = await is_subscribed(user_id)
        need_refs = max(0, 10 - refs)
        await call.answer(f"❌ Доступ закрыт.\n- Подписка: {'✅' if sub_status else '❌'}\n- 10 рефералов: {refs}/10 (ещё {need_refs})", show_alert=True)
    await call.answer()

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.message.edit_caption(caption="⚙ Админ-панель", reply_markup=admin_main_kb(), parse_mode="HTML")
    await call.answer()

# ================= АДМИН: ПОИСК, БАН, РАЗБАН =================
@dp.callback_query(F.data == "admin_search")
async def admin_search_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await call.message.answer("🔍 Введите ID или @username:")
    await state.set_state(AdminSearchState.waiting_user_id)
    await call.answer()

@dp.message(AdminSearchState.waiting_user_id)
async def admin_search_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    query = message.text.strip()
    user_id = int(query) if query.isdigit() else None
    async with aiosqlite.connect(DB) as db:
        if user_id:
            cur = await db.execute("SELECT user_id, username, refs, joined_at, display_name FROM users WHERE user_id=?", (user_id,))
        else:
            cur = await db.execute("SELECT user_id, username, refs, joined_at, display_name FROM users WHERE username LIKE ?", (f"%{query.lstrip('@')}%",))
        row = await cur.fetchone()
    if not row:
        await message.answer("❌ Не найден")
    else:
        uid, uname, refs, joined, dname = row
        banned = await is_banned(uid)
        text = f"🔍 Найден:\nID: {uid}\nUsername: @{uname}\nИмя: {dname or uname}\nРефералов: {refs}\nДата: {joined}\nСтатус: {'🚫 Забанен' if banned else '✅ Активен'}"
        await message.answer(text)
    await state.clear()

@dp.callback_query(F.data == "admin_ban")
async def admin_ban_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await call.message.answer("🚫 Введите ID для бана:", reply_markup=admin_cancel_kb())
    await state.set_state(AdminSearchState.waiting_ban_id)
    await call.answer()

@dp.message(AdminSearchState.waiting_ban_id)
async def admin_ban_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    if not message.text.isdigit():
        await message.answer("❌ Введите число")
        return
    uid = int(message.text)
    if uid in ADMINS:
        await message.answer("❌ Нельзя забанить админа")
    else:
        await ban_user(uid)
        await message.answer(f"✅ Пользователь {uid} забанен")
    await state.clear()

@dp.callback_query(F.data == "admin_unban")
async def admin_unban_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await call.message.answer("✅ Введите ID для разбана:", reply_markup=admin_cancel_kb())
    await state.set_state(AdminSearchState.waiting_unban_id)
    await call.answer()

@dp.message(AdminSearchState.waiting_unban_id)
async def admin_unban_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    if not message.text.isdigit():
        await message.answer("❌ Введите число")
        return
    uid = int(message.text)
    await unban_user(uid)
    await message.answer(f"✅ Пользователь {uid} разбанен")
    await state.clear()

@dp.callback_query(F.data == "admin_cancel")
async def admin_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await call.answer("Отменено")

# ================= ГРАФИКИ =================
async def generate_user_growth_chart():
    async with aiosqlite.connect(DB) as db:
        thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        cur = await db.execute("SELECT DATE(joined_at), COUNT(*) FROM users WHERE joined_at >= ? GROUP BY DATE(joined_at) ORDER BY DATE(joined_at)", (thirty_days_ago,))
        data = await cur.fetchall()
    days = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(30, -1, -1)]
    counts = [0]*31
    for d, c in data:
        try: idx = days.index(d); counts[idx]=c
        except: pass
    fig, ax = plt.subplots(figsize=(10,5))
    ax.plot(days, counts, marker='o', color='#d4af37')
    ax.set_title("Рост пользователей (30 дней)", color='white')
    ax.set_facecolor('#0a0a0a')
    fig.patch.set_facecolor('#0a0a0a')
    ax.tick_params(colors='white')
    plt.xticks(rotation=45)
    plt.tight_layout()
    buf = BytesIO(); plt.savefig(buf, format='png', facecolor='#0a0a0a'); buf.seek(0); plt.close()
    return buf

async def generate_top_refs_chart():
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT username, refs FROM users ORDER BY refs DESC LIMIT 10")
        top = await cur.fetchall()
    if not top:
        fig, ax = plt.subplots(figsize=(8,5))
        ax.text(0.5,0.5,"Нет данных", ha='center', color='white')
        ax.set_facecolor('#0a0a0a')
        fig.patch.set_facecolor('#0a0a0a')
    else:
        names = [row[0] if row[0]!='no_name' else f"ID_{i}" for i,row in enumerate(top)]
        refs = [row[1] for row in top]
        fig, ax = plt.subplots(figsize=(10,6))
        ax.barh(names, refs, color='#d4af37')
        ax.set_xlabel("Рефералы", color='white')
        ax.set_title("Топ-10", color='white')
        ax.tick_params(colors='white')
        ax.set_facecolor('#0a0a0a')
        fig.patch.set_facecolor('#0a0a0a')
        ax.invert_yaxis()
    plt.tight_layout()
    buf = BytesIO(); plt.savefig(buf, format='png', facecolor='#0a0a0a'); buf.seek(0); plt.close()
    return buf

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    await call.answer("Генерирую...")
    growth = await generate_user_growth_chart()
    top = await generate_top_refs_chart()
    await call.message.answer_photo(photo=BufferedInputFile(growth.getvalue(), "growth.png"), caption="📈 Рост")
    await call.message.answer_photo(photo=BufferedInputFile(top.getvalue(), "top.png"), caption="🏆 Топ рефералов")

# ================= РАССЫЛКА =================
async def send_to_user(user_id: int, caption: str, media_list: list, retries=3):
    for attempt in range(retries):
        try:
            if not media_list:
                await bot.send_photo(user_id, photo=BANNER, caption=caption, parse_mode="HTML")
            elif len(media_list) == 1:
                item = media_list[0]
                if item["type"] == "photo":
                    await bot.send_photo(user_id, photo=item["file_id"], caption=caption, parse_mode="HTML")
                else:
                    await bot.send_video(user_id, video=item["file_id"], caption=caption, parse_mode="HTML")
            else:
                group = []
                for i, item in enumerate(media_list):
                    if i == 0:
                        if item["type"] == "photo":
                            group.append(InputMediaPhoto(media=item["file_id"], caption=caption, parse_mode="HTML"))
                        else:
                            group.append(InputMediaVideo(media=item["file_id"], caption=caption, parse_mode="HTML"))
                    else:
                        if item["type"] == "photo":
                            group.append(InputMediaPhoto(media=item["file_id"]))
                        else:
                            group.append(InputMediaVideo(media=item["file_id"]))
                await bot.send_media_group(user_id, media=group[:10])
            return True
        except TelegramForbiddenError:
            logger.warning(f"Не могу отправить {user_id}: бот заблокирован")
            return False
        except TelegramBadRequest as e:
            if "user is deactivated" in str(e).lower():
                logger.warning(f"Пользователь {user_id} деактивирован")
                return False
            logger.warning(f"BadRequest для {user_id}: {e}, попытка {attempt+1}")
        except TelegramRetryAfter as e:
            logger.warning(f"Flood limit, ждём {e.retry_after} сек")
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            logger.error(f"Ошибка {user_id}: {e}, попытка {attempt+1}")
        if attempt < retries - 1:
            await asyncio.sleep(5)
    return False

async def perform_broadcast(origin_message: Message, state: FSMContext):
    data = await state.get_data()
    header = data.get("header", "")
    text = data.get("text", "")
    media_list = data.get("media_list", [])
    caption = f"📢 <b>{header}</b>\n━━━━━━━━━━━━━━\n\n{text}\n\n━━━━━━━━━━━━━━"
    users = await get_all_users()
    sent, failed = 0, 0
    for (uid,) in users:
        if await is_banned(uid):
            failed += 1
            continue
        ok = await send_to_user(uid, caption, media_list)
        if ok:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.2)
    await origin_message.answer(f"✅ Рассылка завершена\nОтправлено: {sent}\nНе доставлено: {failed}")
    await state.clear()

@dp.callback_query(F.data == "admin_bc")
async def admin_bc(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await call.message.answer("✍ Введите заголовок:")
    await state.set_state(BroadcastState.waiting_header)
    await call.answer()

@dp.message(BroadcastState.waiting_header)
async def bc_header(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.update_data(header=message.text)
    await message.answer("✍ Теперь текст:")
    await state.set_state(BroadcastState.waiting_text)

@dp.message(BroadcastState.waiting_text)
async def bc_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.update_data(text=message.text)
    await message.answer("📸 Отправьте фото/видео (можно несколько). Затем нажмите «✅ Готово».\nИли «📸 Пропустить».",
                         reply_markup=media_skip_done_kb())
    await state.set_state(BroadcastState.waiting_media)
    await state.update_data(media_list=[])

@dp.message(BroadcastState.waiting_media, F.photo)
async def bc_add_photo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    media = data.get("media_list", [])
    media.append({"type": "photo", "file_id": message.photo[-1].file_id})
    await state.update_data(media_list=media)
    await message.answer(f"✅ Добавлено. Всего {len(media)}.", reply_markup=media_skip_done_kb())

@dp.message(BroadcastState.waiting_media, F.video)
async def bc_add_video(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    media = data.get("media_list", [])
    media.append({"type": "video", "file_id": message.video.file_id})
    await state.update_data(media_list=media)
    await message.answer(f"✅ Добавлено. Всего {len(media)}.", reply_markup=media_skip_done_kb())

@dp.callback_query(F.data == "skip_media", BroadcastState.waiting_media)
async def bc_skip_media(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await perform_broadcast(call.message, state)
    await call.message.delete()
    await call.answer()

@dp.callback_query(F.data == "done_media", BroadcastState.waiting_media)
async def bc_done_media(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await perform_broadcast(call.message, state)
    await call.message.delete()
    await call.answer()

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.set_state(BroadcastState.waiting_header)
    await message.answer("✍ Введите заголовок:")

# ================= КОМАНДЫ ПОЛЬЗОВАТЕЛЯ =================
@dp.message(CommandStart())
async def start(message: Message):
    if await is_banned(message.from_user.id):
        await message.answer("🚫 Вы забанены")
        return
    ref = None
    if len(message.text.split()) > 1:
        try: ref = int(message.text.split()[1])
        except: pass
    await add_user(message.from_user.id, message.from_user.username, ref)
    user = await get_user(message.from_user.id)
    await message.answer_photo(photo=BANNER, caption=await profile_text(user), reply_markup=profile_kb(message.from_user.id), parse_mode="HTML")

@dp.message(Command("profile"))
async def profile_command(message: Message):
    if await is_banned(message.from_user.id): return
    user = await get_user(message.from_user.id)
    if not user:
        await add_user(message.from_user.id, message.from_user.username)
        user = await get_user(message.from_user.id)
    await message.answer_photo(photo=BANNER, caption=await profile_text(user), reply_markup=profile_kb(message.from_user.id), parse_mode="HTML")

@dp.message(Command("help"))
async def help_command(message: Message):
    if await is_banned(message.from_user.id): return
    await message.answer("📋 Помощь\n/start - профиль\n/profile - профиль\n/help - помощь\n\nТакже используйте кнопки меню.")

# ================= ЗАПУСК =================
async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("🤖 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())