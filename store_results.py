import os
from azure.data.tables import TableServiceClient, TableClient, UpdateMode
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

# Create Table Service Client
table_service_client = TableServiceClient.from_connection_string(connection_string)

# Table Name
table_name = "ExamResults"

# Create Table (if not exists)
try:
    table_service_client.create_table(table_name)
    print(f"Table '{table_name}' created successfully!")
except:
    print(f"Table '{table_name}' already exists.")

# Get Table Client
table_client = table_service_client.get_table_client(table_name)

# Exam Result Data
exam_result = {
    "PartitionKey": "Exam2025",  # Group results by exam year
    "RowKey": "CVA",  # Unique Student ID
    "StudentName": "John Doe",
    "Score": 85,
    "Grade": "B",
    "Status": "Passed",
}

# Insert or Update Record
table_client.upsert_entity(exam_result, mode=UpdateMode.MERGE)
print("Exam result stored successfully!")
