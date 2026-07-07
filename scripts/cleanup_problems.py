"""Clean up problems with missing/broken starter_code and test cases."""
import sqlite3, json

DB = 'data/db/code_tutor.db'

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Step 1: Delete problems with empty starter_code
cur.execute('DELETE FROM problems WHERE starter_code = ""')
deleted = cur.rowcount
print(f"Deleted {deleted} problems with empty starter_code")

# Step 2: Delete problems with starter_code but 0 test cases
rows = conn.execute(
    'SELECT id, title, visible_test_cases_json, test_cases_json FROM problems'
).fetchall()

for r in rows:
    vtcs = json.loads(r['visible_test_cases_json']) if r['visible_test_cases_json'] else []
    tcs = json.loads(r['test_cases_json']) if r['test_cases_json'] else []
    if len(vtcs) == 0 and len(tcs) == 0:
        cur.execute('DELETE FROM problems WHERE id = ?', (r['id'],))
        print(f"Deleted #{r['id']} {r['title']} - no test cases")

conn.commit()
print()

# Step 3: Verify remaining
rows = conn.execute('SELECT id, title, difficulty FROM problems ORDER BY id').fetchall()
print(f"Final remaining: {len(rows)} problems")
for r in rows:
    sc = conn.execute('SELECT starter_code FROM problems WHERE id = ?', (r['id'],)).fetchone()
    sc_ok = bool(sc and sc[0])
    print(f"  #{r['id']} [{r['difficulty']}] {r['title']} | starter={'✅' if sc_ok else '❌'}")

conn.close()