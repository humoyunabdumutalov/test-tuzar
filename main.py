
import asyncio
import os
import io
import json
import uuid
import random
import textwrap # PDF matnlarini to'g'rilash uchun yangi kutubxona
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
from keep_alive import keep_alive

# --- SOZLAMALAR ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_ID = 5031441892  # DIQQAT: O'zingizning Telegram ID raqamingizni shu yerga yozing!

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash-lite')

# --- ASINXRON MA'LUMOTLAR BAZASI ---
db_pool = None

async def init_db_pool():
    global db_pool
    db_pool = await asyncpg.create_pool(
        DATABASE_URL, 
        min_size=5, 
        max_size=20,
        statement_cache_size=0
    )
    async with db_pool.acquire() as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, name TEXT, score INTEGER, tests_taken INTEGER)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS quizzes (quiz_id TEXT PRIMARY KEY, vaqt INTEGER, daraja TEXT, savollar TEXT)''')

async def add_user(user_id, name):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id, name, score, tests_taken) VALUES ($1, $2, 0, 0) ON CONFLICT (user_id) DO NOTHING", str(user_id), name)

POLL_DATA = {}   
SESSION_SCORES = {} 

# --- ORQA FONDAGI OG'IR VAZIFALAR ---
def read_file_sync(file_data, filename):
    text = ""
    try:
        if filename.endswith('.pdf'):
            text = "".join([p.extract_text() or "" for p in PyPDF2.PdfReader(file_data).pages])
        elif filename.endswith('.docx'):
            text = "\n".join([p.text for p in Document(file_data).paragraphs])
    except Exception as e:
        print(f"Faylni o'qishda xatolik: {e}")
    return text

def create_pdf_sync(quiz_id, savollar, file_name):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.cell(0, 10, text=f"TEST ID: {quiz_id}", align='C', new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 10, text="", new_x="LMARGIN", new_y="NEXT")
    
    wrapper = textwrap.TextWrapper(width=75, break_long_words=True)
    
    # PDF tushunmaydigan belgilarni oddiy klaviatura belgilariga almashtirish
    def to_safe_str(t):
        t = str(t).replace('\n', ' ')
        replacements = {
            '—': '-', '–': '-', '−': '-',
            '“': '"', '”': '"', '«': '"', '»': '"',
            '‘': "'", '’': "'", '`': "'",
            '…': '...', '№': '#'
        }
        for old, new in replacements.items():
            t = t.replace(old, new)
        # Qolib ketgan boshqa g'alati belgilarni xavfsiz o'tkazish
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


# --- FSM VA MENYULAR ---
class QuizForm(StatesGroup):
    usul = State()
    daraja = State() 
    soni = State()
    vaqt = State()
    malumot = State()
    msgs_to_delete = State() 

class AdminState(StatesGroup):
    xabar_kutish = State()

asosiy_menyu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📄 Fayldan test tuzish"), KeyboardButton(text="✍️ Mavzudan test tuzish")],
        [KeyboardButton(text="🏆 Reyting"), KeyboardButton(text="👤 Profil")],
        [KeyboardButton(text="ℹ️ Yordam")]
    ], resize_keyboard=True, input_field_placeholder="Quyidagilardan birini tanlang:"
)

bekor_tugma = [KeyboardButton(text="🔙 Bekor qilish")]

daraja_menyu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🟢 Oson"), KeyboardButton(text="🟡 O'rtacha"), KeyboardButton(text="🔴 Qiyin")],
        bekor_tugma
    ], resize_keyboard=True
)

soni_menyu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="5"), KeyboardButton(text="10"), KeyboardButton(text="15"), KeyboardButton(text="20")],
        bekor_tugma
    ], resize_keyboard=True
)

vaqt_menyu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="15 soniya"), KeyboardButton(text="30 soniya")],
        [KeyboardButton(text="60 soniya"), KeyboardButton(text="⏳ Cheklovsiz")],
        bekor_tugma
    ], resize_keyboard=True
)

bekor_menyu = ReplyKeyboardMarkup(keyboard=[bekor_tugma], resize_keyboard=True)
admin_menyu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Statistika"), KeyboardButton(text="📣 Xabar tarqatish")],
        [KeyboardButton(text="🔙 Bosh menyu")]
    ], resize_keyboard=True
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
    await message.answer(f"ID: `{message.from_user.id}`", parse_mode="Markdown")

@dp.message(F.text == "🔙 Bekor qilish")
async def bekor_qilish_handler(message: types.Message, state: FSMContext):
    await delete_tracked_msgs(message.chat.id, state)
    await state.clear()
    await message.answer("❌ Bekor qilindi.", reply_markup=asosiy_menyu)

@dp.message(F.text == "🏆 Reyting")
async def show_reyting(message: types.Message):
    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT name, score FROM users ORDER BY score DESC LIMIT 10")
    
    text = "🏆 **TOP-10 REYTING:**\n\n"
    for i, u in enumerate(users, 1):
        text += f"{i}. {u['name']} — {u['score']} ball\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "👤 Profil")
async def show_profile(message: types.Message):
    async with db_pool.acquire() as conn:
        u = await conn.fetchrow("SELECT name, score, tests_taken FROM users WHERE user_id = $1", str(message.from_user.id))
    
    if u:
        text = f"👤 **Profil:**\n\nIsm: {u['name']}\n✅ Jami ball: {u['score']}\n📝 Yechilgan testlar: {u['tests_taken']} marta"
    else:
        text = "Siz hali test yechmadingiz."
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "ℹ️ Yordam")
async def show_help(message: types.Message):
    help_text = "💡 Test tuzish uchun menyudan usul tanlang. Oson=1, O'rtacha=2, Qiyin=3 ball."
    await message.answer(help_text)

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
                
                ball_qiymati = 3 if "🔴 Qiyin" in daraja else (2 if "🟡 O'rtacha" in daraja else 1)

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
                await message.answer(f"🏁 **Yakunlandi!**\nNatija: {togri_javoblar} ta to'g'ri ({foiz}%)", reply_markup=asosiy_menyu, parse_mode="Markdown")
                return
            else:
                await message.answer("⚠️ Test topilmadi.")
                return

    await message.answer("Salom! Usulni tanlang:", reply_markup=asosiy_menyu)

# --- ADMIN PANEL QISMI ---
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("👑 Admin panel", reply_markup=admin_menyu)

@dp.message(F.text == "🔙 Bosh menyu")
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Asosiy menyu.", reply_markup=asosiy_menyu)

@dp.message(F.text == "📊 Statistika")
async def show_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    async with db_pool.acquire() as conn:
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
        quizzes_count = await conn.fetchval("SELECT COUNT(*) FROM quizzes")
        
    await message.answer(f"📊 **Statistika:**\n👥 Qatnashchilar: {users_count}\n📝 Testlar: {quizzes_count}", parse_mode="Markdown")

# --- YAngi: XABAR TARQATISH MANTIQI ---
@dp.message(F.text == "📣 Xabar tarqatish")
async def ask_broadcast_msg(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("Barcha foydalanuvchilarga yuboriladigan xabarni kiriting\n(Bekor qilish uchun '🔙 Bosh menyu' ni bosing):", reply_markup=admin_menyu)
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
    success = 0
    fail = 0
    for u in users:
        try:
            # copy_message matn, rasm yoki videoni aynan o'zidek qilib hammaga yetkazadi
            await bot.copy_message(chat_id=int(u['user_id']), from_chat_id=message.chat.id, message_id=message.message_id)
            success += 1
            await asyncio.sleep(0.05) # Telegram bloklamasligi uchun tanaffus
        except Exception:
            fail += 1
    
    await message.answer(f"✅ Tarqatish yakunlandi!\n\nYetib bordi: {success} ta\nYetib bormadi (botni bloklaganlar): {fail} ta", reply_markup=asosiy_menyu)
    await state.clear()


# --- MANTIQ QADAMLARI ---
@dp.message(F.text.in_(["📄 Fayldan test tuzish", "✍️ Mavzudan test tuzish"]))
async def usul_tanlash(message: types.Message, state: FSMContext):
    await state.update_data(msgs_to_delete=[]) 
    await track_msg(state, message.message_id) 

    usul = "fayl" if "Fayl" in message.text else "mavzu"
    await state.update_data(usul=usul)
    await state.set_state(QuizForm.daraja)
    msg = await message.answer("Darajani tanlang:", reply_markup=daraja_menyu)
    await track_msg(state, msg.message_id)

@dp.message(QuizForm.daraja)
async def daraja_olish(message: types.Message, state: FSMContext):
    await track_msg(state, message.message_id)
    if "Oson" not in message.text and "O'rtacha" not in message.text and "Qiyin" not in message.text: return
    await state.update_data(daraja=message.text)
    await state.set_state(QuizForm.soni)
    msg = await message.answer("Nechta savol?", reply_markup=soni_menyu)
    await track_msg(state, msg.message_id)

@dp.message(QuizForm.soni)
async def savol_sonini_olish(message: types.Message, state: FSMContext):
    await track_msg(state, message.message_id)
    if not message.text.isdigit(): return
    await state.update_data(soni=int(message.text))
    await state.set_state(QuizForm.vaqt)
    msg = await message.answer("Vaqtni belgilang:", reply_markup=vaqt_menyu)
    await track_msg(state, msg.message_id)

@dp.message(QuizForm.vaqt)
async def vaqtni_olish(message: types.Message, state: FSMContext):
    await track_msg(state, message.message_id)
    matn = message.text
    vaqt = 0 if "Cheklovsiz" in matn else int(matn.split()[0])
    await state.update_data(vaqt=vaqt)
    
    data = await state.get_data()
    await state.set_state(QuizForm.malumot)
    javob = "Fayl yuboring." if data['usul'] == 'fayl' else "Mavzuni yozing:"
    msg = await message.answer(javob, reply_markup=bekor_menyu)
    await track_msg(state, msg.message_id)

# --- MA'LUMOTLARNI QABUL QILISH ---
@dp.message(QuizForm.malumot, F.text)
async def mavzuni_qabul_qilish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if data.get('usul') != 'mavzu': return
    await delete_tracked_msgs(message.chat.id, state)
    wait_msg = await message.answer("🟢 AI test tuzmoqda...", reply_markup=ReplyKeyboardRemove())
    
    daraja_toza = data['daraja'].split()[-1] 
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

        daraja_toza = data['daraja'].split()[-1] 
        qoshimcha = "Matn asosida chuqur mantiqiy o'ylashni talab qiladigan murakkab savollar tuzing. DIQQAT: Telegram qoidasiga ko'ra har bir javob varianti uzunligi 90 ta belgidan oshmasligi QAT'IY SHART. Javoblar qisqa va londa bo'lsin!" if daraja_toza == "Qiyin" else "DIQQAT: Telegram qoidasiga ko'ra har bir javob varianti uzunligi 90 ta belgidan oshmasligi qat'iy shart!"

        prompt = f"Matn asosida {data['soni']} ta test tuz. Qiyinlik: {daraja_toza}. {qoshimcha} FAQAT JSON ro'yxat ber. Variantlarga A, B, C, D yozma!\nNamuna: [{{\"savol\": \"...\", \"variantlar\": [\"J1\", \"J2\", \"J3\", \"J4\"], \"togri_index\": 0}}]\n\nMatn: {text[:8000]}"
        
        await wait_msg.delete()
        wait_msg_new = await message.answer("🟢 AI test tuzmoqda...", reply_markup=ReplyKeyboardRemove())
        
        await generate_and_save(message, prompt, wait_msg_new, state, data['vaqt'], data['daraja'])
    except Exception as e:
        print(f"Fayl xatosi: {e}")
        await wait_msg.delete()
        await message.answer("❌ Xatolik yuz berdi.", reply_markup=asosiy_menyu)
        await state.clear()

@dp.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer):
    poll_id = poll_answer.poll_id
    user_id_int = poll_answer.user.id
    user_id_str = str(user_id_int)
    tanlangan_javob = poll_answer.option_ids[0] if poll_answer.option_ids else -1

    if poll_id in POLL_DATA:
        if tanlangan_javob == POLL_DATA[poll_id]["correct"]:
            ball = POLL_DATA[poll_id]["points"]
            SESSION_SCORES[user_id_int] = SESSION_SCORES.get(user_id_int, 0) + 1
            
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET score = score + $1 WHERE user_id = $2", ball, user_id_str)

async def generate_and_save(message: types.Message, prompt: str, wait_msg: types.Message, state: FSMContext, vaqt: int, daraja: str):
    try:
        response = await asyncio.to_thread(
            model.generate_content, 
            prompt, 
            generation_config={"response_mime_type": "application/json"}
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
        share_link = f"https://t.me/share/url?url={test_link}&text=Yangi test!"
        
        inline_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Yechish", url=test_link), InlineKeyboardButton(text="🔗 Ulashish", url=share_link)],
            [InlineKeyboardButton(text="📥 PDF yuklash", callback_data=f"pdf_{quiz_id}")]
        ])
        
        await message.answer("✅ Tayyor!", reply_markup=inline_kb)
        await state.clear()
    except Exception as e:
        print(f"Gen xatosi: {e}")
        await wait_msg.delete()
        await message.answer("⚠️ Test tuzishda xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.", reply_markup=asosiy_menyu)
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
        
        await asyncio.to_thread(create_pdf_sync, quiz_id, savollar, file_name)
        
        pdf_file = FSInputFile(file_name)
        await bot.send_document(callback.message.chat.id, pdf_file, caption="📥 Marhamat, testning PDF varianti.")
        
        os.remove(file_name)
        await wait_msg.delete()

    except Exception as e:
        print(f"PDF xatolik: {e}")
        await bot.send_message(callback.message.chat.id, f"⚠️ PDF yaratishda xatolik yuz berdi: {str(e)[:100]}")

async def main():
    keep_alive()
    await init_db_pool()
    print("🚀 QOTMAS BOT: PDF muammosi hal qilindi va Tarqatish qo'shildi!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
