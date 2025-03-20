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
from typing import Dict, Any, Optional

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
            user_id INTEGER NOT NULL,
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

    # Questions and answers tables
    conn = get_db_connection("exam_questions.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            question_type TEXT NOT NULL,
            options TEXT,
            correct_answer TEXT NOT NULL
        )
    """)
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_answers (
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
    conn.close()

def save_exam_result(user_id: int, questions: list, answers: dict, score: float) -> int:
    """Save exam result and all related data"""
    # Calculate grade based on score
    grade = calculate_grade(score)  # You'll need to implement this function
    status = "Passed" if score >= 60 else "Failed"
    total_questions = len(questions)
    correct_answers = int((score / 100) * total_questions)
    
    # Save main result
    conn = get_db_connection("exam_results.db")
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO results (
                user_id, score, grade, status, subject,
                total_questions, correct_answers, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            user_id, score, grade, status, "General",
            total_questions, correct_answers
        ))
        exam_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    # Save questions and answers
    questions_conn = get_db_connection("exam_questions.db")
    try:
        for q in questions:
            # Save question
            cursor = questions_conn.cursor()
            cursor.execute("""
                INSERT INTO questions (
                    exam_id, question_text, question_type,
                    options, correct_answer
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                exam_id,
                q['question'],
                q.get('type', 'multiple_choice'),
                json.dumps(q.get('options', [])),
                q['correct_answer']
            ))
            question_id = cursor.lastrowid
            
            # Save user's answer
            user_answer = answers.get(str(q['id']), '')
            is_correct = user_answer == q['correct_answer']
            
            cursor.execute("""
                INSERT INTO user_answers (
                    exam_id, user_id, question_id,
                    answer, is_correct
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                exam_id, user_id, question_id,
                user_answer, is_correct
            ))
        
        questions_conn.commit()
    finally:
        questions_conn.close()
    
    return exam_id

def calculate_grade(score: float) -> str:
    """Calculate letter grade based on score"""
    if score >= 90:
        return "A"
    elif score >= 80:
        return "B"
    elif score >= 70:
        return "C"
    elif score >= 60:
        return "D"
    else:
        return "F"

def save_user_answer(
    user_id: int,
    exam_id: int,
    question_id: int,
    answer: str,
    is_correct: bool,
    time_taken: Optional[int] = None
) -> None:
    """Save user's answer for a question"""
    conn = get_db_connection("exams.db")
    try:
        conn.execute("""
            INSERT INTO user_answers (
                user_id, exam_id, question_id, answer,
                is_correct, time_taken
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            user_id, exam_id, question_id, answer,
            is_correct, time_taken
        ))
        conn.commit()
    finally:
        conn.close()

def get_exam_details(exam_result_id: int, user_id: int) -> dict:
    """Get detailed exam result including questions and answers"""
    conn = get_db_connection("exam_results.db")
    try:
        # Get exam result
        result = conn.execute("""
            SELECT *
            FROM results 
            WHERE id = ? AND user_id = ?
        """, (exam_result_id, user_id)).fetchone()
        
        if not result:
            return None
            
        # Convert row to dictionary
        exam_detail = {
            "id": str(result["id"]),
            "subject": result["subject"],
            "score": result["score"],
            "grade": result["grade"],
            "status": result["status"],
            "timestamp": result["timestamp"],
            "total_questions": result["total_questions"],
            "correct_answers": result["correct_answers"],
            "questions": []  # Initialize with empty list
        }
        
        try:
            # Attempt to get questions data
            questions_conn = get_db_connection("exam_questions.db")
            questions = questions_conn.execute("""
                SELECT q.id, q.question_text as question, q.correct_answer, 
                       ua.answer as user_answer, q.options, q.question_type as type
                FROM questions q
                JOIN user_answers ua ON q.id = ua.question_id
                WHERE ua.exam_id = ? AND ua.user_id = ?
                ORDER BY q.id
            """, (exam_result_id, user_id)).fetchall()
            
            if questions:
                exam_detail["questions"] = [{
                    "id": q["id"],
                    "question": q["question"],
                    "correct_answer": q["correct_answer"],
                    "user_answer": q["user_answer"],
                    "options": json.loads(q["options"]) if q["options"] else None,
                    "type": q["type"]
                } for q in questions]
            
            questions_conn.close()
        except Exception as e:
            print(f"Warning: Could not fetch questions data: {str(e)}")
            # Continue without questions data
            
        return exam_detail
        
    except Exception as e:
        print(f"Database error in get_exam_details: {str(e)}")
        raise
    finally:
        conn.close()  # Close the main connection

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

@app.route("/exam_detail/<int:exam_id>", methods=["GET"])
@jwt_required()
def get_exam_detail(exam_id):
    """Get detailed exam result"""
    try:
        user_id = get_jwt_identity()
        
        # Get exam details
        exam_detail = get_exam_details(exam_id, user_id)
        
        if not exam_detail:
            return jsonify({"error": "Exam not found"}), 404
            
        return jsonify(exam_detail)
        
    except Exception as e:
        print(f"Error in get_exam_detail endpoint: {str(e)}")
        return jsonify({"error": "Failed to get exam detail", "details": str(e)}), 500

@app.route("/submit_exam", methods=["POST"])
@jwt_required()
def submit_exam():
    """Handle exam submission"""
    try:
        user_id = get_jwt_identity()
        data = request.get_json()
        
        if not data or 'questions' not in data or 'answers' not in data:
            return jsonify({"error": "Invalid submission data"}), 400
        
        questions = data['questions']
        answers = data['answers']
        
        # Calculate score
        total_questions = len(questions)
        correct_answers = sum(
            1 for q in questions
            if answers.get(str(q['id'])) == q['correct_answer']
        )
        score = (correct_answers / total_questions) * 100 if total_questions > 0 else 0
        
        # Save all exam data
        exam_id = save_exam_result(user_id, questions, answers, score)
        
        return jsonify({
            "message": "Exam submitted successfully",
            "exam_id": exam_id,
            "score": score,
            "grade": calculate_grade(score)
        })
        
    except Exception as e:
        print(f"Error in submit_exam: {str(e)}")
        return jsonify({"error": "Failed to submit exam"}), 500

# Add this route to test if the API is accessible
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
