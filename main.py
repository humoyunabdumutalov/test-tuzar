import asyncio
import os
import io
import json
import uuid
import random
from contextlib import suppress

# --- QO'SHIMCHA KUTUBXONALAR --
from dotenv import load_dotenv
# Seyfni (.env) ochamiz, bu qator eng birinchi ishlashi shart
load_dotenv() 

import asyncpg
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
import PyPDF2
from docx import Document
from PIL import Image
from keep_alive import keep_alive

# --- SOZLAMALAR ---
# Parollar endi xavfsiz holatda .env faylidan olinadi
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# DIQQAT: O'zingizning Telegram ID raqamingizni shu yerga yozing!
ADMIN_ID = 5031441892  

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Gemini sun'iy intellektini sozlash
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash-lite')

# --- DATA ENGINE (Ma'lumotlar bazasi) ---
db_pool = None

async def init_db_pool():
    """Ma'lumotlar bazasini ishga tushirish va jadvallarni yaratish"""
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=5, max_size=20, statement_cache_size=0)
    
    async with db_pool.acquire() as conn:
        # 1. Foydalanuvchilar jadvali
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY, 
                name TEXT, 
                score INTEGER DEFAULT 0, 
                tests_taken INTEGER DEFAULT 0
            )
        ''')
        
        # 2. Testlar jadvali
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS quizzes (
                quiz_id TEXT PRIMARY KEY, 
                source_type TEXT, 
                savollar TEXT
            )
        ''')
        
        # 3. Eski bazaga yangi ustunlarni xavfsiz qo'shish
        columns_to_add = ["image_tests_made", "file_tests_made", "topic_tests_made"]
        for col in columns_to_add:
            try:
                await conn.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0")
            except Exception:
                pass # Ustun allaqachon mavjud bo'lsa, xato bermaydi

async def add_user(user_id, name):
    """Yangi foydalanuvchini bazaga qo'shish"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, name) VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING", 
            str(user_id), name
        )

# Vaqtinchalik xotira
POLL_DATA = {}   
SESSION_SCORES = {} 

# --- YORDAMCHI FUNKSIYALAR ---
def clean_json_text(text):
    """Sun'iy intellekt javobidan faqat toza JSON matnini qirqib olish"""
    if "```json" in text:
        text = text.split("```json")[1]
    if "```" in text:
        text = text.split("```")[0]
    return text.strip()

def read_file_sync(file_data, filename):
    """PDF yoki Word fayllardan matnni o'qib olish"""
    text = ""
    try:
        if filename.endswith('.pdf'):
            pdf_reader = PyPDF2.PdfReader(file_data)
            for page in pdf_reader.pages:
                text += page.extract_text() or ""
        elif filename.endswith('.docx'):
            doc = Document(file_data)
            for paragraph in doc.paragraphs:
                text += paragraph.text + "\n"
    except Exception as e:
        print(f"Faylni o'qishda xatolik: {e}")
    return text

# --- FSM (Holatlar) VA MENYULAR ---
class QuickQuizForm(StatesGroup):
    source_type = State() # Rasm, fayl yoki mavzu ekanligi
    soni = State()        # Savollar soni
    payload = State()     # Rasm ID, Fayl ID yoki Matn

class AdminState(StatesGroup):
    xabar_kutish = State()

# Menyular
bekor_tugma = [KeyboardButton(text="🔙 Bekor qilish")]

asosiy_menyu = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📸 Rasmdan test"), KeyboardButton(text="📚 Matn/Mavzudan test")],
    [KeyboardButton(text="📊 Mening natijalarim"), KeyboardButton(text="🏆 Reyting")]
], resize_keyboard=True)

soni_menyu = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="5"), KeyboardButton(text="10")], 
    [KeyboardButton(text="15"), KeyboardButton(text="20")], 
    bekor_tugma
], resize_keyboard=True)

admin_menyu = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📊 Umumiy Statistika"), KeyboardButton(text="📣 Xabar tarqatish")],
    [KeyboardButton(text="🔙 Bosh menyu")]
], resize_keyboard=True)


# --- ADMIN PANEL BUYRUQLARI ---
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    """Admin panelni ochish"""
    if message.from_user.id != int(ADMIN_ID): 
        return
    await message.answer("👑 **Admin Panelga Xush Kelibsiz!**", reply_markup=admin_menyu, parse_mode="Markdown")

@dp.message(F.text == "📊 Umumiy Statistika")
async def show_stats_admin(message: types.Message):
    """Tizimning umumiy statistikasini ko'rsatish"""
    if message.from_user.id != int(ADMIN_ID): 
        return
        
    async with db_pool.acquire() as conn:
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
        quizzes_count = await conn.fetchval("SELECT COUNT(*) FROM quizzes") or 0
        image_tests = await conn.fetchval("SELECT SUM(image_tests_made) FROM users") or 0
        file_tests = await conn.fetchval("SELECT SUM(file_tests_made) FROM users") or 0
        topic_tests = await conn.fetchval("SELECT SUM(topic_tests_made) FROM users") or 0
    
    text = (
        f"📊 **STARTAP STATISTIKASI:**\n\n"
        f"👥 Jami a'zolar: {users_count} ta\n"
        f"📝 Yaratilgan testlar: {quizzes_count} ta\n\n"
        f"📈 **Testlar tahlili:**\n"
        f"📸 Rasmdan yasalgan: {image_tests} marta\n"
        f"📄 Fayldan yasalgan: {file_tests} marta\n"
        f"🧠 Mavzudan yasalgan: {topic_tests} marta"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "📣 Xabar tarqatish")
async def ask_broadcast(message: types.Message, state: FSMContext):
    """Xabar tarqatish uchun matn so'rash"""
    if message.from_user.id != int(ADMIN_ID): 
        return
    await message.answer(
        "Tizimdagi barcha foydalanuvchilarga yuboriladigan xabarni kiriting:", 
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔙 Bosh menyu")]], resize_keyboard=True)
    )
    await state.set_state(AdminState.xabar_kutish)

@dp.message(AdminState.xabar_kutish)
async def send_broadcast(message: types.Message, state: FSMContext):
    """Xabarni barchaga tarqatish"""
    if message.text == "🔙 Bosh menyu":
        await state.clear()
        await message.answer("Xabar tarqatish bekor qilindi.", reply_markup=asosiy_menyu)
        return
        
    async with db_pool.acquire() as conn: 
        users = await conn.fetch("SELECT user_id FROM users")
        
    await message.answer("⏳ Xabar tarqatilmoqda, jarayon biroz vaqt olishi mumkin...")
    
    success = 0
    for user in users:
        try:
            await bot.copy_message(
                chat_id=int(user['user_id']), 
                from_chat_id=message.chat.id, 
                message_id=message.message_id
            )
            success += 1
            await asyncio.sleep(0.05) # Telegram limitlariga tushmaslik uchun
        except Exception: 
            pass
            
    await message.answer(f"✅ Xabar muvaffaqiyatli yetib bordi: {success} ta foydalanuvchiga", reply_markup=asosiy_menyu)
    await state.clear()


# --- ASOSIY BUYRUQLAR VA MENYULAR ---
@dp.message(F.text == "🔙 Bosh menyu")
async def back_to_main_from_admin(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Asosiy menyuga qaytdingiz.", reply_markup=asosiy_menyu)

@dp.message(F.text == "🔙 Bekor qilish")
async def bekor_qilish(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Jarayon bekor qilindi.", reply_markup=asosiy_menyu)

@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext, command: CommandObject = None):
    await state.clear()
    await add_user(message.from_user.id, message.from_user.first_name)

    # Agar foydalanuvchi test havolasi orqali kirgan bo'lsa
    if command and command.args:
        quiz_id = command.args
        async with db_pool.acquire() as conn:
            quiz_row = await conn.fetchrow("SELECT savollar FROM quizzes WHERE quiz_id = $1", quiz_id)
            
            if quiz_row:
                # Testlar sonini oshirish
                await conn.execute("UPDATE users SET tests_taken = tests_taken + 1 WHERE user_id = $1", str(message.from_user.id))
                await message.answer("🚀 Test boshlanmoqda...", reply_markup=ReplyKeyboardRemove())
                
                savollar = json.loads(quiz_row['savollar'])
                user_id = message.from_user.id
                SESSION_SCORES[user_id] = 0 
                
                # Savollarni bittalab yuborish
                for data in savollar:
                    q = data['savol'][:250]
                    opts = [str(opt)[:100] for opt in data['variantlar']][:4]
                    correct = int(data.get('togri_index', 0))
                    
                    sent_poll = await bot.send_poll(
                        chat_id=message.chat.id, 
                        question=q, 
                        options=opts, 
                        type='quiz', 
                        correct_option_id=correct, 
                        is_anonymous=False
                    )
                    # To'g'ri javobni xotirada saqlash
                    POLL_DATA[sent_poll.poll.id] = {"correct": correct, "points": 2}
                    await asyncio.sleep(0.5)
                    
                await message.answer("🏁 **Barcha savollar yuborildi!**\nJavoblarni xotirjam yechib chiqing.", reply_markup=asosiy_menyu, parse_mode="Markdown")
                return
            else:
                await message.answer("⚠️ Bu test eskirgan yoki tizimdan o'chirilgan.", reply_markup=asosiy_menyu)
                return
                
    # Oddiy start bosilganda
    welcome_text = (
        "Assalomu alaykum! EdTech platformamizga xush kelibsiz.\n\n"
        "📸 Shunchaki lug'at daftaringizni rasmga oling yoki konspekt PDF faylini menga tashlang. "
        "Men undan darhol test yasab beraman!"
    )
    await message.answer(welcome_text, reply_markup=asosiy_menyu)

@dp.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer):
    """Test javoblarini tekshirish va ball berish"""
    poll_id = poll_answer.poll_id
    user_id_int = poll_answer.user.id
    tanlangan_javob = poll_answer.option_ids[0] if poll_answer.option_ids else -1
    
    if poll_id in POLL_DATA and tanlangan_javob == POLL_DATA[poll_id]["correct"]:
        ball = POLL_DATA[poll_id]["points"]
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET score = score + $1 WHERE user_id = $2", ball, str(user_id_int))

@dp.message(F.text == "📊 Mening natijalarim")
async def show_profile(message: types.Message):
    """Shaxsiy statistikani ko'rsatish"""
    async with db_pool.acquire() as conn:
        user_data = await conn.fetchrow("SELECT score, tests_taken, image_tests_made, file_tests_made FROM users WHERE user_id = $1", str(message.from_user.id))
        
    if user_data: 
        text = (
            f"📊 **Shaxsiy Statistika:**\n\n"
            f"🎯 Yig'ilgan ball: {user_data['score']}\n"
            f"✅ Yechilgan testlar: {user_data['tests_taken']}\n\n"
            f"🛠 **Siz yaratgan testlar:**\n"
            f"📸 Rasmdan: {user_data['image_tests_made']} ta\n"
            f"📄 Fayldan: {user_data['file_tests_made']} ta"
        )
        await message.answer(text, parse_mode="Markdown")
    else: 
        await message.answer("Siz hali bazada ro'yxatdan o'tmadingiz. /start ni bosing.")

@dp.message(F.text == "🏆 Reyting")
async def show_reyting(message: types.Message):
    """Top foydalanuvchilarni ko'rsatish"""
    async with db_pool.acquire() as conn:
        top_users = await conn.fetch("SELECT name, score FROM users ORDER BY score DESC LIMIT 10")
        
    text = "🏆 **TOP-10 QAHRAMONLAR:**\n\n"
    for i, user in enumerate(top_users, 1): 
        text += f"{i}. {user['name']} — {user['score']} ball\n"
        
    await message.answer(text, parse_mode="Markdown")


# --- 1-CLICK TIZIMI (TEST YARATISH) ---

@dp.message(F.text == "📸 Rasmdan test")
async def ask_photo(message: types.Message): 
    await message.answer("📸 Lug'at daftaringizni aniq rasmga olib yuboring.")

@dp.message(F.text == "📚 Matn/Mavzudan test")
async def ask_topic(message: types.Message): 
    await message.answer("📄 PDF/Word fayl tashlang YOKI biror mavzuni yozing.")

@dp.message(F.photo)
async def auto_photo_handler(message: types.Message, state: FSMContext):
    """Rasm yuborilganda avtomatik ishga tushadi"""
    await state.update_data(source_type='image', payload=message.photo[-1].file_id)
    await state.set_state(QuickQuizForm.soni)
    await message.answer("📸 Rasm qabul qilindi! Nechta savoldan iborat test tuzamiz?", reply_markup=soni_menyu)

@dp.message(F.document)
async def auto_doc_handler(message: types.Message, state: FSMContext):
    """Hujjat yuborilganda avtomatik ishga tushadi"""
    filename = message.document.file_name.lower()
    if not (filename.endswith('.pdf') or filename.endswith('.docx')): 
        return await message.answer("⚠️ Kechirasiz, faqat PDF yoki Word (.docx) fayllarni qabul qilaman.")
        
    await state.update_data(source_type='file', payload=message.document.file_id, filename=message.document.file_name)
    await state.set_state(QuickQuizForm.soni)
    await message.answer("📄 Fayl qabul qilindi! Nechta savol tuzamiz?", reply_markup=soni_menyu)

# Boshqa menyu buyruqlari bo'lmagan har qanday oddiy matnni mavzu deb qabul qiladi
@dp.message(F.text, ~F.text.in_(["📸 Rasmdan test", "📚 Matn/Mavzudan test", "📊 Mening natijalarim", "🏆 Reyting", "🔙 Bekor qilish", "/start", "/admin"]))
async def auto_topic_handler(message: types.Message, state: FSMContext):
    """Oddiy matn yuborilganda avtomatik ishga tushadi"""
    await state.update_data(source_type='topic', payload=message.text)
    await state.set_state(QuickQuizForm.soni)
    await message.answer("🧠 Mavzu qabul qilindi! Nechta savol tuzamiz?", reply_markup=soni_menyu)

@dp.message(QuickQuizForm.soni)
async def generate_magic(message: types.Message, state: FSMContext):
    """Sun'iy intellekt orqali testni generatsiya qilish qismi"""
    if not message.text.isdigit(): 
        return
        
    soni = int(message.text)
    data = await state.get_data()
    source = data['source_type']
    
    wait_msg = await message.answer("⚙️ Sun'iy intellekt ma'lumotlarni tahlil qilmoqda... Iltimos kuting.", reply_markup=ReplyKeyboardRemove())
    
    try:
        # 1. Rasm orqali
        if source == 'image':
            file_info = await bot.get_file(data['payload'])
            file_data = io.BytesIO()
            await bot.download_file(file_info.file_path, destination=file_data)
            file_data.seek(0)
            img = Image.open(file_data)
            
            prompt = (
                f"Rasmdagi chet tili so'zlarini o'qib, ularning tarjimasi bo'yicha {soni} ta test tuz. "
                f"FAQAT QAT'IY JSON ARRAY formatida qaytar. Hech qanday qo'shimcha gap qo'shma. "
                f"Namuna: [{{\"savol\": \"Apple so'zining tarjimasi?\", \"variantlar\": [\"Olma\", \"Nok\", \"Uzum\", \"Anor\"], \"togri_index\": 0}}]"
            )
            response = await asyncio.to_thread(model.generate_content, [prompt, img])
            
        # 2. Fayl orqali
        elif source == 'file':
            file_info = await bot.get_file(data['payload'])
            file_data = io.BytesIO()
            await bot.download_file(file_info.file_path, destination=file_data)
            file_data.seek(0)
            
            text = await asyncio.to_thread(read_file_sync, file_data, data['filename'])
            if len(text.strip()) < 10:
                raise ValueError("Fayl ichidan matn topilmadi")
                
            prompt = (
                f"Quyidagi matn asosida {soni} ta test tuz. "
                f"FAQAT QAT'IY JSON ARRAY formatida qaytar. "
                f"Namuna: [{{\"savol\": \"...\", \"variantlar\": [\"1\", \"2\", \"3\", \"4\"], \"togri_index\": 0}}]\n\n"
                f"Matn: {text[:15000]}" # Gemini 15000 belgini bemalol o'qiydi
            )
            response = await asyncio.to_thread(model.generate_content, prompt)
            
        # 3. Mavzu orqali
        elif source == 'topic':
            prompt = (
                f"'{data['payload']}' mavzusida {soni} ta qiziqarli test tuz. "
                f"FAQAT QAT'IY JSON ARRAY formatida qaytar. "
                f"Namuna: [{{\"savol\": \"...\", \"variantlar\": [\"1\", \"2\", \"3\", \"4\"], \"togri_index\": 0}}]"
            )
            response = await asyncio.to_thread(model.generate_content, prompt)

        # JSON ni tozalash va o'qish
        json_matn = clean_json_text(response.text)
        savollar = json.loads(json_matn)
        
        # Javoblarni chalkashtirish (Har doim ham 'A' to'g'ri bo'lib qolmasligi uchun)
        for q in savollar:
            eski_index = int(q.get('togri_index', 0))
            if eski_index < 0 or eski_index > 3: 
                eski_index = 0
                
            togri_matn = q['variantlar'][eski_index]
            random.shuffle(q['variantlar'])
            q['togri_index'] = q['variantlar'].index(togri_matn)

        # Bazaga saqlash
        quiz_id = str(uuid.uuid4())[:8]
        async with db_pool.acquire() as conn:
            # Testni saqlash
            await conn.execute("INSERT INTO quizzes (quiz_id, source_type, savollar) VALUES ($1, $2, $3)", quiz_id, source, json.dumps(savollar))
            
            # Statistikani yangilash
            if source == 'image': 
                await conn.execute("UPDATE users SET image_tests_made = image_tests_made + 1 WHERE user_id = $1", str(message.from_user.id))
            elif source == 'file': 
                await conn.execute("UPDATE users SET file_tests_made = file_tests_made + 1 WHERE user_id = $1", str(message.from_user.id))
            elif source == 'topic': 
                await conn.execute("UPDATE users SET topic_tests_made = topic_tests_made + 1 WHERE user_id = $1", str(message.from_user.id))

        # Yakuniy javob yuborish
        bot_info = await bot.get_me()
        test_link = f"https://t.me/{bot_info.username}?start={quiz_id}"
        share_link = f"https://t.me/share/url?url={test_link}&text=Ajoyib test yaratildi! Bilimingizni sinab ko'ring."
        
        inline_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Testni boshlash", url=test_link)],
            [InlineKeyboardButton(text="🔗 Do'stlarga yuborish", url=share_link)]
        ])
        
        await wait_msg.delete()
        await message.answer(
            f"✅ **Testingiz muvaffaqiyatli tayyorlandi! ({len(savollar)} ta savol)**\n\n"
            f"Pastdagi tugma orqali yechishni boshlang yoki do'stlaringiz bilan ulashing.", 
            reply_markup=inline_kb, 
            parse_mode="Markdown"
        )
        await message.answer("Asosiy menyuga qaytdingiz.", reply_markup=asosiy_menyu)
        await state.clear()

    except Exception as e:
        with suppress(Exception): 
            await wait_msg.delete()
            
        print(f"XATOLIK YUZ BERDI: {e}") # Buni VS Code terminalida ko'rasiz
        
        # Foydalanuvchiga insoniy javob
        if "JSON" in str(e) or "loads" in str(e):
            xato_matni = "⚠️ Sun'iy intellekt testlarni formatlashda adashib ketdi. Iltimos, boshqattan urinib ko'ring."
        elif source == 'image':
            xato_matni = "👀 Kechirasiz, rasmdagi yozuvlarni aniq o'qiy olmadim. Yorug'roq joyda rasmga olib qayta yuboring."
        elif source == 'file':
            xato_matni = "📂 Fayldan matn o'qib bo'lmadi. U rasm shaklida bo'lishi mumkin."
        else:
            xato_matni = "❌ Kutilmagan texnik xatolik yuz berdi. Birozdan so'ng qayta urinib ko'ring."
            
        await message.answer(xato_matni, reply_markup=asosiy_menyu)
        await state.clear()

async def main():
    # Serverni uyg'oq tutuvchi tizim (Render.com uchun)
    keep_alive()
    
    # Bazani ishga tushirish
    await init_db_pool()
    
    print("========================================")
    print("🚀 UPPERLAR MVP BOTI ISHGA TUSHDI!")
    print("========================================")
    
    # Botni ishga tushirish
    await dp.start_polling(bot)

if __name__ == "__main__": 
    asyncio.run(main())
