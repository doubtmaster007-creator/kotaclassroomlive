import os
import psycopg2
import psycopg2.extras

DATABASE_URL = os.getenv("DATABASE_URL", "")

def check_teachers():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM teachers")
        rows = cur.fetchall()
        print("--- Teachers Table ---")
        for row in rows:
            print(row)
        
        cur.execute("SELECT * FROM users WHERE user_id::text LIKE '%82339%'")
        rows = cur.fetchall()
        print("\n--- Users matching 82339 ---")
        for row in rows:
            print(row)
            
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_teachers()
