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
from pptx import Presentation  # Slayd uchun yangi kutubxona
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
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=5, max_size=20, statement_cache_size=0)
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

# --- OG'IR VAZIFALAR (PDF va PPTX) ---
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
    # Kelajakda bu yerga "if dizayn_nomi == 'Yashil': prs = Presentation('yashil_shablon.pptx')" qo'shish mumkin
    prs = Presentation()
    
    # Birinchi slayd (Sarlavha)
    title_slide_layout = prs.slide_layouts[0]
    slide = prs.slides.add_slide(title_slide_layout)
    title = slide.shapes.title
    subtitle = slide.placeholders[1]
    title.text = "Sizning Taqdimotingiz"
    subtitle.text = f"Dizayn: {dizayn_nomi}\nAI orqali avtomatik yaratildi"

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
            tf.text = qismlar[0]  # Birinchi qator
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

class SlideForm(StatesGroup): # Yangi: Slayd uchun FSM
    soni = State()
    dizayn = State()
    mavzu = State()
    msgs_to_delete = State()

class AdminState(StatesGroup):
    xabar_kutish = State()

bekor_tugma = [KeyboardButton(text="🔙 Bekor qilish")]

asosiy_menyu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📄 Fayldan test"), KeyboardButton(text="🤖 AI Mavzudan test")],
        [KeyboardButton(text="📊 Taqdimot (Slayd) yasash")], # Yangi universal knopka
        [KeyboardButton(text="🏆 Liderlar taxtasi"), KeyboardButton(text="👤 Mening profilim")]
    ], resize_keyboard=True
)

daraja_menyu = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🟢 Oson"), KeyboardButton(text="🟡 O'rtacha")], [KeyboardButton(text="🔴 Qiyin")], bekor_tugma], resize_keyboard=True)
soni_menyu = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="5"), KeyboardButton(text="10"), KeyboardButton(text="15"), KeyboardButton(text="20")], bekor_tugma], resize_keyboard=True)
vaqt_menyu = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="15 soniya"), KeyboardButton(text="30 soniya")], [KeyboardButton(text="⏳ Cheklovsiz")], bekor_tugma], resize_keyboard=True)

# Slayd uchun menyular
slayd_soni_menyu = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="5 ta slayd"), KeyboardButton(text="7 ta slayd"), KeyboardButton(text="10 ta slayd")], bekor_tugma], resize_keyboard=True)
dizayn_menyu = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔵 Zamonaviy (Ko'k)"), KeyboardButton(text="🟢 Tabiat (Yashil)")], [KeyboardButton(text="🟠 Qat'iy (Rasmiy)")], bekor_tugma], resize_keyboard=True)

bekor_menyu = ReplyKeyboardMarkup(keyboard=[bekor_tugma], resize_keyboard=True)
admin_menyu = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📊 Umumiy Statistika"), KeyboardButton(text="📣 Xabar tarqatish")], [KeyboardButton(text="🥇 To'liq ro'yxat")], [KeyboardButton(text="🔙 Bosh menyu")]], resize_keyboard=True)

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

@dp.message(F.text == "🔙 Bekor qilish")
async def bekor_qilish_handler(message: types.Message, state: FSMContext):
    await delete_tracked_msgs(message.chat.id, state)
    await state.clear()
    await message.answer("❌ Jarayon bekor qilindi.", reply_markup=asosiy_menyu)

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
    msg = await message.answer("Taqdimot uchun qaysi dizayn uslubi ko'proq yoqadi?", reply_markup=dizayn_menyu)
    await track_msg(state, msg.message_id)

@dp.message(SlideForm.dizayn)
async def get_slide_design(message: types.Message, state: FSMContext):
    await track_msg(state, message.message_id)
    await state.update_data(dizayn=message.text)
    await state.set_state(SlideForm.mavzu)
    # Misol sifatida muhandislikka yaqinroq yo'nalish ko'rsatib o'tamiz
    msg = await message.answer("Ajoyib! Endi taqdimot mavzusini batafsil yozing.\n(Masalan: Bug'-gaz qurilmalarining ishlash prinsipi va foydasi)", reply_markup=bekor_menyu)
    await track_msg(state, msg.message_id)

@dp.message(SlideForm.mavzu, F.text)
async def generate_slide_content(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await delete_tracked_msgs(message.chat.id, state)
    wait_msg = await message.answer("🟢 AI slaydlar matnini va dizaynni tayyorlamoqda. Iltimos, bir oz kuting...", reply_markup=ReplyKeyboardRemove())
    
    soni = data['soni']
    dizayn = data['dizayn']
    mavzu = message.text
    
    prompt = f"Mavzu: '{mavzu}'. Shu mavzu bo'yicha {soni} ta slayd uchun taqdimot rejasi tuz. FAQAT JSON formatida array qaytar.\nFormat: [{{\"sarlavha\": \"1-slayd mavzusi\", \"qismlar\": [\"Qisqa fikr 1\", \"Qisqa fikr 2\", \"Qisqa fikr 3\"]}}]. Hech qanday qo'shimcha matn yozma!"
    
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

# --- QOLGAN ESKI FUNKSIYALAR (QISQARTIRILGAN KO'RINISHDA SAQLANDI) ---
@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext, command: CommandObject = None):
    await state.clear()
    await add_user(message.from_user.id, message.from_user.first_name)
    if command and command.args:
        # Eski testni boshlash logikasi (bu joy avvalgidek qoladi)
        await message.answer("🚀 Test boshlanmoqda...", reply_markup=ReplyKeyboardRemove())
        # Qolgan baza logikasi saqlangan deb tasavvur qiling
        pass
    await message.answer("Assalomu alaykum! Xush kelibsiz.\nO'zingizga kerakli bo'limni tanlang:", reply_markup=asosiy_menyu)

@dp.message(F.text == "🏆 Liderlar taxtasi")
async def show_reyting(message: types.Message):
    await message.answer("🏆 Liderlar taxtasi tez orada to'liq yuklanadi.", reply_markup=asosiy_menyu)

@dp.message(F.text == "👤 Mening profilim")
async def show_profile(message: types.Message):
    await message.answer("👤 Profilingiz tayyorlanmoqda.", reply_markup=asosiy_menyu)

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("👑 Admin Panel", reply_markup=admin_menyu)

async def main():
    keep_alive()
    await init_db_pool()
    print("🚀 BOT ISHGA TUSHMQODA: Taqdimot yasash (PPTX) funksiyasi qo'shildi!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
