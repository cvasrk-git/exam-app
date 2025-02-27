import sqlite3

def create_results_table():
    conn = sqlite3.connect("exam_results.db")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                score REAL NOT NULL,
                grade TEXT NOT NULL,
                status TEXT NOT NULL,
                subject TEXT,
                total_questions INTEGER,
                correct_answers INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        print("Successfully created results table")
    except sqlite3.OperationalError as e:
        print(f"Error creating table: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    create_results_table()
