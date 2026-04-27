import os
import psycopg2
import traceback
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    print("❌ DATABASE_URL not found in .env file!")
    exit(1)

# Auto-fix Supabase port
if "pooler.supabase.com" in DATABASE_URL and ":5432" in DATABASE_URL:
    print("🔄 Auto-correcting port 5432 -> 6543...")
    DATABASE_URL = DATABASE_URL.replace(":5432", ":6543")

try:
    print(f"📡 Connecting to: {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else 'HIDDEN'}")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT version();")
    print('✅ Connection successful! DB Version:', cur.fetchone()[0])
    
    cur.execute("SELECT user_id, phone FROM users LIMIT 5")
    print('Sample Users:', cur.fetchall())
    
    cur.close()
    conn.close()
except Exception as e:
    print(f"❌ Connection failed: {e}")
    traceback.print_exc()
