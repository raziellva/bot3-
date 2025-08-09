import os
import logging
import asyncio
import threading
import concurrent.futures
from pyrogram import Client, filters
import random
import string
import datetime
import subprocess
from pyrogram.types import (Message, InlineKeyboardButton, 
                           InlineKeyboardMarkup, ReplyKeyboardMarkup, 
                           KeyboardButton, CallbackQuery)
from pyrogram.errors import MessageNotModified
import ffmpeg
import re
import time
from pymongo import MongoClient
from config import *
from bson.objectid import ObjectId

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Diccionario de prioridades por plan
PLAN_PRIORITY = {
    "premium": 1,
    "pro": 2,
    "standard": 3,
    "free": 4
}

# Conexión a MongoDB
mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DATABASE_NAME]
pending_col = db["pending"]
users_col = db["users"]
temp_keys_col = db["temp_keys"]
banned_col = db["banned_users"]
pending_confirmations_col = db["pending_confirmations"]
active_compressions_col = db["active_compressions"]

# Configuración del bot
api_id = API_ID
api_hash = API_HASH
bot_token = BOT_TOKEN

app = Client(
    "compress_bot",
    api_id=api_id,
    api_hash=api_hash,
    bot_token=bot_token
)

# Administradores del bot
admin_users = ADMINS_IDS
ban_users = []

# Cargar usuarios baneados y limpiar compresiones activas al iniciar
banned_users_in_db = banned_col.find({}, {"user_id": 1})
for banned_user in banned_users_in_db:
    if banned_user["user_id"] not in ban_users:
        ban_users.append(banned_user["user_id"])

# Limpiar compresiones activas previas al iniciar
active_compressions_col.delete_many({})
logger.info("Compresiones activas previas eliminadas")

# Configuración de compresión de video
video_settings = {
    'resolution': '854x480',
    'crf': '28',
    'audio_bitrate': '64k',
    'fps': '20',
    'preset': 'veryfast',
    'codec': 'libx264'
}

# Variables globales para la cola
compression_queue = asyncio.PriorityQueue()
processing_task = None
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

# ======================== GESTIÓN DE COMPRESIONES ACTIVAS ======================== #

async def has_active_compression(user_id: int) -> bool:
    """Verifica si el usuario ya tiene una compresión activa"""
    return bool(active_compressions_col.find_one({"user_id": user_id}))

async def add_active_compression(user_id: int, file_id: str):
    """Registra una nueva compresión activa"""
    active_compressions_col.insert_one({
        "user_id": user_id,
        "file_id": file_id,
        "start_time": datetime.datetime.now()
    })

async def remove_active_compression(user_id: int):
    """Elimina una compresión activa"""
    active_compressions_col.delete_one({"user_id": user_id})

# ======================== SISTEMA DE CONFIRMACIÓN ======================== #

async def has_pending_confirmation(user_id: int) -> bool:
    """Verifica si el usuario tiene una confirmación pendiente"""
    return bool(pending_confirmations_col.find_one({"user_id": user_id}))

async def create_confirmation(user_id: int, chat_id: int, message_id: int, file_id: str, file_name: str):
    """Crea una nueva confirmación pendiente"""
    return pending_confirmations_col.insert_one({
        "user_id": user_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "file_id": file_id,
        "file_name": file_name,
        "timestamp": datetime.datetime.now()
    }).inserted_id

async def delete_confirmation(confirmation_id: ObjectId):
    """Elimina una confirmación pendiente"""
    pending_confirmations_col.delete_one({"_id": confirmation_id})

async def get_confirmation(confirmation_id: ObjectId):
    """Obtiene una confirmación pendiente"""
    return pending_confirmations_col.find_one({"_id": confirmation_id})

# ======================== AUTO-REGISTRO DE USUARIOS ======================== #

async def register_new_user(user_id: int):
    """Registra un nuevo usuario con plan free si no existe"""
    if not users_col.find_one({"user_id": user_id}):
        users_col.insert_one({
            "user_id": user_id,
            "plan": "free",
            "used": 0,
            "daily_used": 0,
            "last_used_date": None,
            "join_date": datetime.datetime.now()
        })
        logger.info(f"👤 Nuevo usuario registrado: {user_id} (Plan Free)")

# ======================== FUNCIONES PROTECCIÓN DE CONTENIDO ======================== #

async def should_protect_content(user_id: int) -> bool:
    """Determina si el contenido debe protegerse según el plan del usuario"""
    await register_new_user(user_id)
    if user_id in admin_users:
        return False
    user_plan = await get_user_plan(user_id)
    return user_plan["plan"] in ["free", "standard"]

async def send_protected_message(chat_id: int, text: str, **kwargs):
    """Envía un mensaje con protección según el plan del usuario"""
    protect = await should_protect_content(chat_id)
    return await app.send_message(chat_id, text, protect_content=protect, **kwargs)

async def send_protected_video(chat_id: int, video: str, caption: str = None, **kwargs):
    """Envía un video con protección según el plan del usuario"""
    protect = await should_protect_content(chat_id)
    return await app.send_video(chat_id, video, caption=caption, protect_content=protect, **kwargs)

async def send_protected_photo(chat_id: int, photo: str, caption: str = None, **kwargs):
    """Envía una foto con protección según el plan del usuario"""
    protect = await should_protect_content(chat_id)
    return await app.send_photo(chat_id, photo, caption=caption, protect_content=protect, **kwargs)

# ======================== SISTEMA DE PRIORIDAD EN COLA ======================== #

async def get_user_priority(user_id: int) -> int:
    """Obtiene la prioridad del usuario basada en su plan"""
    user_plan = await get_user_plan(user_id)
    return PLAN_PRIORITY.get(user_plan["plan"], 4)

# ======================== SISTEMA DE CLAVES TEMPORALES ======================== #

def generate_temp_key(plan: str, duration_days: int):
    """Genera una clave temporal válida para un plan específico"""
    key = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
    created_at = datetime.datetime.now()
    expires_at = created_at + datetime.timedelta(days=duration_days)
    
    temp_keys_col.insert_one({
        "key": key,
        "plan": plan,
        "created_at": created_at,
        "expires_at": expires_at,
        "used": False,
        "duration_days": duration_days
    })
    
    return key

def is_valid_temp_key(key):
    """Verifica si una clave temporal es válida"""
    now = datetime.datetime.now()
    key_data = temp_keys_col.find_one({
        "key": key,
        "used": False,
        "expires_at": {"$gt": now}
    })
    return bool(key_data)

def mark_key_used(key):
    """Marca una clave como usada"""
    temp_keys_col.update_one({"key": key}, {"$set": {"used": True}})

@app.on_message(filters.command("generatekey") & filters.user(admin_users))
async def generate_key_command(client, message):
    """Genera una nueva clave temporal para un plan específico (solo admins)"""
    try:
        parts = message.text.split()
        if len(parts) != 3:
            await message.reply("⚠️ Formato: /generatekey <plan> <días>\nEjemplo: /generatekey standard 7")
            return
            
        plan = parts[1].lower()
        valid_plans = ["standard", "pro", "premium"]
        if plan not in valid_plans:
            await message.reply(f"⚠️ Plan inválido. Opciones válidas: {', '.join(valid_plans)}")
            return
            
        try:
            duration_days = int(parts[2])
            if duration_days <= 0:
                await message.reply("⚠️ Los días deben ser un número positivo")
                return
        except ValueError:
            await message.reply("⚠️ Días debe ser un número entero")
            return

        key = generate_temp_key(plan, duration_days)
        await message.reply(
            f"🔑 **Clave {plan.capitalize()} generada**\n\n"
            f"Clave: `{key}`\n"
            f"Válida por: {duration_days} días\n\n"
            f"Comparte esta clave con el usuario usando:\n"
            f"`/access {key}`"
        )
    except Exception as e:
        logger.error(f"Error generando clave: {e}", exc_info=True)
        await message.reply("⚠️ Error al generar la clave")

@app.on_message(filters.command("listkeys") & filters.user(admin_users))
async def list_keys_command(client, message):
    """Lista todas las claves temporales activas (solo admins)"""
    try:
        now = datetime.datetime.now()
        keys = list(temp_keys_col.find({"used": False, "expires_at": {"$gt": now}}))
        
        if not keys:
            await message.reply("📭 **No hay claves activas.**")
            return
            
        response = "🔑 **Claves temporales activas:**\n\n"
        for key in keys:
            expires_at = key["expires_at"]
            remaining = expires_at - now
            days = remaining.days
            hours = remaining.seconds // 3600
            minutes = (remaining.seconds % 3600) // 60
            
            response += (
                f"• `{key['key']}`\n"
                f"  ↳ Plan: {key['plan'].capitalize()}\n"
                f"  ↳ Duración: {key['duration_days']} días\n"
                f"  ⏱ Expira en: {days}d {hours}h {minutes}m\n\n"
            )
            
        await message.reply(response)
    except Exception as e:
        logger.error(f"Error listando claves: {e}", exc_info=True)
        await message.reply("⚠️ Error al listar claves")

# ======================== SISTEMA DE PLANES ======================== #

PLAN_LIMITS = {
    "free": 1,
    "standard": 40,
    "pro": 90,
    "premium": 200
}

PLAN_DURATIONS = {
    "free": "1 día",
    "standard": "7 días",
    "pro": "15 días",
    "premium": "30 días"
}

async def get_user_plan(user_id: int) -> dict:
    """Obtiene el plan del usuario desde la base de datos con campos garantizados"""
    await register_new_user(user_id)
    
    user = users_col.find_one({"user_id": user_id})
    if not user:
        new_user = {
            "user_id": user_id,
            "plan": "free",
            "used": 0,
            "daily_used": 0,
            "last_used_date": None,
            "join_date": datetime.datetime.now()
        }
        users_col.insert_one(new_user)
        return new_user
    else:
        update_data = {}
        if "used" not in user:
            update_data["used"] = 0
        if "plan" not in user:
            update_data["plan"] = "free"
        if "daily_used" not in user:
            update_data["daily_used"] = 0
        if "last_used_date" not in user:
            update_data["last_used_date"] = None
        
        if update_data:
            users_col.update_one({"user_id": user_id}, {"$set": update_data})
            user.update(update_data)
        
        return user

async def increment_user_usage(user_id: int):
    """Incrementa el contador de uso del usuario"""
    user = await get_user_plan(user_id)
    
    if user["plan"] == "free":
        users_col.update_one({"user_id": user_id}, {"$inc": {"daily_used": 1}})
    else:
        users_col.update_one({"user_id": user_id}, {"$inc": {"used": 1}})

async def reset_user_usage(user_id: int):
    """Resetea el contador de uso del usuario según su plan"""
    user = await get_user_plan(user_id)
    
    if user["plan"] == "free":
        update_data = {
            "daily_used": 0,
            "last_used_date": datetime.datetime.now()
        }
    else:
        update_data = {"used": 0}
    
    users_col.update_one({"user_id": user_id}, {"$set": update_data})

async def set_user_plan(user_id: int, plan: str):
    """Establece el plan de un usuario y notifica"""
    if plan not in PLAN_LIMITS:
        return False
    user = await get_user_plan(user_id)
    users_col.update_one({"user_id": user_id}, {"$set": {"plan": plan}})
    
    # Notificar al usuario sobre su nuevo plan
    try:
        await send_protected_message(
            user_id,
            f"🎉 **¡Se te ha asignado un nuevo plan!**\n\n"
            f"• **Plan**: {plan.capitalize()}\n"
            f"• **Duración**: {PLAN_DURATIONS[plan]}\n"
            f"• **Videos disponibles**: {PLAN_LIMITS[plan]}\n\n"
            f"¡Disfruta de tus beneficios! 🎬"
        )
    except Exception as e:
        logger.error(f"Error notificando al usuario {user_id}: {e}")
    
    return True

async def check_user_limit(user_id: int) -> bool:
    """Verifica si el usuario ha alcanzado su límite de compresión"""
    user = await get_user_plan(user_id)
    plan_type = user.get("plan", "free")
    
    if plan_type == "free":
        today = datetime.datetime.now().date()
        last_used = user.get("last_used_date")
        
        if last_used is None or last_used.date() < today:
            users_col.update_one(
                {"user_id": user_id},
                {"$set": {"daily_used": 0, "last_used_date": datetime.datetime.now()}}
            )
            return False
        
        daily_used = user.get("daily_used", 0)
        return daily_used >= PLAN_LIMITS["free"]
    
    used_count = user.get("used", 0)
    return used_count >= PLAN_LIMITS.get(plan_type, PLAN_LIMITS["free"])

async def get_plan_info(user_id: int) -> str:
    """Obtiene información del plan del usuario para mostrar"""
    user = await get_user_plan(user_id)
    plan_name = user["plan"].capitalize()
    
    if user["plan"] == "free":
        user_data = users_col.find_one({"user_id": user_id})
        today = datetime.datetime.now().date()
        last_used = user_data.get("last_used_date")
        daily_used = user_data.get("daily_used", 0)
        
        if last_used is None or last_used.date() < today:
            daily_used = 0
        
        return (
            f"╭✠━━━━━━━━━━━━━━━━━━✠╮\n"
            f"┠ **Plan actual**: {plan_name}\n"
            f"┠ **Videos usados hoy**: {daily_used}/1\n"
            f"╰✠━━━━━━━━━━━━━━━━━━✠╯"
        )
    
    used = user.get("used", 0)
    limit = PLAN_LIMITS[user["plan"]]
    remaining = max(0, limit - used)
    
    percent = min(100, (used / limit) * 100) if limit > 0 else 0
    bar_length = 15
    filled = int(bar_length * percent / 100)
    bar = '⬢' * filled + '⬡' * (bar_length - filled)
    
    return (
        f"╭✠━━━━━━━━━━━━━━━━━━✠╮\n"
        f"┠ **Plan actual**: {plan_name}\n"
        f"┠ **Videos usados**: {used}/{limit}\n"
        f"┠ **Restantes**: {remaining}\n"
        f"┠ **Progreso**: [{bar}] {int(percent)}%\n"
        f"╰✠━━━━━━━━━━━━━━━━━━✠╯"
    )

# ======================== FUNCIÓN PARA VERIFICAR VÍDEOS EN COLA ======================== #

async def has_pending_in_queue(user_id: int) -> bool:
    """Verifica si el usuario tiene videos pendientes en la cola"""
    count = pending_col.count_documents({"user_id": user_id})
    return count > 0

# ======================== FIN SISTEMA DE PLANES ======================== #

def sizeof_fmt(num, suffix="B"):
    """Formatea el tamaño de bytes a formato legible"""
    for unit in ["", "K", "M", "G", "T", "P", "E", "Z"]:
        if abs(num) < 1024.0:
            return "%3.2f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.2f%s%s" % (num, "Yi", suffix)

def create_progress_bar(current, total, proceso, length=15):
    """Crea una barra de progreso visual"""
    if total == 0:
        total = 1
    percent = current / total
    filled = int(length * percent)
    bar = '⬢' * filled + '⬡' * (length - filled)
    return (
        f'    ╭━━━[🤖**Compress Bot**]━━━╮\n'
        f'┠ [{bar}] {round(percent * 100)}%\n'
        f'┠ **Procesado**: {sizeof_fmt(current)}/{sizeof_fmt(total)}\n'
        f'┠ **Estado**: __#{proceso}__'
    )

last_progress_update = {}

async def progress_callback(current, total, msg, proceso, start_time):
    """Callback para mostrar progreso de descarga/subida"""
    try:
        now = datetime.datetime.now()
        key = (msg.chat.id, msg.id)
        last_time = last_progress_update.get(key)

        if last_time and (now - last_time).total_seconds() < 5:
            return

        last_progress_update[key] = now

        elapsed = time.time() - start_time
        percentage = current / total
        speed = current / elapsed if elapsed > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0

        progress_bar = create_progress_bar(current, total, proceso)
        try:
            await msg.edit(
                f"   {progress_bar}\n"
                f"┠ **Velocidad** {sizeof_fmt(speed)}/s\n"
                f"┠ **Tiempo restante:** {int(eta)}s\n╰━━━━━━━━━━━━━━━━━━╯\n"
            )
        except MessageNotModified:
            pass
    except Exception as e:
        logger.error(f"Error en progress_callback: {e}", exc_info=True)

# ======================== FUNCIONALIDAD DE COLA CON PRIORIDAD ======================== #

async def process_compression_queue():
    while True:
        priority, timestamp, (client, message, wait_msg) = await compression_queue.get()
        try:
            start_msg = await wait_msg.edit("🗜️**Iniciando compresión**🎬")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(executor, threading_compress_video, client, message, start_msg)
        except Exception as e:
            logger.error(f"Error procesando video: {e}", exc_info=True)
            await app.send_message(message.chat.id, f"⚠️ Error al procesar el video: {str(e)}")
        finally:
            pending_col.delete_one({"video_id": message.video.file_id})
            compression_queue.task_done()

def threading_compress_video(client, message, start_msg):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(compress_video(client, message, start_msg))
    loop.close()

@app.on_message(filters.command(["deleteall"]) & filters.user(admin_users))
async def delete_all_pending(client, message):
    result = pending_col.delete_many({})
    await message.reply(f"🗑️ **Cola eliminada.**\n**Se eliminaron {result.deleted_count} elementos.**")

@app.on_message(filters.regex(r"^/del_(\d+)$") & filters.user(admin_users))
async def delete_one_from_pending(client, message):
    match = message.text.strip().split("_")
    if len(match) != 2 or not match[1].isdigit():
        await message.reply("⚠️ Formato inválido. Usa `/del_1`, `/del_2`, etc.")
        return

    index = int(match[1]) - 1
    cola = list(pending_col.find().sort([("priority", 1), ("timestamp", 1)]))

    if index < 0 or index >= len(cola):
        await message.reply("⚠️ Número fuera de rango.")
        return

    eliminado = cola[index]
    pending_col.delete_one({"_id": eliminado["_id"]})

    file_name = eliminado.get("file_name", "¿?")
    user_id = eliminado["user_id"]
    tiempo = eliminado.get("timestamp")
    tiempo_str = tiempo.strftime("%Y-%m-%d %H:%M:%S") if tiempo else "¿?"

    await message.reply(
        f"✅ Eliminado de la cola:\n"
        f"📁 {file_name}\n👤 ID: `{user_id}`\n⏰ {tiempo_str}"
    )

async def show_queue(client, message):
    """Muestra la cola de compresión"""
    cola = list(pending_col.find().sort([("priority", 1), ("timestamp", 1)]))

    if not cola:
        await message.reply("📭 **La cola está vacía.**")
        return

    priority_to_plan = {v: k for k, v in PLAN_PRIORITY.items()}

    respuesta = "📋 **Cola de Compresión Activa (Priorizada)**\n\n"
    for i, item in enumerate(cola, 1):
        user_id = item["user_id"]
        file_name = item.get("file_name", "¿?")
        tiempo = item.get("timestamp")
        tiempo_str = tiempo.strftime("%H:%M:%S") if tiempo else "¿?"
        
        priority = item.get("priority", 4)
        plan_name = priority_to_plan.get(priority, "free").capitalize()
        
        respuesta += f"{i}. 👤 ID: `{user_id}` | 📁 {file_name} | ⏰ {tiempo_str} | 📋 {plan_name}\n"

    await message.reply(respuesta)

@app.on_message(filters.command("cola") & filters.user(admin_users))
async def ver_cola_command(client, message):
    await show_queue(client, message)

@app.on_message(filters.command("auto") & filters.user(admin_users))
async def startup_command(_, message):
    global processing_task
    msg = await message.reply("🔄 Iniciando procesamiento de la cola...")

    pending_col.update_many(
        {"priority": {"$exists": False}},
        {"$set": {"priority": 4}}
    )

    pendientes = pending_col.find().sort([("priority", 1), ("timestamp", 1)])
    for item in pendientes:
        try:
            user_id = item["user_id"]
            chat_id = item["chat_id"]
            message_id = item["message_id"]
            priority = item.get("priority", 4)
            timestamp = item["timestamp"]
            
            message = await app.get_messages(chat_id, message_id)
            wait_msg = await app.send_message(chat_id, f"🔄 Recuperado desde cola persistente.")
            
            await compression_queue.put((priority, timestamp, (app, message, wait_msg)))
        except Exception as e:
            logger.error(f"Error cargando pendiente: {e}")

    if processing_task is None or processing_task.done():
        processing_task = asyncio.create_task(process_compression_queue())
    await msg.edit("✅ Procesamiento de cola iniciado.")

# ======================== FIN FUNCIONALIDAD DE COLA ======================== #

def update_video_settings(command: str):
    try:
        settings = command.split()
        for setting in settings:
            key, value = setting.split('=')
            video_settings[key] = value
        logger.info(f"⚙️Configuración actualizada⚙️: {video_settings}")
    except Exception as e:
        logger.error(f"Error actualizando configuración: {e}", exc_info=True)

def create_compression_bar(percent, bar_length=10):
    try:
        percent = max(0, min(100, percent))
        filled_length = int(bar_length * percent / 100)
        bar = '⬢' * filled_length + '⬡' * (bar_length - filled_length)
        return f"[{bar}] {int(percent)}%"
    except Exception as e:
        logger.error(f"Error creando barra de progreso: {e}", exc_info=True)
        return f"**Progreso**: {int(percent)}%"

async def compress_video(client, message: Message, start_msg):
    try:
        if not message.video:
            await app.send_message(chat_id=message.chat.id, text="Por favor envía un vídeo válido")
            return

        logger.info(f"Iniciando compresión para chat_id: {message.chat.id}, video: {message.video.file_name}")

        # Registrar compresión activa
        await add_active_compression(message.from_user.id, message.video.file_id)

        msg = await app.send_message(
            chat_id=message.chat.id,
            text="╭✠╼━━━━━━━━━━━━✠╮\n   ┠🗜️𝗗𝗲𝘀𝗰𝗮𝗿𝗴𝗮𝗻𝗱𝗼 𝗩𝗶𝗱𝗲𝗼🎬\n╰✠╼━━━━━━━━━━━━✠╯"
        )
        
        try:
            start_download_time = time.time()
            original_video_path = await app.download_media(
                message.video,
                progress=progress_callback,
                progress_args=(msg, "DESCARGA", start_download_time)
            )
            logger.info(f"Video descargado: {original_video_path}")
        except Exception as e:
            logger.error(f"Error en descarga: {e}", exc_info=True)
            await msg.edit(f"Error en descarga: {e}")
            await remove_active_compression(message.from_user.id)
            return
        
        original_size = os.path.getsize(original_video_path)
        logger.info(f"Tamaño original: {original_size} bytes")
        await notify_group(client, message, original_size, status="start")
        
        try:
            probe = ffmpeg.probe(original_video_path)
            dur_total = float(probe['format']['duration'])
            logger.info(f"Duración del video: {dur_total} segundos")
        except Exception as e:
            logger.error(f"Error obteniendo duración: {e}", exc_info=True)
            dur_total = 0

        await msg.edit(f"🗜️**INICIANDO COMPRESIÓN..**📥\n"
                      f"📦 Tamaño original: {original_size // (1024 * 1024)} MB")
        
        compressed_video_path = f"{os.path.splitext(original_video_path)[0]}_compressed.mp4"
        logger.info(f"Ruta de compresión: {compressed_video_path}")
        
        progress_message = "╭✠╼━━━━━━━━━━━━━━━✠╮\n┠🗜️𝗖𝗼𝗺𝗽𝗿𝗶𝗺𝗶𝗲𝗻𝗱𝗼 𝗩𝗶𝗱𝗲𝗼🎬\n╰✠╼━━━━━━━━━━━━━━━✠╯\n\n"
        await msg.edit(f"{progress_message}Preparando compresión...")

        drawtext_filter = f"drawtext=text='@InfiniteNetwork_KG':x=w-tw-10:y=10:fontsize=20:fontcolor=white"

        ffmpeg_command = [
            'ffmpeg', '-y', '-i', original_video_path,
            '-vf', f"scale={video_settings['resolution']},{drawtext_filter}",
            '-crf', video_settings['crf'],
            '-b:a', video_settings['audio_bitrate'],
            '-r', video_settings['fps'],
            '-preset', video_settings['preset'],
            '-c:v', video_settings['codec'],
            compressed_video_path
        ]
        logger.info(f"Comando FFmpeg: {' '.join(ffmpeg_command)}")

        try:
            start_time = datetime.datetime.now()
            process = subprocess.Popen(ffmpeg_command, stderr=subprocess.PIPE, text=True, bufsize=1)

            progress_message = "╭✠╼━━━━━━━━━━━━━━━✠╮\n┠🗜️𝗖𝗼𝗺𝗺𝗽𝗿𝗶𝗺𝗶𝗲𝗻𝗱𝗼 𝗩𝗶𝗱𝗲𝗼🎬\n╰✠╼━━━━━━━━━━━━━━━✠╯\n\n"
            last_percent = 0
            last_update_time = 0
            time_pattern = re.compile(r"time=(\d+:\d+:\d+\.\d+)")
            
            while True:
                line = process.stderr.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    match = time_pattern.search(line)
                    if match and dur_total > 0:
                        time_str = match.group(1)
                        h, m, s = time_str.split(':')
                        current_time = int(h)*3600 + int(m)*60 + float(s)
                        percent = min(100, (current_time / dur_total) * 100)
                        
                        if percent - last_percent >= 5:
                            bar = create_compression_bar(percent)
                            try:
                                await msg.edit(f"{progress_message}**Progreso**: {bar}")
                            except MessageNotModified:
                                pass
                            last_percent = percent
                            last_update_time = time.time()

            compressed_size = os.path.getsize(compressed_video_path)
            logger.info(f"Compresión completada. Tamaño comprimido: {compressed_size} bytes")
            
            try:
                probe = ffmpeg.probe(compressed_video_path)
                duration = int(float(probe.get('format', {}).get('duration', 0)))
                if duration == 0:
                    for stream in probe.get('streams', []):
                        if 'duration' in stream:
                            duration = int(float(stream['duration']))
                            break
                if duration == 0:
                    duration = 0
                logger.info(f"Duración del video comprimido: {duration} segundos")
            except Exception as e:
                logger.error(f"Error obteniendo duración comprimido: {e}", exc_info=True)
                duration = 0

            thumbnail_path = f"{compressed_video_path}_thumb.jpg"
            try:
                (
                    ffmpeg
                    .input(compressed_video_path, ss=duration//2 if duration > 0 else 0)
                    .filter('scale', 320, -1)
                    .output(thumbnail_path, vframes=1)
                    .overwrite_output()
                    .run(capture_stdout=True, capture_stderr=True)
                )
                logger.info(f"Miniatura generada: {thumbnail_path}")
            except Exception as e:
                logger.error(f"Error generando miniatura: {e}", exc_info=True)
                thumbnail_path = None

            processing_time = datetime.datetime.now() - start_time
            processing_time_str = str(processing_time).split('.')[0]
            
            description = (
                "╭✠╼━━━━━━━━━━━━━━━━━━━━✠╮\n"
                f"┠𝗧𝗶𝗲𝗺𝗽𝗼 𝗱𝗲 𝗽𝗿𝗼𝗰𝗲𝘀𝗮𝗺𝗶𝗲𝗻𝘁𝗼: {processing_time_str}\n"
                "╰✠╼━━━━━━━━━━━━━━━━━━━━✠╯\n"
                "╭✠╼━━━━━━━━━━━━━✠╮\n"
                f"┠⚙️𝗖𝗼𝗻𝗳𝗶𝗴𝘂𝗿𝗮𝗰𝗶𝗼𝗻 𝘂𝘀𝗮𝗱𝗮⚙️\n"
                f"┠**Resolución**:  {video_settings['resolution']}\n┠**CRF**: {video_settings['crf']} | **FPS**: {video_settings['fps']}\n"
                f"╰✠╼━━━━━━━━━━━━━✠╯\n"
            )
            
            try:
                start_upload_time = time.time()
                upload_msg = await app.send_message(chat_id=message.chat.id, text="⏫ **Subiendo video comprimido** 📤")
                
                if thumbnail_path and os.path.exists(thumbnail_path):
                    await send_protected_video(
                        chat_id=message.chat.id,
                        video=compressed_video_path,
                        caption=description,
                        thumb=thumbnail_path,
                        duration=duration,
                        reply_to_message_id=message.id,
                        progress=progress_callback,
                        progress_args=(upload_msg, "SUBIDA", start_upload_time)
                    )
                else:
                    await send_protected_video(
                        chat_id=message.chat.id,
                        video=compressed_video_path,
                        caption=description,
                        duration=duration,
                        reply_to_message_id=message.id,
                        progress=progress_callback,
                        progress_args=(upload_msg, "SUBIDA", start_upload_time)
                    )
                
                await upload_msg.delete()
                logger.info("✅ Video comprimido enviado como respuesta al original")
                await notify_group(client, message, original_size, compressed_size=compressed_size, status="done")
                await increment_user_usage(message.from_user.id)

                try:
                    await start_msg.delete()
                    logger.info("Mensaje 'Iniciando compresión' eliminado")
                except Exception as e:
                    logger.error(f"Error eliminando mensaje de inicio: {e}")

                try:
                    await msg.delete()
                    logger.info("Mensaje de progreso eliminado")
                except Exception as e:
                    logger.error(f"Error eliminando mensaje de progreso: {e}")

            except Exception as e:
                logger.error(f"Error enviando video: {e}", exc_info=True)
                await app.send_message(chat_id=message.chat.id, text="⚠️ **Error al enviar el video comprimido**")
                
        except Exception as e:
            logger.error(f"Error en compresión: {e}", exc_info=True)
            await msg.delete()
            await app.send_message(chat_id=message.chat.id, text=f"Ocurrió un error al comprimir el video: {e}")
        finally:
            try:
                for file_path in [original_video_path, compressed_video_path]:
                    if file_path and os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"Archivo temporal eliminado: {file_path}")
                if 'thumbnail_path' in locals() and thumbnail_path and os.path.exists(thumbnail_path):
                    os.remove(thumbnail_path)
                    logger.info(f"Miniatura eliminada: {thumbnail_path}")
            except Exception as e:
                logger.error(f"Error eliminando archivos temporales: {e}", exc_info=True)
    except Exception as e:
        logger.critical(f"Error crítico en compress_video: {e}", exc_info=True)
        await app.send_message(chat_id=message.chat.id, text="⚠️ Ocurrió un error crítico al procesar el video")
    finally:
        await remove_active_compression(message.from_user.id)

# ======================== INTERFAZ DE USUARIO ======================== #

# Teclado principal
def get_main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("⚙️ Settings"), KeyboardButton("📋 Planes")],
            [KeyboardButton("📊 Mi Plan"), KeyboardButton("ℹ️ Ayuda")],
            [KeyboardButton("👀 Ver Cola")]  # NUEVO BOTÓN AQUÍ
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

@app.on_message(filters.command("settings") & filters.private)
async def settings_menu(client, message):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗜️Compresión General🔧", callback_data="general")],
        [InlineKeyboardButton("📱 Reels y Videos cortos", callback_data="reels")],
        [InlineKeyboardButton("📺 Shows/Reality", callback_data="show")],
        [InlineKeyboardButton("🎬 Anime y series animadas", callback_data="anime")]
    ])

    await send_protected_message(
        message.chat.id, 
        "⚙️𝗦𝗲𝗹𝗲𝗰𝗰𝗶𝗼𝗻𝗮𝗿 𝗖𝗮𝗹𝗶𝗱𝗮𝗱⚙️", 
        reply_markup=keyboard
    )

# ======================== COMANDOS DE PLANES ======================== #

def get_plan_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧩 Estándar", callback_data="plan_standard")],
        [InlineKeyboardButton("💎 Pro", callback_data="plan_pro")],
        [InlineKeyboardButton("👑 Premium", callback_data="plan_premium")]
    ])

async def get_plan_menu(user_id: int):
    user = await get_user_plan(user_id)
    plan_name = user["plan"].capitalize()
    
    if user["plan"] == "free":
        user_data = users_col.find_one({"user_id": user_id})
        today = datetime.datetime.now().date()
        last_used = user_data.get("last_used_date")
        daily_used = user_data.get("daily_used", 0)
        
        if last_used is None or last_used.date() < today:
            daily_used = 0
        
        return (
            f"╭✠━━━━━━━━━━━━━━━━━━━━━━✠╮\n"
            f"┠ **Tu plan actual**: {plan_name}\n"
            f"┠ **Videos usados hoy**: {daily_used}/1\n"
            f"╰✠━━━━━━━━━━━━━━━━━━━━━━✠╯\n\n"
            "📋 **Selecciona un plan para más información:**"
        ), get_plan_menu_keyboard()
    
    used = user.get("used", 0)
    limit = PLAN_LIMITS[user["plan"]]
    remaining = max(0, limit - used)
    
    return (
        f"╭✠━━━━━━━━━━━━━━━━━━━━━━✠╮\n"
        f"┠ **Tu plan actual**: {plan_name}\n"
        f"┠ **Videos usados**: {used}/{limit}\n"
        f"┠ **Restantes**: {remaining}\n"
        f"╰✠━━━━━━━━━━━━━━━━━━━━━━✠╯\n\n"
        "📋 **Selecciona un plan para más información:**"
    ), get_plan_menu_keyboard()

@app.on_message(filters.command("planes") & filters.private)
async def planes_command(client, message):
    try:
        texto, keyboard = await get_plan_menu(message.from_user.id)
        await send_protected_message(
            message.chat.id, 
            texto, 
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error en planes_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id, 
            "⚠️ Error al mostrar los planes"
        )

# ======================== MANEJADOR DE CALLBACKS ======================== #

@app.on_callback_query()
async def callback_handler(client, callback_query: CallbackQuery):
    config_map = {
        "general": "resolution=720x480 crf=28 audio_bitrate=70k fps=22 preset=veryfast codec=libx264",
        "reels": "resolution=420x720 crf=25 audio_bitrate=70k fps=30 preset=veryfast codec=libx264",
        "show": "resolution=854x480 crf=35 audio_bitrate=70k fps=20 preset=veryfast codec=libx264",
        "anime": "resolution=854x480 crf=32 audio_bitrate=64k fps=18 preset=veryfast codec=libx264"
    }

    quality_names = {
        "general": "🗜️Compresión General🔧",
        "reels": "📱 Reels y Videos cortos",
        "show": "📺 Shows/Reality",
        "anime": "🎬 Anime y series animadas"
    }

    # Manejar confirmaciones de compresión
    if callback_query.data.startswith(("confirm_", "cancel_")):
        action, confirmation_id_str = callback_query.data.split('_', 1)
        confirmation_id = ObjectId(confirmation_id_str)
        
        confirmation = await get_confirmation(confirmation_id)
        if not confirmation:
            await callback_query.answer("⚠️ Esta solicitud ha expirado o ya fue procesada.", show_alert=True)
            return
            
        user_id = callback_query.from_user.id
        if user_id != confirmation["user_id"]:
            await callback_query.answer("⚠️ No tienes permiso para esta acción.", show_alert=True)
            return

        if action == "confirm":
            # Verificar límite nuevamente
            if await check_user_limit(user_id):
                await callback_query.answer("⚠️ Has alcanzado tu límite diario/mensual de compresiones.", show_alert=True)
                await delete_confirmation(confirmation_id)
                return

            # Verificar si ya hay una compresión activa o en cola (solo para Free)
            user_plan = await get_user_plan(user_id)
            if user_plan["plan"] == "free" and (await has_active_compression(user_id) or await has_pending_in_queue(user_id)):
                await callback_query.answer("⚠️ Ya hay un video en proceso o en cola.\nEspera a que termine.", show_alert=True)
                await delete_confirmation(confirmation_id)
                return

            try:
                message = await app.get_messages(confirmation["chat_id"], confirmation["message_id"])
            except Exception as e:
                logger.error(f"Error obteniendo mensaje: {e}")
                await callback_query.answer("⚠️ Error al obtener el video. Intenta enviarlo de nuevo.", show_alert=True)
                await delete_confirmation(confirmation_id)
                return

            await delete_confirmation(confirmation_id)
            
            # Editar mensaje de confirmación para mostrar estado
            queue_size = compression_queue.qsize()
            wait_msg = await callback_query.message.edit_text(
                f"⏳ Tu video ha sido añadido a la cola.\nSi tienes Plan Pro o Premium tus vídeos tienen mas prioridad en la cola.\n\n[Los usuarios Free tienen la Prioridad mas baja en el bot]\n\n"
                f"📋 Tamaño actual de la cola: {queue_size}\n\n"
                f"• **Espere que otros procesos terminen** ⏳"
            )

            # Obtener prioridad y encolar
            priority = await get_user_priority(user_id)
            timestamp = datetime.datetime.now()
            
            global processing_task
            if processing_task is None or processing_task.done():
                processing_task = asyncio.create_task(process_compression_queue())
            
            pending_col.insert_one({
                "user_id": user_id,
                "video_id": message.video.file_id,
                "file_name": message.video.file_name,
                "chat_id": message.chat.id,
                "message_id": message.id,
                "timestamp": timestamp,
                "priority": priority
            })
            
            await compression_queue.put((priority, timestamp, (app, message, wait_msg)))
            logger.info(f"Video confirmado y encolado de {user_id}: {message.video.file_name}")

        elif action == "cancel":
            await delete_confirmation(confirmation_id)
            await callback_query.answer("❌ Compresión cancelada.", show_alert=True)
            try:
                await callback_query.message.edit_text("❌ **Compresión cancelada.**")
            except:
                pass
        return

    # Resto de callbacks (planes, configuraciones, etc.)
    if callback_query.data == "plan_back":
        try:
            texto, keyboard = await get_plan_menu(callback_query.from_user.id)
            await callback_query.message.edit_text(texto, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Error en plan_back: {e}", exc_info=True)
            await callback_query.answer("⚠️ Error al volver al menú de planes", show_alert=True)
        return

    if callback_query.data.startswith("plan_"):
        plan_type = callback_query.data.split("_")[1]
        user_id = callback_query.from_user.id
        
        back_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Volver", callback_data="plan_back")]
        ])
        
        if plan_type == "standard":
            await callback_query.message.edit_text(
                "🧩**Plan Estándar**🧩\n\n"
                "✅ Beneficios:\n"
                "• Hasta 40 videos comprimidos\n"
                "❌ Desventajas:\n• Prioridad media en la cola de procesamiento\n• No podrá reenviar del bot ❌\n\n• **Precio:** **180Cup**💵 (antes 200Cup)\n**• Duración 7 dias**\n\n"
                "👨‍💻 **Para acceder a este plan contacta con @InfiniteNetworkAdmin**",
                reply_markup=back_keyboard
            )
            
        elif plan_type == "pro":
            await callback_query.message.edit_text(
                "💎**Plan Pro**💎\n\n"
                "✅ Beneficios:\n"
                "• Hasta 90 videos comprimidos\n"
                "• Prioridad alta en la cola de procesamiento\n• Podrá reenviar del bot☑️\n\n• **Precio:** **400Cup**💵 (antes 450Cup)\n**• Duración 15 dias**\n\n"
                "👨‍💻 **Para acceder a este plan contacta con @InfiniteNetworkAdmin**",
                reply_markup=back_keyboard
            )
            
        elif plan_type == "premium":
            await callback_query.message.edit_text(
                "👑**Plan Premium**👑\n\n"
                "✅ Beneficios:\n"
                "• Hasta 200 videos comprimidos\n"
                "• Máxima prioridad en procesamiento\n"
                "• Soporte prioritario 24/7\n• Podrá reenviar del bot☑️\n\n• **Precio:** **850Cup** (antes 950Cup)💵\n**• Duración 30 dias**\n\n"
                "👨‍💻 **Para acceder a este plan contacta con @InfiniteNetworkAdmin**",
                reply_markup=back_keyboard
            )
        return
    
    config = config_map.get(callback_query.data)

    if config:
        update_video_settings(config)
        back_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Volver", callback_data="back_to_settings")]
        ])
        
        quality_name = quality_names.get(callback_query.data, "Calidad Desconocida")
        
        await callback_query.message.edit_text(
            f"**{quality_name}\naplicada correctamente**✅",
            reply_markup=back_keyboard
        )
    elif callback_query.data == "back_to_settings":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗜️Compresión General🔧", callback_data="general")],
            [InlineKeyboardButton("📱 Reels y Videos cortos", callback_data="reels")],
            [InlineKeyboardButton("📺 Shows/Reality", callback_data="show")],
            [InlineKeyboardButton("🎬 Anime y series animadas", callback_data="anime")]
        ])
        await callback_query.message.edit_text(
            " ⚙️𝗦𝗲𝗹𝗲𝗰𝗰𝗶𝗼𝗻𝗮𝗿 𝗖𝗮𝗹𝗶𝗱𝗮𝗱⚙️",
            reply_markup=keyboard
        )
    else:
        await callback_query.answer("Opción inválida.", show_alert=True)

# ======================== MANEJADOR DE START CON MENÚ ======================== #

@app.on_message(filters.command("start"))
async def start_command(client, message):
    try:
        await register_new_user(message.from_user.id)
        
        if message.from_user.id in ban_users:
            logger.warning(f"Usuario baneado intentó usar /start: {message.from_user.id}")
            return
        
        # Ruta de la imagen del logo (asegúrate de tener el archivo en tu directorio)
        image_path = "logo.jpg"  # Cambia esto si tu archivo tiene otro nombre
        
        caption = (
            "**🤖 Bot para comprimir videos**\n"
            "**Creado por** @InfiniteNetworkAdmin\n\n"
            "**¡Bienvenido!** Usa los botones del menú para interactuar conmigo:\n"
            "• ⚙️ Settings: Configura calidad de compresión\n"
            "• 📋 Planes: Ver opciones de planes disponibles\n"
            "• 📊 Mi Plan: Ver tu plan actual\n"
            "• ℹ️ Ayuda: Obtener información de uso\n"
            "• 👀 Ver Cola: Ver estado de la cola de compresión\n\n" 
            "**⚙️ Versión 8.5.0 ⚙️**"
        )
        
        # Enviar la foto con el caption
        await send_protected_photo(
            chat_id=message.chat.id,
            photo=image_path,
            caption=caption,
            reply_markup=get_main_menu_keyboard()
        )
        logger.info(f"Comando /start ejecutado por {message.from_user.id}")
    except Exception as e:
        logger.error(f"Error en handle_start: {e}", exc_info=True)

# ======================== MANEJADOR DE MENÚ PRINCIPAL ======================== #

@app.on_message(filters.text & filters.private)
async def main_menu_handler(client, message):
    try:
        text = message.text.lower()
        user_id = message.from_user.id

        if user_id in ban_users:
            return
            
        if text == "⚙️ settings":
            await settings_menu(client, message)
        elif text == "📋 planes":
            await planes_command(client, message)
        elif text == "📊 mi plan":
            await my_plan_command(client, message)
        elif text == "ℹ️ ayuda":
            await send_protected_message(
                message.chat.id,
                "👨🏻‍💻 **Información**\n\n"
                "• Configurar calidad: Usa el botón ⚙️ Settings\n"
                "• Para comprimir un video: Envíalo directamente al bot\n"
                "• Ver planes: Usa el botón 📋 Planes\n"
                "• Ver tu estado: Usa el botón 📊 Mi Plan\n"
                "• Usa /start para iniciar en el bot nuevamente\n"
                "• Ver cola de compresión: Usa el botón 👀 Ver Cola\n\n"
                "📩 **Soporte**: @InfiniteNetworkAdmin"
            )
        elif text == "👀 ver cola":  # NUEVO MANEJADOR PARA VER COLA
            await queue_command(client, message)
        else:
            # Manejar otros comandos de texto existentes
            await handle_message(client, message)
            
    except Exception as e:
        logger.error(f"Error en main_menu_handler: {e}", exc_info=True)

# ======================== NUEVO COMANDO PARA DESBANEAR USUARIOS ======================== #

@app.on_message(filters.command("desuser") & filters.user(admin_users))
async def unban_user_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("Formato: /desuser <user_id>")
            return

        user_id = int(parts[1])
        
        if user_id in ban_users:
            ban_users.remove(user_id)
            
        result = banned_col.delete_one({"user_id": user_id})
        
        if result.deleted_count > 0:
            await message.reply(f"✅ Usuario {user_id} desbaneado exitosamente.")
        else:
            await message.reply(f"ℹ️ El usuario {user_id} no estaba baneado.")
            
        logger.info(f"Usuario desbaneado: {user_id} por admin {message.from_user.id}")
    except Exception as e:
        logger.error(f"Error en unban_user_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al desbanear usuario. Formato: /desuser [user_id]")

# ======================== COMANDO PARA ELIMINAR USUARIOS ======================== #
@app.on_message(filters.command(["banuser", "deluser"]) & filters.user(admin_users))
async def ban_or_delete_user_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("Formato: /comando <user_id>")
            return

        ban_user_id = int(parts[1])

        if ban_user_id in admin_users:
            await message.reply("🔒 No puedes banear a un administrador.")
            return

        result = users_col.delete_one({"user_id": ban_user_id})

        if ban_user_id not in ban_users:
            ban_users.append(ban_user_id)
            
        banned_col.insert_one({
            "user_id": ban_user_id,
            "banned_at": datetime.datetime.now()
        })

        await message.reply(
            f"🔒 Usuario {ban_user_id} baneado y eliminado de la base de datos."
            if result.deleted_count > 0 else
            f"🔒 Usuario {ban_user_id} baneado (no estaba en la base de datos)."
        )
    except Exception as e:
        logger.error(f"Error en ban_or_delete_user_command: {e}", exc_info=True)
        await message.reply("⚠️ Error en el comando")

# ======================== ACCESO CON CLAVES PARA PLANES PAGOS ======================== #

@app.on_message(filters.command("access") & filters.private)
async def access_command(client, message):
    try:
        user_id = message.from_user.id
        
        if user_id in ban_users:
            await send_protected_message(message.chat.id, "🔒 Tu acceso ha sido revocado.")
            return
            
        logger.info(f"Comando access recibido de {user_id}")
        
        if len(message.command) < 2:
            await send_protected_message(message.chat.id, "⚠️ Formato: /access <clave>")
            return

        key = message.command[1]

        now = datetime.datetime.now()
        key_data = temp_keys_col.find_one({
            "key": key,
            "used": False,
            "expires_at": {"$gt": now}
        })

        if key_data:
            temp_keys_col.update_one({"_id": key_data["_id"]}, {"$set": {"used": True}})
            new_plan = key_data["plan"]
            await set_user_plan(user_id, new_plan)
            
            await send_protected_message(
                message.chat.id,
                f"✅ **Plan {new_plan.capitalize()} activado!**\n"
                f"Válido por {key_data['duration_days']} días\n\n"
                f"Ahora tienes {PLAN_LIMITS[new_plan]} videos disponibles"
            )
            logger.info(f"Plan actualizado a {new_plan} para {user_id} con clave {key}")
        else:
            await send_protected_message(message.chat.id, "⚠️ **Clave inválida o expirada**")
    except Exception as e:
        logger.error(f"Error en access_command: {e}", exc_info=True)
        await send_protected_message(message.chat.id, "⚠️ **Error al procesar la solicitud de acceso**")

sent_messages = {}

def is_bot_public():
    return BOT_IS_PUBLIC and BOT_IS_PUBLIC.lower() == "true"

# ======================== COMANDOS DE PLANES ======================== #

@app.on_message(filters.command("myplan") & filters.private)
async def my_plan_command(client, message):
    try:
        await register_new_user(message.from_user.id)
        plan_info = await get_plan_info(message.from_user.id)
        await send_protected_message(
            message.chat.id, 
            plan_info,
            reply_markup=get_main_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"Error en my_plan_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id, 
            "⚠️ **Error al obtener información de tu plan**",
            reply_markup=get_main_menu_keyboard()
        )

@app.on_message(filters.command("setplan") & filters.user(admin_users))
async def set_plan_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 3:
            await message.reply("Formato: /setplan <user_id> <plan>")
            return
        
        user_id = int(parts[1])
        plan = parts[2].lower()
        
        if plan not in PLAN_LIMITS:
            await message.reply(f"⚠️ Plan inválido. Opciones válidas: {', '.join(PLAN_LIMITS.keys())}")
            return
        
        if await set_user_plan(user_id, plan):
            await message.reply(f"✅ **Plan del usuario {user_id} actualizado a {plan}.**")
        else:
            await message.reply("⚠️ **Error al actualizar el plan.**")
    except Exception as e:
        logger.error(f"Error en set_plan_command: {e}", exc_info=True)
        await message.reply("⚠️ **Error en el comando**")

@app.on_message(filters.command("resetuser") & filters.user(admin_users))
async def reset_user_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("Formato: /resetuser <user_id>")
            return
        
        user_id = int(parts[1])
        await reset_user_usage(user_id)
        await message.reply(f"✅ **Contador de videos del usuario {user_id} reiniciado a 0.**")
    except Exception as e:
        logger.error(f"Error en reset_user_command: {e}", exc_info=True)
        await message.reply("⚠️ Error en el comando")

@app.on_message(filters.command("userinfo") & filters.user(admin_users))
async def user_info_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("Formato: /userinfo <user_id>")
            return
        
        user_id = int(parts[1])
        user = await get_user_plan(user_id)
        if user:
            plan = user["plan"].capitalize()
            used = user.get("used", 0)
            limit = PLAN_LIMITS[user["plan"]]
            join_date = user.get("join_date", "Desconocido")
            if isinstance(join_date, datetime.datetime):
                join_date = join_date.strftime("%Y-%m-%d %H:%M:%S")
            
            if user["plan"] == "free":
                user_data = users_col.find_one({"user_id": user_id})
                today = datetime.datetime.now().date()
                last_used = user_data.get("last_used_date")
                daily_used = user_data.get("daily_used", 0)
                
                if last_used is None or last_used.date() < today:
                    daily_used = 0
                
                await message.reply(
                    f"👤 **ID**: `{user_id}`\n"
                    f"📝 **Plan**: {plan}\n"
                    f"🔢 **Videos usados hoy**: {daily_used}/1\n"
                    f"📅 **Fecha de registro**: {join_date}"
                )
            else:
                await message.reply(
                    f"👤 **ID**: `{user_id}`\n"
                    f"📝 **Plan**: {plan}\n"
                    f"🔢 **Videos comprimidos**: {used}/{limit}\n"
                    f"📅 **Fecha de registro**: {join_date}"
                )
        else:
            await message.reply("⚠️ Usuario no encontrado")
    except Exception as e:
        logger.error(f"Error en user_info_command: {e}", exc_info=True)
        await message.reply("⚠️ Error en el comando")

# ======================== NUEVO COMANDO RESTUSER ======================== #

@app.on_message(filters.command("restuser") & filters.user(admin_users))
async def reset_all_users_command(client, message):
    try:
        result = users_col.delete_many({})
        
        await message.reply(
            f"♻️ **Todos los usuarios han sido eliminados**\n"
            f"🗑️ Usuarios eliminados: {result.deleted_count}\n"
            f"🔢 Contadores de vídeos restablecidos a 0"
        )
        logger.info(f"Todos los usuarios eliminados por admin {message.from_user.id}")
    except Exception as e:
        logger.error(f"Error en reset_all_users_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al eliminar usuarios")

# ======================== NUEVOS COMANDOS DE ADMINISTRACIÓN ======================== #

@app.on_message(filters.command("user") & filters.user(admin_users))
async def list_users_command(client, message):
    try:
        all_users = list(users_col.find({}))
        
        if not all_users:
            await message.reply("📭 **No hay usuarios registrados.**")
            return

        response = "👥 **Lista de Usuarios Registrados**\n\n"
        for i, user in enumerate(all_users, 1):
            user_id = user["user_id"]
            plan = user["plan"].capitalize()
            
            try:
                user_info = await app.get_users(user_id)
                username = f"@{user_info.username}" if user_info.username else "Sin username"
            except:
                username = "Sin username"
                
            response += f"{i}. {username}\n   👤 ID: `{user_id}`\n   📝 Plan: {plan}\n\n"

        await message.reply(response)
    except Exception as e:
        logger.error(f"Error en list_users_command: {e}", exc_info=True)
        await message.reply("⚠️ **Error al listar usuarios**")

@app.on_message(filters.command("admin") & filters.user(admin_users))
async def admin_stats_command(client, message):
    try:
        pipeline = [
            {"$match": {"plan": {"$exists": True, "$ne": None}}},
            {"$group": {
                "_id": "$plan",
                "count": {"$sum": 1},
                "total_used": {"$sum": "$used"}
            }}
        ]
        stats = list(users_col.aggregate(pipeline))
        
        total_users = users_col.count_documents({})
        total_compressions = users_col.aggregate([
            {"$group": {"_id": None, "total": {"$sum": "$used"}}}
        ])
        total_compressions = next(total_compressions, {}).get("total", 0)
        
        response = "📊 **Estadísticas de Administrador**\n\n"
        response += f"👥 **Total de usuarios:** {total_users}\n"
        response += f"🔢 **Total de compresiones:** {total_compressions}\n\n"
        response += "📝 **Distribución por Planes:**\n"
        
        plan_names = {
            "free": "🆓 Free",
            "standard": "🧩 Estándar",
            "pro": "💎 Pro",
            "premium": "👑 Premium"
        }
        
        for stat in stats:
            plan_type = stat["_id"]
            count = stat["count"]
            used = stat["total_used"]
            plan_name = plan_names.get(
                plan_type, 
                plan_type.capitalize() if plan_type else "❓ Desconocido"
            )
            
            response += (
                f"\n{plan_name}:\n"
                f"  👥 Usuarios: {count}\n"
                f"  🔢 Comprs: {used}\n"
            )
        
        await message.reply(response)
    except Exception as e:
        logger.error(f"Error en admin_stats_command: {e}", exc_info=True)
        await message.reply("⚠️ **Error al generar estadísticas**")

# ======================== NUEVO COMANDO BROADCAST ======================== #

async def broadcast_message(admin_id: int, message_text: str):
    try:
        user_ids = set()
        
        for user in users_col.find({}, {"user_id": 1}):
            user_ids.add(user["user_id"])
        
        user_ids = [uid for uid in user_ids if uid not in ban_users]
        total_users = len(user_ids)
        
        if total_users == 0:
            await app.send_message(admin_id, "📭 No hay usuarios para enviar el mensaje.")
            return
        
        await app.send_message(
            admin_id,
            f"📤 **Iniciando difusión a {total_users} usuarios...**\n"
            f"⏱ Esto puede tomar varios minutos."
        )
        
        success = 0
        failed = 0
        count = 0
        
        for user_id in user_ids:
            count += 1
            try:
                await send_protected_message(user_id, f"📩 **Mensaje del administrador:**\n\n{message_text}")
                success += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error enviando mensaje a {user_id}: {e}")
                failed += 1
                    
        await app.send_message(
            admin_id,
            f"✅ **Difusión completada!**\n\n"
            f"👥 Total de usuarios: {total_users}\n"
            f"✅ Enviados correctamente: {success}\n"
            f"❌ Fallidos: {failed}"
        )
    except Exception as e:
        logger.error(f"Error en broadcast_message: {e}", exc_info=True)
        await app.send_message(admin_id, f"⚠️ Error en difusión: {str(e)}")

@app.on_message(filters.command("msg") & filters.user(admin_users))
async def broadcast_command(client, message):
    try:
        # Verificar si el mensaje tiene texto
        if not message.text or len(message.text.split()) < 2:
            await message.reply("⚠️ Formato: /msg <mensaje>")
            return
            
        # Obtener el texto después del comando
        parts = message.text.split(maxsplit=1)
        broadcast_text = parts[1] if len(parts) > 1 else ""
        
        # Validar que haya texto para difundir
        if not broadcast_text.strip():
            await message.reply("⚠️ El mensaje no puede estar vacío")
            return
            
        admin_id = message.from_user.id
        asyncio.create_task(broadcast_message(admin_id, broadcast_text))
        
        await message.reply(
            "📤 **Difusión iniciada!**\n"
            "⏱ Los mensajes se enviarán progresivamente a todos los usuarios.\n"
            "Recibirás un reporte final cuando se complete."
        )
    except Exception as e:
        logger.error(f"Error en broadcast_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al iniciar la difusión")

# ======================== NUEVO COMANDO PARA VER COLA ======================== #

async def queue_command(client, message):
    """Muestra información sobre la cola de compresión"""
    user_id = message.from_user.id
    
    # Para administradores: mostrar cola completa
    if user_id in admin_users:
        await show_queue(client, message)
        return
    
    # Para usuarios normales: mostrar información resumida
    total = pending_col.count_documents({})
    user_pending = list(pending_col.find({"user_id": user_id}))
    user_count = len(user_pending)
    
    if total == 0:
        response = "📭 **La cola de compresión está vacía.**"
    else:
        # Encontrar la posición del primer video del usuario en la cola ordenada
        cola = list(pending_col.find().sort([("priority", 1), ("timestamp", 1)]))
        user_position = None
        for idx, item in enumerate(cola, 1):
            if item["user_id"] == user_id:
                user_position = idx
                break
        
        if user_count == 0:
            response = (
                f"📋 **Estado de la cola**\n\n"
                f"• Total de videos en cola: {total}\n"
                f"• Tus videos en cola: 0\n\n"
                f"No tienes videos pendientes de compresión."
            )
        else:
            response = (
                f"📋 **Estado de la cola**\n\n"
                f"• Total de videos en cola: {total}\n"
                f"• Tus videos en cola: {user_count}\n"
                f"• Posición de tu primer video: {user_position}\n\n"
                f"⏱ Por favor ten paciencia mientras se procesa tu video."
            )
    
    await send_protected_message(message.chat.id, response)

# ======================== MANEJADORES PRINCIPALES ======================== #

# Manejador para vídeos recibidos
@app.on_message(filters.video)
async def handle_video(client, message: Message):
    try:
        user_id = message.from_user.id
        
        # Paso 1: Auto-registro
        await register_new_user(user_id)
        
        # Paso 2: Verificar baneo
        if user_id in ban_users:
            logger.warning(f"Intento de uso por usuario baneado: {user_id}")
            return
        
        # Paso 3: Verificar si ya tiene una compresión activa o en cola (solo para Free)
        user_plan = await get_user_plan(user_id)
        if user_plan["plan"] == "free" and (await has_active_compression(user_id) or await has_pending_in_queue(user_id)):
            await send_protected_message(
                message.chat.id,
                "⏳ Ya tienes un video en proceso de compresión o en cola.\n"
                "Por favor espera a que termine antes de enviar otro video."
            )
            return
        
        # Paso 4: Verificar si ya tiene una confirmación pendiente
        if await has_pending_confirmation(user_id):
            logger.info(f"Usuario {user_id} tiene confirmación pendiente, ignorando video adicional")
            return
        
        # Paso 5: Verificar límite de plan
        if await check_user_limit(user_id):
            if user_plan["plan"] == "free":
                user_data = users_col.find_one({"user_id": user_id})
                today = datetime.datetime.now().date()
                last_used = user_data.get("last_used_date")
                daily_used = user_data.get("daily_used", 0)
                
                if last_used is None or last_used.date() < today:
                    daily_used = 0
                
                await send_protected_message(
                    message.chat.id,
                    f"⚠️ **Límite diario alcanzado**\n"
                    f"Has usado {daily_used}/1 videos hoy.\n\n"
                    "Los usuarios Free pueden comprimir 1 video por día.\n\n"
                    "📩**Contacta con @InfiniteNetworkAdmin para actualizar tu Plan a Estándar/Pro/Premium**",
                )
            else:
                await send_protected_message(
                    message.chat.id,
                    f"⚠️ **Límite alcanzado**\n"
                    f"Has usado {user_plan['used']}/{PLAN_LIMITS[user_plan['plan']]} videos.\n\n"
                    "📩**Contacta con @InfiniteNetworkAdmin para actualizar tu Plan a Estándar/Pro/Premium**"
                )
            return
        
        # Paso 6: Crear confirmación pendiente
        confirmation_id = await create_confirmation(
            user_id,
            message.chat.id,
            message.id,
            message.video.file_id,
            message.video.file_name
        )
        
        # Paso 7: Enviar mensaje de confirmación con botones
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirmar compresión", callback_data=f"confirm_{confirmation_id}")],
            [InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{confirmation_id}")]
        ])
        
        await send_protected_message(
            message.chat.id,
            f"🎬 **Video recibido para comprimír:** `{message.video.file_name}`\n"
            f"¿Deseas comprimir este video?\n\n"
            f"⚠️ Los del Plan Free solo pueden tener un video en cola a la vez",
            reply_markup=keyboard
        )
        
        logger.info(f"Solicitud de confirmación creada para {user_id}: {message.video.file_name}")
    except Exception as e:
        logger.error(f"Error en handle_video: {e}", exc_info=True)

@app.on_message(filters.text)
async def handle_message(client, message):
    try:
        text = message.text
        username = message.from_user.username
        chat_id = message.chat.id
        user_id = message.from_user.id

        if user_id in ban_users:
            logger.warning(f"Usuario baneado intentó interactuar: {user_id}")
            return

        logger.info(f"Mensaje recibido de {user_id}: {text}")

        await register_new_user(user_id)

        if text.startswith(('/calidad', '.calidad')):
            update_video_settings(text[len('/calidad '):])
            await message.reply(f"⚙️ Configuración Actualizada✅: {video_settings}")
        elif text.startswith(('/settings', '.settings')):
            await settings_menu(client, message)
        elif text.startswith(('/banuser', '.banuser', '/deluser', '.deluser')):
            if user_id in admin_users:
                await ban_or_delete_user_command(client, message)
            else:
                logger.warning(f"Intento no autorizado de banuser/deluser por {user_id}")
        elif text.startswith(('/cola', '.cola')):
            if user_id in admin_users:
                await ver_cola_command(client, message)
        elif text.startswith(('/auto', '.auto')):
            if user_id in admin_users:
                await startup_command(client, message)
        elif text.startswith(('/myplan', '.myplan')):
            await my_plan_command(client, message)
        elif text.startswith(('/setplan', '.setplan')):
            if user_id in admin_users:
                await set_plan_command(client, message)
        elif text.startswith(('/resetuser', '.resetuser')):
            if user_id in admin_users:
                await reset_user_command(client, message)
        elif text.startswith(('/userinfo', '.userinfo')):
            if user_id in admin_users:
                await user_info_command(client, message)
        elif text.startswith(('/planes', '.planes')):
            await planes_command(client, message)
        elif text.startswith(('/generatekey', '.generatekey')):
            if user_id in admin_users:
                await generate_key_command(client, message)
        elif text.startswith(('/listkeys', '.listkeys')):
            if user_id in admin_users:
                await list_keys_command(client, message)
        elif text.startswith(('/user', '.user')):
            if user_id in admin_users:
                await list_users_command(client, message)
        elif text.startswith(('/admin', '.admin')):
            if user_id in admin_users:
                await admin_stats_command(client, message)
        elif text.startswith(('/restuser', '.restuser')):
            if user_id in admin_users:
                await reset_all_users_command(client, message)
        elif text.startswith(('/desuser', '.desuser')):
            if user_id in admin_users:
                await unban_user_command(client, message)
        elif text.startswith(('/msg', '.msg')):
            if user_id in admin_users:
                await broadcast_command(client, message)

        if message.reply_to_message:
            original_message = sent_messages.get(message.reply_to_message.id)
            if original_message:
                user_id = original_message["user_id"]
                sender_info = f"Respuesta de @{message.from_user.username}" if message.from_user.username else f"Respuesta de user ID: {message.from_user.id}"
                await send_protected_message(user_id, f"{sender_info}: {message.text}")
                logger.info(f"Respuesta enviada a {user_id}")
    except Exception as e:
        logger.error(f"Error en handle_message: {e}", exc_info=True)

# ======================== FUNCIONES AUXILIARES ======================== #

async def notify_group(client, message: Message, original_size: int, compressed_size: int = None, status: str = "start"):
    try:
        group_id = -4826894501  # Reemplaza con tu ID de grupo

        user = message.from_user
        username = f"@{user.username}" if user.username else "Sin username"
        file_name = message.video.file_name or "Sin nombre"
        size_mb = original_size // (1024 * 1024)

        if status == "start":
            text = (
                "📤 **Nuevo video recibido para comprimir**\n\n"
                f"👤 **Usuario:** {username}\n"
                f"🆔 **ID:** `{user.id}`\n"
                f"📦 **Tamaño original:** {size_mb} MB\n"
                f"📁 **Nombre:** `{file_name}`"
            )
        elif status == "done":
            compressed_mb = compressed_size // (1024 * 1024)
            text = (
                "📥 **Video comprimido y enviado**\n\n"
                f"👤 **Usuario:** {username}\n"
                f"🆔 **ID:** `{user.id}`\n"
                f"📦 **Tamaño original:** {size_mb} MB\n"
                f"📉 **Tamaño comprimido:** {compressed_mb} MB\n"
                f"📁 **Nombre:** `{file_name}`"
            )

        await app.send_message(chat_id=group_id, text=text)
        logger.info(f"Notificación enviada al grupo: {user.id} - {file_name} ({status})")
    except Exception as e:
        logger.error(f"Error enviando notificación al grupo: {e}")

# ======================== INICIO DEL BOT ======================== #

try:
    logger.info("Iniciando el bot...")
    app.run()
except Exception as e:
    logger.critical(f"Error fatal al iniciar el bot: {e}", exc_info=True)