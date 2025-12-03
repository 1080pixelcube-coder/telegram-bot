from telethon import TelegramClient, events
from flask import Flask
from threading import Thread

# --------------------------
# Bot Configuration
# --------------------------
bot_token = '8374221123:AAGgoW7GvyHY8qFMR4zMPimvXTXlV3K72M0'
api_id = 39563466
api_hash = '90b43b99c539f043db5aed7805f3e207'

source_channel = 'testbot_crypt'
target_group = 'AutoTradingSIG_bot'

# --------------------------
# Telegram Client
# --------------------------
client = TelegramClient('bot_session', api_id, api_hash).start(bot_token=bot_token)

def check_stickers(message_text):
    return '❗' in message_text and '🔥' in message_text

@client.on(events.NewMessage(chats=source_channel))
async def handler(event):
    message = event.message
    if message.message:
        if check_stickers(message.message):
            await client.send_message(target_group, message.message)
            print("Message forwarded!")

# --------------------------
# Flask WebServer
# --------------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_server():
    app.run(host='0.0.0.0', port=10000)  # پورت 10000 باز شود

# اجرای وب‌سرور در یک Thread جداگانه
server_thread = Thread(target=run_server)
server_thread.start()

# --------------------------
# Start Telegram Bot
# --------------------------
print("Bot is running...")
client.run_until_disconnected()
