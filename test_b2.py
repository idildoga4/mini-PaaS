import sqlite3
conn = sqlite3.connect('data/deploy.db')
conn.execute("""
    INSERT INTO deployments (user_email, project_name, github_url, status, container_name, created_at)
    VALUES ('test@test.com', 'orphantest', 'https://github.com/test/test', 'Running', 'app-orphantest', datetime('now'))
""")
conn.commit()
result = conn.execute("SELECT id, project_name, status, container_name FROM deployments WHERE project_name='orphantest'").fetchall()
print('Kayit eklendi:', result)
conn.close()
