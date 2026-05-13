import psycopg2
import os
import json

def check():
    url = os.getenv('DATABASE_URL')
    if not url:
        print("URL MISSING")
        return
    try:
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name = 'tasks'")
        cols = cur.fetchall()
        print(json.dumps(cols, indent=2))
        conn.close()
    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    check()
