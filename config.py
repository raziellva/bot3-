from logging import WARNING
import os


API_ID = os.environ.get("API_ID","28193212")  # Reemplaza con tu API ID
API_HASH = os.environ.get( "API_HASH","14c5ec97b18a391d526e4a461e4a5f82") # Reemplaza con tu API HASH
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8128405431:AAEE47bXEFIfD41jbESyIGM-DTp5diQmVqY") 
ADMINS_IDS = [int(x) for x in os.environ.get("ADMINS", "5004180573,5644237743").split(",") if x]
USERS = [int(x) for x in os.environ.get("USERS", "").split(",") if x]
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://eljoker63:33805157@cluster0.h0icwrx.mongodb.net")
DATABASE_NAME = os.environ.get("DATABASE_NAME", "compressbot1")
BOT_IS_PUBLIC = os.environ.get("BOT_IS_PUBLIC", "false").lower() == "true"