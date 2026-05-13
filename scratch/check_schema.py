import psycopg2
import os

def check_schema():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'backlogs'")
    cols = [r[0] for r in cur.fetchall()]
    print(f"Columns in backlogs: {cols}")
    conn.close()

if __name__ == "__main__":
    check_schema()
