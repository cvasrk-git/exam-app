import os
from azure.data.tables import TableServiceClient
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Get Azure Storage Connection String
connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

# Create Table Client
table_name = "ExamResults"
table_service_client = TableServiceClient.from_connection_string(connection_string)
table_client = table_service_client.get_table_client(table_name)

print("Fetching all exam results...\n")

try:
    entities = table_client.list_entities()

    for result in entities:
        # Use `.get()` to avoid KeyError if key is missing
        row_key = result.get("RowKey", "Unknown")
        user_id = result.get("UserID", "Unknown")
        student_name = result.get("StudentName", "Unknown")
        score = result.get("Score", "N/A")
        grade = result.get("Grade", "N/A")
        status = result.get("Status", "N/A")

        print(f"RowKey: {row_key}, UserID: {user_id}, Student: {student_name}, Score: {score}, Grade: {grade}, Status: {status}")

except Exception as e:
    print(f"❌ Error fetching results: {e}")
