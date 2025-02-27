from flask import Flask, request, jsonify
from flask_cors import CORS
import openai
import json
import re
import os
import time
from dotenv import load_dotenv
from flask_bcrypt import Bcrypt
from flask_jwt_extended import (
    JWTManager, 
    create_access_token, 
    jwt_required, 
    get_jwt_identity
)
import sqlite3
from typing import Dict, Any

# Initialize Flask app
app = Flask(__name__)
CORS(app)

# Load environment variables
load_dotenv()

# Configure app settings
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY")
DEFAULT_TIME_LIMIT = 30

# Initialize extensions
bcrypt = Bcrypt(app)
jwt = JWTManager(app)

# Azure OpenAI Configuration
openai_client = openai.AzureOpenAI(
    azure_endpoint=os.getenv("API_ENDPOINT"),
    api_key=os.getenv("API_KEY"),
    api_version=os.getenv("API_VERSION"),
)

# Global variables
question_start_times: Dict[str, Dict[str, float]] = {}

# Database helper functions
def get_db_connection(db_name: str = "users.db") -> sqlite3.Connection:
    """Create a database connection with Row factory"""
    conn = sqlite3.connect(db_name)
    conn.row_factory = sqlite3.Row
    return conn

def init_databases():
    """Initialize all required database tables"""
    # Users table
    conn = get_db_connection("users.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    # Results table
    conn = get_db_connection("exam_results.db")
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
    conn.close()

# Initialize databases on startup
init_databases()

# Authentication routes
@app.route("/register", methods=["POST"])
def register():
    """Register a new user"""
    try:
        data = request.get_json()
        email = data.get("email")
        password = data.get("password")

        if not email or not password:
            return jsonify({"error": "Email and password are required"}), 400

        hashed_password = bcrypt.generate_password_hash(password).decode("utf-8")

        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT INTO users (email, password) VALUES (?, ?)", 
                (email, hashed_password)
            )
            conn.commit()
            return jsonify({"message": "User registered successfully!"})
        except sqlite3.IntegrityError:
            return jsonify({"error": "Email already exists"}), 400
        finally:
            conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/login", methods=["POST"])
def login():
    """Authenticate user and return JWT token"""
    try:
        data = request.get_json()
        email = data.get("email")
        password = data.get("password")

        if not email or not password:
            return jsonify({"error": "Email and password are required"}), 400

        conn = get_db_connection()
        user = conn.execute(
            "SELECT * FROM users WHERE email = ?", 
            (email,)
        ).fetchone()
        conn.close()

        if user and bcrypt.check_password_hash(user["password"], password):
            token = create_access_token(identity=email)
            return jsonify({"token": token})
        
        return jsonify({"error": "Invalid credentials"}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/protected", methods=["GET"])
@jwt_required()
def protected():
    """Test protected route"""
    current_user = get_jwt_identity()
    return jsonify({"message": f"Welcome {current_user}!"})

# Exam routes
@app.route("/generate_questions", methods=["POST"])
@jwt_required()
def generate_questions():
    """Generate exam questions using Azure OpenAI"""
    try:
        data = request.get_json()
        prompt = data.get("prompt", "").strip()
        user_id = get_jwt_identity()

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

        response = openai_client.chat.completions.create(
            model=os.getenv("DEPLOYMENT_NAME"),
            messages=[{"role": "system", "content": formatted_prompt}],
            temperature=0.7,
            max_tokens=800,
        )

        response_text = response.choices[0].message.content.strip()
        clean_json = re.sub(r"```json\n(.*?)\n```", r"\1", response_text, flags=re.DOTALL)
        questions_json = json.loads(clean_json)

        if not isinstance(questions_json, list):
            raise ValueError("Invalid JSON response from OpenAI.")

        # Initialize question timing
        question_start_times[user_id] = {
            str(q.get("id", "")): time.time() 
            for q in questions_json
        }

        # Ensure required fields
        for question in questions_json:
            question.setdefault("hint", "No hint available")
            question.setdefault("time_limit", DEFAULT_TIME_LIMIT)

        return jsonify({"questions": questions_json})

    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON format received from OpenAI"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/validate_answers", methods=["POST"])
@jwt_required()
def validate_answers():
    """Validate answers and store results"""
    try:
        data = request.get_json()
        user_id = get_jwt_identity()

        if not data or "answers" not in data or "questions" not in data:
            return jsonify({"error": "Invalid request format"}), 400

        answers = data["answers"]
        questions = {str(q["id"]): q for q in data["questions"]}
        subject = data.get("subject", "General")

        # Validate answers
        correct_count = sum(
            1 for q_id, user_answer in answers.items()
            if questions.get(q_id) and 
            str(user_answer).strip().lower() == 
            str(questions[q_id]["correct_answer"]).strip().lower()
        )

        total_questions = len(questions)
        if total_questions == 0:
            return jsonify({"error": "No questions provided"}), 400

        # Calculate results
        score_percentage = round((correct_count / total_questions) * 100, 2)
        grade = (
            "A" if score_percentage >= 90 else
            "B" if score_percentage >= 80 else
            "C" if score_percentage >= 70 else
            "D" if score_percentage >= 50 else "F"
        )
        status = "Passed" if score_percentage >= 50 else "Failed"

        # Store results
        conn = get_db_connection("exam_results.db")
        conn.execute("""
            INSERT INTO results (
                user_id, score, grade, status, subject, 
                total_questions, correct_answers
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id, score_percentage, grade, status, subject,
            total_questions, correct_count
        ))
        conn.commit()
        conn.close()

        return jsonify({
            "user_id": user_id,
            "score": score_percentage,
            "grade": grade,
            "status": status,
            "subject": subject,
            "total_questions": total_questions,
            "correct_answers": correct_count
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/get_results", methods=["GET"])
@jwt_required()
def get_results():
    """Retrieve exam results for the current user"""
    try:
        user_id = get_jwt_identity()
        conn = get_db_connection("exam_results.db")
        
        results = conn.execute("""
            SELECT *
            FROM results 
            WHERE user_id = ? 
            ORDER BY timestamp DESC
        """, (user_id,)).fetchall()
        
        conn.close()

        if not results:
            return jsonify({"message": "No results found"}), 404

        results_list = [{
            "id": row["id"],
            "user_id": row["user_id"],
            "score": row["score"],
            "grade": row["grade"],
            "status": row["status"],
            "timestamp": row["timestamp"],
            "subject": row["subject"],
            "total_questions": row["total_questions"],
            "correct_answers": row["correct_answers"]
        } for row in results]

        # Calculate statistics
        total_exams = len(results_list)
        average_score = round(
            sum(r["score"] for r in results_list) / total_exams 
            if total_exams > 0 else 0, 
            2
        )

        return jsonify({
            "results": results_list,
            "statistics": {
                "total_exams": total_exams,
                "average_score": average_score,
                "last_exam_score": results_list[0]["score"] if results_list else 0
            }
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
