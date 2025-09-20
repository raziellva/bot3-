import os
import logging
import asyncio
import threading
import concurrent.futures
import tempfile
import json
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

# Diccionario de prioridades por plan (ahora solo para límites de cola)
PLAN_PRIORITY = {
    "ultra": 0,  
    "premium": 1,
    "pro": 2,
    "standard": 3
}

# Límite de cola para usuarios premium
PREMIUM_QUEUE_LIMIT = 3
ULTRA_QUEUE_LIMIT = 10

# Conexión a MongoDB
mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DATABASE_NAME]
pending_col = db["pending"]
users_col = db["users"]
temp_keys_col = db["temp_keys"]
banned_col = db["banned_users"]
pending_confirmations_col = db["pending_confirmations"]
active_compressions_col = db["active_compressions"]
user_settings_col = db["user_settings"]
download_tasks_col = db["download_tasks"]  # Nueva colección para tareas de descarga

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
download_tasks_col.delete_many({})  # Limpiar tareas de descarga previas
logger.info("Compresiones activas previas eliminadas")

# Configuración de compresión de video (configuración global por defecto)
DEFAULT_VIDEO_SETTINGS = {
    'resolution': '854x480',
    'crf': '28',
    'audio_bitrate': '64k',
    'fps': '22',
    'preset': 'veryfast',
    'codec': 'libx264'
}

# Variables globales para las colas
download_queue = asyncio.Queue()
compression_queue = asyncio.Queue()
processing_task = None
download_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)  # 2 descargas simultáneas
compression_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)  # 1 compresión a la vez

# Conjunto para rastrear mensajes de progreso activos
active_messages = set()

# ======================== NUEVO SISTEMA DE DESCARGAS SIMULTÁNEAS ======================== #

async def process_download_queue():
    """Procesa la cola de descargas con hasta 2 workers simultáneos"""
    while True:
        client, message, wait_msg, confirmation_data = await download_queue.get()
        try:
            # Verificar si la tarea aún está en download_tasks_col (no fue cancelada)
            download_task = download_tasks_col.find_one({
                "chat_id": message.chat.id,
                "message_id": message.id
            })
            if not download_task:
                logger.info(f"Tarea de descarga cancelada, saltando: {message.video.file_name}")
                download_queue.task_done()
                continue

            # Procesar la descarga
            await process_download(client, message, wait_msg, confirmation_data)
        except Exception as e:
            logger.error(f"Error procesando descarga: {e}", exc_info=True)
            try:
                await app.send_message(message.chat.id, f"⚠️ Error al descargar el video: {str(e)}")
            except:
                pass
        finally:
            download_queue.task_done()

async def process_download(client, message, wait_msg, confirmation_data):
    """Procesa una descarga individual"""
    user_id = message.from_user.id
    original_message_id = message.id
    
    try:
        # Crear mensaje de progreso de descarga
        download_msg = await app.send_message(
            chat_id=message.chat.id,
            text="📥 **Iniciando Descarga** 📥",
            reply_to_message_id=message.id
        )
        active_messages.add(download_msg.id)
        
        # Registrar tarea de descarga
        register_cancelable_task(user_id, "download", None, original_message_id=original_message_id, progress_message_id=download_msg.id)
        
        start_download_time = time.time()
        
        # Descargar el video
        file_path = await download_media_with_cancellation(
            message, download_msg, user_id, start_download_time
        )
        
        # Verificar si se canceló durante la descarga
        if user_id not in cancel_tasks:
            logger.info("Descarga cancelada por el usuario")
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
            unregister_cancelable_task(user_id)
            if download_msg.id in active_messages:
                active_messages.remove(download_msg.id)
            try:
                await download_msg.delete()
            except:
                pass
            return
        
        # Descarga completada, agregar a la cola de compresión
        logger.info(f"Video descargado: {file_path}")
        
        # Actualizar estado en la base de datos
        download_tasks_col.update_one(
            {"chat_id": message.chat.id, "message_id": message.id},
            {"$set": {"status": "downloaded", "file_path": file_path}}
        )
        
        # Agregar a la cola de compresión
        pending_col.insert_one({
            "user_id": user_id,
            "video_id": message.video.file_id,
            "file_name": message.video.file_name,
            "file_path": file_path,
            "chat_id": message.chat.id,
            "message_id": message.id,
            "timestamp": datetime.datetime.now(),
            "confirmation_data": confirmation_data
        })
        
        # Notificar al usuario que la descarga completó y está en cola de compresión
        queue_size = compression_queue.qsize()
        await download_msg.edit(
            f"✅ **Descarga completada**\n\n"
            f"📋 Posición en cola de compresión: {queue_size + 1}\n\n"
            f"• **Esperando turno para comprimir** ⏳"
        )
        
        # Agregar a la cola de compresión
        await compression_queue.put((app, message, download_msg, file_path, confirmation_data))
        
    except Exception as e:
        logger.error(f"Error en process_download: {e}", exc_info=True)
        try:
            await app.send_message(message.chat.id, f"⚠️ Error en la descarga: {str(e)}")
        except:
            pass
        finally:
            if 'download_msg' in locals() and download_msg.id in active_messages:
                active_messages.remove(download_msg.id)
            unregister_cancelable_task(user_id)

async def process_compression_queue():
    """Procesa la cola de compresión (1 a la vez)"""
    while True:
        client, message, wait_msg, file_path, confirmation_data = await compression_queue.get()
        try:
            # Verificar si la tarea aún está en pending_col (no fue cancelada)
            pending_task = pending_col.find_one({
                "chat_id": message.chat.id,
                "message_id": message.id
            })
            if not pending_task:
                logger.info(f"Tarea de compresión cancelada, saltando: {message.video.file_name}")
                compression_queue.task_done()
                continue

            # Iniciar compresión
            start_msg = await wait_msg.edit("🗜️**Iniciando compresión**🎬")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                compression_executor, 
                threading_compress_video, 
                client, message, start_msg, file_path, confirmation_data
            )
        except Exception as e:
            logger.error(f"Error procesando compresión: {e}", exc_info=True)
            try:
                await app.send_message(message.chat.id, f"⚠️ Error al comprimir el video: {str(e)}")
            except:
                pass
        finally:
            # Eliminar de pending_col y descargar tareas
            pending_col.delete_one({"video_id": message.video.file_id})
            download_tasks_col.delete_one({
                "chat_id": message.chat.id,
                "message_id": message.id
            })
            compression_queue.task_done()

def threading_compress_video(client, message, start_msg, file_path, confirmation_data):
    """Wrapper para ejecutar compresión en un hilo separado"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(compress_video(client, message, start_msg, file_path, confirmation_data))
    loop.close()

# ======================== FIN NUEVO SISTEMA DE DESCARGAS SIMULTÁNEAS ======================== #

# ... (el resto del código se mantiene igual hasta la función download_media_with_cancellation)

async def download_media_with_cancellation(message, msg, user_id, start_time):
    """Descarga medios con capacidad de cancelación"""
    try:
        # Crear directorio temporal si no existe
        os.makedirs("downloads", exist_ok=True)
        
        # Obtener información del archivo
        file_id = message.video.file_id
        file_name = message.video.file_name or f"video_{file_id}.mp4"
        file_path = os.path.join("downloads", file_name)
        
        # Iniciar descarga
        downloaded = await app.download_media(
            message.video,
            file_name=file_path,
            progress=progress_callback,
            progress_args=(msg, "DESCARGA", start_time)
        )
        
        return file_path
        
    except asyncio.CancelledError:
        # Re-lanzar la excepción de cancelación
        raise
    except Exception as e:
        logger.error(f"Error en descarga: {e}", exc_info=True)
        raise

# ======================== MODIFICACIÓN EN EL MANEJADOR DE CALLBACKS ======================== #

@app.on_callback_query()
async def callback_handler(client, callback_query: CallbackQuery):
    # ... (código anterior se mantiene igual)
    
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
                await callback_query.answer("⚠️ Has alcanzado tu límite mensual de compresiones.", show_alert=True)
                await delete_confirmation(confirmation_id)
                return

            # Verificar si ya hay una compresión activa o en cola
            user_plan = await get_user_plan(user_id)
            queue_limit = await get_user_queue_limit(user_id)
            
            # Contar tareas en cola de descarga y compresión
            download_count = download_tasks_col.count_documents({"user_id": user_id})
            pending_count = pending_col.count_documents({"user_id": user_id})
            total_pending = download_count + pending_count
            
            # Verificar límites de cola según el plan
            if total_pending >= queue_limit:
                await callback_query.answer(
                    f"⚠️ Ya tienes {total_pending} videos en proceso (límite: {queue_limit}).\n"
                    "Espera a que se procesen antes de enviar más.",
                    show_alert=True
                )
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
            download_size = download_queue.qsize()
            wait_msg = await callback_query.message.edit_text(
                f"⏳ Tu video ha sido añadido a la cola de descarga.\n\n"
                f"📋 Tamaño actual de cola de descarga: {download_size}\n\n"
                f"• **Descargando** ⏳"
            )

            # Registrar tarea de descarga
            download_tasks_col.insert_one({
                "user_id": user_id,
                "video_id": message.video.file_id,
                "file_name": message.video.file_name,
                "chat_id": message.chat.id,
                "message_id": message.id,
                "timestamp": datetime.datetime.now(),
                "status": "pending"
            })
            
            # Agregar a la cola de descarga
            await download_queue.put((app, message, wait_msg, confirmation))
            logger.info(f"Video confirmado y encolado para descarga de {user_id}: {message.video.file_name}")

        elif action == "cancel":
            await delete_confirmation(confirmation_id)
            await callback_query.answer("⛔ Compresión cancelada.⛔", show_alert=True)
            try:
                await callback_query.message.edit_text("⛔ **Compresión cancelada.** ⛔")
                # Borrar mensaje después de 5 segundos
                await asyncio.sleep(5)
                await callback_query.message.delete()
            except:
                pass
        return

    # ... (el resto del código se mantiene igual)

# ======================== MODIFICACIÓN EN LA FUNCIÓN DE COMPRESIÓN ======================== #

async def compress_video(client, message: Message, start_msg, file_path, confirmation_data):
    """Función de compresión modificada para usar el archivo ya descargado"""
    try:
        if not message.video:
            await app.send_message(chat_id=message.chat.id, text="Por favor envía un vídeo válido")
            return

        logger.info(f"Iniciando compresión para chat_id: {message.chat.id}, video: {message.video.file_name}")
        user_id = message.from_user.id
        original_message_id = message.id

        # Obtener configuración personalizada del usuario
        user_video_settings = await get_user_video_settings(user_id)

        # Registrar compresión activa
        await add_active_compression(user_id, message.video.file_id)

        # Crear mensaje de progreso como respuesta al video original
        msg = await app.send_message(
            chat_id=message.chat.id,
            text="🗜️ **Iniciando Compresión** 🎬",
            reply_to_message_id=message.id
        )
        active_messages.add(msg.id)
        
        # Agregar botón de cancelación
        cancel_button = InlineKeyboardMarkup([[
            InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{user_id}")
        ]])
        await msg.edit_reply_markup(cancel_button)
        
        try:
            # Verificar si se canceló antes de comenzar la compresión
            if user_id not in cancel_tasks:
                logger.info("Compresión cancelada por el usuario antes de comenzar")
                await remove_active_compression(user_id)
                unregister_cancelable_task(user_id)
                try:
                    await start_msg.delete()
                except:
                    pass
                if msg.id in active_messages:
                    active_messages.remove(msg.id)
                try:
                    await msg.delete()
                except:
                    pass
                await send_protected_message(
                    message.chat.id,
                    "⛔ **Compresión cancelada** ⛔",
                    reply_to_message_id=original_message_id
                )
                return
                
            original_size = os.path.getsize(file_path)
            logger.info(f"Tamaño original: {original_size} bytes")
            await notify_group(client, message, original_size, status="start")
            
            try:
                probe = ffmpeg.probe(file_path)
                dur_total = float(probe['format']['duration'])
                logger.info(f"Duración del video: {dur_total} segundos")
            except Exception as e:
                logger.error(f"Error obteniendo duración: {e}", exc_info=True)
                dur_total = 0

            # Mensaje de inicio de compresión como respuesta al video
            await msg.edit(
                "╭━━━━[🤖**Compress Bot**]━━━━━╮\n"
                "┠➣ 🗜️𝗖𝗼𝗺𝗽𝗿𝗶𝗺𝗶𝗲𝗻𝗱𝗼 𝗩𝗶𝗱𝗲𝗼🎬\n"
                "┠➣ **Progreso**: 📤 𝘊𝘢𝘳𝘨𝘢𝘯𝘥𝘰 𝘝𝘪𝘥𝘦𝘰 📤\n"
                "╰━━━━━━━━━━━━━━━━━━━━━╯",
                reply_markup=cancel_button
            )
            
            compressed_video_path = f"{os.path.splitext(file_path)[0]}_compressed.mp4"
            logger.info(f"Ruta de compresión: {compressed_video_path}")
            
            # ... (el resto del código de compresión se mantiene igual, usando file_path en lugar de original_video_path)

        except Exception as e:
            logger.error(f"Error en compresión: {e}", exc_info=True)
            await msg.delete()
            await app.send_message(chat_id=message.chat.id, text=f"Ocurrió un error al comprimir el video: {e}")
        finally:
            try:
                # Limpiar mensajes activos
                if msg.id in active_messages:
                    active_messages.remove(msg.id)
                if 'upload_msg' in locals() and upload_msg.id in active_messages:
                    active_messages.remove(upload_msg.id)
                    
                # Eliminar archivo descargado (ya no se necesita)
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
                    logger.info(f"Archivo temporal eliminado: {file_path}")
                    
                for compressed_path in [compressed_video_path]:
                    if compressed_path and os.path.exists(compressed_path):
                        os.remove(compressed_path)
                        logger.info(f"Archivo comprimido eliminado: {compressed_path}")
                if 'thumbnail_path' in locals() and thumbnail_path and os.path.exists(thumbnail_path):
                    os.remove(thumbnail_path)
                    logger.info(f"Miniatura eliminada: {thumbnail_path}")
            except Exception as e:
                logger.error(f"Error eliminando archivos temporales: {e}", exc_info=True)
    except Exception as e:
        logger.critical(f"Error crítico en compress_video: {e}", exc_info=True)
        await app.send_message(chat_id=message.chat.id, text="⚠️ Ocurrió un error crítico al procesar el video")
    finally:
        await remove_active_compression(user_id)
        unregister_cancelable_task(user_id)
        unregister_ffmpeg_process(user_id)
        
# ======================== INICIALIZACIÓN DE LAS COLAS AL INICIAR EL BOT ======================== #

# Mover la creación de las tareas dentro del manejador de inicio
@app.on_start()
async def on_start(client):
    global processing_task, download_task
    
    logger.info("🔄 Iniciando procesamiento de las colas...")

    # Cargar tareas de descarga pendientes
    pendientes = download_tasks_col.find({"status": "pending"}).sort([("timestamp", 1)])
    for item in pendientes:
        try:
            user_id = item["user_id"]
            chat_id = item["chat_id"]
            message_id = item["message_id"]
            
            message = await app.get_messages(chat_id, message_id)
            wait_msg = await app.send_message(chat_id, f"🔄 Recuperado desde cola persistente.")
            
            await download_queue.put((app, message, wait_msg, {}))
        except Exception as e:
            logger.error(f"Error cargando descarga pendiente: {e}")

    # Cargar tareas de compresión pendientes
    compresiones_pendientes = pending_col.find().sort([("timestamp", 1)])
    for item in compresiones_pendientes:
        try:
            user_id = item["user_id"]
            chat_id = item["chat_id"]
            message_id = item["message_id"]
            file_path = item["file_path"]
            confirmation_data = item.get("confirmation_data", {})
            
            message = await app.get_messages(chat_id, message_id)
            wait_msg = await app.send_message(chat_id, f"🔄 Recuperado desde cola de compresión persistente.")
            
            await compression_queue.put((app, message, wait_msg, file_path, confirmation_data))
        except Exception as e:
            logger.error(f"Error cargando compresión pendiente: {e}")

    # Iniciar workers si no están activos
    if download_task is None or download_task.done():
        download_task = asyncio.create_task(process_download_queue())
        
    if processing_task is None or processing_task.done():
        processing_task = asyncio.create_task(process_compression_queue())
        
    logger.info("✅ Procesamiento de colas iniciado.")

# ======================== FIN FUNCIONALIDAD DE COLA ======================== #

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
        user_id = message.from_user.id
        original_message_id = message.id  # Guardar ID del mensaje original para cancelación

        # Obtener configuración personalizada del usuario
        user_video_settings = await get_user_video_settings(user_id)

        # Registrar compresión activa
        await add_active_compression(user_id, message.video.file_id)

        # Crear mensaje de progreso como respuesta al video original
        msg = await app.send_message(
            chat_id=message.chat.id,
            text="📥 **Iniciando Descarga** 📥",
            reply_to_message_id=message.id  # Respuesta al video original
        )
        # Registrar este mensaje en mensajes activos
        active_messages.add(msg.id)
        
        # Agregar botón de cancelación
        cancel_button = InlineKeyboardMarkup([[
            InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{user_id}")
        ]])
        await msg.edit_reply_markup(cancel_button)
        
        try:
            start_download_time = time.time()
            # Registrar tarea de descarga
            register_cancelable_task(user_id, "download", None, original_message_id=original_message_id, progress_message_id=msg.id)
            
            original_video_path = await app.download_media(
                message.video,
                progress=progress_callback,
                progress_args=(msg, "DESCARGA", start_download_time)
            )
            
            # Verificar si se canceló durante la descarga
            if user_id not in cancel_tasks:
                logger.info("Descarga cancelada por el usuario")
                if original_video_path and os.path.exists(original_video_path):
                    os.remove(original_video_path)
                await remove_active_compression(user_id)
                unregister_cancelable_task(user_id)
                # Borrar mensaje de inicio
                try:
                    await start_msg.delete()
                except:
                    pass
                # Remover de mensajes activos y borrar mensaje de progreso
                if msg.id in active_messages:
                    active_messages.remove(msg.id)
                try:
                    await msg.delete()
                except:
                    pass
                # Enviar mensaje de cancelación respondiendo al video original
                await send_protected_message(
                    message.chat.id,
                    "⛔ **Compresión cancelada** ⛔",
                    reply_to_message_id=original_message_id
                )
                return
                
            logger.info(f"Video descargado: {original_video_path}")
        except Exception as e:
            logger.error(f"Error en descarga: {e}", exc_info=True)
            await msg.edit(f"Error en descarga: {e}")
            await remove_active_compression(user_id)
            unregister_cancelable_task(user_id)
            # Remover de mensajes activos
            if msg.id in active_messages:
                active_messages.remove(msg.id)
            return
        
        # Verificar si se canceló después de la descarga
        if user_id not in cancel_tasks:
            if original_video_path and os.path.exists(original_video_path):
                os.remove(original_video_path)
            await remove_active_compression(user_id)
            unregister_cancelable_task(user_id)
            # Borrar mensaje de inicio
            try:
                await start_msg.delete()
            except:
                pass
            # Remover de mensajes activos y borrar mensaje de progreso
            if msg.id in active_messages:
                active_messages.remove(msg.id)
            try:
                await msg.delete()
            except:
                pass
            # Enviar mensaje de cancelación respondiendo al video original
                await send_protected_message(
                    message.chat.id,
                    "⛔ **Compresión cancelada** ⛔",
                    reply_to_message_id=original_message_id
                )
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

        # Mensaje de inicio de compresión como respuesta al video
        await msg.edit(
            "╭━━━━[🤖**Compress Bot**]━━━━━╮\n"
            "┠➣ 🗜️𝗖𝗼𝗺𝗽𝗿𝗶𝗺𝗶𝗲𝗻𝗱𝗼 𝗩𝗶𝗱𝗲𝗼🎬\n"
            "┠➣ **Progreso**: 📤 𝘊𝘢𝘳𝘨𝘢𝘯𝘥𝘰 𝘝𝘪𝘥𝘦𝘰 📤\n"
            "╰━━━━━━━━━━━━━━━━━━━━━╯",
            reply_markup=cancel_button
        )
        
        compressed_video_path = f"{os.path.splitext(original_video_path)[0]}_compressed.mp4"
        logger.info(f"Ruta de compresión: {compressed_video_path}")
        
        drawtext_filter = f"drawtext=text='@InfiniteNetwork_KG':x=w-tw-10:y=10:fontsize=20:fontcolor=white"

        ffmpeg_command = [
            'ffmpeg', '-y', '-i', original_video_path,
            '-vf', f"scale={user_video_settings['resolution']},{drawtext_filter}",
            '-crf', user_video_settings['crf'],
            '-b:a', user_video_settings['audio_bitrate'],
            '-r', user_video_settings['fps'],
            '-preset', user_video_settings['preset'],
            '-c:v', user_video_settings['codec'],
            compressed_video_path
        ]
        logger.info(f"Comando FFmpeg: {' '.join(ffmpeg_command)}")

        try:
            start_time = datetime.datetime.now()
            process = subprocess.Popen(ffmpeg_command, stderr=subprocess.PIPE, text=True, bufsize=1)
            
            # Registrar tarea de ffmpeg
            register_cancelable_task(user_id, "ffmpeg", process, original_message_id=original_message_id, progress_message_id=msg.id)
            register_ffmpeg_process(user_id, process)
            
            last_percent = 0
            last_update_time = 0
            time_pattern = re.compile(r"time=(\d+:\d+:\d+\.\d+)")
            
            while True:
                # Verificar si se canceló durante la compresión
                if user_id not in cancel_tasks:
                    process.kill()
                    # Limpiar mensaje de progreso
                    if msg.id in active_messages:
                        active_messages.remove(msg.id)
                    try:
                        await msg.delete()
                        await start_msg.delete()
                    except:
                        pass
                    # Enviar mensaje de cancelación respondiendo al video original
                    await send_protected_message(
                        message.chat.id,
                        "⛔ **Compresión cancelada** ⛔",
                        reply_to_message_id=original_message_id
                    )
                    if original_video_path and os.path.exists(original_video_path):
                        os.remove(original_video_path)
                    if compressed_video_path and os.path.exists(compressed_video_path):
                        os.remove(compressed_video_path)
                    await remove_active_compression(user_id)
                    unregister_cancelable_task(user_id)
                    unregister_ffmpeg_process(user_id)
                    return
                
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
                        
                        # Obtener el tamaño actual del archivo comprimido
                        compressed_size = 0
                        if os.path.exists(compressed_video_path):
                            compressed_size = os.path.getsize(compressed_video_path)
                        
                        # Calcular tiempos transcurrido y restante
                        elapsed_time = datetime.datetime.now() - start_time
                        elapsed_seconds = elapsed_time.total_seconds()
                        
                        if percent > 0:
                            remaining_seconds = (elapsed_seconds / percent) * (100 - percent)
                        else:
                            remaining_seconds = 0
                        
                        # Formatear tiempos
                        elapsed_str = format_time(elapsed_seconds)
                        remaining_str = format_time(remaining_seconds)
                        
                        if percent - last_percent >= 5 or time.time() - last_update_time >= 5:
                            bar = create_compression_bar(percent)
                            # Agregar botón de cancelación
                            cancel_button = InlineKeyboardMarkup([[
                                InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{user_id}")
                            ]])
                            try:
                                await msg.edit(
                                    f"╭━━━━[**🤖Compress Bot**]━━━━━╮\n"
                                    f"┠➣ 🗜️𝗖𝗼𝗺𝗽𝗿𝗶𝗺𝗶𝗲𝗻𝗱𝗼 𝗩𝗶𝗱𝗲𝗼🎬\n"
                                    f"┠➣ **Progreso**: {bar}\n"
                                    f"┠➣ **Tamaño**: {sizeof_fmt(compressed_size)}\n"
                                    f"┠➣ **Tiempo transcurrido**: {elapsed_str}\n"
                                    f"┠➣ **Tiempo restante**: {remaining_str}\n"
                                    f"╰━━━━━━━━━━━━━━━━━━━━━╯",
                                    reply_markup=cancel_button
                                )
                            except MessageNotModified:
                                pass
                            except Exception as e:
                                logger.error(f"Error editando mensaje de progreso: {e}")
                                if msg.id in active_messages:
                                    active_messages.remove(msg.id)
                            last_percent = percent
                            last_update_time = time.time()

            # Verificar si se canceló después de la compresión
            if user_id not in cancel_tasks:
                if original_video_path and os.path.exists(original_video_path):
                    os.remove(original_video_path)
                if compressed_video_path and os.path.exists(compressed_video_path):
                    os.remove(compressed_video_path)
                await remove_active_compression(user_id)
                unregister_cancelable_task(user_id)
                unregister_ffmpeg_process(user_id)
                # Borrar mensaje de inicio
                try:
                    await start_msg.delete()
                except:
                    pass
                # Remover de mensajes activos y borrar mensaje de progreso
                if msg.id in active_messages:
                    active_messages.remove(msg.id)
                try:
                    await msg.delete()
                except:
                    pass
                # Enviar mensaje de cancelación respondiendo al video original
                    await send_protected_message(
                        message.chat.id,
                        "⛔ **Compresión cancelada** ⛔",
                        reply_to_message_id=original_message_id
                    )
                return

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
                "╭✠━━━━━━━━━━━━━━━━━━━━✠╮\n"
                f"┠➣🗜️**Vídeo comprimído**🎬\n┠➣**Tiempo transcurrido**: {processing_time_str}\n╰✠━━━━━━━━━━━━━━━━━━━━✠╯\n"
            )
            
            try:
                start_upload_time = time.time()
                # Mensaje de subida como respuesta al video original
                upload_msg = await app.send_message(
                    chat_id=message.chat.id,
                    text="📤 **Subiendo video comprimido** 📤",
                    reply_to_message_id=message.id
                )
                # Registrar mensaje de subida
                active_messages.add(upload_msg.id)
                
                # Registrar tarea de subida
                register_cancelable_task(user_id, "upload", None, original_message_id=original_message_id, progress_message_id=upload_msg.id)
                
                # Verificar si se canceló antes de la subida
                if user_id not in cancel_tasks:
                    if original_video_path and os.path.exists(original_video_path):
                        os.remove(original_video_path)
                    if compressed_video_path and os.path.exists(compressed_video_path):
                        os.remove(compressed_video_path)
                    if thumbnail_path and os.path.exists(thumbnail_path):
                        os.remove(thumbnail_path)
                    await remove_active_compression(user_id)
                    unregister_cancelable_task(user_id)
                    unregister_ffmpeg_process(user_id)
                    # Borrar mensajes
                    try:
                        await start_msg.delete()
                        await msg.delete()
                        await upload_msg.delete()
                    except:
                        pass
                    # Remover de mensajes activos
                    if msg.id in active_messages:
                        active_messages.remove(msg.id)
                    if upload_msg.id in active_messages:
                        active_messages.remove(upload_msg.id)
                    # Enviar mensaje de cancelación respondiendo al video original
                    await send_protected_message(
                        message.chat.id,
                        "⛔ **Compresión cancelada** ⛔",
                        reply_to_message_id=original_message_id
                    )
                    return
                
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
                
                try:
                    await upload_msg.delete()
                    logger.info("Mensaje de subida eliminado")
                except:
                    pass
                logger.info("✅ Video comprimido enviado como respuesta al original")
                await notify_group(client, message, original_size, compressed_size=compressed_size, status="done")
             
                # ACTUALIZAR CONTADOR DE VIDEOS COMPRIMIDOS (CORREGIDO)
                users_col.update_one(
                    {"user_id": user_id},
                    {"$inc": {"compressed_videos": 1}},
                    upsert=True
                )
                
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
                # Limpiar mensajes activos
                if msg.id in active_messages:
                    active_messages.remove(msg.id)
                if 'upload_msg' in locals() and upload_msg.id in active_messages:
                    active_messages.remove(upload_msg.id)
                    
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
        await remove_active_compression(user_id)
        unregister_cancelable_task(user_id)
        unregister_ffmpeg_process(user_id)

# ======================== INTERFAZ DE USUARIO ======================== #

# Teclado principal
def get_main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("⚙️ Settings"), KeyboardButton("📋 Planes")],
            [KeyboardButton("📊 Mi Plan"), KeyboardButton("ℹ️ Ayuda")],
            [KeyboardButton("👀 Ver Cola"), KeyboardButton("🗑️ Cancelar Cola")]
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
        # No incluir el plan ultra en el menú público
    ])

async def get_plan_menu(user_id: int):
    user = await get_user_plan(user_id)
    
    if user is None or user.get("plan") is None:
        return (
            "**No tienes un plan activo.**\n\n"
            "Adquiere un plan para usar el bot.\n\n"
            "📋 **Selecciona un plan para más información:**"
        ), get_plan_menu_keyboard()
    
    plan_name = user["plan"].capitalize()
    
    return (
        f"╭✠━━━━━━━━━━━━━━━━━━━━━━✠╮\n"
        f"┠➣ **Tu plan actual**: {plan_name}\n"
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
        "general": "resolution=854x480 crf=28 audio_bitrate=64k fps=22 preset=veryfast codec=libx264",
        "reels": "resolution=420x720 crf=25 audio_bitrate=64k fps=30 preset=veryfast codec=libx264",
        "show": "resolution=854x480 crf=32 audio_bitrate=64k fps=20 preset=veryfast codec=libx264",
        "anime": "resolution=854x480 crf=32 audio_bitrate=64k fps=18 preset=veryfast codec=libx264"
    }

    quality_names = {
        "general": "🗜️Compresión General🔧",
        "reels": "📱 Reels y Videos cortos",
        "show": "📺 Shows/Reality",
        "anime": "🎬 Anime y series animadas"
    }

    # Manejar cancelación de tareas
    if callback_query.data.startswith("cancel_task_"):
        user_id = int(callback_query.data.split("_")[2])
        if callback_query.from_user.id != user_id:
            await callback_query.answer("⚠️ Solo el propietario puede cancelar esta tarea", show_alert=True)
            return
            
        if cancel_user_task(user_id):
            # Guardar el original_message_id antes de desregistrar
            original_message_id = cancel_tasks[user_id].get("original_message_id")
            progress_message_id = cancel_tasks[user_id].get("progress_message_id")
            unregister_cancelable_task(user_id)
            unregister_ffmpeg_process(user_id)
            # Remover mensaje de activos y eliminarlo
            msg_to_delete = callback_query.message
            if msg_to_delete.id in active_messages:
                active_messages.remove(msg_to_delete.id)
            try:
                await msg_to_delete.delete()
            except Exception as e:
                logger.error(f"Error eliminando mensaje de progreso: {e}")
            await callback_query.answer("⛔ Compresión cancelada! ⛔", show_alert=True)
            # Enviar mensaje de cancelación respondiendo al video original
            try:
                await app.send_message(
                    callback_query.message.chat.id,
                    "⛔ **Compresión cancelada** ⛔",
                    reply_to_message_id=original_message_id
                )
            except:
                # Si falla, enviar sin reply
                await app.send_message(
                    callback_query.message.chat.id,
                    "⛔ **Compresión cancelada** ⛔"
                )
        else:
            await callback_query.answer("⚠️ No se pudo cancelar la tarea", show_alert=True)
        return

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
                await callback_query.answer("⚠️ Has alcanzado tu límite mensual de compresiones.", show_alert=True)
                await delete_confirmation(confirmation_id)
                return

            # Verificar si ya hay una compresión activa o en cola
            user_plan = await get_user_plan(user_id)
            queue_limit = await get_user_queue_limit(user_id)
            pending_count = pending_col.count_documents({"user_id": user_id})
            
            # Verificar límites de cola según el plan
            if pending_count >= queue_limit:
                await callback_query.answer(
                    f"⚠️ Ya tienes {pending_count} videos en cola (límite: {queue_limit}).\n"
                    "Espera a que se procesen antes de enviar más.",
                    show_alert=True
                )
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
                f"⏳ Tu video ha sido añadido to la cola.\n\n"
                f"📋 Tamaño actual de la cola: {queue_size}\n\n"
                f"• **Espere que otros procesos terminen** ⏳"
            )

            # Obtener timestamp y encolar
            timestamp = datetime.datetime.now()
            
            global processing_task
            if processing_task is None or processing_task.done():
                processing_task = asyncio.create_task(process_compression_queue())
            
            # Insertar en pending_col incluyendo el wait_message_id
            pending_col.insert_one({
                "user_id": user_id,
                "video_id": message.video.file_id,
                "file_name": message.video.file_name,
                "chat_id": message.chat.id,
                "message_id": message.id,
                "wait_message_id": wait_msg.id,  # <--- Nuevo campo
                "timestamp": timestamp
            })
            
            await compression_queue.put((app, message, wait_msg))
            logger.info(f"Video confirmado y encolado de {user_id}: {message.video.file_name}")

        elif action == "cancel":
            await delete_confirmation(confirmation_id)
            await callback_query.answer("⛔ Compresión cancelada.⛔", show_alert=True)
            try:
                await callback_query.message.edit_text("⛔ **Compresión cancelada.** ⛔")
                # Borrar mensaje después de 5 segundos
                await asyncio.sleep(5)
                await callback_query.message.delete()
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

    # Manejar el callback para mostrar planes desde el mensaje de start o video
    if callback_query.data in ["show_plans_from_start", "show_plans_from_video"]:
        try:
            texto, keyboard = await get_plan_menu(callback_query.from_user.id)
            await callback_query.message.edit_text(texto, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Error mostrando planes desde callback: {e}", exc_info=True)
            await callback_query.answer("⚠️ Error al mostrar los planes", show_alert=True)
        return

    # Manejar callbacks de planes
    elif callback_query.data.startswith("plan_"):
        plan_type = callback_query.data.split("_")[1]
        user_id = callback_query.from_user.id
        
        # Nuevo teclado con botón de contratar
        back_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Volver", callback_data="plan_back"),
             InlineKeyboardButton("📝 Contratar Plan", url="https://t.me/InfiniteNetworkAdmin?text=Hola,+estoy+interesad@+en+un+plan+del+bot+de+comprimír+vídeos")]
        ])
        
        if plan_type == "standard":
            await callback_query.message.edit_text(
                "🧩**Plan Estándar**🧩\n\n"
                "✅ **Beneficios:**\n"
                "• **Videos para comprimir: ilimitados**\n\n"
                "❌ **Desventajas:**\n• **No podá reenviar del bot**\n• **Solo podá comprimír 1 video a la ves**\n\n• **Precio:** **180Cup**💵\n• **Duración 7 dias**\n\n",
                reply_markup=back_keyboard
            )
            
        elif plan_type == "pro":
            await callback_query.message.edit_text(
                "💎**Plan Pro**💎\n\n"
                "✅ **Beneficios:**\n"
                "• **Videos para comprimir: ilimitados**\n"
                "• **Podá reenviar del bot**\n\n❌ **Desventajas**\n• **Solo podá comprimír 1 video a la ves**\n\n• **Precio:** **300Cup**💵\n• **Duración 15 dias**\n\n",
                reply_markup=back_keyboard
            )
            
        elif plan_type == "premium":
            await callback_query.message.edit_text(
                "👑**Plan Premium**👑\n\n"
                "✅ **Beneficios:**\n"
                "• **Videos para comprimir: ilimitados**\n"
                "• **Soporte prioritario 24/7**\n• **Podá reenviar del bot**\n"
                f"• **Múltiples videos en cola** (hasta {PREMIUM_QUEUE_LIMIT})\n\n"
                "• **Precio:** **500Cup**💵\n• **Duración 30 dias**\n\n",
                reply_markup=back_keyboard
            )
        return

    # Manejar configuraciones de calidad
    config = config_map.get(callback_query.data)
    if config:
        # Actualizar configuración personalizada del usuario
        user_id = callback_query.from_user.id
        if await update_user_video_settings(user_id, config):
            back_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Volver", callback_data="back_to_settings")]
            ])
            
            quality_name = quality_names.get(callback_query.data, "Calidad Desconocida")
            
            await callback_query.message.edit_text(
                f"**{quality_name}\naplicada correctamente**✅",
                reply_markup=back_keyboard
            )
        else:
            await callback_query.answer("❌ Error al aplicar la configuración", show_alert=True)
    elif callback_query.data == "back_to_settings":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗜️Compresión General🔧", callback_data="general")],
            [InlineKeyboardButton("📱 Reels y Videos cortos", callback_data="reels")],
            [InlineKeyboardButton("📺 Shows/Reality", callback_data="show")],
            [InlineKeyboardButton("🎬 Anime y series animadas", callback_data="anime")]
        ])
        await callback_query.message.edit_text(
            "⚙️𝗦𝗲𝗹𝗲𝗰𝗰𝗶𝗼𝗻𝗮𝗿 𝗖𝗮𝗹𝗶𝗱𝗮𝗱⚙️",
            reply_markup=keyboard
        )
    else:
        await callback_query.answer("Opción inválida.", show_alert=True)

# ======================== MANEJADOR DE START CON MENÚ ======================== #

@app.on_message(filters.command("start"))
async def start_command(client, message):
    try:
        user_id = message.from_user.id
        
        # Verificar si el usuario está baneado
        if user_id in ban_users:
            logger.warning(f"Usuario baneado intentó usar /start: {user_id}")
            return

        # Verificar si el usuario tiene un plan (está registrado)
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            # Usuario sin plan: mostrar mensaje de acceso denegado con botón de ofertas
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💠Planes💠", callback_data="show_plans_from_start")]
            ])
            await send_protected_message(
                message.chat.id,
                "**Usted no tiene acceso al bot.**\n\n⬇️**Toque para ver nuestros planes**⬇️",
                reply_markup=keyboard
            )
            return

        # Usuario con plan: mostrar menú normal
        # Ruta de la imagen del logo
        image_path = "logo.jpg"
        
        caption = (
            "**🤖 Bot para comprimir videos**\n"
            "➣**Creado por** @InfiniteNetworkAdmin\n\n"
            "**¡Bienvenido!** Puedo reducir el tamaño de los vídeos hasta un 80% o más y se verán bien sin perder tanta calidad\nUsa los botones del menú para interactuar conmigo.\nSi tiene duda use el botón ℹ️ Ayuda\n\n"
            "**⚙️ Versión 20.0.5 ⚙️**"
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
            # Crear teclado con botón de soporte
            support_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("👨🏻‍💻 Soporte", url="https://t.me/InfiniteNetworkAdmin")]
            ])
            
            await send_protected_message(
                message.chat.id,
                "👨🏻‍💻 **Información**\n\n"
                "➣ **Configurar calidad**:\n• Usa el botón ⚙️ Settings\n"
                "➣ **Para comprimir un video**:\n• Envíalo directamente al bot\n"
                "➣ **Ver planes**:\n• Usa el botón 📋 Planes\n"
                "➣ **Ver tu estado**:\n• Usa el botón 📊 Mi Plan\n"
                "➣ **Usa** /start **para iniciar en el bot nuevamente o para actualizar**\n"
                "➣ **Ver cola de compresión**:\n• Usa el botón 👀 Ver Cola\n"
                "➣ **Cancelar videos de la cola**:\n• Usa el botón 🗑️ Cancelar Cola\n➣ **Para ver su configuración de compresión actual use** /calidad\n\n",
                reply_markup=support_keyboard
            )
        elif text == "👀 ver cola":
            await queue_command(client, message)
        elif text == "🗑️ cancelar cola":
            await cancel_queue_command(client, message)
        elif text == "/cancel":
            await cancel_command(client, message)
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
            await message.reply(f"Usuario {user_id} desbaneado exitosamente.")
            # Notificar al usuario que fue desbaneado
            try:
                await app.send_message(
                    user_id,
                    "✅ **Tu acceso al bot ha sido restaurado.**\n\n"
                    "Ahora puedes volver a usar el bot."
                )
            except Exception as e:
                logger.error(f"No se pudo notificar al usuario {user_id}: {e}")
        else:
            await message.reply(f"El usuario {user_id} no estaba baneado.")
            
        logger.info(f"Usuario desbaneado: {user_id} por admin {message.from_user.id}")
    except Exception as e:
        logger.error(f"Error en unban_user_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al desbanear usuario. Formato: /desuser [user_id]")

# ======================== NUEVO COMANDO DELETEUSER ======================== #

@app.on_message(filters.command("deleteuser") & filters.user(admin_users))
async def delete_user_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("Formato: /deleteuser <user_id>")
            return

        user_id = int(parts[1])
        
        # Eliminar usuario de la base de datos
        result = users_col.delete_one({"user_id": user_id})
        
        # Agregar a lista de baneados si no está
        if user_id not in ban_users:
            ban_users.append(user_id)
            
        # Agregar a colección de baneados
        banned_col.insert_one({
            "user_id": user_id,
            "banned_at": datetime.datetime.now()
        })
        
        # Eliminar tareas pendientes del usuario
        pending_result = pending_col.delete_many({"user_id": user_id})
        
        # Eliminar configuración personalizada del usuario
        user_settings_col.delete_one({"user_id": user_id})
        
        await message.reply(
            f"Usuario {user_id} eliminado y baneado exitosamente.\n"
            f"🗑️ Tareas pendientes eliminadas: {pending_result.deleted_count}"
        )
        
        logger.info(f"Usuario eliminado y baneado: {user_id} por admin {message.from_user.id}")
        
        # Notificar al usuario que perdió el acceso
        try:
            await app.send_message(
                user_id,
                "🔒 **Tu acceso al bot ha sido revocado.**\n\n"
                "No podrás usar el bot hasta nuevo aviso."
            )
        except Exception as e:
            logger.error(f"No se pudo notificar al usuario {user_id}: {e}")
            
    except Exception as e:
        logger.error(f"Error en delete_user_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al eliminar usuario. Formato: /deleteuser [user_id]")

# ======================== NUEVO COMANDO PARA VER USUARIOS BANEADOS ======================== #

@app.on_message(filters.command("viewban") & filters.user(admin_users))
async def view_banned_users_command(client, message):
    try:
        banned_users = list(banned_col.find({}))
        
        if not banned_users:
            await message.reply("**No hay usuarios baneados.**")
            return

        response = "**Usuarios Baneados**\n\n"
        for i, banned_user in enumerate(banned_users, 1):
            user_id = banned_user["user_id"]
            banned_at = banned_user.get("banned_at", "Fecha desconocida")
            
            # Obtener información del usuario de Telegram
            try:
                user = await app.get_users(user_id)
                username = f"@{user.username}" if user.username else "Sin username"
            except:
                username = "Sin username"
            
            if isinstance(banned_at, datetime.datetime):
                banned_at_str = banned_at.strftime("%Y-%m-%d %H:%M:%S")
            else:
                banned_at_str = str(banned_at)
                
            response += f"{i}• 👤 {username}\n   🆔 ID: `{user_id}`\n   ⏰ Fecha: {banned_at_str}\n\n"

        await message.reply(response)
    except Exception as e:
        logger.error(f"Error en view_banned_users_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al obtener la lista de usuarios baneados")

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
            await message.reply("No puedes banear a un administrador.")
            return

        result = users_col.delete_one({"user_id": ban_user_id})

        if ban_user_id not in ban_users:
            ban_users.append(ban_user_id)
            
        banned_col.insert_one({
            "user_id": ban_user_id,
            "banned_at": datetime.datetime.now()
        })

        # Eliminar configuración personalizada del usuario
        user_settings_col.delete_one({"user_id": ban_user_id})

        await message.reply(
            f"Usuario {ban_user_id} baneado y eliminado de la base de datos."
            if result.deleted_count > 0 else
            f"Usuario {ban_user_id} baneado (no estaba en la base de datos)."
        )
    except Exception as e:
        logger.error(f"Error en ban_or_delete_user_command: {e}", exc_info=True)
        await message.reply("⚠️ Error en el comando")

@app.on_message(filters.command("key") & filters.private)
async def key_command(client, message):
    try:
        user_id = message.from_user.id
        
        if user_id in ban_users:
            await send_protected_message(message.chat.id, "🚫 Tu acceso ha sido revocado.")
            return
            
        logger.info(f"Comando key recibido de {user_id}")
        
        # Obtener la clave directamente del texto del mensaje
        if not message.text or len(message.text.split()) < 2:
            await send_protected_message(message.chat.id, "❌ Formato: /key <clave>")
            return

        key = message.text.split()[1].strip()  # Obtener la clave directamente del texto

        now = datetime.datetime.now()
        key_data = temp_keys_col.find_one({
        "key": key,
        "used": False
    })

        if not key_data:
            await send_protected_message(message.chat.id, "❌ **Clave inválida o ya ha sido utilizada.**")
            return

        # Verificar si la clave ha expirado
        if key_data["expires_at"] < now:
            await send_protected_message(message.chat.id, "❌ **La clave ha expirado.**")
            return

        # Si llegamos aquí, la clave es válida
        temp_keys_col.update_one({"_id": key_data["_id"]}, {"$set": {"used": True}})
        new_plan = key_data["plan"]
        
        # Calcular fecha de expiración usando los nuevos campos
        duration_value = key_data["duration_value"]
        duration_unit = key_data["duration_unit"]
        
        if duration_unit == "minutes":
            expires_at = datetime.datetime.now() + datetime.timedelta(minutes=duration_value)
        elif duration_unit == "hours":
            expires_at = datetime.datetime.now() + datetime.timedelta(hours=duration_value)
        else:  # días por defecto
            expires_at = datetime.datetime.now() + datetime.timedelta(days=duration_value)
            
        success = await set_user_plan(user_id, new_plan, notify=False, expires_at=expires_at)
        
        if success:
            # Texto para mostrar la duración en formato amigable
            duration_text = f"{duration_value} {duration_unit}"
            if duration_value == 1:
                duration_text = duration_text[:-1]  # Remover la 's' final para singular
            
            await send_protected_message(
                message.chat.id,
                f"✅ **Plan {new_plan.capitalize()} activado!**\n"
                f"**Válido por {duration_text}**\n\n"
                f"Use el comando /start para iniciar en el bot"
            )
            logger.info(f"Plan actualizado a {new_plan} para {user_id} con clave {key}")
        else:
            await send_protected_message(message.chat.id, "❌ **Error al activar el plan. Contacta con el administrador.**")

    except Exception as e:
        logger.error(f"Error en key_command: {e}", exc_info=True)
        await send_protected_message(message.chat.id, "❌ **Error al procesar la solicitud de acceso**")

sent_messages = {}

def is_bot_public():
    return BOT_IS_PUBLIC and BOT_IS_PUBLIC.lower() == "true"

# ======================== COMANDOS DE PLANES ======================== #

@app.on_message(filters.command("myplan") & filters.private)
async def my_plan_command(client, message):
    try:
        user_id = message.from_user.id
        user_plan = await get_user_plan(user_id)
        
        if user_plan is None or user_plan.get("plan") is None:
            # Mostrar mensaje con botón de planes
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💠 Planes 💠", callback_data="show_plans_from_start")]
            ])
            await send_protected_message(
                message.chat.id,
                "**No tienes un plan activo.**\n\n⬇️**Toque para ver nuestros planes**⬇️",
                reply_markup=keyboard
            )
        else:
            plan_info = await get_plan_info(user_id)
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
        
        if plan not in PLAN_DURATIONS:
            await message.reply(f"⚠️ Plan inválido. Opciones válidas: {', '.join(PLAN_DURATIONS.keys())}")
            return
        
        # Usar set_user_plan sin expires_at para que calcule automáticamente
        if await set_user_plan(user_id, plan):
            await message.reply(f"**Plan del usuario {user_id} actualizado a {plan}.**")
        else:
            await message.reply("⚠️ **Error al actualizar el plan.**")
    except Exception as e:
        logger.error(f"Error en set_plan_command: {e}", exc_info=True)
        await message.reply("⚠️ **Error en el comando**")

@app.on_message(filters.command("userinfo") & filters.user(admin_users))
async def user_info_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("Formato: /userinfo <user_id>")
            return
        
        user_id = int(parts[1])
        user = await get_user_plan(user_id)
        
        # Obtener información del usuario de Telegram
        try:
            user_info = await app.get_users(user_id)
            username = f"@{user_info.username}" if user_info.username else "Sin username"
        except:
            username = "Sin username"
            
        if user:
            plan_name = user["plan"].capitalize() if user.get("plan") else "Ninguno"
            join_date = user.get("join_date", "Desconocido")
            expires_at = user.get("expires_at", "No expira")
            compressed_videos = user.get("compressed_videos", 0)  # Nuevo campo

            if isinstance(join_date, datetime.datetime):
                join_date = join_date.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(expires_at, datetime.datetime):
                expires_at = expires_at.strftime("%Y-%m-%d %H:%M:%S")

            await message.reply(
                f"👤**Usuario**: {username}\n"
                f"🆔 **ID**: `{user_id}`\n"
                f"📝 **Plan**: {plan_name}\n"
                f"🎬 **Videos comprimidos**: {compressed_videos}\n"
                f"📅 **Fecha de registro**: {join_date}\n"
                f"⏰ **Expira**: {expires_at}"
            )
        else:
            await message.reply("⚠️ Usuario no registrado o sin plan")
    except Exception as e:
        logger.error(f"Error en user_info_command: {e}", exc_info=True)
        await message.reply("⚠️ Error en el comando")

# ======================== NUEVO COMANDO RESTUSER ======================== #

@app.on_message(filters.command("restuser") & filters.user(admin_users))
async def reset_all_users_command(client, message):
    try:
        result = users_col.delete_many({})
        
        # También eliminar todas las configuraciones personalizadas
        user_settings_col.delete_many({})
        
        await message.reply(
            f"**Todos los usuarios han sido eliminados**\n"
            f"Usuarios eliminados: {result.deleted_count}"
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
            await message.reply("⛔**No hay usuarios registrados.**⛔")
            return

        response = "**Lista de Usuarios Registrados**\n\n"
        for i, user in enumerate(all_users, 1):
            user_id = user["user_id"]
            plan = user["plan"].capitalize() if user.get("plan") else "Ninguno"
            
            try:
                user_info = await app.get_users(user_id)
                username = f"@{user_info.username}" if user_info.username else "Sin username"
            except:
                username = "Sin username"
                
            response += f"{i}• 👤 {username}\n   🆔 ID: `{user_id}`\n   📝 Plan: {plan}\n\n"

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
                "count": {"$sum": 1}
            }}
        ]
        stats = list(users_col.aggregate(pipeline))
        
        total_users = users_col.count_documents({})
        
        response = "📊 **Estadísticas de Administrador**\n\n"
        response += f"👥 **Total de usuarios:** {total_users}\n\n"
        response += "📝 **Distribución por Planes:**\n"
        
        plan_names = {
            "standard": "🧩 Estándar",
            "pro": "💎 Pro",
            "premium": "👑 Premium",
            "ultra": "🚀 Ultra"
        }
        
        for stat in stats:
            plan_type = stat["_id"]
            count = stat["count"]
            plan_name = plan_names.get(
                plan_type, 
                plan_type.capitalize() if plan_type else "❓ Desconocido"
            )
            
            response += (
                f"\n{plan_name}:\n"
                f"  👥 Usuarios: {count}\n"
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
                await send_protected_message(user_id, f"**🔔Notificación:**\n\n{message_text}")
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
    user_plan = await get_user_plan(user_id)
    
    if user_plan is None or user_plan.get("plan") is None:
        await send_protected_message(
            message.chat.id,
            "**Usted no tiene acceso para usar este bot.**\n\n"
            "Por favor, adquiera un plan para poder ver la cola de compresión."
        )
        return
    
    # Para administradores: mostrar cola completa
    if user_id in admin_users:
        await show_queue(client, message)
        return
    
    # Para usuarios normales: mostrar información resumida
    total = pending_col.count_documents({})
    user_pending = list(pending_col.find({"user_id": user_id}))
    user_count = len(user_pending)
    
    if total == 0:
        response = "📋**La cola de compresión está vacía.**"
    else:
        # Encontrar la posición del primer video del usuario en la cola ordenada
        cola = list(pending_col.find().sort([("timestamp", 1)]))
        user_position = None
        for idx, item in enumerate(cola, 1):
            if item["user_id"] == user_id:
                user_position = idx
                break
        
        if user_count == 0:
            response = (
                f"**Estado de la cola**\n\n"
                f"• Total de videos en cola: {total}\n"
                f"• Tus videos en cola: 0\n\n"
                f"📋**No tienes videos pendientes de compresión.**"
            )
        else:
            response = (
                f"**Estado de la cola**\n\n"
                f"• Total de videos en cola: {total}\n"
                f"• Tus videos en cola: {user_count}\n"
                f"• Posición de tu primer video: {user_position}\n\n"
                f"**⏳ Por favor espere**."
            )
    
    await send_protected_message(message.chat.id, response)

# ======================== NUEVA FUNCIÓN PARA NOTIFICAR A TODOS LOS USUARIOS ======================== #

async def notify_all_users(message_text: str):
    """Envía un mensaje a todos los usuarios registrados y no baneados"""
    try:
        user_ids = set()
        
        # Obtener todos los usuarios registrados (que tienen un plan)
        for user in users_col.find({}, {"user_id": 1}):
            user_ids.add(user["user_id"])
        
        # Filtrar usuarios baneados
        user_ids = [uid for uid in user_ids if uid not in ban_users]
        total_users = len(user_ids)
        
        if total_users == 0:
            return 0, 0
        
        success = 0
        failed = 0
        
        for user_id in user_ids:
            try:
                await send_protected_message(user_id, message_text)
                success += 1
                # Pequeña pausa para no saturar
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"Error enviando mensaje de notificación a {user_id}: {e}")
                failed += 1
                    
        return success, failed
    except Exception as e:
        logger.error(f"Error en notify_all_users: {e}", exc_info=True)
        return 0, 0

# ======================== NUEVO COMANDO RESTART ======================== #

async def restart_bot():
    """Función para reiniciar el bot cancelando todos los procesos"""
    try:
        # 1. Cancelar todos los procesos FFmpeg activos
        for user_id, process in list(ffmpeg_processes.items()):
            try:
                if process.poll() is None:
                    process.terminate()
                    time.sleep(1)
                    if process.poll() is None:
                        process.kill()
            except Exception as e:
                logger.error(f"Error terminando proceso FFmpeg para {user_id}: {e}")
        
        # 2. Limpiar estructuras de datos de procesos
        ffmpeg_processes.clear()
        cancel_tasks.clear()
        
        # 3. Limpiar mensajes activos
        active_messages.clear()
        
        # 4. Limpiar la cola de compresión
        while not compression_queue.empty():
            try:
                compression_queue.get_nowait()
                compression_queue.task_done()
            except asyncio.QueueEmpty:
                break
        
        # 5. Eliminar todos los pendientes de la base de datos
        result = pending_col.delete_many({})
        logger.info(f"Eliminados {result.deleted_count} elementos de la cola")
        
        # 6. Limpiar compresiones activas
        active_compressions_col.delete_many({})
        
        # 7. Notificar a todos los usuarios
        notification_text = (
            "🔔**Notificación:**\n\n"
            "El bot ha sido reiniciado\ntodos los procesos se han cancelado.\n\n✅ **Ahora puedes enviar nuevos videos para comprimir**."
        )
        
        # Enviar notificación a todos los usuarios en segundo plano
        success, failed = await notify_all_users(notification_text)
        
        # 8. Notificar al grupo de administradores
        try:
            await app.send_message(
                -4826894501,  # Reemplaza con tu ID de grupo
                f"**Notificación de reinicio completada!**\n\n"
                f"✅ Enviados correctamente: {success}\n"
                f"❌ Fallidos: {failed}"
            )
        except Exception as e:
            logger.error(f"Error enviando notificación de reinicio al grupo: {e}")
        
        return True, success, failed
    except Exception as e:
        logger.error(f"Error en restart_bot: {e}", exc_info=True)
        return False, 0, 0

@app.on_message(filters.command("restart") & filters.user(admin_users))
async def restart_command(client, message):
    """Comando para reiniciar el bot y cancelar todos los procesos"""
    try:
        msg = await message.reply("🔄 Reiniciando bot...")
        
        success, notifications_sent, notifications_failed = await restart_bot()
        
        if success:
            await msg.edit(
                "**Bot reiniciado con éxito**\n\n"
                "✅ Todos los procesos activos cancelados\n"
                "✅ Cola de compresión vaciada\n"
                "✅ Procesos FFmpeg terminados\n"
                "✅ Estado interno limpiado\n\n"
                f"📤 Notificaciones enviadas: {notifications_sent}\n"
                f"❌ Notificaciones fallidas: {notifications_failed}"
            )
        else:
            await msg.edit("⚠️ **Error al reiniciar el bot.**")
    except Exception as e:
        logger.error(f"Error en restart_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al ejecutar el comando de reinicio")

# ======================== NUEVOS COMANDOS PARA CONFIGURACIÓN PERSONALIZADA ======================== #

@app.on_message(filters.command(["calidad", "quality"]) & filters.private)
async def calidad_command(client, message):
    """Permite a los usuarios establecer su configuración personalizada de compresión"""
    try:
        user_id = message.from_user.id
        
        # Verificar si el usuario tiene un plan activo
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            await send_protected_message(
                message.chat.id,
                "**Usted no tiene acceso para usar este bot.**\n\n⬇️**Toque para ver nuestros planes**⬇️"
            )
            return
            
        # Verificar si se proporcionaron parámetros
        if len(message.text.split()) < 2:
            # Mostrar la configuración actual del usuario
            current_settings = await get_user_video_settings(user_id)
            response = (
                "**Tu configuración actual de compresión:**\n\n"
                f"• **Resolución**: `{current_settings['resolution']}`\n"
                f"• **CRF**: `{current_settings['crf']}`\n"
                f"• **Bitrate de audio**: `{current_settings['audio_bitrate']}`\n"
                f"• **FPS**: `{current_settings['fps']}`\n"
                f"• **Preset**: `{current_settings['preset']}`\n"
                f"• **Códec**: `{current_settings['codec']}`\n\n"
                "Para restablecer a la configuración por defecto, usa /resetcalidad"
            )
            await send_protected_message(message.chat.id, response)
            return
            
        # Procesar la nueva configuración
        command_text = message.text.split(maxsplit=1)[1]
        success = await update_user_video_settings(user_id, command_text)
        
        if success:
            new_settings = await get_user_video_settings(user_id)
            response = "✅ **Configuración actualizada correctamente:**\n\n"
            for key, value in new_settings.items():
                response += f"• **{key}**: `{value}`\n"
                
            await send_protected_message(message.chat.id, response)
        else:
            await send_protected_message(
                message.chat.id,
                "❌ **Error al actualizar la configuración.**\n"
                "Formato correcto: /calidad resolution=854x480 crf=28 audio_bitrate=64k fps=25 preset=veryfast codec=libx264"
            )
            
    except Exception as e:
        logger.error(f"Error en calidad_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id,
            "❌ **Error al procesar el comando.**\n"
            "Formato correcto: /calidad resolution=854x480 crf=28 audio_bitrate=64k fps=25 preset=veryfast codec=libx264"
        )

@app.on_message(filters.command("resetcalidad") & filters.private)
async def reset_calidad_command(client, message):
    """Restablece la configuración del usuario a los valores por defecto"""
    try:
        user_id = message.from_user.id
        await reset_user_video_settings(user_id)
        
        default_settings = await get_user_video_settings(user_id)
        response = "✅ **Configuración restablecida a los valores por defecto:**\n\n"
        for key, value in default_settings.items():
            response += f"• **{key}**: `{value}`\n"
            
        await send_protected_message(message.chat.id, response)
        
    except Exception as e:
        logger.error(f"Error en reset_calidad_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id,
            "❌ **Error al restablecer la configuración.**"
        )

# ======================== MANEJADORES PRINCIPALES ======================== #

# Manejador para vídeos recibidos
@app.on_message(filters.video)
async def handle_video(client, message: Message):
    try:
        user_id = message.from_user.id
        
        # Paso 1: Verificar baneo
        if user_id in ban_users:
            logger.warning(f"Intento de uso por usuario baneado: {user_id}")
            return
        
        # Paso 2: Verificar si el usuario tiene un plan
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            # Mostrar mensaje con botón de ofertas
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💠Planes💠", callback_data="show_plans_from_video")]
            ])
            await send_protected_message(
                message.chat.id,
                "**No tienes un plan activo.**\n\n"
                "**Adquiere un plan para usar el bot.**\n\n",
                reply_markup=keyboard
            )
            return
        
        # Paso 3: Verificar si ya tiene una confirmación pendiente
        if await has_pending_confirmation(user_id):
            logger.info(f"Usuario {user_id} tiene confirmación pendiente, ignorando video adicional")
            return
        
        # Paso 4: Verificar límite de plan
        if await check_user_limit(user_id):
            await send_protected_message(
                message.chat.id,
                f"⚠️ **Límite alcanzado**\n"
                f"Tu plan ha expirado.\n\n"
                "👨🏻‍💻**Contacta con @InfiniteNetworkAdmin para renovar tu Plan**"
            )
            return
        
        # Paso 5: Verificar si el usuario puede agregar más vídeos a la cola
        has_active = await has_active_compression(user_id)
        queue_limit = await get_user_queue_limit(user_id)
        pending_count = pending_col.count_documents({"user_id": user_id})

        # Verificar límites de cola según el plan
        if pending_count >= queue_limit:
            await send_protected_message(
                message.chat.id,
                f"Ya tienes {pending_count} videos en cola (límite: {queue_limit}).\n"
                "Por favor espera a que se procesen antes de enviar más."
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
        
        # Paso 7: Enviar mensaje de confirmación con botones (respondiendo al video)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Confirmar compresión 🟢", callback_data=f"confirm_{confirmation_id}")],
            [InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_{confirmation_id}")]
        ])
        
        await send_protected_message(
            message.chat.id,
            f"🎬 **Video recibido para comprimír:** `{message.video.file_name}`\n\n"
            f"¿Deseas comprimir este video?",
            reply_to_message_id=message.id,  # Respuesta al video original
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
            return
            
        logger.info(f"Mensaje recibido de {user_id}: {text}")

        if text.startswith(('/calidad', '.calidad', '/quality', '.quality')):
            await calidad_command(client, message)
        elif text.startswith(('/resetcalidad', '.resetcalidad')):
            await reset_calidad_command(client, message)
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
        elif text.startswith(('/delkeys', '.delkeys')):
            if user_id in admin_users:
                await del_keys_command(client, message)
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
        elif text.startswith(('/deleteuser', '.deleteuser')):
            if user_id in admin_users:
                await delete_user_command(client, message)
        elif text.startswith(('/viewban', '.viewban')):
            if user_id in admin_users:
                await view_banned_users_command(client, message)
        elif text.startswith(('/msg', '.msg')):
            if user_id in admin_users:
                await broadcast_command(client, message)
        elif text.startswith(('/cancel', '.cancel')):
            await cancel_command(client, message)
        elif text.startswith(('/cancelqueue', '.cancelqueue')):
            await cancel_queue_command(client, message)
        elif text.startswith(('/key', '.key')):
            await key_command(client, message)
        elif text.startswith(('/restart', '.restart')):
            if user_id in admin_users:
                await restart_command(client, message)
        elif text.startswith(('/getdb', '.getdb')):
            if user_id in admin_users:
                await get_db_command(client, message)
        elif text.startswith(('/restdb', '.restdb')):
            if user_id in admin_users:
                await rest_db_command(client, message)

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