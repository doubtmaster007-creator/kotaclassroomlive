import psycopg2
import os

def check_constraints():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set")
        return
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    
    print("--- Constraints on 'backlogs' table ---")
    cur.execute("""
        SELECT conname, pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE contype = 'c' AND conrelid = 'backlogs'::regclass;
    """)
    constraints = cur.fetchall()
    for conname, definition in constraints:
        print(f"Constraint: {conname}")
        print(f"Definition: {definition}")
        
    print("\n--- Columns in 'backlogs' table ---")
    cur.execute("SELECT column_name, data_type, column_default FROM information_schema.columns WHERE table_name = 'backlogs'")
    for row in cur.fetchall():
        print(row)
        
    conn.close()

if __name__ == "__main__":
    check_constraints()
