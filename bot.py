import os
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from razorpay import Client
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, "fantasy_lock_bot.db")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
ADMIN_USER_IDS = {int(x.strip()) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()}

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
razorpay_client = Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

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
        await message.answer("❌ Unauthorized.")
        return
    await message.answer("Enter a unique match name (used in invite links):")
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

# Add remaining handlers for CreateMatchState.description, price, validity, file here as in your original code

@dp.message(CommandStart())
async def start_handler(message: types.Message, state: FSMContext):
    args = message.text.strip().split(maxsplit=1)
    if len(args) > 1:
        match_name = args[1].strip()
        await process_match_name(message, match_name)
    else:
        await message.answer("Welcome! Please enter the match name to unlock:")
        await state.set_state(UserStates.waiting_for_match_name)

@dp.message(UserStates.waiting_for_match_name)
async def waiting_for_match(message: types.Message, state: FSMContext):
    match_name = message.text.strip()
    await process_match_name(message, match_name)
    await state.clear()

async def process_match_name(message: types.Message, match_name: str):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT price, expires_at, filename, description FROM matches WHERE match_name=?", (match_name,))
        row = c.fetchone()

    if not row:
        await message.answer("Invalid or expired match name. Please verify invite link.")
        return

    price, expires_at, filename, description = row
    expires_dt = datetime.fromisoformat(expires_at)
    if datetime.now(timezone.utc) > expires_dt:
        await message.answer("Invite link and payment link expired.")
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
            c.execute(
                "INSERT OR REPLACE INTO payments (user_id, match_name, razorpay_order_id, paid, payment_link_url) VALUES (?, ?, ?, 0, ?)",
                (user_id, match_name, payment_link["id"], payment_url))
            conn.commit()

        me = await bot.get_me()
        deep_link = f"https://t.me/{me.username}?start={match_name}"

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=f"Pay ₹{price} to Unlock", url=payment_url)]
        ])

        await message.answer(
            f"Match: {match_name}\nPrice: ₹{price}\n\nInvite Link:\n{deep_link}\n\nPlease pay to unlock content.",
            reply_markup=keyboard)

async def deliver_content(user_id, filename, description):
    try:
        if filename and filename.startswith("http"):
            await bot.send_message(user_id, f"Content unlocked! Here:\n{filename}")
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
