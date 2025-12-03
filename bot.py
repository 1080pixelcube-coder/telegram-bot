from telethon import TelegramClient, events

api_id = 39563466
api_hash = '90b43b99c539f043db5aed7805f3e207'
phone_number = '+989933270565'  

source_channel = 'AradAhmadi_Ch'
target_group = 'AutoTradingSIG_bot'

client = TelegramClient('session_name', api_id, api_hash)

def check_stickers(message_text):
    return '❗' in message_text and '🔥' in message_text

@client.on(events.NewMessage(chats=source_channel))
async def handler(event):
    message = event.message
    if message.message:  
        if check_stickers(message.message):
            await client.send_message(target_group, message.message)
            print("Message forwarded!")

client.start()
print("Bot is running...")
client.run_until_disconnected()
