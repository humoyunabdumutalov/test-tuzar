import asyncio
import os
import io
import json
import uuid
import random
import textwrap
import asyncpg
from contextlib import suppress
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
from fpdf import FPDF
import PyPDF2
from docx import Document
from pptx import Presentation
from keep_alive import keep_alive

# --- SOZLAMALAR ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_ID = 5031441892  # O'zingizning Telegram ID raqamingizni yozing!

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash-lite')

# --- BAZA ---
db_pool = None

async def init_db_pool():
    global db_pool
    db_pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=5, max_size=20, statement_cache_size=0
    )
    async with db_pool.acquire() as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, name TEXT, score INTEGER, tests_taken INTEGER)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS quizzes (quiz_id TEXT PRIMARY KEY, vaqt INTEGER, daraja TEXT, savollar TEXT)''')

async def add_user(user_id, name):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id, name, score, tests_taken) VALUES ($1, $2, 0, 0) ON CONFLICT (user_id) DO NOTHING", str(user_id), name)

POLL_DATA = {}   
SESSION_SCORES = {} 

async def notify_admin(context: str, error_msg: str):
    if ADMIN_ID != 0:
        with suppress(Exception):
            await bot.send_message(ADMIN_ID, f"🚨 **TIZIMDA XATOLIK**\n*Joylashuv:* {context}\n\n`{error_msg}`", parse_mode="Markdown")

# --- OG'IR VAZIFALAR ---
def read_file_sync(file_data, filename):
    text = ""
    try:
        if filename.endswith('.pdf'):
            text = "".join([p.extract_text() or "" for p in PyPDF2.PdfReader(file_data).pages])
        elif filename.endswith('.docx'):
            text = "\n".join([p.text for p in Document(file_data).paragraphs])
    except Exception as e:
        print(f"Fayl o'qish xatosi: {e}")
    return text

def create_pdf_sync(quiz_id, savollar, file_name, bot_username):
    pdf = FPDF()
    pdf.add_page()
    
    pdf.set_font("helvetica", style="I", size=10)
    pdf.set_text_color(128, 128, 128)
    pdf.cell(0, 10, text=f"Ushbu test @{bot_username} tomonidan yaratildi | Art of Engineering", align='R', new_x="LMARGIN", new_y="NEXT")
    
    pdf.set_font("helvetica", style="B", size=14)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 10, text=f"TEST ID: {quiz_id}", align='C', new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, text="", new_x="LMARGIN", new_y="NEXT")
    
    pdf.set_font("helvetica", size=12)
    wrapper = textwrap.TextWrapper(width=75, break_long_words=True)
    
    def to_safe_str(t):
        t = str(t).replace('\n', ' ')
        for old, new in {'—': '-', '–': '-', '−': '-', '“': '"', '”': '"', '«': '"', '»': '"', '‘': "'", '’': "'", '`': "'", '…': '...', '№': '#'}.items():
            t = t.replace(old, new)
        return t.encode('windows-1252', 'replace').decode('windows-1252')
    
    for i, s in enumerate(savollar, 1):
        q_text = to_safe_str(s.get('savol', ''))
        for line in wrapper.wrap(f"{i}. {q_text}"):
            pdf.cell(0, 8, text=line, new_x="LMARGIN", new_y="NEXT")
        
        for v_idx, v in enumerate(s.get('variantlar', [])):
            v_text = to_safe_str(v)
            for line in wrapper.wrap(f"   {chr(65+v_idx)}) {v_text}"):
                pdf.cell(0, 8, text=line, new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 5, text="", new_x="LMARGIN", new_y="NEXT")
        
    pdf.output(file_name)

def create_pptx_sync(slaydlar_json, file_name, dizayn_nomi):
    prs = Presentation()
    
    # 1-Slayd (Sarlavha)
    title_slide_layout = prs.slide_layouts[0]
    slide = prs.slides.add_slide(title_slide_layout)
    title = slide.shapes.title
    subtitle = slide.placeholders[1]
    title.text = "Sizning Taqdimotingiz"
    subtitle.text = f"Tanlangan dizayn: {dizayn_nomi}\nAI orqali avtomatik yaratildi"

    # Matnli slaydlar
    bullet_slide_layout = prs.slide_layouts[1]
    for data in slaydlar_json:
        slide = prs.slides.add_slide(bullet_slide_layout)
        shapes = slide.shapes
        title_shape = shapes.title
        body_shape = shapes.placeholders[1]
        
        title_shape.text = data.get('sarlavha', 'Sarlavha yo\'q')
        tf = body_shape.text_frame
        
        qismlar = data.get('qismlar', [])
        if qismlar:
            tf.text = qismlar[0]  
            for point in qismlar[1:]:
                p = tf.add_paragraph()
                p.text = point
                p.level = 0
                
    prs.save(file_name)

# --- FSM VA MENYULAR ---
class QuizForm(StatesGroup):
    usul = State()
    daraja = State() 
    soni = State()
    vaqt = State()
    malumot = State()
    msgs_to_delete = State() 

class SlideForm(StatesGroup): 
    soni = State()
    dizayn = State()
    mavzu = State()
    msgs_to_delete = State()

class AdminState(StatesGroup):
    xabar_kutish = State()

bekor_tugma = [KeyboardButton(text="🔙 Bekor qilish")]

asosiy_menyu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📄 Fayldan test yasash"), KeyboardButton(text="🤖 AI Mavzudan yasash")],
        [KeyboardButton(text="📊 Taqdimot (Slayd) yasash")],
        [KeyboardButton(text="🏆 Liderlar taxtasi"), KeyboardButton(text="👤 Mening profilim")],
        [KeyboardButton(text="ℹ️ Yordam va qoidalar")]
    ], resize_keyboard=True, input_field_placeholder="Bo'limni tanlang 👇"
)

daraja_menyu = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🟢 Oson (1 ball)"), KeyboardButton(text="🟡 O'rtacha (2 ball)")], [KeyboardButton(text="🔴 Qiyin (3 ball)")], bekor_tugma], resize_keyboard=True)
soni_menyu = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="5"), KeyboardButton(text="10"), KeyboardButton(text="15"), KeyboardButton(text="20")], bekor_tugma], resize_keyboard=True)
vaqt_menyu = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="15 soniya"), KeyboardButton(text="30 soniya")], [KeyboardButton(text="60 soniya"), KeyboardButton(text="⏳ Cheklovsiz")], bekor_tugma], resize_keyboard=True)

slayd_soni_menyu = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="5 ta slayd"), KeyboardButton(text="7 ta slayd"), KeyboardButton(text="10 ta slayd")], bekor_tugma], resize_keyboard=True)
bekor_menyu = ReplyKeyboardMarkup(keyboard=[bekor_tugma], resize_keyboard=True)
admin_menyu = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📊 Umumiy Statistika"), KeyboardButton(text="📣 Xabar tarqatish")], [KeyboardButton(text="🥇 To'liq ro'yxat (Profillar)")], [KeyboardButton(text="🔙 Bosh menyu")]], resize_keyboard=True)

# YAngi: Inline Dizayn tugmalari
dizayn_inline = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="🔵 Zamonaviy (Ko'k)", callback_data="dizayn_kok")],
        [InlineKeyboardButton(text="🟢 Tabiat (Yashil)", callback_data="dizayn_yashil")],
        [InlineKeyboardButton(text="🟠 Rasmiy (Apelsin)", callback_data="dizayn_rasmiy")]
    ]
)

# --- YORDAMCHI FUNKSIYALAR ---
async def track_msg(state: FSMContext, msg_id: int):
    data = await state.get_data()
    msgs = data.get('msgs_to_delete', [])
    msgs.append(msg_id)
    await state.update_data(msgs_to_delete=msgs)

async def delete_tracked_msgs(chat_id: int, state: FSMContext):
    data = await state.get_data()
    msgs = data.get('msgs_to_delete', [])
    for msg_id in msgs:
        with suppress(TelegramBadRequest):
            await bot.delete_message(chat_id, msg_id)
    await state.update_data(msgs_to_delete=[])

# --- ASOSIY BUYRUQLAR ---
@dp.message(Command("id"))
async def get_id(message: types.Message):
    await message.answer(f"Sizning ID raqamingiz: `{message.from_user.id}`\nUni nusxalab oling.", parse_mode="Markdown")

@dp.message(F.text == "🔙 Bekor qilish")
async def bekor_qilish_handler(message: types.Message, state: FSMContext):
    await delete_tracked_msgs(message.chat.id, state)
    await state.clear()
    await message.answer("❌ Jarayon bekor qilindi.", reply_markup=asosiy_menyu)

@dp.message(F.text == "🏆 Liderlar taxtasi")
async def show_reyting(message: types.Message):
    user_id = str(message.from_user.id)
    async with db_pool.acquire() as conn:
        top_users = await conn.fetch("SELECT name, score FROM users ORDER BY score DESC LIMIT 10")
        
        user_data = await conn.fetchrow("SELECT score FROM users WHERE user_id = $1", user_id)
        if user_data:
            user_score = user_data['score']
            higher_users_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE score > $1", user_score)
            user_rank = higher_users_count + 1
            
            person_above = await conn.fetchrow("SELECT score FROM users WHERE score > $1 ORDER BY score ASC LIMIT 1", user_score)
            diff = person_above['score'] - user_score if person_above else 0
        else:
            user_score, user_rank, diff = 0, "Yo'q", 0

    text = "🏆 **TOP-10 QAHRAMONLAR:**\n\n"
    for i, u in enumerate(top_users, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "🎗"
        text += f"{medal} {i}. {u['name']} — {u['score']} ball\n"
        
    text += f"\n──────────────\n"
    text += f"👤 **Sizning o'rningiz:** {user_rank}\n"
    text += f"🎯 **Shaxsiy ballingiz:** {user_score}\n"
    
    if diff > 0:
        text += f"🚀 *Keyingi o'ringa o'tish uchun sizga yana {diff} ball kerak!*"
    elif user_rank == 1:
        text += f"👑 *Siz hozirda eng yuqori o'rindasiz!*"
        
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "👤 Mening profilim")
async def show_profile(message: types.Message):
    async with db_pool.acquire() as conn:
        u = await conn.fetchrow("SELECT name, score, tests_taken FROM users WHERE user_id = $1", str(message.from_user.id))
    if u:
        text = f"👤 **Sizning Profilingiz:**\n\nIsm: {u['name']}\n✅ Jami to'plangan ball: {u['score']}\n📝 Yechilgan testlar soni: {u['tests_taken']} marta"
    else:
        text = "Siz hali test yechmadingiz."
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "ℹ️ Yordam va qoidalar")
async def show_help(message: types.Message):
    help_text = "💡 **Qoidalar:**\n\nTest yaratish uchun pastdagi tugmalardan birini tanlang. Qiyinlik darajasiga qarab ballar beriladi:\n🟢 Oson = 1 ball\n🟡 O'rtacha = 2 ball\n🔴 Qiyin = 3 ball\n\nSlayd yasash uchun esa mavzuni batafsilroq yozing."
    await message.answer(help_text, parse_mode="Markdown")

@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext, command: CommandObject = None):
    await state.clear()
    await add_user(message.from_user.id, message.from_user.first_name)

    if command and command.args:
        quiz_id = command.args
        async with db_pool.acquire() as conn:
            quiz_row = await conn.fetchrow("SELECT vaqt, daraja, savollar FROM quizzes WHERE quiz_id = $1", quiz_id)
            if quiz_row:
                await conn.execute("UPDATE users SET tests_taken = tests_taken + 1 WHERE user_id = $1", str(message.from_user.id))
                await message.answer("🚀 Test boshlanmoqda...", reply_markup=ReplyKeyboardRemove())
                vaqt_cheklovi, daraja, savollar_json = quiz_row['vaqt'], quiz_row['daraja'], quiz_row['savollar']
                savollar = json.loads(savollar_json)
                user_id = message.from_user.id
                SESSION_SCORES[user_id] = 0 
                jami_savollar = len(savollar)
                ball_qiymati = 3 if "Qiyin" in daraja else (2 if "O'rtacha" in daraja else 1)

                for data in savollar:
                    q = data['savol'][:250]
                    opts = [str(opt)[:100] for opt in data['variantlar']][:4]
                    correct = int(data.get('togri_index', 0))
                    quiz_kwargs = {
                        "chat_id": message.chat.id, "question": q, "options": opts,
                        "type": 'quiz', "correct_option_id": correct, "is_anonymous": False 
                    }
                    if vaqt_cheklovi > 0: quiz_kwargs["open_period"] = vaqt_cheklovi
                    sent_poll = await bot.send_poll(**quiz_kwargs)
                    POLL_DATA[sent_poll.poll.id] = {"correct": correct, "points": ball_qiymati}
                    await asyncio.sleep(vaqt_cheklovi + 1 if vaqt_cheklovi > 0 else 2.0)

                if vaqt_cheklovi == 0:
                    await message.answer("🏁 **Barcha testlar yuborildi!**\nJavoblarni xotirjam yeching. Ballar avtomatik hisoblanadi.", reply_markup=asosiy_menyu, parse_mode="Markdown")
                    return

                togri_javoblar = SESSION_SCORES.get(user_id, 0)
                foiz = int((togri_javoblar / jami_savollar) * 100) if jami_savollar > 0 else 0
                await message.answer(f"🏁 **Yakunlandi!**\nSizning Natijangiz: {togri_javoblar} ta to'g'ri ({foiz}%)", reply_markup=asosiy_menyu, parse_mode="Markdown")
                return
            else:
                await message.answer("⚠️ Ushbu test topilmadi yoki muddati o'tgan.")
                return
    await message.answer("Assalomu alaykum! Xush kelibsiz.\nO'zingizga kerakli bo'limni tanlang:", reply_markup=asosiy_menyu)

@dp.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer):
    poll_id = poll_answer.poll_id
    user_id_int = poll_answer.user.id
    tanlangan_javob = poll_answer.option_ids[0] if poll_answer.option_ids else -1
    if poll_id in POLL_DATA and tanlangan_javob == POLL_DATA[poll_id]["correct"]:
        ball = POLL_DATA[poll_id]["points"]
        SESSION_SCORES[user_id_int] = SESSION_SCORES.get(user_id_int, 0) + 1
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET score = score + $1 WHERE user_id = $2", ball, str(user_id_int))

# --- ADMIN PANEL QISMI ---
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("👑 **Admin Panelga Xush Kelibsiz!**", reply_markup=admin_menyu, parse_mode="Markdown")

@dp.message(F.text == "🔙 Bosh menyu")
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Asosiy menyuga qaytdingiz.", reply_markup=asosiy_menyu)

@dp.message(F.text == "📊 Umumiy Statistika")
async def show_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    async with db_pool.acquire() as conn:
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
        quizzes_count = await conn.fetchval("SELECT COUNT(*) FROM quizzes")
    await message.answer(f"📊 **Loyiha Statistikasi:**\n👥 Qatnashchilar: {users_count} ta\n📝 Yaratilgan testlar: {quizzes_count} ta", parse_mode="Markdown")

@dp.message(F.text == "🥇 To'liq ro'yxat (Profillar)")
async def show_full_rating(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id, name, score FROM users ORDER BY score DESC LIMIT 50")
    text = "👑 **TOP-50 Ro'yxat (Profillar):**\nBarcha foydalanuvchilar ismining ustiga bosib, yutuq berish uchun ularning lichkasiga o'tishingiz mumkin.\n\n"
    for i, u in enumerate(users, 1):
        text += f"{i}. [{u['name']}](tg://user?id={u['user_id']}) — {u['score']} ball\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "📣 Xabar tarqatish")
async def ask_broadcast_msg(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("Barcha foydalanuvchilarga yuboriladigan xabarni (rasm/matn) kiriting:", reply_markup=admin_menyu)
    await state.set_state(AdminState.xabar_kutish)

@dp.message(AdminState.xabar_kutish)
async def send_broadcast_msg(message: types.Message, state: FSMContext):
    if message.text == "🔙 Bosh menyu":
        await state.clear()
        await message.answer("Bosh menyu.", reply_markup=asosiy_menyu)
        return
    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users")
    await message.answer("⏳ Xabar yuborilmoqda. Iltimos, kuting...")
    success, fail = 0, 0
    for u in users:
        try:
            await bot.copy_message(chat_id=int(u['user_id']), from_chat_id=message.chat.id, message_id=message.message_id)
            success += 1
            await asyncio.sleep(0.05) 
        except Exception:
            fail += 1
    await message.answer(f"✅ Tarqatish yakunlandi!\n\nYetib bordi: {success} ta\nBloklaganlar: {fail} ta", reply_markup=asosiy_menyu)
    await state.clear()

# --- SLAYD YASASH MANTIQI (YANGI) ---
@dp.message(F.text == "📊 Taqdimot (Slayd) yasash")
async def start_slide_creation(message: types.Message, state: FSMContext):
    await state.update_data(msgs_to_delete=[])
    await track_msg(state, message.message_id)
    await state.set_state(SlideForm.soni)
    msg = await message.answer("Nechta slayd kerakligini tanlang:", reply_markup=slayd_soni_menyu)
    await track_msg(state, msg.message_id)

@dp.message(SlideForm.soni)
async def get_slide_count(message: types.Message, state: FSMContext):
    await track_msg(state, message.message_id)
    soni_str = message.text.split()[0]
    if not soni_str.isdigit(): return
    await state.update_data(soni=int(soni_str))
    
    await state.set_state(SlideForm.dizayn)
    
    # DIQQAT: O'zingiz yasadigan 3 ta dizaynning rasmini bitta qilib (collage) yasab, telegramga yuklab, linkini shu yerga yozing.
    # Hozircha ishlashi uchun vaqtinchalik namuna rasm qo'ydim:
    rasm_url = "https://dummyimage.com/600x400/000/fff&text=Dizayn+Shablonlari" 
    
    try:
        msg = await message.answer_photo(
            photo=rasm_url,
            caption="Taqdimot uchun quyidagi dizaynlardan birini tanlang:", 
            reply_markup=dizayn_inline
        )
    except Exception:
        # Agar rasm ochilmasa, oddiy matn orqali davom etadi
        msg = await message.answer("Taqdimot uchun quyidagi dizaynlardan birini tanlang:", reply_markup=dizayn_inline)
        
    await track_msg(state, msg.message_id)

@dp.callback_query(SlideForm.dizayn, F.data.startswith("dizayn_"))
async def get_slide_design_inline(callback: types.CallbackQuery, state: FSMContext):
    dizayn_tanlovi = callback.data.split("_")[1]
    await state.update_data(dizayn=dizayn_tanlovi)
    await state.set_state(SlideForm.mavzu)
    
    await callback.message.delete() # Tanlab bo'lingach rasmli xabarni o'chiramiz
    
    msg = await bot.send_message(
        callback.message.chat.id, 
        "Ajoyib tanlov! Endi taqdimot mavzusini batafsil yozing.\n(Masalan: Issiqlik elektr stansiyalarining texnologik jarayonlari)", 
        reply_markup=bekor_menyu
    )
    await track_msg(state, msg.message_id)
    await callback.answer()

@dp.message(SlideForm.mavzu, F.text)
async def generate_slide_content(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await delete_tracked_msgs(message.chat.id, state)
    wait_msg = await message.answer("🟢 AI slaydlar matnini tayyorlamoqda. Iltimos, bir oz kuting...", reply_markup=ReplyKeyboardRemove())
    
    soni = data['soni']
    dizayn = data['dizayn']
    mavzu = message.text
    
    # Promptni mukammallashtirdik: ortiqcha yozuvlar yo'q, chuqur ilmiy ma'lumotlar bor!
    prompt = f"""Mavzu: '{mavzu}'. Shu mavzu bo'yicha {soni} ta slayd uchun mukammal va keng qamrovli taqdimot rejasi tuz.
    QOIDALAR:
    1. Sarlavhalarda "1-slayd", "2-slayd" kabi raqamlar QAT'IYAN ishlashilmasin. Faqat sof sarlavha yozilsin.
    2. Har bir slaydning "qismlar" ro'yxatida ma'lumotlar iloji boricha ko'p, batafsil, ilmiy va chuqur bo'lsin (kamida 4-5 ta uzun va tushunarli gaplar).
    3. FAQAT JSON formatida array qaytar. Boshqa hech qanday so'z yozma!
    Format: [{{"sarlavha": "Sof mavzu nomi", "qismlar": ["Batafsil ma'lumot 1...", "Batafsil ma'lumot 2..."]}}]"""
    
    try:
        response = await asyncio.to_thread(model.generate_content, prompt, generation_config={"response_mime_type": "application/json"})
        slaydlar_json = json.loads(response.text)
        
        file_name = f"/tmp/Taqdimot_{message.from_user.id}.pptx"
        await asyncio.to_thread(create_pptx_sync, slaydlar_json, file_name, dizayn)
        
        pptx_file = FSInputFile(file_name)
        await bot.send_document(message.chat.id, pptx_file, caption=f"✅ Marhamat, sizning taqdimotingiz tayyor!\nTanlangan dizayn: {dizayn}", reply_markup=asosiy_menyu)
        
        os.remove(file_name)
        await wait_msg.delete()
        await state.clear()
        
    except Exception as e:
        await notify_admin("Slayd Generatsiyasi", str(e))
        await wait_msg.delete()
        await message.answer("⚠️ Taqdimot yasashda texnik xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.", reply_markup=asosiy_menyu)
        await state.clear()

# --- TEST YASASH MANTIQI ---
@dp.message(F.text.in_(["📄 Fayldan test yasash", "🤖 AI Mavzudan yasash"]))
async def usul_tanlash(message: types.Message, state: FSMContext):
    await state.update_data(msgs_to_delete=[]) 
    await track_msg(state, message.message_id) 

    usul = "fayl" if "Fayl" in message.text else "mavzu"
    await state.update_data(usul=usul)
    await state.set_state(QuizForm.daraja)
    msg = await message.answer("Qiyinlik darajasini tanlang:", reply_markup=daraja_menyu)
    await track_msg(state, msg.message_id)

@dp.message(QuizForm.daraja)
async def daraja_olish(message: types.Message, state: FSMContext):
    await track_msg(state, message.message_id)
    if "Oson" not in message.text and "O'rtacha" not in message.text and "Qiyin" not in message.text: return
    await state.update_data(daraja=message.text)
    await state.set_state(QuizForm.soni)
    msg = await message.answer("Nechta savol tuzamiz?", reply_markup=soni_menyu)
    await track_msg(state, msg.message_id)

@dp.message(QuizForm.soni)
async def savol_sonini_olish(message: types.Message, state: FSMContext):
    await track_msg(state, message.message_id)
    if not message.text.isdigit(): return
    await state.update_data(soni=int(message.text))
    await state.set_state(QuizForm.vaqt)
    msg = await message.answer("Har bir savol uchun vaqt belgilang:", reply_markup=vaqt_menyu)
    await track_msg(state, msg.message_id)

@dp.message(QuizForm.vaqt)
async def vaqtni_olish(message: types.Message, state: FSMContext):
    await track_msg(state, message.message_id)
    matn = message.text
    vaqt = 0 if "Cheklovsiz" in matn else int(matn.split()[0])
    await state.update_data(vaqt=vaqt)
    
    data = await state.get_data()
    await state.set_state(QuizForm.malumot)
    javob = "Ma'lumotli faylni (Word yoki PDF) yuboring." if data['usul'] == 'fayl' else "Mavzuni batafsil yozing:"
    msg = await message.answer(javob, reply_markup=bekor_menyu)
    await track_msg(state, msg.message_id)

@dp.message(QuizForm.malumot, F.text)
async def mavzuni_qabul_qilish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if data.get('usul') != 'mavzu': return
    await delete_tracked_msgs(message.chat.id, state)
    wait_msg = await message.answer("🟢 AI test tuzmoqda...", reply_markup=ReplyKeyboardRemove())
    
    daraja_toza = data['daraja'].split()[-2] if "ball" in data['daraja'] else data['daraja'].split()[-1]
    qoshimcha = "Mantiqiy o'ylashni talab qiladigan murakkab savollar tuzing. DIQQAT: Telegram qoidasiga ko'ra har bir javob varianti uzunligi 90 ta belgidan oshmasligi QAT'IY SHART. Javoblar qisqa, aniq va londa bo'lsin!" if daraja_toza == "Qiyin" else "DIQQAT: Telegram qoidasiga ko'ra har bir javob varianti uzunligi 90 ta belgidan oshmasligi qat'iy shart!"
    
    prompt = f"'{message.text}' bo'yicha {data['soni']} ta test tuz. Qiyinlik: {daraja_toza}. {qoshimcha} FAQAT JSON ro'yxat ber. Variantlarga A, B, C, D yozma!\nNamuna: [{{\"savol\": \"...\", \"variantlar\": [\"J1\", \"J2\", \"J3\", \"J4\"], \"togri_index\": 0}}]"
    await generate_and_save(message, prompt, wait_msg, state, data['vaqt'], data['daraja'])

@dp.message(QuizForm.malumot, F.document)
async def faylni_qabul_qilish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if data.get('usul') != 'fayl': return
    await delete_tracked_msgs(message.chat.id, state)
    wait_msg = await message.answer("🔄 Fayl o'qilmoqda...", reply_markup=ReplyKeyboardRemove())

    try:
        file_info = await bot.get_file(message.document.file_id)
        file_data = io.BytesIO()
        await bot.download(file_info, destination=file_data)
        file_data.seek(0)
        filename = message.document.file_name.lower()
        
        text = await asyncio.to_thread(read_file_sync, file_data, filename)

        if not text.strip():
            await wait_msg.delete()
            await message.answer("⚠️ Fayl ichidan matn topilmadi. Boshqa fayl yuboring.", reply_markup=asosiy_menyu)
            await state.clear()
            return

        daraja_toza = data['daraja'].split()[-2] if "ball" in data['daraja'] else data['daraja'].split()[-1]
        qoshimcha = "Matn asosida chuqur mantiqiy o'ylashni talab qiladigan murakkab savollar tuzing. DIQQAT: Telegram qoidasiga ko'ra har bir javob varianti uzunligi 90 ta belgidan oshmasligi QAT'IY SHART. Javoblar qisqa va londa bo'lsin!" if daraja_toza == "Qiyin" else "DIQQAT: Telegram qoidasiga ko'ra har bir javob varianti uzunligi 90 ta belgidan oshmasligi qat'iy shart!"

        prompt = f"Matn asosida {data['soni']} ta test tuz. Qiyinlik: {daraja_toza}. {qoshimcha} FAQAT JSON ro'yxat ber. Variantlarga A, B, C, D yozma!\nNamuna: [{{\"savol\": \"...\", \"variantlar\": [\"J1\", \"J2\", \"J3\", \"J4\"], \"togri_index\": 0}}]\n\nMatn: {text[:8000]}"
        
        await wait_msg.delete()
        wait_msg_new = await message.answer("🟢 AI test tuzmoqda. Bu bir necha soniya olishi mumkin...", reply_markup=ReplyKeyboardRemove())
        
        await generate_and_save(message, prompt, wait_msg_new, state, data['vaqt'], data['daraja'])
    except Exception as e:
        await notify_admin("Fayldan test tuzish jarayoni", str(e))
        await wait_msg.delete()
        await message.answer("❌ Xatolik yuz berdi. Iltimos, boshqa fayl kiritib ko'ring.", reply_markup=asosiy_menyu)
        await state.clear()

async def generate_and_save(message: types.Message, prompt: str, wait_msg: types.Message, state: FSMContext, vaqt: int, daraja: str):
    try:
        response = await asyncio.to_thread(
            model.generate_content, prompt, generation_config={"response_mime_type": "application/json"}
        )
        savollar = json.loads(response.text)
        await wait_msg.delete()

        for data in savollar:
            eski_index = int(data.get('togri_index', 0))
            if eski_index < 0 or eski_index > 3: eski_index = 0
            togri_javob_matni = data['variantlar'][eski_index]
            random.shuffle(data['variantlar'])
            data['togri_index'] = data['variantlar'].index(togri_javob_matni)

        quiz_id = str(uuid.uuid4())[:8]
        
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO quizzes (quiz_id, vaqt, daraja, savollar) VALUES ($1, $2, $3, $4)", quiz_id, vaqt, daraja, json.dumps(savollar))

        bot_info = await bot.get_me()
        test_link = f"https://t.me/{bot_info.username}?start={quiz_id}"
        share_link = f"https://t.me/share/url?url={test_link}&text=Yangi test tuzildi!"
        
        inline_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Testni Yechish", url=test_link), InlineKeyboardButton(text="🔗 Ulashish", url=share_link)],
            [InlineKeyboardButton(text="📥 PDF Hujjat Yuklash", callback_data=f"pdf_{quiz_id}")]
        ])
        
        await message.answer("✅ **Test muvaffaqiyatli tayyorlandi!**", reply_markup=inline_kb, parse_mode="Markdown")
        await state.clear()
    except Exception as e:
        await notify_admin("AI generatsiya / Saqlash", str(e))
        await wait_msg.delete()
        await message.answer("⚠️ Test tuzishda texnik xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.", reply_markup=asosiy_menyu)
        await state.clear()

@dp.callback_query(F.data.startswith("pdf_"))
async def send_pdf(callback: types.CallbackQuery):
    try:
        quiz_id = callback.data.split("_")[1]
        
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT savollar FROM quizzes WHERE quiz_id = $1", quiz_id)

        if not row:
            await callback.answer("⚠️ Topilmadi.", show_alert=True)
            return
            
        await callback.answer("🔄 PDF tayyorlanmoqda...", show_alert=False)
        wait_msg = await bot.send_message(callback.message.chat.id, "🔄 Hujjat shakllantirilmoqda. Iltimos, bir oz kuting...")
        
        savollar = json.loads(row['savollar']) if isinstance(row['savollar'], str) else row['savollar']
        file_name = f"/tmp/test_{quiz_id}.pdf"
        
        bot_info = await bot.get_me()
        bot_uname = bot_info.username

        await asyncio.to_thread(create_pdf_sync, quiz_id, savollar, file_name, bot_uname)
        
        pdf_file = FSInputFile(file_name)
        await bot.send_document(callback.message.chat.id, pdf_file, caption="📥 Marhamat, testning PDF varianti.")
        
        os.remove(file_name)
        await wait_msg.delete()

    except Exception as e:
        await notify_admin("PDF yaratish", str(e))
        await bot.send_message(callback.message.chat.id, f"⚠️ PDF yaratishda xatolik yuz berdi.")

async def main():
    keep_alive()
    await init_db_pool()
    print("🚀 BOT ISHGA TUSHMQODA: Barcha tizimlar to'liq va mukammal ishlab turibdi!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
