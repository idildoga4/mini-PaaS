import sqlite3
conn = sqlite3.connect('/app/data/deploy.db')
cur = conn.cursor()
cur.execute("PRAGMA table_info(deployments)")
cols = [row[1] for row in cur.fetchall()]
print("Mevcut kolonlar:", cols)
if 'error_message' not in cols:
    cur.execute("ALTER TABLE deployments ADD COLUMN error_message TEXT")
    conn.commit()
    print("error_message kolonu eklendi")
else:
    print("error_message zaten var")
conn.close()
