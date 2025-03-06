from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from typing import List, Optional
from dataclasses import dataclass
from bson import ObjectId
import logging
import os

logger = logging.getLogger("exam_db")

@dataclass
class ExamQuestion:
    text: str

@dataclass
class Exam:
    exam_id: str
    name: str
    questions: List[ExamQuestion]
    duration: int
    difficulty: str

class ExamDBDriver:
    def __init__(self, mongo_uri: str = None, db_name: str = "coral-ai"):
        self.mongo_uri = mongo_uri or os.getenv("MONGO_URI") or "mongodb://localhost:27017"
        self.db_name = db_name
        self.client = None
        self.db = None
        self.exams_collection = None
        self.submissions_collection = None
        self._connect()

    def _connect(self):
        try:
            # Set a shorter timeout for faster failure
            self.client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=5000)
            # Force a connection to verify it works
            self.client.admin.command('ping')
            self.db = self.client[self.db_name]
            self.exams_collection = self.db["exams"]
            self.submissions_collection = self.db["submissions"]
            logger.info("Connected to MongoDB successfully")
        except (ConnectionFailure, ServerSelectionTimeoutError) as e:
            logger.error(f"MongoDB connection failed: {e}")
            self.client = None
            self.db = None
            self.exams_collection = None
            self.submissions_collection = None

    #GET EXAM BY ID       

    def get_exam_by_id(self, exam_id: str) -> Optional[Exam]:
        if not self.client:
            logger.error("MongoDB client not connected")
            return None
            
        try:
            logger.info(f"Attempting to fetch exam with ID: {exam_id}")
            
            if not ObjectId.is_valid(exam_id):
                logger.error(f"Invalid exam ID format: {exam_id}")
                return None

            exam_data = self.exams_collection.find_one({"_id": ObjectId(exam_id)})
            
            if not exam_data:
                logger.error(f"No exam found for ID: {exam_id}")
                return None
            
            logger.info(f"Found exam: {exam_data.get('name', 'Unnamed')} with {len(exam_data.get('questions', []))} questions")
                
            return Exam(
                exam_id=str(exam_data["_id"]),
                name=exam_data.get("name", "Unnamed Exam"),
                questions=[ExamQuestion(text=q.get("text", "")) for q in exam_data.get("questions", [])],
                duration=exam_data.get("duration", 0),
                difficulty=exam_data.get("difficulty", "Medium")
            )
        
        except Exception as e:
            logger.error(f"MongoDB Error: {str(e)}", exc_info=True)
            return None 
        
    # GET PERSONALIZED QUES FROM SUBMISSION TABLE
            
    def get_personalized_questions_from_submission(self, exam_id: str) -> Optional[List[ExamQuestion]]:
        """
        Retrieve personalized questions from the most recent submission for a given exam ID.
        
        Args:
            exam_id: The ID of the exam
            
        Returns:
            A list of ExamQuestion objects if found, None otherwise
        """
        if not self.client:
            logger.error("MongoDB client not connected")
            return None
            
        try:
            logger.info(f"Attempting to fetch personalized questions for exam ID: {exam_id}")
            
            if not ObjectId.is_valid(exam_id):
                logger.error(f"Invalid exam ID format: {exam_id}")
                return None
                
            # Find the most recent submission for this exam that has personalized questions
            submission = self.submissions_collection.find_one(
                {
                    "examId": exam_id,
                    "personalizedQuestions": {"$exists": True, "$ne": []}
                },
                sort=[("createdAt", -1)]  # Sort by creation date, most recent first
            )
            
            if not submission:
                logger.info(f"No submission with personalized questions found for exam ID: {exam_id}")
                return None
                
            personalized_questions = submission.get("personalizedQuestions", [])
            logger.info(f"Found {len(personalized_questions)} personalized questions in submission {submission.get('_id')}")
            
            return [ExamQuestion(text=q.get("text", "")) for q in personalized_questions]
            
        except Exception as e:
            logger.error(f"MongoDB Error while fetching personalized questions: {str(e)}", exc_info=True)
            return None

    # SAVE CONVERSATION TRANSCRIPT TO THE SUBMISSIONS TABLE IN THE DB    

    def save_conversation_transcript(self, exam_id: str, conversation: list) -> bool:
        """
        Save the conversation transcript to the submission document for a specific exam.
        
        Args:
            exam_id: The ID of the exam
            conversation: List of conversation messages with role, content, and timestamp
            
        Returns:
            True if successful, False otherwise
        """
        if not self.client:
            logger.error("MongoDB client not connected")
            return False
            
        try:
            logger.info(f"Saving conversation transcript for exam ID: {exam_id}")
            
            if not ObjectId.is_valid(exam_id):
                logger.error(f"Invalid exam ID format: {exam_id}")
                return False
                
            # Find the most recent submission for this exam
            submission = self.submissions_collection.find_one(
                {"examId": exam_id},
                sort=[("createdAt", -1)]  # Sort by creation date, most recent first
            )
            
            if not submission:
                logger.error(f"No submission found for exam ID: {exam_id}")
                return False
                
            # Update the submission with the conversation transcript
            result = self.submissions_collection.update_one(
                {"_id": submission["_id"]},
                {
                    "$set": {
                        "submissionTranscript": conversation,
                        "status": "completed"  # Update status to completed
                    }
                }
            )
            
            if result.modified_count > 0:
                logger.info(f"Successfully saved conversation transcript to submission {submission['_id']}")
                return True
            else:
                logger.warning(f"No changes made when saving transcript to submission {submission['_id']}")
                return False
                
        except Exception as e:
            logger.error(f"MongoDB Error while saving conversation transcript: {str(e)}", exc_info=True)
            return False