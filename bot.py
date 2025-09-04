# --- START OF FILE bot.py ---

import asyncio
import httpx
import json
import time
import io
import logging
import os
import textwrap
import pytz
import random
import re
from datetime import datetime
from telethon import TelegramClient, events
from telethon.tl.types import Message
from telethon.tl.types import InputMediaDice
from PIL import Image
from dotenv import load_dotenv
from urllib.parse import quote

# .env faylini yuklash
load_dotenv()

# API ma'lumotlari
api_id = int(os.environ.get("API_ID"))
api_hash = os.environ.get("API_HASH")
session_name = os.environ.get("SESSION_NAME", "suii_userbot_session")
gemini_api_key = os.environ.get("GEMINI_API_KEY")
my_telegram_id = int(os.environ.get("MY_TELEGRAM_ID", 0))

# Pollinations.ai (Image) API endpoint
POLLINATIONS_IMAGE_API_BASE_URL = "https://image.pollinations.ai/prompt/"
POLLINATIONS_IMAGE_MODEL = "flux"

# Logging konfiguratsiyasi
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Telegram client
client = TelegramClient(session_name, api_id, api_hash)

# Gemini API endpoint (base)
GEMINI_BASE_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash"
MAX_MESSAGE_LENGTH = 4096
DEFAULT_SLEEP_TIME = 60

# Sozlamalar fayli
SETTINGS_FILE = "bot_settings.json"

# Standart sozlamalar
default_settings = {
    "allow_all_users": False,
    "auto_reply_enabled": True,
    "online_mode": False,
}

# Anti-flood uchun lug'at (user_id: last_reply_timestamp)
user_reply_cooldown = {}
COOLDOWN_SECONDS = 3 

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding='utf-8') as file:
                settings = json.load(file)
                for key, value in default_settings.items():
                    settings.setdefault(key, value)
                return settings
        except json.JSONDecodeError:
            pass
    save_settings(default_settings)
    return default_settings

def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w", encoding='utf-8') as file:
            json.dump(settings, file, indent=4, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Sozlamalar faylini saqlashda xatolik: {e}")

# Global o'zgaruvchilar
settings = load_settings()
allow_all_users = settings["allow_all_users"]
auto_reply_enabled = settings["auto_reply_enabled"]
online_mode = settings["online_mode"]
active_auto_send_tasks = {}

def mask_sensitive_info(text, api_key, gemini_base_url):
    masked_key = re.sub(r"([A-Za-z0-9_-]{15})([A-Za-z0-9_-]+)", r"\1********", api_key) if api_key else "API_KEY_HIDDEN"
    masked_gemini_url = re.sub(r"(https?://[^/]+)/.*", r"\1/API_ENDPOINT_HIDDEN", gemini_base_url) if gemini_base_url else "GEMINI_API_URL_HIDDEN"
    text = text.replace(api_key, masked_key) if api_key else text
    text = text.replace(gemini_base_url, masked_gemini_url) if gemini_base_url else text
    return text

async def generate_image_from_pollinations(prompt: str):
    try:
        encoded_prompt = quote(prompt, safe='')
        api_url = f"{POLLINATIONS_IMAGE_API_BASE_URL}{encoded_prompt}?model={POLLINATIONS_IMAGE_MODEL}"
        async with httpx.AsyncClient() as client_http:
            response = await client_http.get(api_url, timeout=120)
            response.raise_for_status()
            return response.content
    except Exception as e:
        logging.error(f"Pollinations.ai so'rovda xatolik: {e}", exc_info=True)
        return None

async def generate_image_with_progress(prompt, event):
    thinking_message = await event.reply("🎨 Rasm AI tomonidan chizilmoqda, kuting... ⏳")
    try:
        image_bytes = await generate_image_from_pollinations(prompt)
        if image_bytes:
            image_bytes_io = io.BytesIO(image_bytes)
            file = await client.upload_file(image_bytes_io, file_name="generated_image.png")
            await client.send_file(event.chat_id, file=file, caption=f"Sizning Rasmingiz: `{prompt}`", parse_mode='markdown', reply_to=event.message.id)
            await thinking_message.delete()
        else:
            await thinking_message.edit("Rasm yaratilmadi! API bilan bog'liq xatolik bo'lishi mumkin.")
    except Exception as e:
        logging.error(f"Rasm generatsiya qilishda kutilmagan xatolik: {e}", exc_info=True)
        await thinking_message.edit(f"Rasm generatsiya qilishda kutilmagan xatolik: {e}")

PERSONA_FILE = "persona.json"
def load_persona():
    try:
        with open(PERSONA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f).get("persona", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

CHAT_HISTORY_DIR = "chat_histories"
def create_chat_history_dir():
    os.makedirs(os.path.join(CHAT_HISTORY_DIR, 'group'), exist_ok=True)
    os.makedirs(os.path.join(CHAT_HISTORY_DIR, 'user'), exist_ok=True)
def get_chat_history_file_path(chat_id, sender_id, is_private):
    create_chat_history_dir()
    if is_private:
        return os.path.join(CHAT_HISTORY_DIR, 'user', f"user_{sender_id}.json")
    else:
        group_dir = os.path.join(CHAT_HISTORY_DIR, 'group', str(chat_id))
        os.makedirs(group_dir, exist_ok=True)
        return os.path.join(group_dir, f"user_{sender_id}.json")

def load_chat_history(chat_id, sender_id, is_private):
    file_path = get_chat_history_file_path(chat_id, sender_id, is_private)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get("history", []), data.get("last_activity", 0)
        except (json.JSONDecodeError, Exception):
            pass
    return [], 0

def save_chat_history(chat_id, sender_id, is_private, history, last_activity):
    file_path = get_chat_history_file_path(chat_id, sender_id, is_private)
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump({"history": history, "last_activity": last_activity}, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Suhbat tarixini saqlashda xatolik: {e}", exc_info=True)

async def get_gemini_response(prompt, chat_id, sender_id, is_private):
    try:
        history, _ = load_chat_history(chat_id, sender_id, is_private)
        contents = []
        persona = load_persona()
        if persona:
            persona_parts = [f"Sizning rolingiz: {persona.get('role')}", f"Javob berish uslubingiz: {persona.get('style')}", f"Kontekst/Misol: {persona.get('context_example')}", f"Admin haqida: {persona.get('admin')}"]
            full_persona_text = "\n".join(filter(None, persona_parts))
            if full_persona_text:
                contents.extend([{"role": "user", "parts": [{"text": f"Sizning shaxsiyatingiz va ko'rsatmalar:\n{full_persona_text}"}]}, {"role": "model", "parts": [{"text": "Tushundim."}]}])

        contents.extend(history)
        contents.append({"role": "user", "parts": [{"text": prompt[:MAX_MESSAGE_LENGTH]}]})
        request_data = {"contents": contents, "generationConfig": {"temperature": 1, "maxOutputTokens": 4096, "topP": 0.95}, "safetySettings": [{"category": c, "threshold": "BLOCK_NONE"} for c in ["HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_DANGEROUS_CONTENT", "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_HARASSMENT"]], "tools": [{"googleSearch": {}}]}
        headers = {'Content-Type': 'application/json'}
        async with httpx.AsyncClient() as client_http:
            api_url = f"{GEMINI_BASE_API_URL}:generateContent?key={gemini_api_key}"
            response = await client_http.post(api_url, headers=headers, json=request_data, timeout=120)
            response.raise_for_status()
            json_response = response.json()

        if "candidates" in json_response and json_response["candidates"]:
            response_text = json_response["candidates"][0]["content"]["parts"][0]["text"]
            response_text = re.sub(r"^\*\s(?![\*\s])", "• ", response_text, flags=re.MULTILINE)
            history.append({"role": "user", "parts": [{"text": prompt[:MAX_MESSAGE_LENGTH]}]})
            history.append({"role": "model", "parts": [{"text": response_text[:MAX_MESSAGE_LENGTH]}]})
            save_chat_history(chat_id, sender_id, is_private, history[-10:], time.time())
            return response_text
        else:
            error_reason = json_response.get('promptFeedback', {}).get('blockReason', 'Noma\'lum')
            return f"AI javob berishda qiyinchilikka uchradi. Sabab: {error_reason}"
    except httpx.RequestError:
        return "Tashqi API bilan bog'lanishda xatolik yuz berdi."
    except Exception as e:
        masked_error = mask_sensitive_info(str(e), gemini_api_key, GEMINI_BASE_API_URL)
        logging.error(f"Kutilmagan xatolik: {masked_error}", exc_info=True)
        return "Kutilmagan xatolik yuz berdi."

async def get_account_stats():
    stats = {'users': 0, 'groups': 0, 'channels': 0, 'bots': 0, 'unread': 0}
    async for dialog in client.iter_dialogs():
        if dialog.is_user:
            if dialog.entity.bot: stats['bots'] += 1
            else: stats['users'] += 1
        elif dialog.is_group: stats['groups'] += 1
        elif dialog.is_channel: stats['channels'] += 1
        stats['unread'] += dialog.unread_count
    return stats

def load_active_groups(filename="active_groups.json"):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f).get("groups", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_active_groups(group_ids, filename="active_groups.json"):
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump({"groups": group_ids}, f, indent=4)
    except Exception as e:
        logging.error(f"Faol guruhlar faylini saqlashda xatolik: {e}")

async def delete_message_after_delay(command_message, reply_message=None, delay=15):
    await asyncio.sleep(delay)
    try:
        if command_message: await command_message.delete()
        if reply_message: await reply_message.delete()
    except Exception: pass

async def set_online_status(status: bool):
    global online_mode
    online_mode = status
    settings["online_mode"] = status
    save_settings(settings)
    logging.info(f"Online rejim o'zgartirildi: {online_mode}")

async def account_online_loop():
    while True:
        if online_mode: logging.debug("Account online holatda saqlanmoqda.")
        await asyncio.sleep(DEFAULT_SLEEP_TIME)

async def handle_online_command(event, status):
    if status == "on":
        await set_online_status(True)
        reply_message = await event.reply("✅ Account online rejimga o'tkazildi.")
    elif status == "off":
        await set_online_status(False)
        reply_message = await event.reply("☑️ Account offline rejimga o'tkazildi.")
    else:
        reply_message = await event.reply("Noto'g'ri buyruq. `on` yoki `off` ishlating.")
    asyncio.create_task(delete_message_after_delay(event, reply_message))

async def handle_admin_command(event, command):
    global allow_all_users, auto_reply_enabled
    if event.sender_id != my_telegram_id: return
    reply_message = None

    if command == "setuser all":
        allow_all_users, auto_reply_enabled = True, True
        settings.update({"allow_all_users": True, "auto_reply_enabled": True})
        reply_message = await event.reply("Barcha foydalanuvchilarga ruxsat berildi va avto-javob yoqildi.")
    elif command == "setuser off":
        allow_all_users, auto_reply_enabled = False, False
        settings.update({"allow_all_users": False, "auto_reply_enabled": False})
        reply_message = await event.reply("Faqat admin uchun ruxsatlar qoldirildi va avto-javob o'chirildi.")
    elif command == "statistika":
        stats = await get_account_stats()
        uzbek_time = datetime.now(pytz.timezone('Asia/Tashkent')).strftime("%Y-%m-%d %H:%M:%S")
        stats_msg = (f"📊 **Statistika**:\n\n"
                     f"👤 Foydalanuvchilar: {stats['users']}\n👥 Guruhlar: {stats['groups']}\n"
                     f"📢 Kanallar: {stats['channels']}\n🤖 Botlar: {stats['bots']}\n"
                     f"✉️ O'qilmagan: {stats['unread']}\n\n🕰️ Vaqt: {uzbek_time}")
        reply_message = await event.reply(stats_msg)
    elif command == "sendavto on":
        auto_reply_enabled = True
        settings["auto_reply_enabled"] = True
        reply_message = await event.reply("Avtomatik javob yoqildi!")
    elif command == "sendavto off":
        auto_reply_enabled = False
        settings["auto_reply_enabled"] = False
        reply_message = await event.reply("Avtomatik javob o'chirildi!")
    elif command.startswith("online "):
        await handle_online_command(event, command.split(" ", 1)[1].strip())
        return
    elif command == "active status":
        groups = []
        for gid in load_active_groups():
            try:
                entity = await client.get_entity(gid)
                groups.append(f"`{gid}`: {entity.title}")
            except Exception:
                groups.append(f"`{gid}`: Noma'lum")
        reply_message = await event.reply(f"**Faol guruhlar:**\n" + ("\n".join(groups) or "Yo'q"))
    elif command == "set active":
        active_groups = load_active_groups()
        if event.chat_id not in active_groups:
            active_groups.append(event.chat_id)
            save_active_groups(active_groups)
            reply_message = await event.reply(f"Ushbu guruh faol ro'yxatga qo'shildi!")
        else: reply_message = await event.reply("Bu guruh avvaldan faol!")
    elif command == "del active":
        active_groups = load_active_groups()
        if event.chat_id in active_groups:
            active_groups.remove(event.chat_id)
            save_active_groups(active_groups)
            reply_message = await event.reply(f"Ushbu guruh faol ro'yxatdan o'chirildi!")
        else: reply_message = await event.reply("Bu guruh faol ro'yxatda yo'q!")
    elif command == "clear history":
        file_path = get_chat_history_file_path(event.chat_id, event.sender_id, event.is_private)
        if os.path.exists(file_path):
            os.remove(file_path)
            reply_message = await event.reply("Suhbat tarixi o'chirildi.")
        else: reply_message = await event.reply("Suhbat tarixi topilmadi.")
    elif command == "del":
        deleted_count = 0
        async for msg in client.iter_messages(event.chat_id, from_user='me'):
            try:
                await msg.delete()
                deleted_count += 1
            except Exception: pass
        reply_message = await event.reply(f"Bu chatdagi {deleted_count} ta xabarim o'chirildi.")
    else: reply_message = await event.reply("Noto'g'ri admin buyrug'i!")
    
    save_settings(settings)
    asyncio.create_task(delete_message_after_delay(event, reply_message))

async def send_long_message(chat_id, text, reply_to=None, parse_mode="markdown"):
    for part in textwrap.wrap(text, MAX_MESSAGE_LENGTH, replace_whitespace=False):
        await client.send_message(chat_id, part, reply_to=reply_to, parse_mode=parse_mode)

async def handle_gemini_command(event, prompt):
    if event.sender_id != my_telegram_id and not allow_all_users: return
    thinking_message = await event.reply("Javob yozilmoqda... ⏳")
    response_text = await get_gemini_response(prompt, event.chat_id, event.sender_id, event.is_private)
    await thinking_message.delete()
    if response_text:
        await send_long_message(event.chat_id, response_text, reply_to=event.message.id)

async def handle_image_command(event, prompt):
    if event.sender_id != my_telegram_id and not allow_all_users: return
    await generate_image_with_progress(prompt, event)

async def search_for_reply(original_text: str, search_limit_per_chat=1000):
    logging.info(f"Detektivlik boshlandi: '{original_text}' uchun javob qidirilmoqda.")
    found_replies = []
    
    async for dialog in client.iter_dialogs(limit=100):
        if dialog.is_group and hasattr(dialog.entity, 'participants_count') and dialog.entity.participants_count > 200:
            try:
                async for message in client.iter_messages(dialog.id, search=original_text, limit=search_limit_per_chat):
                    if message.sender_id != my_telegram_id and message.reply_to_msg_id:
                        reply_message = await message.get_reply_message()
                        if reply_message and reply_message.sender_id != my_telegram_id:
                            found_replies.append(reply_message)
                            if len(found_replies) >= 5:
                                logging.info(f"5 ta mos javob topildi. Qidiruv to'xtatildi.")
                                return found_replies
            except Exception as e:
                logging.warning(f"'{dialog.title}' guruhini skanerlashda xatolik: {e}")
                
    logging.info(f"Detektivlik yakunlandi. Jami topilgan javoblar: {len(found_replies)} ta.")
    return found_replies

async def handle_auto_reply(event):
    if (not auto_reply_enabled or not event.is_group or 
        event.chat_id not in load_active_groups() or not event.text or 
        event.sender_id == my_telegram_id):
        return

    sender = await event.get_sender()
    if sender and sender.bot: return

    try:
        replied_msg = await event.get_reply_message()
        if not (replied_msg and replied_msg.from_id and replied_msg.from_id.user_id == my_telegram_id):
            return
    except Exception: return

    current_time = time.time()
    last_reply_time = user_reply_cooldown.get(event.sender_id, 0)
    if current_time - last_reply_time < COOLDOWN_SECONDS:
        logging.info(f"Anti-flood: {event.sender_id} IDli foydalanuvchiga javob berilmadi.")
        return
    user_reply_cooldown[event.sender_id] = current_time

    prompt = event.text.strip()

    async with client.action(event.chat_id, 'typing'):
        try:
            detective_task = asyncio.create_task(search_for_reply(prompt))
            found_replies = await asyncio.wait_for(detective_task, timeout=15.0)

            if found_replies:
                chosen_reply = random.choice(found_replies)
                logging.info("Detektiv muvaffaqiyatli. Topilgan javob yuborilmoqda.")
                await event.reply(chosen_reply)
                return
        except asyncio.TimeoutError:
            logging.info("Detektivlik vaqti tugadi. Javob topilmadi.")
        except Exception as e:
            logging.error(f"Detektivlik jarayonida kutilmagan xatolik: {e}", exc_info=True)
    
    logging.info("AIga murojaat qilinmoqda...")
    async with client.action(event.chat_id, 'typing'):
        response = await get_gemini_response(prompt, event.chat_id, event.sender_id, event.is_private)
        if response:
            await send_long_message(event.chat_id, response, reply_to=event.message.id)

async def _do_auto_send(chat_id, text, interval, count, original_msg_id):
    try:
        for i in range(count):
            await client.send_message(chat_id, text)
            if i < count - 1: await asyncio.sleep(interval)
        await client.send_message(chat_id, "**Avto-xabar yuborish yakunlandi.**", reply_to=original_msg_id)
    except asyncio.CancelledError:
        await client.send_message(chat_id, "**Avto-xabar yuborish to'xtatildi.**", reply_to=original_msg_id)
    except Exception as e:
        await client.send_message(chat_id, f"**Avto-xabar yuborishda xatolik:** {e}", reply_to=original_msg_id)
    finally:
        active_auto_send_tasks.pop(chat_id, None)

async def handle_auto_text_command(event: Message):
    if event.sender_id != my_telegram_id: return
    chat_id = event.chat_id
    if event.text.lower().strip() == ".text stop":
        if chat_id in active_auto_send_tasks: active_auto_send_tasks[chat_id].cancel()
        else: await event.reply("Bu chatda faol vazifa yo'q.")
        return
    match = re.match(r"^\.text\s+(\d+)\s+(\d+)\s+(.+)", event.text, re.DOTALL)
    if not match:
        reply_msg = await event.reply("Noto'g'ri format. `.text <interval> <count> <xabar>`")
        asyncio.create_task(delete_message_after_delay(event, reply_msg))
        return
    if chat_id in active_auto_send_tasks:
        reply_msg = await event.reply("Vazifa allaqachon faol. `.text stop` bilan to'xtating.")
        asyncio.create_task(delete_message_after_delay(event, reply_msg))
        return
    interval, count, msg_text = match.groups()
    task = asyncio.create_task(_do_auto_send(chat_id, msg_text, int(interval), int(count), event.id))
    active_auto_send_tasks[chat_id] = task
    await event.delete()

async def handle_info_command(event: Message):
    if event.sender_id != my_telegram_id or not event.reply_to_msg_id:
        await event.delete()
        return
    try:
        replied_msg = await event.get_reply_message()
        user = await client.get_entity(replied_msg.sender_id)
        
        status_text = "N/A"
        if user.status:
            status_text = user.status.__class__.__name__.replace("UserStatus", "")
            if hasattr(user.status, 'was_online'):
                 status_text += f" ({datetime.fromtimestamp(user.status.was_online).astimezone(pytz.timezone('Asia/Tashkent')).strftime('%Y-%m-%d %H:%M')})"
        
        info = (f"👤 **Foydalanuvchi Ma'lumotlari**\n\n"
                f"**ID:** `{user.id}`\n"
                f"**Ism:** {user.first_name or 'N/A'}\n"
                f"**Familiya:** {user.last_name or 'N/A'}\n"
                f"**Username:** @{user.username or 'Yo`q'}\n"
                f"**Bot:** {'Ha' if user.bot else 'Yo`q'}\n"
                f"**Status:** `{status_text}`\n"
                f"**Scam:** {'Ha' if user.scam else 'Yo`q'}\n"
                f"**Profilga havola:** [bu yerda](tg://user?id={user.id})")
        
        full_user = await client.get_entity(user)
        if hasattr(full_user, 'about') and full_user.about:
            info += f"\n\n**Bio:**\n`{full_user.about}`"
        
        await event.edit(info)
    except Exception as e:
        await event.edit(f"Ma'lumot olishda xatolik: {e}")

async def handle_help_command(event: Message):
    if event.sender_id != my_telegram_id: return
    help_text = """
    **🤖 Suii Userbot Boshqaruv Paneli**

    **🧠 AI & Rasm Generatsiyasi:**
    `.ai <so'rov>` - Gemini AI bilan suhbat.
    `.chatgpt <so'rov>` - `.ai` ning alternativi.
    `.pic <so'rov>` - Matndan rasm chizish (Pollinations AI).
    `.clear history` - Joriy chat uchun AI suhbat tarixini tozalash.

    **🛠️ Asosiy Buyruqlar:**
    `.text <vaqt> <soni> <xabar>` - Avtomatik xabar yuborish.
      * `<vaqt>` - sekundlarda interval.
      * `<soni>` - takrorlashlar soni.
    `.text stop` - Avtomatik xabar yuborishni to'xtatish.
    `.info` - Reply qilingan foydalanuvchi haqida ma'lumot olish.
    `.del` - Chatdagi o'zingizning barcha xabarlaringizni o'chirish.
    `.tosh` - Omadli (5 yoki 6) zar tushguncha urinish.

    **⚙️ Adminstrator Buyruqlari (`.adm` bilan):**
    `.adm setuser all` - Barcha foydalanuvchilarga ruxsat berish.
    `.adm setuser off` - Faqat admin (siz) uchun ishlash.
    `.adm sendavto on/off` - Guruhlarda avtomatik javob berishni yoqish/o'chirish.
    `.adm online on/off` - Doimiy "online" rejimini yoqish/o'chirish.
    `.adm statistika` - Akkaunt statistikasi.
    
    **🗣️ Faol Guruhlarni Boshqarish:**
    `.adm set active` - Joriy guruhni avto-javob uchun faollashtirish.
    `.adm del active` - Joriy guruhni faollar ro'yxatidan o'chirish.
    `.adm active status` - Barcha faol guruhlar ro'yxati.
    
    **ℹ️ Yordam:**
    `.help` - Ushbu yordam menyusini ko'rsatish.
    """
    reply_message = await event.reply(textwrap.dedent(help_text))
    asyncio.create_task(delete_message_after_delay(event, reply_message, delay=60))

async def handle_tosh_command(event: Message):
    """Omadli (5 yoki 6) tosh tushguncha 'o'chirib-yuborish' usulida ishlaydi."""
    if event.sender_id != my_telegram_id:
        return

    await event.delete() 

    max_tries = 20
    SAFE_INTERVAL = 1.0 

    for i in range(max_tries):
        sent_message = None
        try:
            sent_message = await client.send_message(
                event.chat_id, 
                file=InputMediaDice(emoticon='🎲')
            )
            
            await asyncio.sleep(0.8) 
            
            if sent_message.media and sent_message.media.value in [5, 6]:
                logging.info(f"Omadli tosh topildi! Natija: {sent_message.media.value}")
                return
            else:
                await sent_message.delete()
                await asyncio.sleep(SAFE_INTERVAL)

        except Exception as e:
            logging.error(f"Tosh yuborish/o'chirishda xatolik: {e}", exc_info=True)
            if sent_message:
                try:
                    await sent_message.delete()
                except Exception:
                    pass
            return

    logging.warning(f"{max_tries} urinishda omadli tosh topilmadi.")

@client.on(events.NewMessage)
async def my_event_handler(event: Message):
    if not (event.is_private or event.is_group): return
    try:
        text_lower = event.text.lower() if event.text else ""
        if text_lower.startswith(".adm "): await handle_admin_command(event, event.text[5:].strip())
        elif text_lower.startswith(".text"): await handle_auto_text_command(event)
        elif text_lower.startswith((".ai ", ".chatgpt ")):
            prefix_len = 4 if text_lower.startswith(".ai ") else 9
            await handle_gemini_command(event, event.text[prefix_len:].strip())
        elif text_lower.startswith(".pic "): await handle_image_command(event, event.text[5:].strip())
        elif text_lower == ".info": await handle_info_command(event)
        elif text_lower == ".help": await handle_help_command(event)
        elif text_lower == ".tosh": await handle_tosh_command(event) 
        elif event.text: await handle_auto_reply(event)
    except Exception as e:
        masked_error = mask_sensitive_info(str(e), gemini_api_key, GEMINI_BASE_API_URL)
        logging.error(f"Xatolik yuz berdi: {masked_error}", exc_info=True)

async def main():
    try:
        create_chat_history_dir()
        await client.start()
        logging.info("Bot ishga tushirildi.")
        me = await client.get_me()
        global my_telegram_id
        if not my_telegram_id: my_telegram_id = me.id
        logging.info(f"Userbot {me.first_name} (@{me.username}) nomi bilan ishlamoqda. ID: {my_telegram_id}")
        await asyncio.gather(client.run_until_disconnected(), account_online_loop())
    except Exception as e:
        logging.critical(f"Bot ishga tushirishda kutilmagan xatolik: {e}", exc_info=True)
    finally:
        logging.info("Bot to'xtatildi.")
        if client.is_connected(): await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot foydalanuvchi tomonidan to'xtatildi.")