from pymongo import MongoClient
import os
from dotenv import load_dotenv

load_dotenv()
MONGO_URI = os.getenv("MONGODB_URI")
MONGO_DB = os.getenv("MONGODB_DB", "test")
URLS_COLLECTION = "en_urls"

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
col = db[URLS_COLLECTION]

print(f"Connecting to DB={MONGO_DB}, Coll={URLS_COLLECTION}")

# Drop problematic indexes
try:
    print("Dropping index en_url_1 ...")
    col.drop_index("en_url_1")
    print("Dropped en_url_1")
except Exception as e:
    print(f"Error dropping en_url_1: {e}")

try:
    print("Dropping index published_date_-1_en_url_1 ...")
    col.drop_index("published_date_-1_en_url_1")
    print("Dropped published_date_-1_en_url_1")
except Exception as e:
    print(f"Error dropping published_date_-1_en_url_1: {e}")

print("Remaining indexes:")
for idx in col.list_indexes():
    print(f"IND: {idx.get('name')}")
