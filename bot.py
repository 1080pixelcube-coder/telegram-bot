from telethon import TelegramClient, events
import asyncio
import logging

# ---------------- TELEGRAM CONFIG ----------------
api_id = 39563466
api_hash = "90b43b99c539f043db5aed7805f3e207"
phone_number = "+989933270565"
session_name = "bot_session"  # فایل session باید این اسم داشته باشد

# ---------------- LOGGING ----------------
logging.basicConfig(
    filename='bot_journal.log',   # فایل journal
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------------- TELEGRAM CLIENT ----------------
client = TelegramClient(session_name, api_id, api_hash)

# ---------------- EVENTS ----------------
@client.on(events.NewMessage(chats="AradAhmadi_Ch"))
async def forward_message(event):
    try:
        await client.send_message("AutoTradingSIG_bot", event.message)
        logger.info(f"پیام فوروارد شد: {event.message.text[:50]}")
        print(f"پیام فوروارد شد: {event.message.text[:50]}")
    except Exception as e:
        logger.error(f"خطا در فوروارد: {e}")
        print(f"خطا در فوروارد: {e}")

# ---------------- RUN BOT ----------------
async def main():
    await client.start(phone=phone_number)
    logger.info("بات با موفقیت متصل شد ✔")
    print("بات با موفقیت متصل شد ✔")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
