import os
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from razorpay import Client
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
ADMIN_USER_IDS = {int(x.strip()) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()}

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
razorpay_client = Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
DB = "fantasy_lock_bot.db"

def init_db():
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS creators (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                phone TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS matches (
                match_name TEXT PRIMARY KEY,
                creator_id INTEGER,
                description TEXT,
                filename TEXT,
                price INTEGER,
                expires_at TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                match_name TEXT,
                razorpay_order_id TEXT,
                paid INTEGER DEFAULT 0,
                paid_at TEXT,
                payment_link_url TEXT,
                UNIQUE(user_id, match_name)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                amount INTEGER,
                upi TEXT,
                status TEXT,
                requested_at TEXT
            )
        ''')
        conn.commit()
init_db()

class CreateMatchState(StatesGroup):
    ask_match_name = State()
    description = State()
    price = State()
    validity = State()
    file = State()

class UserStates(StatesGroup):
    waiting_for_match_name = State()

class WithdrawalStates(StatesGroup):
    amount = State()
    upi = State()

@dp.message(Command("enroll"))
async def enroll_creator(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO creators (user_id, username) VALUES (?, ?)", (user_id, username))
        conn.commit()
    await message.answer("✅ You are enrolled as a match creator! Use /createlock to create locks.")

@dp.message(Command("createlock"))
async def start_createlock(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_USER_IDS:
        await message.answer("❌ You are not authorized to create locks.")
        return
    await message.answer("Enter a unique match name (will be part of invite links):")
    await state.set_state(CreateMatchState.ask_match_name)

@dp.message(CreateMatchState.ask_match_name)
async def capture_match_name(message: types.Message, state: FSMContext):
    match_name = message.text.strip()
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM matches WHERE match_name=?", (match_name,))
        if c.fetchone():
            await message.answer("❌ Match name already exists, choose another.")
            return
    await state.update_data(match_name=match_name)
    await message.answer("Send description:")
    await state.set_state(CreateMatchState.description)

@dp.message(CreateMatchState.description)
async def set_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    await message.answer("Send price in INR (numbers only):")
    await state.set_state(CreateMatchState.price)

@dp.message(CreateMatchState.price)
async def set_price(message: types.Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("Please enter a valid positive price.")
        return
    await state.update_data(price=int(message.text))
    await message.answer("Send validity duration in minutes:")
    await state.set_state(CreateMatchState.validity)

@dp.message(CreateMatchState.validity)
async def set_validity(message: types.Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("Please enter a valid positive duration.")
        return
    await state.update_data(validity=int(message.text))
    await message.answer("Send the file/document/video/photo or a direct http/https link:")
    await state.set_state(CreateMatchState.file)

@dp.message(CreateMatchState.file, F.content_type.in_({
    types.ContentType.PHOTO,
    types.ContentType.DOCUMENT,
    types.ContentType.VIDEO,
    types.ContentType.AUDIO,
    types.ContentType.ANIMATION,
    types.ContentType.VOICE,
    types.ContentType.TEXT
}))
async def set_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    match_name = data["match_name"]
    description = data["description"]
    price = data["price"]
    validity = data["validity"]
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=validity)).isoformat()

    filename = None
    if message.photo:
        photo = message.photo[-1]
        os.makedirs("files", exist_ok=True)
        filename = f"files/{match_name}.jpg"
        file_obj = await bot.get_file(photo.file_id)
        await bot.download(file_obj, destination=filename)
    elif message.document:
        os.makedirs("files", exist_ok=True)
        filename = f"files/{match_name}_{message.document.file_name}"
        file_obj = await bot.get_file(message.document.file_id)
        await bot.download(file_obj, destination=filename)
    elif message.video:
        os.makedirs("files", exist_ok=True)
        filename = f"files/{match_name}.mp4"
        file_obj = await bot.get_file(message.video.file_id)
        await bot.download(file_obj, destination=filename)
    elif message.text and message.text.startswith(("http://", "https://")):
        filename = message.text.strip()
    else:
        await message.answer("Please send a file (photo, document, video, audio) or a valid URL (http/https).")
        return

    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO matches (match_name, creator_id, description, filename, price, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (match_name, message.from_user.id, description, filename, price, expires_at))
        conn.commit()

    me = await bot.get_me()
    deep_link = f"https://t.me/{me.username}?start={match_name}"
    await message.answer(f"✅ Match '{match_name}' created!\nInvite link:\n{deep_link}")
    await state.clear()

@dp.message(CreateMatchState.file)
async def fallback_file_handler(message: types.Message):
    await message.answer("Unsupported file type. Please send photo, document, video, audio, or a valid http/https URL.")

@dp.message(CommandStart())
async def start_handler(message: types.Message, state: FSMContext):
    args = message.text.strip().split(maxsplit=1)
    if len(args) > 1:
        match_name = args[1].strip()
        await process_match_name(message, match_name)
    else:
        await message.answer("Welcome! Please enter the match name you want to unlock:")
        await state.set_state(UserStates.waiting_for_match_name)

class UserStates(StatesGroup):
    waiting_for_match_name = State()

@dp.message(UserStates.waiting_for_match_name)
async def waiting_for_match_name(message: types.Message, state: FSMContext):
    match_name = message.text.strip()
    await process_match_name(message, match_name)
    await state.clear()

async def process_match_name(message: types.Message, match_name: str):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT price, expires_at, filename, description FROM matches WHERE match_name=?", (match_name,))
        row = c.fetchone()

    if not row:
        await message.answer("❌ Invalid or expired match name. Check your invite link.")
        return

    price, expires_at, filename, description = row
    expires_dt = datetime.fromisoformat(expires_at)
    if datetime.now(timezone.utc) > expires_dt:
        await message.answer("❌ This invite and payment link have expired.")
        return

    user_id = message.from_user.id
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT paid FROM payments WHERE user_id=? AND match_name=? AND paid=1", (user_id, match_name))
        paid = c.fetchone()

    if paid:
        await deliver_content(user_id, filename, description)
    else:
        payment_link = razorpay_client.payment_link.create({
            "amount": price * 100,
            "currency": "INR",
            "description": f"Unlock {match_name}",
            "notes": {"user_id": str(user_id), "match_name": match_name}
        })
        payment_url = payment_link["short_url"]

        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO payments (user_id, match_name, razorpay_order_id, paid, payment_link_url) VALUES (?, ?, ?, 0, ?)",
                      (user_id, match_name, payment_link["id"], payment_url))
            conn.commit()

        me = await bot.get_me()
        deep_link = f"https://t.me/{me.username}?start={match_name}"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Pay ₹{price} to Unlock", url=payment_url)]
        ])

        await message.answer(
            f"Match: {match_name}\nPrice: ₹{price}\n\nInvite Link (share):\n{deep_link}\n\nPay using the button below to unlock content.",
            reply_markup=keyboard
        )

@dp.message(Command("dashboard"))
async def creator_dashboard(message: types.Message):
    if message.from_user.id not in ADMIN_USER_IDS:
        await message.answer("❌ Unauthorized.")
        return
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT match_name, price FROM matches WHERE creator_id=?", (message.from_user.id,))
        rows = c.fetchall()
    if not rows:
        await message.answer("You have not created any matches.")
        return
    response = "📊 Your Matches and Sales:\n"
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        for match_name, price in rows:
            c.execute("SELECT COUNT(*) FROM payments WHERE match_name=? AND paid=1", (match_name,))
            count = c.fetchone()[0] or 0
            revenue = count * price
            response += f"\n{match_name}: Sold {count} | Revenue ₹{revenue}"
    await message.answer(response)

@dp.message(Command("withdraw"))
async def request_withdrawal(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_USER_IDS:
        await message.answer("❌ Unauthorized.")
        return
    await message.answer("Send withdrawal amount (INR):")
    await state.set_state(WithdrawalStates.amount)

@dp.message(WithdrawalStates.amount)
async def capture_withdraw_amount(message: types.Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("Please enter a valid positive amount.")
        return
    await state.update_data(amount=int(message.text))
    await message.answer("Send your UPI ID:")
    await state.set_state(WithdrawalStates.upi)

@dp.message(WithdrawalStates.upi)
async def capture_withdraw_upi(message: types.Message, state: FSMContext):
    upi = message.text.strip()
    data = await state.get_data()
    amount = data["amount"]
    user_id = message.from_user.id
    username = message.from_user.username or ""
    requested_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO withdrawals (user_id, username, amount, upi, status, requested_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (user_id, username, amount, upi, "pending", requested_at))
        conn.commit()
    await message.answer("✅ Withdrawal request submitted.")
    await state.clear()

async def deliver_content(user_id, filename, description):
    try:
        if filename and filename.startswith("http"):
            await bot.send_message(user_id, f"🎉 Content unlocked! Access here:\n{filename}")
        elif filename:
            await bot.send_document(user_id, open(filename, "rb"), caption=description)
        else:
            await bot.send_message(user_id, "No content available.")
    except Exception as e:
        logging.error(f"Error delivering content to user {user_id}: {e}")
        await bot.send_message(user_id, "Error delivering content, please contact support.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot))
