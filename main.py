import asyncio
import os
import io
import json
import uuid
import random
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

# 👑 ADMIN SOZLAMASI (O'zingizning ID raqamingizni yozing)
ADMIN_ID = 5031441892

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash-lite')

DB_FILE = "quizzes.json"
USERS_FILE = "users.json"

def bazani_oqish(fayl_nomi):
    if os.path.exists(fayl_nomi):
        with open(fayl_nomi, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except: return {}
    return {}

def bazaga_yozish(fayl_nomi, data):
    with open(fayl_nomi, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

QUIZZES = bazani_oqish(DB_FILE)
USERS = bazani_oqish(USERS_FILE) 

POLL_DATA = {}   
SESSION_SCORES = {} 
# ------------------

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
# -----------------------

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

def add_user(user_id, first_name):
    uid = str(user_id)
    if uid not in USERS:
        USERS[uid] = {"name": first_name, "score": 0, "tests_taken": 0}
        bazaga_yozish(USERS_FILE, USERS)

# --- ASOSIY BUYRUQLAR VA YANGI TUGMALAR ---
@dp.message(Command("id"))
async def get_id(message: types.Message):
    await message.answer(f"Sizning ID raqamingiz: `{message.from_user.id}`", parse_mode="Markdown")

@dp.message(F.text == "🔙 Bekor qilish")
async def bekor_qilish_handler(message: types.Message, state: FSMContext):
    await delete_tracked_msgs(message.chat.id, state)
    await state.clear()
    await message.answer("❌ Jarayon bekor qilindi. Bosh menyuga qaytdik!", reply_markup=asosiy_menyu)

@dp.message(F.text == "🏆 Reyting")
async def show_reyting(message: types.Message):
    sorted_users = sorted(USERS.values(), key=lambda x: x.get('score', 0), reverse=True)
    text = "🏆 **TOP-10 BILIMDONLAR REYTINGI:**\n\n"
    for i, u in enumerate(sorted_users[:10], 1):
        text += f"{i}. {u['name']} — {u['score']} ball\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "👤 Profil")
async def show_profile(message: types.Message):
    uid = str(message.from_user.id)
    if uid in USERS:
        u = USERS[uid]
        text = f"👤 **Sizning profilingiz:**\n\nIsm: {u['name']}\n✅ Jami to'g'ri javoblar: {u['score']} ta\n📝 Yechilgan testlar: {u.get('tests_taken', 0)} marta"
    else:
        text = "Siz hali test yechmadingiz."
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "ℹ️ Yordam")
async def show_help(message: types.Message):
    help_text = (
        "💡 **Qanday foydalaniladi?**\n\n"
        "1. Menyudan test tuzish usulini tanlang.\n"
        "2. Daraja, savollar soni va vaqtni belgilang.\n"
        "3. Kerakli matn yoki Word/PDF hujjatini yuboring.\n"
        "4. AI test tuzishini kuting va havolani do'stlaringizga ulashing!\n\n"
        "Maksimal savollar soni: 20 ta.\n"
        "Admin bilan aloqa: Bot muallifiga murojaat qiling."
    )
    await message.answer(help_text, parse_mode="Markdown")

@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext, command: CommandObject = None):
    await state.clear()
    add_user(message.from_user.id, message.from_user.first_name)

    if command and command.args:
        quiz_id = command.args
        if quiz_id in QUIZZES:
            await message.answer("🚀 Test boshlanmoqda! Tayyorlaning...", reply_markup=ReplyKeyboardRemove())
            quiz_data = QUIZZES[quiz_id]
            savollar = quiz_data if isinstance(quiz_data, list) else quiz_data.get("savollar", [])
            vaqt_cheklovi = 0 if isinstance(quiz_data, list) else quiz_data.get("vaqt", 0)

            user_id = message.from_user.id
            SESSION_SCORES[user_id] = 0 
            jami_savollar = len(savollar)

            uid_str = str(user_id)
            USERS[uid_str]["tests_taken"] = USERS[uid_str].get("tests_taken", 0) + 1
            bazaga_yozish(USERS_FILE, USERS)

            for data in savollar:
                q = data['savol'][:250]
                opts = [str(opt)[:100] for opt in data['variantlar']][:4]
                correct = int(data.get('togri_index', 0))
                if correct > 3 or correct < 0: correct = 0

                quiz_kwargs = {
                    "chat_id": message.chat.id, "question": q, "options": opts,
                    "type": 'quiz', "correct_option_id": correct, "is_anonymous": False 
                }
                if vaqt_cheklovi > 0: quiz_kwargs["open_period"] = vaqt_cheklovi

                sent_poll = await bot.send_poll(**quiz_kwargs)
                POLL_DATA[sent_poll.poll.id] = correct

                await asyncio.sleep(vaqt_cheklovi + 1 if vaqt_cheklovi > 0 else 2.0)

            togri_javoblar = SESSION_SCORES.get(user_id, 0)
            foiz = int((togri_javoblar / jami_savollar) * 100) if jami_savollar > 0 else 0
            xulosa = "Zo'r natija! 🌟" if foiz >= 80 else ("Yaxshi, izlanish kerak! 📚" if foiz >= 50 else "Ko'proq o'qishingiz kerak! 🔄")

            natija_matni = (f"🏁 **Test yakunlandi!**\n\n📊 Natijangiz: {togri_javoblar} ta to'g'ri ({foiz}%)\n{xulosa}\n\n"
                            f"O'zingiz test tuzish uchun /start bosing.")
            await message.answer(natija_matni, reply_markup=asosiy_menyu, parse_mode="Markdown")
            return
        else:
            await message.answer("⚠️ Bu test havolasi topilmadi.")

    xabar = f"Salom, {message.from_user.first_name}! 👋\nMen universal test botiman. Usulni tanlang:"
    await message.answer(xabar, reply_markup=asosiy_menyu)

# --- ADMIN PANEL QISMI ---
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("👑 Admin panelga xush kelibsiz!", reply_markup=admin_menyu)

@dp.message(F.text == "🔙 Bosh menyu")
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Asosiy menyuga qaytdik.", reply_markup=asosiy_menyu)

@dp.message(F.text == "📊 Statistika")
async def show_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer(f"📊 **Statistika:**\n👥 Qatnashchilar: {len(USERS)}\n📝 Tuzilgan testlar: {len(QUIZZES)}", parse_mode="Markdown")

@dp.message(F.text == "📣 Xabar tarqatish")
async def ask_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.set_state(AdminState.xabar_kutish)
    await message.answer("Barcha foydalanuvchilarga qanday xabar yuboramiz?\n*(Bekor qilish uchun 🔙 Bosh menyu)*", parse_mode="Markdown")

@dp.message(AdminState.xabar_kutish)
async def send_broadcast(message: types.Message, state: FSMContext):
    if message.text == "🔙 Bosh menyu": return
    await state.clear()
    sent = 0
    wait_msg = await message.answer("⏳ Tarqatilmoqda...")
    for uid in USERS.keys():
        try:
            await message.copy_to(chat_id=int(uid))
            sent += 1
            await asyncio.sleep(0.05)
        except: pass
    await wait_msg.edit_text(f"✅ Xabar {sent} ta foydalanuvchiga yetkazildi!", reply_markup=admin_menyu)

# --- MANTIQ QADAMLARI ---
@dp.message(F.text.in_(["📄 Fayldan test tuzish", "✍️ Mavzudan test tuzish"]))
async def usul_tanlash(message: types.Message, state: FSMContext):
    await state.update_data(msgs_to_delete=[]) 
    await track_msg(state, message.message_id) 

    if message.text == "📄 Fayldan test tuzish": await state.update_data(usul="fayl")
    elif message.text == "✍️ Mavzudan test tuzish": await state.update_data(usul="mavzu")

    await state.set_state(QuizForm.daraja)
    msg = await message.answer("Test qanday qiyinlik darajasida bo'lsin?", reply_markup=daraja_menyu)
    await track_msg(state, msg.message_id)

@dp.message(QuizForm.daraja)
async def daraja_olish(message: types.Message, state: FSMContext):
    await track_msg(state, message.message_id)
    if message.text not in ["🟢 Oson", "🟡 O'rtacha", "🔴 Qiyin"]:
        msg = await message.answer("⚠️ Tugmalardan birini tanlang.")
        await track_msg(state, msg.message_id)
        return

    await state.update_data(daraja=message.text)
    await state.set_state(QuizForm.soni)
    msg = await message.answer("Nechta test savoli tuzamiz?", reply_markup=soni_menyu)
    await track_msg(state, msg.message_id)

@dp.message(QuizForm.soni)
async def savol_sonini_olish(message: types.Message, state: FSMContext):
    await track_msg(state, message.message_id)
    if not message.text.isdigit() or int(message.text) > 20:
        msg = await message.answer("⚠️ Maksimal 20 gacha raqam kiriting.")
        await track_msg(state, msg.message_id)
        return

    await state.update_data(soni=int(message.text))
    await state.set_state(QuizForm.vaqt)
    msg = await message.answer("Har bir savol uchun qancha vaqt ajratamiz?", reply_markup=vaqt_menyu)
    await track_msg(state, msg.message_id)

@dp.message(QuizForm.vaqt)
async def vaqtni_olish(message: types.Message, state: FSMContext):
    await track_msg(state, message.message_id)
    matn = message.text
    if matn == "15 soniya": vaqt = 15
    elif matn == "30 soniya": vaqt = 30
    elif matn == "60 soniya": vaqt = 60
    elif matn == "⏳ Cheklovsiz": vaqt = 0
    else:
        msg = await message.answer("⚠️ Tugmalardan birini tanlang.")
        await track_msg(state, msg.message_id)
        return

    await state.update_data(vaqt=vaqt)
    data = await state.get_data()
    await state.set_state(QuizForm.malumot)

    javob = "Menga PDF/Word faylini yuboring." if data['usul'] == 'fayl' else f"Qaysi mavzuda {data['soni']} ta test tuzmoqchisiz? Mavzuni yozing:"
    msg = await message.answer(f"✅ Qabul qilindi. {javob}", reply_markup=bekor_menyu)
    await track_msg(state, msg.message_id)

# --- MA'LUMOTLARNI QABUL QILISH VA TAYYORLASH ---
@dp.message(QuizForm.malumot, F.text)
async def mavzuni_qabul_qilish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if data.get('usul') != 'mavzu': return

    await track_msg(state, message.message_id)
    await delete_tracked_msgs(message.chat.id, state)

    wait_msg = await message.answer(f"🟢 AI '{message.text}' mavzusida ({data['daraja']}) {data['soni']} ta test tuzmoqda. Kuting...", reply_markup=ReplyKeyboardRemove())
    prompt = f"'{message.text}' mavzusi bo'yicha {data['soni']} ta test tuz. Qiyinlik: {data['daraja']}. FAQAT JSON ro'yxat ber. DIQQAT: Variantlar ichiga A, B, C, D harflarini umuman yozma!\nNamuna: [{{\"savol\": \"...\", \"variantlar\": [\"Javob1\", \"Javob2\", \"Javob3\", \"Javob4\"], \"togri_index\": 0}}]"
    await generate_and_save(message, prompt, wait_msg, state, data['vaqt'])

@dp.message(QuizForm.malumot, F.document)
async def faylni_qabul_qilish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if data.get('usul') != 'fayl': return

    await track_msg(state, message.message_id)
    await delete_tracked_msgs(message.chat.id, state)
    wait_msg = await message.answer("🔄 Fayl yuklanmoqda...", reply_markup=ReplyKeyboardRemove())

    try:
        file_info = await bot.get_file(message.document.file_id)
        file_data = io.BytesIO()
        await bot.download(file_info, destination=file_data)
        file_data.seek(0)

        filename = message.document.file_name.lower()
        if filename.endswith('.pdf'): text = "".join([p.extract_text() or "" for p in PyPDF2.PdfReader(file_data).pages])
        elif filename.endswith('.docx'): text = "\n".join([p.text for p in Document(file_data).paragraphs])

        if len(text.strip()) < 50:
            await wait_msg.edit_text("⚠️ Faylda yetarli matn yo'q.")
            return

        await wait_msg.edit_text(f"🟢 AI fayl asosida ({data['daraja']}) {data['soni']} ta test tuzmoqda. Kuting...")
        prompt = f"Matn asosida {data['soni']} ta test tuz. Qiyinlik: {data['daraja']}. FAQAT JSON ro'yxat ber. DIQQAT: Variantlar ichiga A, B, C, D harflarini umuman yozma!\nNamuna: [{{\"savol\": \"...\", \"variantlar\": [\"Javob1\", \"Javob2\", \"Javob3\", \"Javob4\"], \"togri_index\": 0}}]\n\nMatn: {text[:8000]}"
        await generate_and_save(message, prompt, wait_msg, state, data['vaqt'])
    except Exception as e:
        await wait_msg.edit_text("❌ Faylni o'qishda xatolik.")
        await state.clear()

@dp.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer):
    poll_id = poll_answer.poll_id
    user_id_int = poll_answer.user.id
    user_id_str = str(user_id_int)
    tanlangan_javob = poll_answer.option_ids[0] if poll_answer.option_ids else -1

    if poll_id in POLL_DATA:
        if tanlangan_javob == POLL_DATA[poll_id]:
            SESSION_SCORES[user_id_int] = SESSION_SCORES.get(user_id_int, 0) + 1
            if user_id_str not in USERS: USERS[user_id_str] = {"name": poll_answer.user.first_name, "score": 0, "tests_taken": 0}
            USERS[user_id_str]["score"] = USERS[user_id_str].get("score", 0) + 1
            bazaga_yozish(USERS_FILE, USERS)

async def generate_and_save(message: types.Message, prompt: str, wait_msg: types.Message, state: FSMContext, vaqt: int):
    try:
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        savollar = json.loads(response.text)
        await wait_msg.delete()

        for data in savollar:
            eski_index = int(data.get('togri_index', 0))
            if eski_index < 0 or eski_index > 3: eski_index = 0
            togri_javob_matni = data['variantlar'][eski_index]
            random.shuffle(data['variantlar'])
            yangi_index = data['variantlar'].index(togri_javob_matni)
            data['togri_index'] = yangi_index

        quiz_id = str(uuid.uuid4())[:8]
        QUIZZES[quiz_id] = {"vaqt": vaqt, "savollar": savollar}
        bazaga_yozish(DB_FILE, QUIZZES)

        bot_info = await bot.get_me()
        bot_username = bot_info.username
        
        test_link = f"https://t.me/{bot_username}?start={quiz_id}"
        share_link = f"https://t.me/share/url?url={test_link}&text=Yangi testni yechib ko'ring!"
        
        inline_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Testni yechish", url=test_link)],
            [InlineKeyboardButton(text="🔗 Ulashish", url=share_link)],
            [InlineKeyboardButton(text="📥 PDF yuklab olish", callback_data=f"pdf_{quiz_id}")]
        ])
        
        await message.answer("✅ Barcha testlar tayyorlandi! Qanday harakatni tanlaysiz?", reply_markup=inline_kb)
        await state.clear()

    except Exception as e:
        await wait_msg.edit_text("⚠️ Xatolik yuz berdi. /start ni bosing.", reply_markup=asosiy_menyu)
        await state.clear()

@dp.callback_query(F.data.startswith("pdf_"))
async def send_pdf(callback: types.CallbackQuery):
    quiz_id = callback.data.split("_")[1]
    if quiz_id not in QUIZZES:
        await callback.answer("⚠️ Test bazada topilmadi.", show_alert=True)
        return
        
    await callback.answer("🔄 PDF tayyorlanmoqda...", show_alert=False)
    
    quiz_data = QUIZZES[quiz_id]
    savollar = quiz_data.get("savollar", []) if isinstance(quiz_data, dict) else quiz_data
    
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    
    pdf.multi_cell(0, 10, text=f"TEST TUZAR BOT - Test ID: {quiz_id}", align='C')
    pdf.cell(0, 10, text="", new_x="LMARGIN", new_y="NEXT")
    
    for i, s in enumerate(savollar, 1):
        q_text = s['savol'].replace('\n', ' ').encode('latin-1', 'replace').decode('latin-1')
        pdf.multi_cell(0, 8, text=f"{i}. {q_text}")
        for v_idx, v in enumerate(s['variantlar']):
            v_text = str(v).replace('\n', ' ').encode('latin-1', 'replace').decode('latin-1')
            pdf.multi_cell(0, 8, text=f"   {chr(65+v_idx)}) {v_text}")
        pdf.cell(0, 5, text="", new_x="LMARGIN", new_y="NEXT")
        
    file_name = f"test_{quiz_id}.pdf"
    pdf.output(file_name)
    
    pdf_file = FSInputFile(file_name)
    await bot.send_document(callback.message.chat.id, pdf_file, caption="📥 Testning PDF varianti.")
    os.remove(file_name)

async def main():
    keep_alive()
    print("🚀 Barcha tugmalar va PDF yuklash funksiyasi ishga tushdi!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
