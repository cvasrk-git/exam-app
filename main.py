from flask import Flask, request, jsonify
from flask_cors import CORS
import openai
import json
import re
import os
import time
import uuid
from dotenv import load_dotenv
from azure.data.tables import TableServiceClient, UpdateMode
from flask_bcrypt import Bcrypt
from flask_jwt_extended import JWTManager, create_access_token
import sqlite3
from flask_jwt_extended import jwt_required, get_jwt_identity

app = Flask(__name__)
CORS(app)  # Allow React frontend to call API

# Load environment variables from .env file
load_dotenv()

bcrypt = Bcrypt(app)
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY")
jwt = JWTManager(app)

# Azure OpenAI Configuration
API_KEY = os.getenv("API_KEY")
API_ENDPOINT = os.getenv("API_ENDPOINT")
DEPLOYMENT_NAME = os.getenv("DEPLOYMENT_NAME")
API_VERSION = os.getenv("API_VERSION")

# Azure Table Storage Configuration
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

# Database connection
def get_db_connection():
    conn = sqlite3.connect("users.db")
    conn.row_factory = sqlite3.Row
    return conn

# Create users table
def create_users_table():
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

create_users_table()  # Ensure table exists on startup

# Initialize Azure Table Service Client
table_service_client = TableServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
table_name = "ExamResults"

# Ensure Table Exists
try:
    table_service_client.create_table(table_name)
    print(f"✅ Table '{table_name}' created successfully!")
except Exception:
    print(f"ℹ️ Table '{table_name}' already exists.")

# Get Table Client
table_client = table_service_client.get_table_client(table_name)

# OpenAI Client
openai_client = openai.AzureOpenAI(
    azure_endpoint=API_ENDPOINT,
    api_key=API_KEY,
    api_version=API_VERSION,
)

DEFAULT_TIME_LIMIT = 30  # Default question time limit in seconds
question_start_times = {}  # Store question start times

@app.route("/protected", methods=["GET"])
@jwt_required()
def protected():
    current_user = get_jwt_identity()
    return jsonify({"message": f"Welcome {current_user}!"})

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")

    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()

    if user and bcrypt.check_password_hash(user["password"], password):
        token = create_access_token(identity=email)
        return jsonify({"token": token})
    else:
        return jsonify({"error": "Invalid credentials"}), 401

# Database connection for exam results
def get_results_db_connection():
    conn = sqlite3.connect("exam_results.db")
    conn.row_factory = sqlite3.Row
    return conn

# Create results table
def create_results_table():
    conn = get_results_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            score REAL NOT NULL,
            grade TEXT NOT NULL,
            status TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

# Ensure the table is created on startup
create_results_table()

@app.route("/register", methods=["POST"])
def register():
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")

    hashed_password = bcrypt.generate_password_hash(password).decode("utf-8")

    conn = get_db_connection()
    try:
        conn.execute("INSERT INTO users (email, password) VALUES (?, ?)", (email, hashed_password))
        conn.commit()
        return jsonify({"message": "User registered successfully!"})
    except sqlite3.IntegrityError:
        return jsonify({"error": "Email already exists"}), 400
    finally:
        conn.close()

@app.route("/generate_questions", methods=["POST"])
def generate_questions():
    """Generate exam questions using OpenAI"""
    data = request.json
    prompt = data.get("prompt", "").strip()
    user_id = data.get("user_id")

    if not prompt:
        return jsonify({"error": "Prompt is required"}), 400

    formatted_prompt = f"""
    Generate a set of exam questions based on the prompt:
    '{prompt}'

    Each question must be a dictionary with:
    - 'id': unique question ID
    - 'question': question text
    - 'type': one of ('mcq', 'true_false', 'short_answer', 'coding', 'essay')
    - 'options': list of possible answers (only for 'mcq' and 'true_false')
    - 'correct_answer': correct answer text (except for 'coding' and 'essay')
    - 'hint': a short hint for the question
    - 'time_limit': time in seconds (default {DEFAULT_TIME_LIMIT})

    Format the response as a JSON array of question objects without Markdown formatting.
    """

    try:
        response = openai_client.chat.completions.create(
            model=DEPLOYMENT_NAME,
            messages=[{"role": "system", "content": formatted_prompt}],
            temperature=0.7,
            max_tokens=800,
        )

        response_text = response.choices[0].message.content.strip()
        clean_json = re.sub(r"```json\n(.*?)\n```", r"\1", response_text, flags=re.DOTALL)

        try:
            questions_json = json.loads(clean_json)
            if not isinstance(questions_json, list):
                raise ValueError("Invalid JSON response from OpenAI.")

            question_start_times[user_id] = {}
            for question in questions_json:
                question_id = str(question.get("id", ""))
                question.setdefault("hint", "No hint available")  # Ensure hint is always present
                question.setdefault("time_limit", DEFAULT_TIME_LIMIT)
                question_start_times[user_id][question_id] = time.time()

            return jsonify({"questions": questions_json})

        except json.JSONDecodeError:
            return jsonify({"error": "Invalid JSON format received from OpenAI"}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/validate_answers", methods=["POST"])
def validate_answers():
    """Validate user answers and store results in SQLite database"""
    try:
        data = request.get_json()

        # Validate request
        if not data or "answers" not in data or "questions" not in data or "user_id" not in data:
            return jsonify({"error": "Invalid request. Expected 'user_id', 'answers', and 'questions'."}), 400

        user_id = data["user_id"]
        answers = data["answers"]
        questions = {str(q["id"]): q for q in data["questions"]}  # Map questions by ID

        correct_count = 0  # Correct answer counter
        total_questions = len(questions)

        if total_questions == 0:
            return jsonify({"error": "No questions provided."}), 400

        for q_id, user_answer in answers.items():
            question_data = questions.get(q_id)

            if not question_data or "correct_answer" not in question_data:
                continue

            correct_answer = question_data["correct_answer"]

            # Normalize answers for comparison
            normalized_user_answer = str(user_answer).strip().lower()
            normalized_correct_answer = str(correct_answer).strip().lower()

            if normalized_user_answer == normalized_correct_answer:
                correct_count += 1

        # Score Calculation
        score_percentage = (correct_count / total_questions) * 100 if total_questions else 0
        score_percentage = round(score_percentage, 2)  # Round score to 2 decimal places

        # Assign Grade
        grade = (
            "A" if score_percentage >= 90 else
            "B" if score_percentage >= 80 else
            "C" if score_percentage >= 70 else
            "D" if score_percentage >= 50 else "F"
        )

        # Assign Status
        status = "Passed" if score_percentage >= 50 else "Failed"

        # Store results in SQLite
        conn = get_results_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO results (user_id, score, grade, status)
            VALUES (?, ?, ?, ?)
        """, (user_id, score_percentage, grade, status))
        conn.commit()
        conn.close()

        print("✅ Exam result successfully saved in SQLite!")

        return jsonify({
            "user_id": user_id,
            "Score": score_percentage,
            "Grade": grade,
            "Status": status
        })

    except Exception as e:
        print(f"❌ Error saving exam result: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/get_results", methods=["GET"])
@jwt_required()
def get_results():
    """Retrieve exam results for the logged-in user"""
    try:
        user_id = get_jwt_identity()  # Get user ID from token

        conn = get_results_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM results WHERE user_id = ? ORDER BY timestamp DESC", (user_id,))
        results = cursor.fetchall()
        conn.close()

        if not results:
            return jsonify({"message": "No results found for this user."}), 404

        results_list = [
            {
                "userid": row["user_id"],
                "score": row["score"],
                "grade": row["grade"],
                "status": row["status"],
                "timestamp": row["timestamp"],
            }
            for row in results
        ]

        return jsonify({"results": results_list})

    except Exception as e:
        print(f"❌ Error retrieving exam results: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
