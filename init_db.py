import sqlite3
import os

def init_databases():
    """Initialize all required database tables with fresh schema"""
    
    # First, remove existing database files if they exist
    db_files = ["users.db", "exam_results.db", "exam_questions.db"]
    for db_file in db_files:
        if os.path.exists(db_file):
            try:
                os.remove(db_file)
                print(f"Removed existing {db_file}")
            except Exception as e:
                print(f"Error removing {db_file}: {e}")

    # Initialize Users database
    conn = sqlite3.connect("users.db")
    try:
        conn.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL
            )
        """)
        conn.commit()
        print("Successfully created users table")
    except Exception as e:
        print(f"Error creating users table: {e}")
    finally:
        conn.close()

    # Initialize Results database
    conn = sqlite3.connect("exam_results.db")
    try:
        conn.execute("""
            CREATE TABLE results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                score REAL NOT NULL,
                grade TEXT NOT NULL,
                status TEXT NOT NULL,
                subject TEXT NOT NULL,
                total_questions INTEGER,
                correct_answers INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        print("Successfully created results table")
    except Exception as e:
        print(f"Error creating results table: {e}")
    finally:
        conn.close()

    # Initialize Questions database
    conn = sqlite3.connect("exam_questions.db")
    try:
        # Create questions table
        conn.execute("""
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exam_id INTEGER NOT NULL,
                question_text TEXT NOT NULL,
                question_type TEXT NOT NULL,
                options TEXT,
                correct_answer TEXT NOT NULL,
                subject TEXT NOT NULL
            )
        """)
        
        # Create user answers table
        conn.execute("""
            CREATE TABLE user_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exam_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                question_id INTEGER NOT NULL,
                answer TEXT NOT NULL,
                is_correct BOOLEAN NOT NULL,
                time_taken INTEGER,
                FOREIGN KEY (question_id) REFERENCES questions (id)
            )
        """)
        conn.commit()
        print("Successfully created questions and user_answers tables")
    except Exception as e:
        print(f"Error creating questions/answers tables: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    print("Initializing databases...")
    init_databases()
    print("Database initialization completed")