import os
from pyrogram import Client, filters
import random
import string
import datetime
import subprocess
from pyrogram.types import Message
import ffmpeg
import os
from pyrogram import Client

# Configuracion del bot
api_id = os.getenv('API_ID')
api_hash = os.getenv('API_HASH')
bot_token = os.getenv('TOKEN')

# Administradores y Usuarios del bot
admin_users = list(map(int, os.getenv('ADMINS').split(',')))
users = list(map(int, os.getenv('USERS').split(',')))
temp_users = []
ban_users = []
allowed_users = admin_users + users + temp_users
app = Client("my_bot", api_id=api_id, api_hash=api_hash, bot_token=bot_token)

# Configuración de compresión de video
# Puedes ajustar estos valores según tus necesidades
# Estos valores son para una compresión moderada, puedes ajustarlos según tus necesidades
# Resolución, CRF, bitrate de audio, FPS, preset y codec
# Puedes cambiar estos valores según tus preferencias
# Ejemplo: 'resolution': '1280x720', 'crf': '23', 'audio_bitrate': '128k', 'fps': '30', 'preset': 'medium', 'codec': 'libx264'
# Estos valores son para una compresión moderada, puedes ajustarlos según tus necesidades
video_settings = {
    'resolution': '854x480',
    'crf': '32',
    'audio_bitrate': '60k',
    'fps': '18',
    'preset': 'veryfast',
    'codec': 'libx264'
}

def update_video_settings(command: str):
    settings = command.split()
    for setting in settings:
        key, value = setting.split('=')
        video_settings[key] = value

async def compress_video(client, message: Message):  # Cambiar a async
    if message.reply_to_message and message.reply_to_message.video:
        msg = await app.send_message(chat_id=message.chat.id, text="🗜️Descargando Video 📹...")
        original_video_path = await app.download_media(message.reply_to_message.video)
        original_size = os.path.getsize(original_video_path)
        await msg.edit(f"𝐈𝐧𝐢𝐜𝐢𝐚𝐧𝐝𝐨 𝐂𝐨𝐦𝐩𝐫𝐞𝐬𝐢𝐨𝐧..\n"
                                                              f"📚Tamaño original: {original_size // (1024 * 1024)} MB")
        compressed_video_path = f"{os.path.splitext(original_video_path)[0]}_compressed.mkv"
        ffmpeg_command = [
            'ffmpeg', '-y', '-i', original_video_path,
            '-s', video_settings['resolution'], '-crf', video_settings['crf'],
            '-b:a', video_settings['audio_bitrate'], '-r', video_settings['fps'],
            '-preset', video_settings['preset'], '-c:v', video_settings['codec'],
            compressed_video_path
        ]
        try:
            start_time = datetime.datetime.now()
            process = subprocess.Popen(ffmpeg_command, stderr=subprocess.PIPE, text=True)
            await msg.edit("🗜️𝐂𝐨𝐦𝐩𝐫𝐢𝐦𝐢𝐞𝐧𝐝𝐨 𝐕𝐢𝐝𝐞𝐨 📹...")
            while True:
                output = process.stderr.readline()
                if output == '' and process.poll() is not None:
                    break
                if output:
                    print(output.strip())
            # Recuperar tamaño y duración
            compressed_size = os.path.getsize(compressed_video_path)
            
            # AQUI COMIENZA MI CODIGO ELJOKER63
            # Analiza el video comprimido y obtiene la duracion
            try:
                probe = ffmpeg.probe(compressed_video_path)
                duration = int(float(probe.get('format', {}).get('duration', 0)))
                if duration == 0:
                    for stream in probe.get('streams', []):
                        if 'duration' in stream:
                            duration = int(float(stream['duration']))
                            break
                if duration == 0:
                    print(f"Warning: Couldn't determine duration for {compressed_video_path}. Setting to 0.")
                    duration = 0

            except Exception as e:
                print(f"Error probing video {compressed_video_path}: {str(e)}")
                print("Setting duration to 0 and continuing...")
                duration = 0

            # Generar miniatura (opcional)
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
            except Exception as e:
                print(f"Error generating thumbnail for {compressed_video_path}: {str(e)}")
                print("Continuing without thumbnail...")
                thumbnail_path = None

            # AQUI TERMINA MI CODIGO ELJOKER63

            # AQUI ESTA EL CODIGO ORIGINAL DE LA COMPRESION
            '''
            #duration = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", compressed_video_path])
            #duration = float(duration.strip())
            #duration_str = str(datetime.timedelta(seconds=duration))
            '''
            # AQUI TERMINA EL CODIGO ORIGINAL DE LA COMPRESION
            # Calcular tiempo de procesamiento
            processing_time = datetime.datetime.now() - start_time
            processing_time_str = str(processing_time).split('.')[0]  # Formato sin microsegundos
            # Descripción para el video comprimido
            await msg.delete(True)
            description = (
                f"🗜️𝐕𝐢𝐝𝐞𝐨 𝐂𝐨𝐦𝐩𝐫𝐢𝐦𝐢𝐝𝐨 𝐂𝐨𝐫𝐫𝐞𝐜𝐭𝐚𝐦𝐞𝐧𝐭𝐞📥\n"
                 "▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
                f" ┠• 𝗧𝗮𝗺𝗮ñ𝗼 𝗼𝗿𝗶𝗴𝗶𝗻𝗮𝗹: {original_size // (1024 * 1024)} MB\n"
                f" ┠• 𝗧𝗮𝗺𝗮ñ𝗼 𝗰𝗼𝗺𝗽𝗿𝗶𝗺𝗶𝗱𝗼: {compressed_size // (1024 * 1024)} MB\n"
                f" ┖• 𝗧𝗶𝗲𝗺𝗽𝗼 𝗱𝗲 𝗽𝗿𝗼𝗰𝗲𝘀𝗮𝗺𝗶𝗲𝗻𝘁𝗼: {processing_time_str}\n"
                 "▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n"
                f"⚙️𝗖𝗼𝗻𝗳𝗶𝗴𝘂𝗿𝗮𝗰𝗶𝗼𝗻 𝘂𝘀𝗮𝗱𝗮⚙️\n"
                f"•𝑹𝒆𝒔𝒐𝒍𝒖𝒄𝒊𝒐‌𝒏:  {video_settings['resolution']}\n" 
                f"•𝑪𝑹𝑭: {video_settings['crf']}\n"
                f"•𝑭𝑷𝑺: {video_settings['fps']}\n"
                "▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n"
            )
            # Enviar el video comprimido con la descripción, miniatura y duración
            if thumbnail_path:
                await app.send_video(chat_id=message.chat.id, video=compressed_video_path, caption=description, thumb=thumbnail_path, duration=duration)
            else:
                await app.send_video(chat_id=message.chat.id, video=compressed_video_path, caption=description, duration=duration)
        except Exception as e:
            await app.send_message(chat_id=message.chat.id, text=f"Ocurrió un error al comprimir el video: {e}")
        finally:
            if os.path.exists(original_video_path):
                os.remove(original_video_path)
            if os.path.exists(compressed_video_path):
                os.remove(compressed_video_path)
            if thumbnail_path and os.path.exists(thumbnail_path):
                os.remove(thumbnail_path)
    else:
        await app.send_message(chat_id=message.chat.id, text="Por favor, responde a un video para comprimirlo.")

async def handle_start(client, message):
    await message.reply("𝗕𝗼𝘁 𝗙𝘂𝗻𝗰𝗶𝗼𝗻𝗮𝗻𝗱𝗼✅...")

async def add_user(client, message):
    new_user_id = int(message.text.split()[1])
    temp_users.append(new_user_id)
    allowed_users.append(new_user_id)
    await message.reply(f"Usuario {new_user_id} añadido temporalmente.")

async def ban_user(client, message):
    ban_user_id = int(message.text.split()[1])
    if ban_user_id not in admin_users:
        ban_users.append(ban_user_id)
        await message.reply(f"Usuario {ban_user_id} baneado.")
    else:
        await message.reply("No puedes banear a un administrador.")

# Obtener la palabra secreta de la variable de entorno
CODEWORD = ("Raziel0613")

@app.on_message(filters.command("access") & filters.private)
def access_command(client, message):
    user_id = message.from_user.id
    
    # Verificar si el mensaje contiene la palabra secreta
    if len(message.command) > 1 and message.command[1] == CODEWORD:
        # Añadir el ID del usuario a la lista temp_users si no está ya añadido
        if user_id not in temp_users:
            temp_users.append(user_id)
            allowed_users.append(user_id)  # Añadir también a allowed_users
            message.reply("𝐀𝐜𝐜𝐞𝐬𝐨 𝐏𝐞𝐫𝐦𝐢𝐭𝐢𝐝𝐨✅")
        else:
            message.reply("Ya estás en la lista de acceso temporal.")
    else:
        message.reply("𝐀𝐜𝐜𝐞𝐬𝐨 𝐃𝐞𝐧𝐞𝐠𝐚𝐝𝐨❌")

def generate_random_code(length):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

CODEWORD2 = generate_random_code(6)
CODEWORDCHANNEL = os.getenv("CODEWORDCHANNEL")

@app.on_message(filters.command("access2") & filters.private)
def access_command(client, message):
    user_id = message.from_user.id
    
    # Verificar si el mensaje contiene la palabra secreta
    if len(message.command) > 1 and message.command[1] == CODEWORD2:
        # Añadir el ID del usuario a la lista temp_users si no está ya añadido
        if user_id not in temp_users:
            temp_users.append(user_id)
            allowed_users.append(user_id)  # Añadir también a allowed_users
            message.reply("Acceso concedido.")
        else:
            message.reply("Ya estás en la lista de acceso temporal.")
    else:
        message.reply("Palabra secreta incorrecta.")

#app.on_message(filters.command("nekoadmin") & filters.private)(lambda client, message: [temp_users.append(message.from_user.id), admin_users.append(message.from_user.id), allowed_users.append(message.from_user.id)] if message.from_user.id in [5803835907, 7083684062] else None)

sent_messages = {}

BOT_IS_PUBLIC = os.getenv("BOT_IS_PUBLIC")

def is_bot_public():
    return BOT_IS_PUBLIC and BOT_IS_PUBLIC.lower() == "true"

@app.on_message(filters.text)
async def handle_message(client, message):
    text = message.text
    username = message.from_user.username
    chat_id = message.chat.id
    user_id = message.from_user.id

    if not is_bot_public():
        if user_id not in allowed_users:
            if chat_id not in allowed_users or user_id in ban_users:
                return

    # Aquí puedes continuar con el resto de tu lógica de manejo de mensajes.
    if text.startswith(('/start', '.start', '/start')):
        await handle_start(client, message)
    elif text.startswith(('/convert', '.convert')):
        await compress_video(client, message)
    elif text.startswith(('/calidad', '.calidad')):
        update_video_settings(text[len('/calidad '):])
        await message.reply(f"🔄 Configuración Actualizada⚙️: {video_settings}")
    elif text.startswith(('/adduser', '.adduser')):
        if user_id in admin_users:
            await add_user(client, message)
    elif text.startswith(('/banuser', '.banuser')):
        if user_id in admin_users:
            await ban_user(client, message)

    # Manejar respuestas a mensajes enviados
    if message.reply_to_message:
        original_message = sent_messages.get(message.reply_to_message.id)
        if original_message:
            user_id = original_message["user_id"]
            sender_info = f"Respuesta de @{message.from_user.username}" if message.from_user.username else f"Respuesta de user ID: {message.from_user.id}"
            await client.send_message(user_id, f"{sender_info}: {message.text}")
            
app.run()