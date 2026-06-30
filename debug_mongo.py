from pymongo import MongoClient
import os
import sys
from dotenv import load_dotenv

load_dotenv()
MONGO_URI = os.getenv("MONGODB_URI")
MONGO_DB = os.getenv("MONGODB_DB", "test")
URLS_COLLECTION = "en_urls"

with open("debug_mongo_out.txt", "w", encoding="utf-8") as f:
    f.write(f"Connecting to DB={MONGO_DB}, Coll={URLS_COLLECTION}\n")
    
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    col = db[URLS_COLLECTION]

    count = col.count_documents({})
    f.write(f"Collection count: {count}\n")

    f.write("Indexes:\n")
    for idx in col.list_indexes():
        f.write(f"IND: {idx}\n")

    f.write("\nDocuments:\n")
    for doc in col.find():
        f.write(f"DOC: {doc}\n")
