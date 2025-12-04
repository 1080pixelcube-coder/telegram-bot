from flask import Flask, request
from telethon import TelegramClient, events
import asyncio
import logging
import os

# ---------------- TELEGRAM CONFIG ----------------
api_id = 39563466
api_hash = "90b43b99c539f043db5aed7805f3e207"
phone_number = "+989933270565"
session_name = "bot_session"  # فایل session باید همین اسم باشد

# ---------------- LOGGING ----------------
logging.basicConfig(
    filename='bot_journal.log',   # فایل journal
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------------- TELEGRAM CLIENT ----------------
client = TelegramClient(session_name, api_id, api_hash)

# ---------------- FLASK ----------------
app = Flask(__name__)

# ---------------- TELEGRAM EVENTS ----------------
@client.on(events.NewMessage(chats="AradAhmadi_Ch"))
async def forward_message(event):
    try:
        await client.send_message("AutoTradingSIG_bot", event.message)
        logger.info(f"پیام فوروارد شد: {event.message.text[:50]}")
        print(f"پیام فوروارد شد: {event.message.text[:50]}")
    except Exception as e:
        logger.error(f"خطا در فوروارد: {e}")
        print(f"خطا در فوروارد: {e}")

# ---------------- FLASK ROUTES ----------------
@app.route("/", methods=["GET"])
def home():
    return "Bot is running!"

@app.route("/startbot", methods=["GET"])
def start_bot():
    """Start Telethon bot asynchronously"""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.create_task(client.start(phone=phone_number))
    loop.create_task(client.run_until_disconnected())
    return "Bot started ✔", 200

