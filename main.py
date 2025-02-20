from flask import Flask, request, jsonify
from flask_cors import CORS
import openai
import json
import re
import time  # Import time module for tracking timestamps

app = Flask(__name__)
CORS(app)  # Allow React frontend to call API

API_KEY = "API_KEY"
API_ENDPOINT = "https://API_ENDPOINT.openai.azure.com/"
DEPLOYMENT_NAME = "DEPLOYMENT_NAME"  # Your Azure OpenAI model deployment
API_VERSION = "API_VERSION"

# Configure Azure OpenAI
openai_client = openai.AzureOpenAI(
    azure_endpoint=API_ENDPOINT,
    api_key=API_KEY,
    api_version=API_VERSION,
)


# Default time limit per question (in seconds)
DEFAULT_TIME_LIMIT = 30

# Dictionary to store question start times {user_id: {question_id: start_time}}
question_start_times = {}


@app.route("/generate_questions", methods=["POST"])
def generate_questions():
    """Generate exam questions based on user input prompt"""
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
    - 'time_limit': time in seconds (default {DEFAULT_TIME_LIMIT})

    Format the response as a JSON array of question objects **without Markdown formatting**.
    """

    try:
        response = openai_client.chat.completions.create(
            model=DEPLOYMENT_NAME,
            messages=[{"role": "system", "content": formatted_prompt}],
            temperature=0.7,
            max_tokens=800,  # 🔹 Increased to avoid truncation
        )

        response_text = response.choices[0].message.content.strip()
        print("\n✅ Raw Response:", response_text)  # Debugging

        # 🔹 Remove potential Markdown ```json ... ``` wrapping
        clean_json = re.sub(r"```json\n(.*?)\n```", r"\1", response_text, flags=re.DOTALL)

        try:
            questions_json = json.loads(clean_json)  # Convert string to JSON
            if not isinstance(questions_json, list):
                raise ValueError("Response is not a valid JSON list of questions.")

            # ✅ Debug: Print parsed JSON
            print("\n🔍 Parsed Questions JSON:", json.dumps(questions_json, indent=2))

            # Initialize question timers
            question_start_times[user_id] = {}

            for question in questions_json:
                question_id = str(question.get("id", ""))
                question.setdefault("time_limit", DEFAULT_TIME_LIMIT)  # Ensure time_limit
                question_start_times[user_id][question_id] = time.time()  # Start time

                # ✅ Ensure options exist for MCQ
                if question["type"] == "mcq" and not question.get("options"):
                    question["options"] = ["Option A", "Option B", "Option C", "Option D"]

            return jsonify({"questions": questions_json})

        except json.JSONDecodeError:
            return jsonify({"error": "Invalid JSON format received from OpenAI"}), 500

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
