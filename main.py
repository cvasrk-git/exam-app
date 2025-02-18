from flask import Flask, request, jsonify
from flask_cors import CORS
import openai
import json
import re
import time  # Import time module for tracking timestamps

app = Flask(__name__)
CORS(app)  # Allow React frontend to call API

# Configure Azure OpenAI
openai_client = openai.AzureOpenAI(
    api_key="260Bhbwr7C2u5IEHvNZXOf3EYYNqMGebapo9TjUg4rhcNAHzyVK6JQQJ99BBACHYHv6XJ3w3AAAAACOGCLay",
    api_version="2024-05-01-preview",
    azure_endpoint="https://aique-m6xlx4yt-eastus2.openai.azure.com/",
)

# Default time limit per question (in seconds)
DEFAULT_TIME_LIMIT = 30

# Dictionary to store question start times {user_id: {question_id: start_time}}
question_start_times = {}


@app.route("/get_questions", methods=["GET"])
def get_questions():
    technology = request.args.get("technology")
    difficulty = request.args.get("difficulty")
    q_type = request.args.get("type")
    user_id = request.args.get("user_id")  # Unique user identifier

    if not technology or not difficulty or not q_type or not user_id:
        return jsonify({"error": "Missing required parameters: technology, difficulty, type, or user_id"}), 400

    prompt = f"""
    Generate 5 {difficulty} level {q_type} questions for {technology}.
    Each question should include:
    - 'id' (unique question number)
    - 'question' (question text)
    - 'options' (list of possible answers, if applicable)
    - 'correct_answer' (correct answer text)
    - 'time_limit' (time allowed to answer in seconds, default {DEFAULT_TIME_LIMIT}s).

    Return the response in JSON format as a list of dictionaries.
    """

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.7,
            max_tokens=500,
        )

        response_text = response.choices[0].message.content
        clean_json = re.sub(r"```json\n(.*?)\n```", r"\1", response_text, flags=re.DOTALL)

        try:
            questions_json = json.loads(clean_json)
            if not isinstance(questions_json, list):
                raise ValueError("Generated response is not a valid list of questions.")

            # Initialize time tracking for each question for this user
            question_start_times[user_id] = {}

            for question in questions_json:
                question_id = str(question.get("id"))
                question.setdefault("time_limit", DEFAULT_TIME_LIMIT)
                question_start_times[user_id][question_id] = time.time()  # Store start time

        except json.JSONDecodeError:
            return jsonify({"error": "Invalid JSON format received from OpenAI"}), 500

        return jsonify({
            "difficulty": difficulty,
            "technology": technology,
            "type": q_type,
            "questions": questions_json
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/validate_answers", methods=["POST"])
def validate_answers():
    try:
        data = request.get_json()
        if not data or "answers" not in data or "questions" not in data or "user_id" not in data:
            return jsonify({"error": "Invalid request. Expected 'user_id', 'answers', and 'questions' in JSON."}), 400

        user_id = data["user_id"]
        answers = data["answers"]
        questions = {str(q["id"]): q for q in data["questions"]}
        validation_results = []
        correct_count = 0

        if user_id not in question_start_times:
            return jsonify({"error": "No recorded start times for this user. Please start the exam again."}), 400

        for q_id, user_answer in answers.items():
            question_data = questions.get(q_id)
            if not question_data:
                validation_results.append({"id": q_id, "status": "Question not found", "correct": False})
                continue

            # Retrieve stored start time
            start_time = question_start_times[user_id].get(q_id, None)
            if start_time is None:
                validation_results.append({"id": q_id, "status": "Start time missing", "correct": False})
                continue

            time_taken = time.time() - start_time
            time_limit = question_data.get("time_limit", DEFAULT_TIME_LIMIT)

            # Check if the answer was submitted on time
            if time_taken > time_limit:
                validation_results.append({
                    "id": q_id,
                    "question": question_data["question"],
                    "user_answer": user_answer,
                    "correct_answer": question_data["correct_answer"],
                    "time_taken": round(time_taken, 2),
                    "time_limit": time_limit,
                    "status": "Timeout",
                    "accuracy": "0%"
                })
                continue

            # Check correctness
            correct_answer = question_data.get("correct_answer")
            is_correct = str(user_answer).strip().lower() == str(correct_answer).strip().lower()
            if is_correct:
                correct_count += 1

            validation_results.append({
                "id": q_id,
                "question": question_data["question"],
                "user_answer": user_answer,
                "correct_answer": correct_answer,
                "time_taken": round(time_taken, 2),
                "time_limit": time_limit,
                "status": "Correct" if is_correct else "Incorrect",
                "accuracy": "100%" if is_correct else "0%"
            })

        total_questions = len(questions)
        score_percentage = (correct_count / total_questions) * 100 if total_questions else 0
        ranking = "Excellent" if score_percentage >= 80 else "Good" if score_percentage >= 50 else "Needs Improvement"

        return jsonify({
            "validation": validation_results,
            "score": score_percentage,
            "ranking": ranking
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
