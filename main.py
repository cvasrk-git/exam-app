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
    print("Starting database initialization...")
    
    try:
        # Create exam_questions.db first
        print("Creating exam_questions.db tables...")
        conn = get_db_connection("exam_questions.db")
        cursor = conn.cursor()
        
        # Create questions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exam_id INTEGER NOT NULL,
                question_text TEXT NOT NULL,
                question_type TEXT NOT NULL,
                options TEXT,
                correct_answer TEXT,
                subject TEXT DEFAULT 'General'
            )
        """)
        print("Created questions table")
        
        # Create user_answers table with accuracy_percentage column
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exam_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                question_id INTEGER NOT NULL,
                answer TEXT,
                is_correct INTEGER,
                accuracy_percentage REAL DEFAULT 0,
                time_taken INTEGER,
                FOREIGN KEY (question_id) REFERENCES questions (id)
            )
        """)
        print("Created user_answers table")
        conn.commit()
        conn.close()

        # Create exam_results.db
        print("Creating exam_results.db tables...")
        conn = get_db_connection("exam_results.db")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                score REAL NOT NULL,
                grade TEXT NOT NULL,
                status TEXT NOT NULL,
                subject TEXT DEFAULT 'General',
                total_questions INTEGER NOT NULL,
                correct_answers INTEGER NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

        # Create detailed results table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS detailed_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exam_id INTEGER NOT NULL,
                question_id INTEGER NOT NULL,
                score FLOAT,
                feedback TEXT,
                evaluation_data TEXT,
                FOREIGN KEY (exam_id) REFERENCES results (id)
            )
        """)
        conn.commit()
        conn.close()
        print("Database initialization completed successfully")

    except Exception as e:
        print(f"Error during database initialization: {str(e)}")
        raise
    finally:
        try:
            conn.close()
        except:
            pass

def verify_db_structure():
    """Verify database structure and print current state"""
    try:
        # Check exam_questions.db structure
        conn = get_db_connection("exam_questions.db")
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        print("\nTables in exam_questions.db:", [t[0] for t in tables])
        
        # Print schema for each table
        for table in tables:
            schema = conn.execute(f"PRAGMA table_info({table[0]})").fetchall()
            print(f"\nSchema for {table[0]}:")
            for col in schema:
                print(f"  {col[1]} ({col[2]})")
        
        return True
    except Exception as e:
        print(f"Error verifying database structure: {str(e)}")
        return False
    finally:
        try:
            conn.close()
        except:
            pass

def save_exam_result(user_id: str, questions: list, answers: dict, score: float, subject: str, detailed_results: list = None) -> int:
    """Save exam result and all related data"""
    # Calculate grade based on score
    grade = calculate_grade(score)
    status = "Passed" if score >= 60 else "Failed"
    total_questions = len(questions)
    
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
            user_id, score, grade, status, subject,
            total_questions, int((score / 100) * total_questions)
        ))
        exam_id = cursor.lastrowid
        conn.commit()

        # Save detailed results if provided
        if detailed_results:
            for result in detailed_results:
                cursor.execute("""
                    INSERT INTO detailed_results (
                        exam_id, question_id, score, feedback,
                        evaluation_data
                    ) VALUES (?, ?, ?, ?, ?)
                """, (
                    exam_id,
                    result["question_id"],
                    result["score"],
                    result.get("feedback"),
                    json.dumps(result.get("evaluation"))
                ))
            conn.commit()

        # Save individual questions and answers
        for question in questions:
            q_id = str(question["id"])
            q_type = question.get("type", "").lower()
            correct_answer = question.get("correct_answer", "")
            
            # Save question
            cursor.execute("""
                INSERT INTO questions (
                    id, exam_id, question_text, question_type,
                    correct_answer, subject
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                q_id, exam_id, question["question"], q_type,
                correct_answer, question.get("subject", subject)
            ))
            question_id = cursor.lastrowid
            
            # Save user's answer
            user_answer = answers.get(str(q_id), '')
            is_correct = None
            if q_type not in ['essay', 'coding']:
                is_correct = 1 if str(user_answer).strip().lower() == str(correct_answer).strip().lower() else 0
            
            cursor.execute("""
                INSERT INTO user_answers (
                    exam_id, user_id, question_id,
                    answer, is_correct, time_taken
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                exam_id, user_id, question_id,
                user_answer, is_correct, None
            ))
        
        conn.commit()
        return exam_id

    except Exception as e:
        print(f"Error saving exam result: {str(e)}")
        conn.rollback()
        raise
    finally:
        conn.close()  # Close the main connection

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
            "score": result["score"],
            "grade": result["grade"],
            "status": result["status"],
            "timestamp": result["timestamp"],
            "total_questions": result["total_questions"],
            "correct_answers": result["correct_answers"],
            "questions": [],
            "subject": result["subject"] if result["subject"] != "General" else None
        }
        
        try:
            # Get questions data with accuracy percentage
            questions_conn = get_db_connection("exam_questions.db")
            questions = questions_conn.execute("""
                SELECT 
                    q.id, 
                    q.question_text as question, 
                    q.correct_answer, 
                    ua.answer as user_answer, 
                    q.options, 
                    q.question_type as type,
                    q.subject,
                    ua.accuracy_percentage,
                    ua.is_correct
                FROM questions q
                JOIN user_answers ua ON q.id = ua.question_id
                WHERE ua.exam_id = ? AND ua.user_id = ?
                ORDER BY q.id
            """, (exam_result_id, user_id)).fetchall()
            
            if questions:
                if not exam_detail["subject"]:
                    subjects = [q["subject"] for q in questions if q["subject"] != "General"]
                    if subjects:
                        from collections import Counter
                        exam_detail["subject"] = Counter(subjects).most_common(1)[0][0]
                    else:
                        exam_detail["subject"] = "General"
                
                exam_detail["questions"] = [{
                    "id": q["id"],
                    "question": q["question"],
                    "correct_answer": q["correct_answer"],
                    "user_answer": q["user_answer"],
                    "options": json.loads(q["options"]) if q["options"] else None,
                    "type": q["type"],
                    "subject": q["subject"],
                    "accuracy_percentage": q["accuracy_percentage"],
                    "is_correct": bool(q["is_correct"])
                } for q in questions]
            
            questions_conn.close()
            return exam_detail
            
        except Exception as e:
            print(f"Warning: Could not fetch questions data: {str(e)}")
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
        first_name = data.get("first_name")
        last_name = data.get("last_name")

        # Validate required fields
        if not all([email, password, first_name, last_name]):
            return jsonify({
                "error": "Email, password, first name, and last name are required"
            }), 400

        hashed_password = bcrypt.generate_password_hash(password).decode("utf-8")

        conn = get_db_connection("users.db")
        try:
            conn.execute(
                """
                INSERT INTO users (email, password, first_name, last_name)
                VALUES (?, ?, ?, ?)
                """, 
                (email, hashed_password, first_name, last_name)
            )
            conn.commit()
            return jsonify({
                "message": "User registered successfully!",
                "user": {
                    "email": email,
                    "first_name": first_name,
                    "last_name": last_name
                }
            })
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

        conn = get_db_connection("users.db")
        try:
            user = conn.execute(
                "SELECT * FROM users WHERE email = ?", 
                (email,)
            ).fetchone()

            if user and bcrypt.check_password_hash(user["password"], password):
                access_token = create_access_token(identity=email)
                return jsonify({
                    "token": access_token,
                    "user": {
                        "email": user["email"],
                        "first_name": user["first_name"],
                        "last_name": user["last_name"]
                    }
                })
            else:
                return jsonify({"error": "Invalid email or password"}), 401
        finally:
            conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/protected", methods=["GET"])
@jwt_required()
def protected():
    """Test protected route"""
    current_user = get_jwt_identity()
    return jsonify({"message": f"Welcome {current_user}!"})

# Exam routes
def extract_subject(text: str) -> str:
    """Extract subject from text based on common subjects"""
    subjects = [
        # Academic subjects
        "Mathematics", "Physics", "Chemistry", "Biology",
        "History", "Geography", "Literature", "English",
        # Technology subjects
        "Python", "Java", "JavaScript", "TypeScript",
        "React", "Angular", "Vue", "NodeJS",
        "Database", "SQL", "MongoDB", "AWS",
        "Docker", "Kubernetes", "DevOps", "Machine Learning",
        "Artificial Intelligence", "Web Development",
        "Mobile Development", "Cloud Computing",
        "Cybersecurity", "Networking", "Data Structures",
        "Algorithms", "Software Engineering"
    ]
    
    text_lower = text.lower()
    for subject in subjects:
        if subject.lower() in text_lower:
            return subject
    return "General"

def extract_question_type(text: str) -> str:
    """Extract question type from prompt text, defaults to mcq if no match found"""
    # Define keywords for each question type
    type_keywords = {
        "mcq": ["mcq", "multiple choice", "multiple-choice"],
        "true_false": ["true false", "true/false", "true-false", "t/f"],
        "short_answer": ["short answer", "short-answer", "brief answer"],
        "coding": ["coding", "code", "programming"],
        "essay": ["essay", "long answer", "written response"]
    }
    
    text_lower = text.lower()
    for q_type, keywords in type_keywords.items():
        if any(keyword in text_lower for keyword in keywords):
            return q_type
    return "mcq"  # Default to mcq instead of None

def extract_difficulty_level(text: str) -> str:
    """Extract difficulty level from prompt text based on keywords"""
    level_keywords = {
        "basic": ["basic", "beginner", "elementary", "easy", "fundamental", "simple", "entry level", "entry-level"],
        "intermediate": ["intermediate", "medium", "moderate", "middle", "regular"],
        "advanced": ["advanced", "expert", "difficult", "hard", "complex", "challenging", "advanced level", "expert level"]
    }
    
    text_lower = text.lower()
    for level, keywords in level_keywords.items():
        if any(keyword in text_lower for keyword in keywords):
            return level
    return "intermediate"  # Default to intermediate if no level is specified

@app.route("/generate_questions", methods=["POST"])
@jwt_required()
def generate_questions():
    """Generate exam questions using Azure OpenAI"""
    try:
        data = request.get_json()
        prompt = data.get("prompt", "").strip()
        user_id = get_jwt_identity()

        if not prompt:
            return jsonify({
                "error": "Prompt is required. Please provide a detailed prompt including subject, question type, and difficulty level."
            }), 400

        # Extract subject, question type, and difficulty level from prompt
        subject = extract_subject(prompt)
        question_type = extract_question_type(prompt)
        difficulty_level = extract_difficulty_level(prompt)
        
        formatted_prompt = f"""
        Generate {difficulty_level.upper()} level {question_type.upper()} questions for the subject '{subject}' based on the prompt:
        '{prompt}'

        Difficulty Level: {difficulty_level}
        {get_level_specific_rules(difficulty_level)}

        Follow these strict rules for the {question_type} type:
        {get_question_type_rules(question_type)}

        Each question must be a dictionary with:
        - 'id': unique question ID
        - 'question': clear, well-formed question text
        - 'type': "{question_type}"
        - 'level': "{difficulty_level}"
        {get_type_specific_fields(question_type)}
        - 'hint': specific, helpful hint that guides without giving away the answer
        - 'time_limit': time in seconds (default {DEFAULT_TIME_LIMIT})
        - 'subject': "{subject}"

        Format the response as a JSON array of question objects without Markdown formatting.
        """
        print(f"Generating questions with level: {difficulty_level}")
        
        response = openai_client.chat.completions.create(
            model=os.getenv("DEPLOYMENT_NAME"),
            messages=[
                {
                    "role": "system", 
                    "content": f"You are an expert {question_type} question generator specializing in {difficulty_level} level questions. Generate only {difficulty_level} {question_type} questions following the specified format."
                },
                {"role": "user", "content": formatted_prompt}
            ],
            temperature=0.7,
            max_tokens=800,
        )

        response_text = response.choices[0].message.content.strip()
        clean_json = re.sub(r"```json\n(.*?)\n```", r"\1", response_text, flags=re.DOTALL)
        questions_json = json.loads(clean_json)

        # Validate questions
        validate_questions(questions_json, question_type, difficulty_level)

        return jsonify({
            "questions": questions_json,
            "subject": subject,
            "question_type": question_type,
            "level": difficulty_level
        })

    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON format received from OpenAI"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def get_question_type_rules(q_type: str) -> str:
    """Get specific rules for each question type"""
    rules = {
        "mcq": """
        - Each question must have exactly 4 distinct options
        - One and only one correct answer
        - Options should be plausible but clearly distinguishable
        - Avoid "all of the above" or "none of the above" options
        Example:
        {
            "id": 1,
            "type": "mcq",
            "question": "Which keyword is used to prevent a class from being inherited in Java?",
            "options": ["final", "static", "private", "sealed"],
            "correct_answer": "final",
            "hint": "Think about class modifiers that restrict inheritance"
        }
        """,
        
        "true_false": """
        - Question must be a complete, verifiable statement
        - Must use ["True", "False"] as options
        - Avoid negative statements
        - Make statements specific and unambiguous
        Example:
        {
            "id": 1,
            "type": "true_false",
            "question": "Java supports multiple inheritance through interfaces.",
            "options": ["True", "False"],
            "correct_answer": "True",
            "hint": "Consider Java's inheritance and interface mechanisms"
        }
        """,
        
        "short_answer": """
        - Question should require a specific, brief answer
        - No options should be provided
        - Correct answer should be clear and concise
        - Avoid questions that could have multiple valid answers
        Example:
        {
            "id": 1,
            "type": "short_answer",
            "question": "What Java keyword is used to define a constant variable?",
            "correct_answer": "final",
            "hint": "Think about variable modifiers that prevent value changes"
        }
        """
    }
    return rules.get(q_type, "Follow standard question format rules.")

def get_type_specific_fields(q_type: str) -> str:
    """Get required fields based on question type"""
    fields = {
        "mcq": """
        - 'options': list of exactly 4 possible answers
        - 'correct_answer': exact text matching one of the options""",
        
        "true_false": """
        - 'options': ["True", "False"]
        - 'correct_answer': either "True" or "False\"""",
        
        "short_answer": """
        - 'correct_answer': the expected brief answer""",
        
        "coding": """
        - 'correct_answer': sample solution code
        - 'test_cases': list of input/output test cases""",
        
        "essay": """
        - 'correct_answer': model answer or grading rubric"""
    }
    return fields.get(q_type, "")

def validate_questions(questions: list, expected_type: str, expected_level: str) -> None:
    """Validate questions match the expected type, format, and difficulty level"""
    for question in questions:
        # Validate question type
        if question.get("type") != expected_type:
            raise ValueError(f"Question type mismatch. Expected {expected_type}, got {question.get('type')}")
        
        # Validate difficulty level
        if question.get("level") != expected_level:
            question["level"] = expected_level
        
        # Validate type-specific requirements
        if expected_type == "mcq":
            if not question.get("options") or len(question["options"]) != 4:
                raise ValueError("MCQ questions must have exactly 4 options")
        elif expected_type == "true_false":
            if question.get("options") != ["True", "False"]:
                question["options"] = ["True", "False"]
        elif expected_type == "short_answer":
            if "options" in question:
                del question["options"]

def get_question_examples(q_type: str, subject: str = None) -> dict:
    """Get example questions and answers based on question type and subject"""
    examples = {
        "essay": {
            "programming": [
                {
                    "question": "Explain the differences between REST and GraphQL APIs, including their advantages and disadvantages.",
                    "correct_answer": """
                    A comprehensive comparison of REST and GraphQL should include:

                    1. Architecture:
                    - REST: Resource-based with multiple endpoints
                    - GraphQL: Single endpoint with query language

                    2. Data Fetching:
                    - REST: Multiple round trips for complex data
                    - GraphQL: Single request for multiple resources

                    3. Advantages of REST:
                    - Caching capabilities
                    - Widely adopted and understood
                    - Better for simple CRUD operations

                    4. Advantages of GraphQL:
                    - Flexible data fetching
                    - Reduced over-fetching
                    - Strong typing system

                    5. Use cases for each
                    6. Performance considerations
                    """,
                    "rubric": {
                        "understanding": 30,
                        "completeness": 25,
                        "examples": 20,
                        "analysis": 25
                    }
                }
            ],
            "general": [
                {
                    "question": "Discuss the impact of artificial intelligence on modern society.",
                    "correct_answer": """
                    Key points to cover:
                    1. Economic impact (automation, job market changes)
                    2. Social implications (privacy, social interaction)
                    3. Ethical considerations
                    4. Future prospects and challenges
                    5. Current applications and their effects
                    """,
                    "rubric": {
                        "critical_thinking": 30,
                        "evidence": 25,
                        "structure": 25,
                        "conclusion": 20
                    }
                }
            ]
        },
        "short_answer": {
            "programming": [
                {
                    "question": "What is the purpose of the 'static' keyword in Java?",
                    "correct_answer": "To declare members that belong to the class rather than instances",
                    "explanation": "The static keyword indicates class-level scope rather than instance-level"
                },
                {
                    "question": "What does SOLID stand for in object-oriented design?",
                    "correct_answer": "Single responsibility, Open-closed, Liskov substitution, Interface segregation, Dependency inversion",
                    "explanation": "These are the five basic principles of object-oriented programming and design"
                }
            ],
            "mathematics": [
                {
                    "question": "What is the derivative of e^x?",
                    "correct_answer": "e^x",
                    "explanation": "The exponential function is its own derivative"
                }
            ]
        },
        "coding": {
            "python": [
                {
                    "question": "Write a function that returns the fibonacci sequence up to n numbers",
                    "correct_answer": """
                    def fibonacci(n):
                        sequence = [0, 1]
                        while len(sequence) < n:
                            sequence.append(sequence[-1] + sequence[-2])
                        return sequence[:n]
                    """,
                    "test_cases": [
                        {"input": "5", "output": "[0, 1, 1, 2, 3]"},
                        {"input": "3", "output": "[0, 1, 1]"}
                    ],
                    "evaluation_criteria": {
                        "correctness": 40,
                        "efficiency": 30,
                        "style": 15,
                        "documentation": 15
                    }
                }
            ],
            "javascript": [
                {
                    "question": "Write a function to deep clone an object in JavaScript",
                    "correct_answer": """
                    function deepClone(obj) {
                        if (obj === null || typeof obj !== 'object') {
                            return obj;
                        }
                        
                        const clone = Array.isArray(obj) ? [] : {};
                        
                        for (let key in obj) {
                            if (Object.prototype.hasOwnProperty.call(obj, key)) {
                                clone[key] = deepClone(obj[key]);
                            }
                        }
                        
                        return clone;
                    }
                    """,
                    "test_cases": [
                        {
                            "input": "{ a: 1, b: { c: 2 } }",
                            "output": "{ a: 1, b: { c: 2 } }"
                        }
                    ],
                    "evaluation_criteria": {
                        "correctness": 40,
                        "handling_edge_cases": 30,
                        "performance": 30
                    }
                }
            ]
        }
    }
    
    if subject and q_type in examples:
        return examples[q_type].get(subject.lower(), examples[q_type].get("general", []))
    return examples.get(q_type, [])

def evaluate_essay(question: str, correct_answer: str, user_answer: str) -> dict:
    """Evaluate essay answers using OpenAI"""
    prompt = f"""
    Evaluate this essay response based on the following criteria:
    Question: {question}
    Model Answer: {correct_answer}
    Student's Answer: {user_answer}

    Please analyze:
    1. Content relevance (0-100)
    2. Accuracy of information (0-100)
    3. Clarity and organization (0-100)
    4. Grammar and language (0-100)

    Return a JSON with:
    - Individual scores for each criterion
    - Overall score (weighted average)
    - Specific feedback for improvement
    - Key points covered and missed
    """

    try:
        response = openai_client.chat.completions.create(
            model=os.getenv("DEPLOYMENT_NAME"),
            messages=[
                {"role": "system", "content": "You are an expert essay evaluator."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=500
        )
        
        evaluation = json.loads(response.choices[0].message.content)
        return evaluation
    except Exception as e:
        return {
            "error": f"Evaluation failed: {str(e)}",
            "overall_score": 0,
            "feedback": "Automatic evaluation failed. Please review manually."
        }

def evaluate_code(question: str, correct_answer: str, user_answer: str) -> dict:
    """Evaluate coding answers"""
    prompt = f"""
    Evaluate this code solution based on the following:
    Question: {question}
    Model Solution: {correct_answer}
    Student's Solution: {user_answer}

    Analyze:
    1. Correctness (0-100)
    2. Code efficiency (0-100)
    3. Code style and readability (0-100)
    4. Error handling (0-100)

    Return a JSON with:
    - Scores for each criterion
    - Overall score
    - Specific feedback
    - Suggested improvements
    """

    try:
        response = openai_client.chat.completions.create(
            model=os.getenv("DEPLOYMENT_NAME"),
            messages=[
                {"role": "system", "content": "You are an expert code reviewer."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=500
        )
        
        evaluation = json.loads(response.choices[0].message.content)
        return evaluation
    except Exception as e:
        return {
            "error": f"Evaluation failed: {str(e)}",
            "overall_score": 0,
            "feedback": "Automatic evaluation failed. Please review manually."
        }

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
        questions = data["questions"]
        subject = data.get("subject", "General")

        correct_count = 0
        total_gradeable_questions = 0
        detailed_results = []

        # Create a new exam result first to get the exam_id
        conn = get_db_connection("exam_results.db")
        cursor = conn.cursor()
        
        # Insert initial result to get exam_id
        cursor.execute("""
            INSERT INTO results (
                user_id, score, grade, status, subject,
                total_questions, correct_answers, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (user_id, 0, 'P', 'In Progress', subject, len(questions), 0))
        exam_id = cursor.lastrowid
        conn.commit()
        conn.close()

        # Now use exam_questions.db for storing questions and answers
        questions_conn = get_db_connection("exam_questions.db")
        questions_cursor = questions_conn.cursor()

        try:
            for question in questions:
                q_id = str(question["id"])
                
                # Insert the question first
                questions_cursor.execute("""
                    INSERT INTO questions (
                        exam_id, question_text, question_type,
                        options, correct_answer, subject
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    exam_id,
                    question["question"],
                    question.get("type", ""),
                    json.dumps(question.get("options", [])),
                    question.get("correct_answer", ""),
                    question.get("subject", subject)
                ))
                question_id = questions_cursor.lastrowid

                # Get user's answer
                user_answer = answers.get(str(q_id), "")
                
                # Calculate score based on question type
                question_type = question.get("type", "").lower()
                question_score = 0
                feedback = None
                evaluation_data = None

                if question_type in ["mcq", "true_false"]:
                    is_correct = str(user_answer).strip().lower() == str(question.get("correct_answer", "")).strip().lower()
                    question_score = 100 if is_correct else 0
                    total_gradeable_questions += 1
                    if is_correct:
                        correct_count += 1
                elif question_type == "essay":
                    evaluation = evaluate_essay(
                        question["question"],
                        question.get("correct_answer", ""),
                        user_answer
                    )
                    question_score = evaluation.get("overall_score", 0)
                    feedback = evaluation.get("feedback")
                    evaluation_data = json.dumps(evaluation)
                    total_gradeable_questions += 1
                elif question_type == "coding":
                    evaluation = evaluate_code(
                        question["question"],
                        question.get("correct_answer", ""),
                        user_answer
                    )
                    question_score = evaluation.get("overall_score", 0)
                    feedback = evaluation.get("feedback")
                    evaluation_data = json.dumps(evaluation)
                    total_gradeable_questions += 1

                # Insert user's answer
                questions_cursor.execute("""
                    INSERT INTO user_answers (
                        exam_id, user_id, question_id,
                        answer, is_correct, time_taken
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    exam_id,
                    user_id,
                    question_id,
                    user_answer,
                    1 if question_score == 100 else 0,
                    None
                ))

                detailed_results.append({
                    "question_id": question_id,
                    "score": question_score,
                    "feedback": feedback,
                    "evaluation": json.loads(evaluation_data) if evaluation_data else None
                })

            questions_conn.commit()

            # Calculate final score
            final_score = (correct_count / total_gradeable_questions * 100) if total_gradeable_questions > 0 else 0
            
            # Update the final result
            conn = get_db_connection("exam_results.db")
            conn.execute("""
                UPDATE results 
                SET score = ?, grade = ?, status = ?, correct_answers = ?
                WHERE id = ?
            """, (final_score, calculate_grade(final_score), "Completed", correct_count, exam_id))
            conn.commit()
            conn.close()

            return jsonify({
                "exam_id": exam_id,
                "score": final_score,
                "grade": calculate_grade(final_score),
                "correct_answers": correct_count,
                "total_questions": total_gradeable_questions,
                "detailed_results": detailed_results
            })

        finally:
            questions_conn.close()

    except Exception as e:
        print(f"Error in validate_answers: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/get_results", methods=["GET"])
@jwt_required()
def get_results():
    """Retrieve exam results for the current user"""
    try:
        user_id = get_jwt_identity()
        
        # First get results from results table
        results_conn = get_db_connection("exam_results.db")
        results = results_conn.execute("""
            SELECT * FROM results 
            WHERE user_id = ? 
            ORDER BY timestamp DESC
        """, (user_id,)).fetchall()
        results_conn.close()

        if not results:
            return jsonify({"message": "No results found"}), 404

        # Get subjects from questions table for each exam
        questions_conn = get_db_connection("exam_questions.db")
        results_list = []
        
        for row in results:
            # Convert sqlite3.Row to dict
            result_dict = dict(row)
            
            # Get the most common subject for this exam
            subjects = questions_conn.execute("""
                SELECT subject, COUNT(*) as count 
                FROM questions 
                WHERE exam_id = ? 
                GROUP BY subject 
                ORDER BY count DESC 
                LIMIT 1
            """, (result_dict["id"],)).fetchone()

            # Convert subjects Row to dict if it exists
            subject = dict(subjects)["subject"] if subjects else result_dict["subject"] if "subject" in result_dict else "General"
            
            results_list.append({
                "id": result_dict["id"],
                "user_id": result_dict["user_id"],
                "score": result_dict["score"],
                "grade": result_dict["grade"],
                "status": result_dict["status"],
                "timestamp": result_dict["timestamp"],
                "subject": subject,
                "total_questions": result_dict["total_questions"],
                "correct_answers": result_dict["correct_answers"]
            })
        
        questions_conn.close()

        # Calculate statistics by subject
        subjects = {}
        for r in results_list:
            subj = r["subject"]
            if subj not in subjects:
                subjects[subj] = {"count": 0, "total_score": 0}
            subjects[subj]["count"] += 1
            subjects[subj]["total_score"] += r["score"]

        subject_stats = {
            subj: {
                "average_score": round(data["total_score"] / data["count"], 2),
                "exam_count": data["count"]
            }
            for subj, data in subjects.items()
        }

        return jsonify({
            "results": results_list,
            "statistics": {
                "total_exams": len(results_list),
                "average_score": round(
                    sum(r["score"] for r in results_list) / len(results_list)
                    if results_list else 0, 
                    2
                ),
                "last_exam_score": results_list[0]["score"] if results_list else 0,
                "by_subject": subject_stats
            }
        })

    except Exception as e:
        print(f"Error in get_results: {str(e)}")
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
        score = round((correct_answers / total_questions) * 100, 2) if total_questions > 0 else 0
        
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

@app.route("/update_profile", methods=["PUT"])
@jwt_required()
def update_profile():
    try:
        current_user_email = get_jwt_identity()  # This gets the email from the token
        data = request.get_json()
        
        first_name = data.get("first_name")
        last_name = data.get("last_name")
        
        if not all([first_name, last_name]):
            return jsonify({"error": "First name and last name are required"}), 400

        conn = get_db_connection("users.db")
        try:
            # Update using email instead of user_id
            conn.execute(
                """
                UPDATE users 
                SET first_name = ?, last_name = ?
                WHERE email = ?
                """,
                (first_name, last_name, current_user_email)
            )
            conn.commit()
            
            # Fetch updated user data
            user = conn.execute(
                "SELECT email, first_name, last_name FROM users WHERE email = ?",
                (current_user_email,)
            ).fetchone()
            
            if user:
                updated_user = {
                    "email": user["email"],
                    "first_name": user["first_name"],
                    "last_name": user["last_name"]
                }
                return jsonify({
                    "message": "Profile updated successfully",
                    "user": updated_user
                })
            else:
                return jsonify({"error": "User not found"}), 404
                
        finally:
            conn.close()
    except Exception as e:
        print(f"Error updating profile: {str(e)}")  # Add logging
        return jsonify({"error": "Failed to update profile"}), 500

# Add this route to test if the API is accessible
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy"}), 200

def get_level_specific_rules(level: str) -> str:
    """Get specific rules for each difficulty level"""
    rules = {
        "basic": """
        - Questions should cover fundamental concepts
        - Use simple, clear language
        - Focus on direct recall and basic understanding
        - Avoid complex terminology
        - Include straightforward scenarios
        """,
        
        "intermediate": """
        - Questions should require deeper understanding
        - Test application of concepts
        - Can include some technical terminology
        - May combine multiple basic concepts
        - Include practical scenarios
        """,
        
        "advanced": """
        - Questions should test complex understanding
        - Include advanced concepts and edge cases
        - Use technical terminology appropriately
        - Require analysis and problem-solving
        - Can combine multiple concepts
        - Include challenging scenarios
        """
    }
    return rules.get(level, "Follow standard difficulty level guidelines.")

if __name__ == "__main__":
    init_databases()
    verify_db_structure()
    app.run(debug=True, port=5000)
