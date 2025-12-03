from telethon import TelegramClient, events

# ⚡ فقط Bot Token لازم است
bot_token = '8374221123:AAGgoW7GvyHY8qFMR4zMPimvXTXlV3K72M0'  # این را از BotFather بگیر

api_id = 39563466
api_hash = '90b43b99c539f043db5aed7805f3e207'

source_channel = 'AradAhmadi_Ch'
target_group = 'AutoTradingSIG_bot'

# استفاده از Bot Token برای session
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

print("Bot is running...")
client.run_until_disconnected()
