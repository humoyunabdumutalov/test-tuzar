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
from PIL import Image
from keep_alive import keep_alive

# --- SOZLAMALAR ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_ID = 5031441892  # <--- O'zingizning ID raqamingizni yozing

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash-lite')

# --- DATA ENGINE (Ma'lumotlar bazasi) ---
db_pool = None

async def init_db_pool():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=5, max_size=20, statement_cache_size=0)
    async with db_pool.acquire() as conn:
        # Foydalanuvchi ma'lumotlari va faollik tahlili
        await conn.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY, 
            name TEXT, 
            score INTEGER DEFAULT 0, 
            tests_taken INTEGER DEFAULT 0,
            image_tests_made INTEGER DEFAULT 0,
            file_tests_made INTEGER DEFAULT 0,
            topic_tests_made INTEGER DEFAULT 0
        )''')
        # Testlar haqida ma'lumot
        await conn.execute('''CREATE TABLE IF NOT EXISTS quizzes (
            quiz_id TEXT PRIMARY KEY, 
            source_type TEXT, 
            savollar TEXT
        )''')

async def add_user(user_id, name):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id, name) VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING", str(user_id), name)

POLL_DATA = {}   
SESSION_SCORES = {} 

# --- YORDAMCHI FUNKSIYALAR ---
def clean_json_text(text):
    """AI qaytargan matndan faqat sof JSONni ajratib olish"""
    if "```json" in text:
        text = text.split("```json")[1]
    if "```" in text:
        text = text.split("```")[0]
    return text.strip()

def read_file_sync(file_data, filename):
    text = ""
    try:
        if filename.endswith('.pdf'):
            text = "".join([p.extract_text() or "" for p in PyPDF2.PdfReader(file_data).pages])
        elif filename.endswith('.docx'):
            text = "\n".join([p.text for p in Document(file_data).paragraphs])
    except Exception as e: print(f"Fayl xatosi: {e}")
    return text

# --- FSM VA MENYULAR ---
class QuickQuizForm(StatesGroup):
    source_type = State(); soni = State(); payload = State()

bekor_tugma = [KeyboardButton(text="🔙 Bekor qilish")]
asosiy_menyu = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📸 Rasmdan test"), KeyboardButton(text="📚 Matn/Mavzudan test")],
    [KeyboardButton(text="📊 Mening natijalarim"), KeyboardButton(text="🏆 Reyting")]
], resize_keyboard=True)

soni_menyu = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="10"), KeyboardButton(text="15")], 
    [KeyboardButton(text="20"), KeyboardButton(text="30")], 
    bekor_tugma
], resize_keyboard=True)

# --- ASOSIY BUYRUQLAR (Start va Test yechish) ---
@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext, command: CommandObject = None):
    await state.clear()
    await add_user(message.from_user.id, message.from_user.first_name)

    # Agar test ssilkasi orqali kirgan bo'lsa
    if command and command.args:
        quiz_id = command.args
        async with db_pool.acquire() as conn:
            quiz_row = await conn.fetchrow("SELECT savollar FROM quizzes WHERE quiz_id = $1", quiz_id)
            if quiz_row:
                await conn.execute("UPDATE users SET tests_taken = tests_taken + 1 WHERE user_id = $1", str(message.from_user.id))
                await message.answer("🚀 Test boshlanmoqda...", reply_markup=ReplyKeyboardRemove())
                savollar = json.loads(quiz_row['savollar'])
                user_id = message.from_user.id
                SESSION_SCORES[user_id] = 0 
                jami = len(savollar)

                for data in savollar:
                    q = data['savol'][:250]
                    opts = [str(opt)[:100] for opt in data['variantlar']][:4]
                    correct = int(data.get('togri_index', 0))
                    sent_poll = await bot.send_poll(chat_id=message.chat.id, question=q, options=opts, type='quiz', correct_option_id=correct, is_anonymous=False)
                    POLL_DATA[sent_poll.poll.id] = {"correct": correct, "points": 2}
                    await asyncio.sleep(0.5)

                await message.answer("🏁 **Barcha savollar yuborildi!**\nJavoblarni xotirjam yechib chiqing.", reply_markup=asosiy_menyu, parse_mode="Markdown")
                return
            else:
                await message.answer("⚠️ Bu test eskirgan yoki o'chib ketgan.")
                return
    await message.answer("Assalomu alaykum! EdTech platformamizga xush kelibsiz.\n\n💡 **Qanday ishlatiladi?**\nShunchaki lug'at daftaringizni rasmga oling yoki konspekt PDF faylini menga tashlang. Men undan darhol test yasab beraman!", reply_markup=asosiy_menyu)

@dp.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer):
    poll_id = poll_answer.poll_id
    user_id_int = poll_answer.user.id
    tanlangan_javob = poll_answer.option_ids[0] if poll_answer.option_ids else -1
    if poll_id in POLL_DATA and tanlangan_javob == POLL_DATA[poll_id]["correct"]:
        ball = POLL_DATA[poll_id]["points"]
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET score = score + $1 WHERE user_id = $2", ball, str(user_id_int))

@dp.message(F.text == "🔙 Bekor qilish")
async def bekor_qilish(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Jarayon bekor qilindi.", reply_markup=asosiy_menyu)

# --- STATISTIKA VA REYTING (Gamifikatsiya) ---
@dp.message(F.text == "📊 Mening natijalarim")
async def show_profile(message: types.Message):
    async with db_pool.acquire() as conn:
        u = await conn.fetchrow("SELECT score, tests_taken, image_tests_made, file_tests_made FROM users WHERE user_id = $1", str(message.from_user.id))
    if u: 
        text = f"📊 **Shaxsiy Statistika:**\n\n🎯 Yig'ilgan ball: {u['score']}\n✅ Yechilgan testlar: {u['tests_taken']}\n\n"
        text += f"🛠 **Siz yaratgan testlar:**\n📸 Rasmdan: {u['image_tests_made']} ta\n📄 Fayldan: {u['file_tests_made']} ta"
        await message.answer(text, parse_mode="Markdown")
    else: 
        await message.answer("Siz hali bazada yo'qsiz.")

@dp.message(F.text == "🏆 Reyting")
async def show_reyting(message: types.Message):
    async with db_pool.acquire() as conn:
        top_users = await conn.fetch("SELECT name, score FROM users ORDER BY score DESC LIMIT 10")
    text = "🏆 **TOP-10 QAHRAMONLAR:**\n\n"
    for i, u in enumerate(top_users, 1):
        text += f"{i}. {u['name']} — {u['score']} ball\n"
    await message.answer(text, parse_mode="Markdown")

# --- 1-CLICK TIZIMI (RASM, FAYL YOKI MAVZU) ---
@dp.message(F.text == "📸 Rasmdan test")
async def ask_photo(message: types.Message):
    await message.answer("📸 Lug'at daftaringiz yoki kitobdagi so'zlar ro'yxatini aniq rasmga olib yuboring.")

@dp.message(F.text == "📚 Matn/Mavzudan test")
async def ask_topic(message: types.Message):
    await message.answer("Menga ixtiyoriy PDF/Word fayl tashlang YOKI biror mavzuni yozib yuboring (Masalan: 'Muqobil energiya manbalari').")

@dp.message(F.photo)
async def auto_photo_handler(message: types.Message, state: FSMContext):
    await state.update_data(source_type='image', payload=message.photo[-1].file_id)
    await state.set_state(QuickQuizForm.soni)
    await message.answer("📸 Rasm qabul qilindi!\nNechta savoldan iborat test tuzamiz?", reply_markup=soni_menyu)

@dp.message(F.document)
async def auto_doc_handler(message: types.Message, state: FSMContext):
    if not (message.document.file_name.endswith('.pdf') or message.document.file_name.endswith('.docx')):
        await message.answer("⚠️ Kechirasiz, men hozircha faqat PDF va Word (.docx) fayllarni o'qiy olaman.")
        return
    await state.update_data(source_type='file', payload=message.document.file_id, filename=message.document.file_name)
    await state.set_state(QuickQuizForm.soni)
    await message.answer("📄 Fayl qabul qilindi!\nNechta savol tuzamiz?", reply_markup=soni_menyu)

@dp.message(F.text, ~F.text.in_(["📸 Rasmdan test", "📚 Matn/Mavzudan test", "📊 Mening natijalarim", "🏆 Reyting", "🔙 Bekor qilish", "/start", "/admin"]))
async def auto_topic_handler(message: types.Message, state: FSMContext):
    # Oddiy matn yozsa, avtomatik mavzu sifatida qabul qiladi
    await state.update_data(source_type='topic', payload=message.text)
    await state.set_state(QuickQuizForm.soni)
    await message.answer("🧠 Mavzu qabul qilindi!\nNechta savol tuzamiz?", reply_markup=soni_menyu)

@dp.message(QuickQuizForm.soni)
async def generate_magic(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return
    soni = int(message.text)
    data = await state.get_data()
    source = data['source_type']
    
    wait_msg = await message.answer("⚙️ Sun'iy intellekt ma'lumotlarni tahlil qilmoqda... Iltimos kuting.", reply_markup=ReplyKeyboardRemove())
    
    try:
        if source == 'image':
            file_info = await bot.get_file(data['payload'])
            file_data = io.BytesIO()
            await bot.download_file(file_info.file_path, destination=file_data)
            file_data.seek(0)
            img = Image.open(file_data)
            prompt = f"Rasmdagi so'zlarni o'qib, {soni} ta test tuz. FAQAT QAT'IY JSON ARRAY ber. Namuna: [{{\"savol\": \"Apple?\", \"variantlar\": [\"Olma\", \"Nok\", \"Uzum\", \"Anor\"], \"togri_index\": 0}}]"
            response = await asyncio.to_thread(model.generate_content, [prompt, img])
            
        elif source == 'file':
            file_info = await bot.get_file(data['payload'])
            file_data = io.BytesIO()
            await bot.download_file(file_info.file_path, destination=file_data)
            file_data.seek(0)
            text = await asyncio.to_thread(read_file_sync, file_data, data['filename'])
            if len(text) < 20: raise ValueError("Fayldan yetarlicha matn o'qib bo'lmadi.")
            prompt = f"Quyidagi matn asosida {soni} ta test tuz. FAQAT QAT'IY JSON ARRAY ber. Namuna: [{{\"savol\": \"...\", \"variantlar\": [\"1\", \"2\", \"3\", \"4\"], \"togri_index\": 0}}]\n\nMatn: {text[:10000]}"
            response = await asyncio.to_thread(model.generate_content, prompt)
            
        elif source == 'topic':
            prompt = f"'{data['payload']}' mavzusida {soni} ta ilmiy test tuz. FAQAT QAT'IY JSON ARRAY ber. Namuna: [{{\"savol\": \"...\", \"variantlar\": [\"1\", \"2\", \"3\", \"4\"], \"togri_index\": 0}}]"
            response = await asyncio.to_thread(model.generate_content, prompt)

        # JSON ni tozalash va o'qish
        json_text = clean_json_text(response.text)
        savollar = json.loads(json_text)
        
        # Testlarni xavfsiz aralashtirish
        for q in savollar:
            eski_index = int(q.get('togri_index', 0))
            if eski_index < 0 or eski_index > 3: eski_index = 0
            togri_matn = q['variantlar'][eski_index]
            random.shuffle(q['variantlar'])
            q['togri_index'] = q['variantlar'].index(togri_matn)

        # Bazaga saqlash va Data Engine ni yangilash
        quiz_id = str(uuid.uuid4())[:8]
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO quizzes (quiz_id, source_type, savollar) VALUES ($1, $2, $3)", quiz_id, source, json.dumps(savollar))
            if source == 'image': await conn.execute("UPDATE users SET image_tests_made = image_tests_made + 1 WHERE user_id = $1", str(message.from_user.id))
            elif source == 'file': await conn.execute("UPDATE users SET file_tests_made = file_tests_made + 1 WHERE user_id = $1", str(message.from_user.id))
            elif source == 'topic': await conn.execute("UPDATE users SET topic_tests_made = topic_tests_made + 1 WHERE user_id = $1", str(message.from_user.id))

        # Yakuniy havola yasash
        bot_info = await bot.get_me()
        test_link = f"[https://t.me/](https://t.me/){bot_info.username}?start={quiz_id}"
        share_link = f"[https://t.me/share/url?url=](https://t.me/share/url?url=){test_link}&text=Yangi qiziqarli test yaratildi!"
        
        await wait_msg.delete()
        await message.answer(f"✅ **Testingiz tayyor! ({len(savollar)} ta savol)**\n\nPastdagi tugma orqali yechishni boshlang yoki do'stlaringizga yuboring.", 
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Testni boshlash", url=test_link), InlineKeyboardButton(text="🔗 Ulashish", url=share_link)]]), 
                             parse_mode="Markdown")
        await message.answer("Asosiy menyuga qaytdingiz.", reply_markup=asosiy_menyu)
        await state.clear()

    except Exception as e:
        await wait_msg.delete()
        # Aqlli (Insoniy) Error Handling
        if "JSON" in str(e) or "loads" in str(e):
            await message.answer("⚠️ Sun'iy intellekt ma'lumotlarni guruhlashda adashib ketdi. Iltimos, boshqattan urinib ko'ring.", reply_markup=asosiy_menyu)
        elif source == 'image':
            await message.answer("👀 Kechirasiz, rasmdagi yozuvlarni aniq o'qiy olmadim. Iltimos, daftaringizni yorug'roq joyda rasmga olib qayta yuboring.", reply_markup=asosiy_menyu)
        elif source == 'file':
            await message.answer("📂 Fayldagi matnlarni o'qishning imkoni bo'lmadi. U rasm shaklida skaner qilingan bo'lishi mumkin.", reply_markup=asosiy_menyu)
        else:
            await message.answer("❌ Kutilmagan texnik xatolik yuz berdi. Birozdan so'ng qayta urinib ko'ring.", reply_markup=asosiy_menyu)
        await state.clear()

async def main():
    keep_alive()
    await init_db_pool()
    print("🚀 MVP BOT ISHGA TUSHDI!")
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
