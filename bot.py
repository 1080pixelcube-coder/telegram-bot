from telethon import TelegramClient, events

api_id = 33853978
api_hash = '4361c67308be251a52210ab51602e122'

source_channel = '@TestmyConfig'    
target_group = '@AutoTradingSIG_bot'        


client = TelegramClient('my_forward_session', api_id, api_hash)

def has_fire_and_warning(text):
    if not text:
        return False
    return ('🔥' in text) and ('❗' in text or '❗️' in text)

@client.on(events.NewMessage(chats=source_channel))
async def handler(event):
    text = event.message.message

    if has_fire_and_warning(text):
        try:
            await client.forward_messages(
                entity=target_group,
                messages=event.message
            )
            print("Massage Forwarded !")
        except Exception as e:
            print("error", e)

print("Logining . . .")
client.start()    

print("Bot is running . . .")
client.run_until_disconnected()