import os
import re
import html
import asyncio
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest
from aiogram.types import (
    ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery,
)
from supabase import create_client, Client

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hrbot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
_fs = os.getenv("FORCE_SUBS_CHANNEL")
FORCE_SUBS_CHAT = int(_fs) if _fs.lstrip("-").isdigit() else _fs
FORCE_SUBS_LINK = os.getenv("FORCE_SUBS_LINK") or f"https://t.me/{_fs.lstrip('@')}"
OWNER_BOT_ID = int(os.getenv("OWNER_BOT_ID"))
MAX_GROUPS = 3

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
)
dp = Dispatcher()

BOT_ID = 0
BOT_USERNAME = ""
GROUP = {"group", "supergroup"}
P, S, D = "primary", "success", "danger"
CYCLE = [P, S, D]

# ==========================================
# TEMPLATE BAWAAN (GLOBAL)
# ==========================================
DEFAULT_SETTINGS = {
    "mute_text": "✅ Transaksi selesai!\n{name}, silakan isi <b>honest review</b> dulu ya supaya bisa chat lagi.",
    "btn_review": "Isi Honest Review",
    "notif_unmute": "✅ {name} sudah mengisi honest review dan di-unmute.",
    "btn_limit": "⚠️ Jika Limit",
    "btn_unmute": "🔓 Unmute"
}
SETTINGS = DEFAULT_SETTINGS.copy()

DEFAULTS = {
    "command_name": "done",
}

MUTED = ChatPermissions(
    can_send_messages=False, can_send_audios=False, can_send_documents=False,
    can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
    can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
    can_add_web_page_previews=False,
)
ALLOW = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True,
)

PENDING: dict[int, set[int]] = {}
KNOWN_USERS: set[int] = set()
TASKS: set = set()
MUTE_MSGS: dict[tuple[int, int], int] = {}

class Cfg(StatesGroup):
    wait = State()

class Bc(StatesGroup):
    wait = State()

class Lmt(StatesGroup):
    wait = State()

class Tpl(StatesGroup):
    wait = State()

# ==========================================
# HELPERS & DATABASE SETTINGS
# ==========================================
def B(text, cb=None, url=None, style=None, emoji=None):
    kw = {"text": text}
    if cb:
        kw["callback_data"] = cb
    if url:
        kw["url"] = url
    if style:
        kw["style"] = style
    if emoji:
        kw["icon_custom_emoji_id"] = emoji
    return InlineKeyboardButton(**kw)

def KB(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])

async def db(fn):
    return await asyncio.to_thread(fn)

def _fetch_all(table, cols):
    out, i = [], 0
    while True:
        r = supabase.table(table).select(cols).range(i, i + 999).execute()
        data = r.data or []
        out += data
        if len(data) < 1000:
            return out
        i += 1000

async def load_settings():
    global SETTINGS
    try:
        r = await db(lambda: supabase.table("bot_settings").select("*").execute())
        data = {row["key"]: row["value"] for row in (r.data or [])}
        SETTINGS = {**DEFAULT_SETTINGS, **data}
        log.info("Global templates loaded.")
    except Exception as e:
        log.error(f"Failed to load settings: {e}")

async def save_setting(key, value):
    await db(lambda: supabase.table("bot_settings").upsert({"key": key, "value": value}).execute())
    SETTINGS[key] = value

def mention(u):
    return f'<a href="tg://user?id={u.id}">{html.escape(u.first_name or "User")}</a>'

def norm_cmd(c):
    return (c or DEFAULTS["command_name"]).strip().lstrip("/.!").lower()

def split_button(raw):
    emoji_id = None
    m = re.search(r'emoji[-_]id="(\d+)"', raw)
    if m:
        emoji_id = m.group(1)
    t = re.sub(r"<tg-emoji[^>]*>.*?</tg-emoji>", "", raw)
    t = html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
    return t[:64], emoji_id

def review_url(cfg):
    if cfg.get("channel_link"):
        return cfg["channel_link"]
    if cfg.get("comsect_id"):
        return "https://t.me/c/" + str(cfg["comsect_id"]).replace("-100", "", 1)
    return None

def review_kb(cfg, extra=None):
    rows = []
    url = review_url(cfg)
    if url:
        t, e = split_button(cfg.get("button_text") or SETTINGS["btn_review"])
        rows.append([B(t, url=url, style=P, emoji=e)])
    if extra:
        rows.append(extra)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

def render_wording(cfg, who):
    # Menggunakan settingan grup, jika kosong gunakan settingan global
    txt = cfg.get("reply_text") or SETTINGS["mute_text"]
    return txt.replace("{name}", who)

async def check_subs(uid):
    try:
        m = await bot.get_chat_member(FORCE_SUBS_CHAT, uid)
        return m.status in ("member", "administrator", "creator") or (
            m.status == "restricted" and getattr(m, "is_member", False)
        )
    except Exception:
        return False

async def is_admin(chat_id, uid):
    try:
        m = await bot.get_chat_member(chat_id, uid)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

async def show(ev, text, kb=None):
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=kb)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
        await ev.message.answer(text, reply_markup=kb)
    else:
        await ev.answer(text, reply_markup=kb)

async def save_user(u):
    if u.id in KNOWN_USERS:
        return
    try:
        await db(lambda: supabase.table("bot_users").upsert(
            {"user_id": u.id, "first_name": u.first_name, "username": u.username}
        ).execute())
        KNOWN_USERS.add(u.id)
    except Exception as e:
        log.error(f"save_user: {e}")

async def get_cfg(gid, owner=None):
    def q():
        x = supabase.table("group_configs").select("*").eq("group_id", gid)
        if owner is not None:
            x = x.eq("owner_id", owner)
        return x.execute()
    r = await db(q)
    return r.data[0] if r.data else None

async def my_groups(uid):
    r = await db(lambda: supabase.table("group_configs").select("*").eq("owner_id", uid).execute())
    return r.data or []

async def count(table):
    r = await db(lambda: supabase.table(table).select("*", count="exact").limit(1).execute())
    return r.count or 0

async def set_pending(uid, gid):
    await db(lambda: supabase.table("pending_reviews").upsert({"user_id": uid, "group_id": gid}).execute())
    PENDING.setdefault(uid, set()).add(gid)

async def clear_pending(uid, gid):
    await db(lambda: supabase.table("pending_reviews").delete().eq("user_id", uid).eq("group_id", gid).execute())
    if uid in PENDING:
        PENDING[uid].discard(gid)
        if not PENDING[uid]:
            del PENDING[uid]

async def delete_mute_msg(gid, uid):
    mid = MUTE_MSGS.pop((gid, uid), None)
    if not mid:
        return
    try:
        await bot.delete_message(gid, mid)
    except Exception as e:
        log.warning(f"delete_mute_msg: {e}")

async def purge_group(gid):
    await db(lambda: supabase.table("pending_reviews").delete().eq("group_id", gid).execute())
    await db(lambda: supabase.table("group_configs").delete().eq("group_id", gid).execute())
    for uid in list(PENDING):
        PENDING[uid].discard(gid)
        if not PENDING[uid]:
            del PENDING[uid]
    for key in list(MUTE_MSGS):
        if key[0] == gid:
            MUTE_MSGS.pop(key, None)

async def unmute(gid, uid):
    try:
        ch = await bot.get_chat(gid)
        perms = ch.permissions if ch.permissions and ch.permissions.can_send_messages else ALLOW
        await bot.restrict_chat_member(gid, uid, perms)
        return True
    except Exception as e:
        log.error(f"unmute: {e}")
        return False

# ==========================================
# TEKS & KEYBOARD
# ==========================================
HOME_TEXT = (
    "🤖 <b>Helper Honest Review Bot</b>\n\n"
    "Bot otomatis <b>mute</b> member setelah transaksi selesai dan <b>unmute</b> "
    "begitu mereka mengisi honest review di comsect.\n\nPilih menu 👇"
)
HELP_TEXT = (
    "📖 <b>Panduan</b>\n\n"
    "1. ➕ <b>Tambah Grup</b> → masukkan bot ke grup transaksi, jadikan admin (hak <i>Restrict Users</i>).\n"
    "2. Tekan tombol <b>Daftarkan Grup Ini</b> yang muncul di grup (atau ketik /init).\n"
    "3. Buka ⚙️ pengaturan grup → set channel/comsect, command, wording, teks tombol.\n"
    "4. Di grup: admin reply member lalu ketik /done atau .done → member di-mute.\n"
    "5. Saat member komentar di comsect → otomatis di-unmute.\n\n"
    "<i>Maks 3 grup per akun. Wording mendukung {name}. Kirim emoji premium langsung di teks wording/tombol.</i>"
)
JOIN_TEXT = "⚠️ <b>Kamu belum subscribe!</b>\nJoin channel official dulu untuk memakai bot ini."
PROMPTS = {
    "ch": (
        "🔗 <b>Set Channel Honest Review & Comsect</b>\n\nKirim salah satu:\n"
        "• Link <b>post</b> honest review, contoh <code>https://t.me/namach/2</code>\n"
        "• Forward post honest review dari channel tersebut\n"
        "• ID grup comsect (angka, awalan -100)\n\n"
        "Comsect dideteksi otomatis & unmute hanya berlaku untuk komentar di post itu. Masukkan bot ke comsect dan jadikan "
        "<b>admin</b> supaya bisa membaca komentar."
    ),
    "cmd": "⌨️ Kirim nama command, contoh <code>done</code>.\nOtomatis aktif sebagai /done, .done, dan !done.",
    "wd": "💬 Kirim teks wording balasan.\nPakai <code>{name}</code> untuk mention member. Format teks & emoji premium ikut tersimpan.",
    "bt": "🔘 Kirim teks tombol (maks 64 karakter).\nEmoji premium akan jadi ikon tombol.",
}

def home_kb(uid):
    rows = [
        [B("➕ Tambah Grup", cb="add", style=P), B("📋 Grup Saya", cb="mine", style=S)],
        [B("📖 Panduan", cb="help")],
    ]
    if uid == OWNER_BOT_ID:
        rows.append([B("⚙️ Template Pesan", cb="tpl", style=S)])
        rows.append([B("📢 Broadcast", cb="bc", style=D), B("📊 Statistik", cb="stat", style=P)])
    return KB(*rows)

def join_kb():
    return KB([B("📢 Join Channel", url=FORCE_SUBS_LINK, style=P)],
              [B("✅ Saya Sudah Join", cb="chk", style=S)])

def group_text(cfg):
    n = norm_cmd(cfg.get("command_name"))
    post_line = f"📌 Post review: <code>#{cfg['review_post_id']}</code>\n" if cfg.get("review_post_id") else ""
    bt_text = split_button(cfg.get("button_text") or SETTINGS['btn_review'])[0]
    return (
        f"⚙️ <b>{html.escape(cfg.get('title') or 'Grup')}</b>\n<code>{cfg['group_id']}</code>\n\n"
        f"🔗 Channel: {html.escape(cfg.get('channel_link') or '— belum diatur')}\n{post_line}"
        f"💬 Comsect ID: <code>{cfg.get('comsect_id') or '—'}</code>\n"
        f"⌨️ Command: <code>/{n}</code> · <code>.{n}</code>\n"
        f"📝 Wording:\n<blockquote>{cfg.get('reply_text') or SETTINGS['mute_text']}</blockquote>\n"
        f"🔘 Tombol: {html.escape(bt_text)}"
    )

def group_kb(gid):
    return KB(
        [B("🔗 Channel & Comsect", cb=f"s:ch:{gid}", style=P), B("⌨️ Command", cb=f"s:cmd:{gid}", style=P)],
        [B("💬 Wording", cb=f"s:wd:{gid}", style=S), B("🔘 Teks Tombol", cb=f"s:bt:{gid}", style=S)],
        [B("🧪 Preview", cb=f"pv:{gid}")],
        [B("🗑 Hapus Grup", cb=f"del:{gid}", style=D), B("⬅️ Kembali", cb="mine")],
    )

async def show_group(ev, cfg):
    await show(ev, group_text(cfg), group_kb(cfg["group_id"]))

def cfg_link_kb(gid):
    return KB([B("⚙️ Atur di PM Bot", url=f"https://t.me/{BOT_USERNAME}?start=cfg_{gid}", style=P)])

# ==========================================
# MIDDLEWARE
# ==========================================
class Gate(BaseMiddleware):
    async def __call__(self, handler, ev, data):
        if isinstance(ev, CallbackQuery):
            if not ev.message or ev.message.chat.type != "private" or ev.data == "chk":
                return await handler(ev, data)
        elif isinstance(ev, Message):
            if ev.chat.type != "private":
                return await handler(ev, data)
        else:
            return await handler(ev, data)

        u = ev.from_user
        if not u:
            return await handler(ev, data)
        await save_user(u)
        if u.id == OWNER_BOT_ID or await check_subs(u.id):
            return await handler(ev, data)

        if isinstance(ev, CallbackQuery):
            await ev.answer("⚠️ Join channel dulu ya!", show_alert=True)
            await ev.message.answer(JOIN_TEXT, reply_markup=join_kb())
        else:
            await ev.answer(JOIN_TEXT, reply_markup=join_kb())

dp.message.outer_middleware(Gate())
dp.callback_query.outer_middleware(Gate())

# ==========================================
# PRIVATE: MENU UTAMA
# ==========================================
@dp.message(Command("start"), F.chat.type == "private")
async def start_cmd(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    arg = command.args
    if arg:
        if arg.startswith("cfg_"):
            try:
                cfg = await get_cfg(int(arg[4:]), m.from_user.id)
            except ValueError:
                cfg = None
            if cfg:
                return await show_group(m, cfg)

        elif arg.startswith("limit_"):
            try:
                gid = int(arg[6:])
            except ValueError:
                return await show(m, HOME_TEXT, home_kb(m.from_user.id))

            if gid not in PENDING.get(m.from_user.id, set()):
                return await m.answer("✅ Kamu tidak sedang dalam masa pending review atau sudah di-unmute.")

            cfg = await get_cfg(gid)
            if not cfg or not cfg.get("comsect_id"):
                return await m.answer("⚠️ Grup ini belum mengatur comsect tujuan. Silakan hubungi admin.")

            await state.set_state(Lmt.wait)
            await state.update_data(gid=gid)
            return await m.answer(
                "📝 <b>Kirim Honest Review Kamu</b>\n\n"
                "Kirimkan teks, foto, atau video untuk review kamu di sini."
            )

    await show(m, HOME_TEXT, home_kb(m.from_user.id))

@dp.message(Lmt.wait, F.chat.type == "private")
async def limit_review_handler(m: Message, state: FSMContext):
    data = await state.get_data()
    gid = data.get("gid")
    uid = m.from_user.id

    if gid not in PENDING.get(uid, set()):
        await state.clear()
        return await m.answer("✅ Kamu sudah tidak dalam masa pending review.")

    cfg = await get_cfg(gid)
    comsect_id = cfg.get("comsect_id")
    if not comsect_id:
        await state.clear()
        return await m.answer("⚠️ Comsect tujuan tidak ditemukan, silakan lapor ke admin grup.")

    try:
        u = m.from_user
        uname_text = f" (@{u.username})" if u.username else ""
        sender_info = (
            f"📝 <b>Review via Bot (Limit)</b>\n"
            f"👤 Dari: {mention(u)}{uname_text}\n"
            f"🆔 ID: <code>{u.id}</code>\n"
            f"➖➖➖➖➖➖➖➖➖➖\n"
        )

        reply_id = cfg.get("review_post_id")

        if m.text:
            combined_text = f"{sender_info}{m.html_text}"
            await bot.send_message(
                chat_id=comsect_id,
                text=combined_text,
                reply_to_message_id=reply_id
            )
        else:
            original_caption = m.html_text or ""
            combined_caption = f"{sender_info}{original_caption}" if original_caption else sender_info

            await bot.copy_message(
                chat_id=comsect_id,
                from_chat_id=m.chat.id,
                message_id=m.message_id,
                reply_to_message_id=reply_id,
                caption=combined_caption
            )

        if await unmute(gid, uid):
            await delete_mute_msg(gid, uid)
            await clear_pending(uid, gid)
            await m.answer("✅ <b>Review terkirim!</b>\nKamu sudah otomatis di-unmute di grup.")
            try:
                notif = SETTINGS["notif_unmute"].replace("{name}", mention(u))
                await bot.send_message(gid, notif)
            except Exception:
                pass
        else:
            await m.answer("✅ Review berhasil diteruskan ke comsect, namun gagal melakukan unmute (pastikan hak admin bot di grup masih ada).")

        await state.clear()
    except Exception as e:
        log.error(f"Limit review error: {e}")
        await m.answer(f"❌ Gagal meneruskan pesan ke comsect. Pastikan bot dijadikan admin penuh di comsect.\nError: {e}")

@dp.callback_query(F.data == "chk")
async def cb_chk(c: CallbackQuery):
    if await check_subs(c.from_user.id):
        await save_user(c.from_user)
        await show(c, HOME_TEXT, home_kb(c.from_user.id))
        await c.answer("✅ Terverifikasi!")
    else:
        await c.answer("❌ Kamu belum join channel!", show_alert=True)

@dp.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, HOME_TEXT, home_kb(c.from_user.id))
    await c.answer()

@dp.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await show(c, HELP_TEXT, KB([B("⬅️ Kembali", cb="home")]))
    await c.answer()

@dp.callback_query(F.data == "add")
async def cb_add(c: CallbackQuery):
    url = f"https://t.me/{BOT_USERNAME}?startgroup=true&admin=restrict_members"
    text = (
        "➕ <b>Tambah Grup</b>\n\n"
        "1. Tekan tombol di bawah, pilih grup transaksimu.\n"
        "2. Beri bot hak admin <i>Restrict Users</i>.\n"
        "3. Di grup, tekan <b>Daftarkan Grup Ini</b> (atau ketik /init).\n\n"
        f"<i>Maksimal {MAX_GROUPS} grup per akun.</i>"
    )
    await show(c, text, KB([B("➕ Tambahkan Bot ke Grup", url=url, style=P)], [B("⬅️ Kembali", cb="home")]))
    await c.answer()

@dp.callback_query(F.data == "mine")
async def cb_mine(c: CallbackQuery, state: FSMContext):
    await state.clear()
    gs = await my_groups(c.from_user.id)
    if not gs:
        await show(c, "📋 Kamu belum punya grup terdaftar.",
                   KB([B("➕ Tambah Grup", cb="add", style=P)], [B("⬅️ Kembali", cb="home")]))
    else:
        rows = [[B(f"⚙️ {g.get('title') or g['group_id']}", cb=f"g:{g['group_id']}", style=CYCLE[i % 3])]
                for i, g in enumerate(gs)]
        rows.append([B("⬅️ Kembali", cb="home")])
        await show(c, f"📋 <b>Grup Saya</b> ({len(gs)}/{MAX_GROUPS})\nPilih grup untuk diatur:", KB(*rows))
    await c.answer()

# ==========================================
# PRIVATE: PENGATURAN GRUP
# ==========================================
@dp.callback_query(F.data.startswith("g:"))
async def cb_group(c: CallbackQuery, state: FSMContext):
    await state.clear()
    cfg = await get_cfg(int(c.data[2:]), c.from_user.id)
    if not cfg:
        return await c.answer("Grup tidak ditemukan.", show_alert=True)
    await show_group(c, cfg)
    await c.answer()

@dp.callback_query(F.data.startswith("s:"))
async def cb_set(c: CallbackQuery, state: FSMContext):
    _, field, gid = c.data.split(":")
    gid = int(gid)
    if not await get_cfg(gid, c.from_user.id):
        return await c.answer("Grup tidak ditemukan.", show_alert=True)
    await state.set_state(Cfg.wait)
    await state.update_data(gid=gid, field=field)
    await show(c, PROMPTS[field], KB([B("❌ Batal", cb=f"g:{gid}", style=D)]))
    await c.answer()

@dp.callback_query(F.data.startswith("pv:"))
async def cb_preview(c: CallbackQuery):
    cfg = await get_cfg(int(c.data[3:]), c.from_user.id)
    if not cfg:
        return await c.answer("Grup tidak ditemukan.", show_alert=True)
    try:
        await c.message.answer("🧪 <b>Preview:</b>\n\n" + render_wording(cfg, mention(c.from_user)),
                               reply_markup=review_kb(cfg))
        await c.answer()
    except TelegramBadRequest:
        await c.answer("Wording/tombol tidak valid, set ulang.", show_alert=True)

@dp.callback_query(F.data.startswith("del:"))
async def cb_del(c: CallbackQuery):
    gid = int(c.data[4:])
    await show(c, "🗑 Yakin hapus grup ini dari bot?",
               KB([B("Ya, Hapus", cb=f"delok:{gid}", style=D), B("Batal", cb=f"g:{gid}", style=S)]))
    await c.answer()

@dp.callback_query(F.data.startswith("delok:"))
async def cb_delok(c: CallbackQuery):
    gid = int(c.data[6:])
    if await get_cfg(gid, c.from_user.id):
        await purge_group(gid)
    await show(c, "✅ Grup dihapus.", KB([B("📋 Grup Saya", cb="mine", style=P)]))
    await c.answer()

async def bot_note(cid):
    try:
        mm = await bot.get_chat_member(cid, BOT_ID)
        if mm.status == "administrator":
            return "\n✅ Bot sudah admin di comsect."
        if mm.status == "member":
            return "\n⚠️ Bot ada di comsect tapi belum admin. Jadikan admin agar bisa membaca komentar."
    except Exception:
        pass
    return "\n⚠️ Bot belum ada di comsect. Masukkan bot & jadikan admin."

async def resolve_channel(m: Message):
    chat, post = None, None
    fo = getattr(m, "forward_origin", None)
    if fo is not None and getattr(fo, "type", None) == "channel":
        chat, post = fo.chat, fo.message_id
    elif m.text:
        t = m.text.strip()
        if re.fullmatch(r"-?\d{5,}", t):
            cid = int(t)
            return {"comsect_id": cid}, "ID comsect disimpan." + await bot_note(cid), True
        mp = re.search(r"t\.me/c/(\d+)/(\d+)", t)
        if mp:
            post = int(mp.group(2))
            try:
                chat = await bot.get_chat(int("-100" + mp.group(1)))
            except Exception:
                return ({"channel_link": t, "review_post_id": post},
                        "Link post tersimpan, tapi comsect tidak bisa dideteksi (channel privat).\nKirim ID comsect (angka) ya.",
                        False)
        elif "t.me/+" in t or "joinchat" in t:
            return ({"channel_link": t},
                    "Link tersimpan, tapi comsect tidak bisa dideteksi dari link privat.\nKirim ID comsect (angka) ya.",
                    False)
        else:
            mt = re.search(r"(?:t\.me/|@)([A-Za-z][A-Za-z0-9_]{3,})(?:/(\d+))?", t)
            if mt:
                post = int(mt.group(2)) if mt.group(2) else None
                try:
                    chat = await bot.get_chat("@" + mt.group(1))
                except Exception:
                    return None
    if not chat:
        return None
    try:
        full = await bot.get_chat(chat.id)
    except Exception:
        full = chat
    uname = getattr(full, "username", None)
    link = None
    if uname:
        link = f"https://t.me/{uname}" + (f"/{post}" if post else "")
    elif post:
        link = f"https://t.me/c/{str(full.id).replace('-100', '', 1)}/{post}"
    linked = getattr(full, "linked_chat_id", None)
    if not link and not linked:
        return None
    upd = {"review_post_id": post}
    if link:
        upd["channel_link"] = link
    if linked:
        upd["comsect_id"] = linked
    pnote = f" (post #{post})" if post else " (tanpa post spesifik, semua komentar dihitung)"
    if linked:
        return upd, "Link review & comsect terdeteksi" + pnote + "." + await bot_note(linked), True
    return upd, "Link tersimpan" + pnote + ", tapi channel tidak punya grup diskusi.\nKirim ID comsect (angka) manual.", False

@dp.message(Cfg.wait, F.chat.type == "private")
async def cfg_input(m: Message, state: FSMContext):
    d = await state.get_data()
    gid, field = d.get("gid"), d.get("field")
    if not await get_cfg(gid, m.from_user.id):
        await state.clear()
        return await m.answer("Grup tidak ditemukan.")

    note, done = "", True
    if field == "ch":
        r = await resolve_channel(m)
        if r is None:
            return await m.answer("⚠️ Tidak terbaca. Kirim @username/link channel publik, forward pesan channel, atau ID comsect.")
        upd, note, done = r
    elif field == "cmd":
        name = norm_cmd(m.text or "")
        if not re.fullmatch(r"\S{1,32}", name):
            return await m.answer("⚠️ Command tidak valid. Gunakan 1-32 karakter tanpa spasi (bisa 1 huruf atau emoji).")
        upd, note = {"command_name": name}, f"Aktif: /{name}  .{name}  !{name}"
    elif field == "wd":
        if not m.text:
            return await m.answer("⚠️ Kirim dalam bentuk teks.")
        upd = {"reply_text": m.html_text[:700]}
    elif field == "bt":
        if not m.text:
            return await m.answer("⚠️ Kirim dalam bentuk teks.")
        upd = {"button_text": m.html_text[:200]}
    else:
        return await state.clear()

    await db(lambda: supabase.table("group_configs").update(upd).eq("group_id", gid).execute())
    await m.answer("✅ Tersimpan!" + (f"\n{note}" if note else ""))
    if done:
        await state.clear()
        cfg = await get_cfg(gid)
        await m.answer(group_text(cfg), reply_markup=group_kb(gid))

# ==========================================
# PRIVATE: TEMPLATE PESAN (OWNER)
# ==========================================
TPL_KEYS = {
    "mute_text": "📝 Teks Mute",
    "btn_review": "🔘 Tombol Isi HR",
    "notif_unmute": "🔔 Notif Unmute",
    "btn_limit": "⚠️ Tombol Limit",
    "btn_unmute": "🔓 Tombol Unmute"
}

async def render_tpl_menu(ev):
    rows = []
    for k, label in TPL_KEYS.items():
        rows.append([B(f"{label}: {split_button(SETTINGS.get(k))[0][:20]}...", cb=f"tpl:{k}")])
    rows.append([B("⬅️ Kembali", cb="home")])
    await show(ev, "⚙️ <b>Pengaturan Template Global</b>\nPilih teks/tombol yang ingin diubah:\n\n" 
                   "<i>Pengaturan ini berlaku untuk semua grup, kecuali grup tersebut sudah mengatur teksnya sendiri di menu pengaturan grup.</i>", KB(*rows))

@dp.callback_query(F.data == "tpl")
async def cb_tpl(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != OWNER_BOT_ID:
        return await c.answer()
    await state.clear()
    await render_tpl_menu(c)
    await c.answer()

@dp.callback_query(F.data.startswith("tpl:"))
async def cb_tpl_edit(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != OWNER_BOT_ID:
        return await c.answer()
    key = c.data.split(":")[1]
    await state.set_state(Tpl.wait)
    await state.update_data(key=key)
    
    info = "(Gunakan {name} untuk mention member)" if key in ("mute_text", "notif_unmute") else "(Emoji premium didukung)"
    await show(c, f"Kirim teks baru untuk <b>{TPL_KEYS.get(key, key)}</b>.\n\nNilai saat ini:\n<blockquote>{SETTINGS.get(key)}</blockquote>\n\n{info}", KB([B("❌ Batal", cb="tpl", style=D)]))
    await c.answer()

@dp.message(Tpl.wait, F.chat.type == "private")
async def tpl_input(m: Message, state: FSMContext):
    if m.from_user.id != OWNER_BOT_ID:
        return
    d = await state.get_data()
    key = d.get("key")
    if not key or not m.text:
        await state.clear()
        return await m.answer("Gagal menyimpan.")

    val = m.html_text[:1000]
    if key in ("btn_review", "btn_limit", "btn_unmute"):
        t, e = split_button(val)
        if not t:
            return await m.answer("⚠️ Teks tombol tidak boleh kosong.")
    
    await save_setting(key, val)
    await state.clear()
    
    rows = []
    for k, label in TPL_KEYS.items():
        rows.append([B(f"{label}: {split_button(SETTINGS.get(k))[0][:20]}...", cb=f"tpl:{k}")])
    rows.append([B("⬅️ Kembali", cb="home")])
    await m.answer(f"✅ Template <b>{TPL_KEYS.get(key)}</b> berhasil diperbarui!\n\n⚙️ <b>Pengaturan Template Global</b>\nPilih teks/tombol yang ingin diubah:", reply_markup=KB(*rows))

# ==========================================
# PRIVATE: BROADCAST & STATISTIK (OWNER)
# ==========================================
async def start_bc(ev, state: FSMContext):
    await state.set_state(Bc.wait)
    await show(ev, "📢 <b>Broadcast</b>\nKirim pesan yang mau disebar (teks/foto/video/dll, format & emoji premium ikut).",
               KB([B("❌ Batal", cb="home", style=D)]))

@dp.message(Command("broadcast"), F.chat.type == "private")
async def cmd_bc(m: Message, state: FSMContext):
    if m.from_user.id == OWNER_BOT_ID:
        await start_bc(m, state)

@dp.callback_query(F.data == "bc")
async def cb_bc(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != OWNER_BOT_ID:
        return await c.answer()
    await start_bc(c, state)
    await c.answer()

@dp.callback_query(F.data == "stat")
async def cb_stat(c: CallbackQuery):
    if c.from_user.id != OWNER_BOT_ID:
        return await c.answer()
    u, g, p = await count("bot_users"), await count("group_configs"), await count("pending_reviews")
    await show(c, f"📊 <b>Statistik</b>\n\n👥 User: <b>{u}</b>\n🏘 Grup: <b>{g}</b>\n⏳ Pending review: <b>{p}</b>",
               KB([B("⬅️ Kembali", cb="home")]))
    await c.answer()

@dp.message(Bc.wait, F.chat.type == "private")
async def bc_msg(m: Message, state: FSMContext):
    if m.from_user.id != OWNER_BOT_ID:
        return
    total = await count("bot_users")
    await state.update_data(mid=m.message_id, cid=m.chat.id)
    await m.answer(
        f"Kirim pesan di atas ke <b>{total}</b> user?",
        reply_to_message_id=m.message_id,
        reply_markup=KB(
            [B("📌 Kirim + Pin", cb="bcgo:1", style=S), B("📨 Kirim Saja", cb="bcgo:0", style=P)],
            [B("❌ Batal", cb="home", style=D)],
        ),
    )

async def run_broadcast(from_chat, mid, pin, status):
    ids = [r["user_id"] for r in await db(lambda: _fetch_all("bot_users", "user_id"))]
    ok = fail = 0
    for uid in ids:
        for _ in range(2):
            try:
                sent = await bot.copy_message(uid, from_chat, mid)
                if pin:
                    try:
                        await bot.pin_chat_message(uid, sent.message_id)
                    except Exception:
                        pass
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except Exception:
                fail += 1
                break
        else:
            fail += 1
        await asyncio.sleep(0.05)
    try:
        await status.edit_text(
            f"✅ <b>Broadcast selesai!</b>\nBerhasil: {ok}\nGagal/Blokir: {fail}",
            reply_markup=KB([B("🏠 Menu", cb="home", style=P)]),
        )
    except Exception:
        pass

@dp.callback_query(F.data.startswith("bcgo:"))
async def cb_bcgo(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != OWNER_BOT_ID:
        return await c.answer()
    d = await state.get_data()
    if "mid" not in d:
        return await c.answer("Sesi habis, ulangi broadcast.", show_alert=True)
    pin = c.data.endswith("1")
    await state.clear()
    await c.answer("Broadcast dimulai")
    await c.message.edit_text("🚀 Broadcast berjalan...", reply_markup=None)
    t = asyncio.create_task(run_broadcast(d["cid"], d["mid"], pin, c.message))
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)

# ==========================================
# GRUP: REGISTRASI
# ==========================================
async def register_group(chat, user):
    if not await check_subs(user.id):
        return False, "⚠️ Join channel official dulu lewat PM bot."
    if not await is_admin(chat.id, user.id):
        return False, "❌ Hanya admin grup yang bisa mendaftarkan grup."
    ex = await get_cfg(chat.id)
    if ex and ex["owner_id"] != user.id:
        return False, "❌ Grup ini sudah didaftarkan oleh akun lain."
    if not ex:
        if len(await my_groups(user.id)) >= MAX_GROUPS:
            return False, f"❌ Limit maksimal {MAX_GROUPS} grup per akun tercapai."
        try:
            # Mengosongkan reply_text & button_text agar grup otomatis menggunakan template global
            await db(lambda: supabase.table("group_configs").insert({
                "group_id": chat.id, "owner_id": user.id, "title": chat.title,
                "command_name": DEFAULTS["command_name"],
                "reply_text": None,
                "button_text": None,
            }).execute())
        except Exception as e:
            return False, f"❌ Gagal mendaftarkan grup: {e}"
    text = f"✅ Grup terdaftar!\n📌 ID: <code>{chat.id}</code>\nLanjut atur di PM bot 👇"
    try:
        bm = await bot.get_chat_member(chat.id, BOT_ID)
        if bm.status != "administrator" or not getattr(bm, "can_restrict_members", False):
            text += "\n\n⚠️ Jadikan bot admin dengan hak <b>Restrict Users</b> agar bisa mute."
    except Exception:
        pass
    return True, text

@dp.message(Command("init"), F.chat.type.in_(GROUP))
async def init_cmd(m: Message):
    if not m.from_user:
        return
    ok, text = await register_group(m.chat, m.from_user)
    await m.reply(text, reply_markup=cfg_link_kb(m.chat.id) if ok else None)

@dp.callback_query(F.data == "reg")
async def cb_reg(c: CallbackQuery):
    ok, text = await register_group(c.message.chat, c.from_user)
    await c.answer()
    await c.message.edit_text(text, reply_markup=cfg_link_kb(c.message.chat.id) if ok else None)

@dp.my_chat_member(F.chat.type.in_(GROUP))
async def on_member(ev):
    st, old = ev.new_chat_member.status, ev.old_chat_member.status
    if st in ("left", "kicked"):
        await purge_group(ev.chat.id)
        return
    if st in ("member", "administrator") and old in ("left", "kicked"):
        try:
            await bot.send_message(
                ev.chat.id,
                "👋 <b>Halo!</b> Jadikan bot admin (hak <i>Restrict Users</i>), lalu daftarkan grup transaksi ini.\n"
                "<i>Kalau ini grup comsect, abaikan saja.</i>",
                reply_markup=KB([B("✅ Daftarkan Grup Ini", cb="reg", style=S)]),
            )
        except Exception:
            pass

# ==========================================
# GRUP: MUTE & UNMUTE
# ==========================================
@dp.callback_query(F.data.startswith("um:"))
async def cb_um(c: CallbackQuery):
    _, gid, uid = c.data.split(":")
    gid, uid = int(gid), int(uid)
    if not await is_admin(gid, c.from_user.id):
        return await c.answer("Hanya admin yang bisa.", show_alert=True)
    if await unmute(gid, uid):
        MUTE_MSGS.pop((gid, uid), None)
        await clear_pending(uid, gid)
        await c.answer("Di-unmute")
        await c.message.edit_text(
            c.message.html_text + f"\n\n🔓 Di-unmute manual oleh {mention(c.from_user)}.", reply_markup=None)
    else:
        await c.answer("Gagal unmute, cek hak admin bot.", show_alert=True)

THREADS: dict = {}

def comment_post(m: Message):
    rm = m.reply_to_message
    if rm is not None:
        fo = getattr(rm, "forward_origin", None)
        if fo is not None and getattr(fo, "type", None) == "channel":
            THREADS[(m.chat.id, rm.message_id)] = fo.message_id
            return fo.message_id
    tid = getattr(m, "message_thread_id", None)
    return THREADS.get((m.chat.id, tid)) if tid else None

async def try_unmute(m: Message):
    uid = m.from_user.id
    for gid in list(PENDING.get(uid, ())):
        r = await db(lambda g=gid: supabase.table("group_configs").select("comsect_id,review_post_id").eq("group_id", g).execute())
        if r.data and r.data[0].get("comsect_id") == m.chat.id:
            post = r.data[0].get("review_post_id")
            if post and comment_post(m) != post:
                continue
            if await unmute(gid, uid):
                await delete_mute_msg(gid, uid)
                await clear_pending(uid, gid)
                try:
                    notif = SETTINGS["notif_unmute"].replace("{name}", mention(m.from_user))
                    await bot.send_message(gid, notif)
                except Exception:
                    pass

async def try_mute(m: Message):
    first = m.text.strip().split(maxsplit=1)[0].lower()
    if not first or first[0] not in "/.!":
        return
    name = first[1:].split("@")[0]
    gid = m.chat.id
    cfg = await get_cfg(gid)
    if not cfg or name != norm_cmd(cfg.get("command_name")):
        return

    if not m.reply_to_message:
        return
    target = m.reply_to_message.from_user
    if not target:
        return

    anon = bool(m.sender_chat and m.sender_chat.id == gid)
    sender = m.from_user.id if m.from_user else None
    if not (anon or sender == cfg["owner_id"] or (sender and await is_admin(gid, sender))):
        return

    if sender is not None and target.id == sender:
        return
    if target.is_bot or target.id == 777000 or await is_admin(gid, target.id):
        return

    try:
        await bot.restrict_chat_member(gid, target.id, MUTED)
        await set_pending(target.id, gid)
        who = mention(target)
        text = render_wording(cfg, who) + f"\n\n🔇 {who} di-mute sampai review selesai."

        limit_url = f"https://t.me/{BOT_USERNAME}?start=limit_{gid}"
        
        t_limit, e_limit = split_button(SETTINGS["btn_limit"])
        t_um, e_um = split_button(SETTINGS["btn_unmute"])
        extra_btns = [
            B(t_limit, url=limit_url, emoji=e_limit),
            B(t_um, cb=f"um:{gid}:{target.id}", style=S, emoji=e_um)
        ]

        sent = await m.reply_to_message.reply(text, reply_markup=review_kb(cfg, extra_btns))
        if sent:
            MUTE_MSGS[(gid, target.id)] = sent.message_id
    except Exception as e:
        await m.reply(f"❌ Bot gagal mute. Pastikan bot admin dengan hak Restrict Users.\nError: {e}")

@dp.message(F.chat.type.in_(GROUP))
async def group_handler(m: Message):
    fo = getattr(m, "forward_origin", None)
    if getattr(m, "is_automatic_forward", False) and fo is not None and getattr(fo, "type", None) == "channel":
        THREADS[(m.chat.id, m.message_id)] = fo.message_id
    u = m.from_user
    anon = bool(m.sender_chat and m.sender_chat.id == m.chat.id)
    if not u or (u.is_bot and not anon):
        return
    if u.id in PENDING:
        await try_unmute(m)
    if m.reply_to_message and m.text and m.text[0] in "/.!":
        await try_mute(m)

# ==========================================
# FALLBACK PRIVATE
# ==========================================
@dp.message(F.chat.type == "private")
async def fallback(m: Message):
    await show(m, HOME_TEXT, home_kb(m.from_user.id))

# ==========================================
# MAIN
# ==========================================
async def main():
    global BOT_ID, BOT_USERNAME
    me = await bot.get_me()
    BOT_ID, BOT_USERNAME = me.id, me.username
    await load_settings()
    for r in await db(lambda: _fetch_all("pending_reviews", "user_id,group_id")):
        PENDING.setdefault(r["user_id"], set()).add(r["group_id"])
    print("Bot is running...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
