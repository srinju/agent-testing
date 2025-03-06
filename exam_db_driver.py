import datetime
import logging
import os
from typing import Dict, List, Optional, Union

import pymongo
from bson import ObjectId
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, PyMongoError

from exam_models import Exam, ExamQuestion

# Configure logging
logger = logging.getLogger("exam_db")

class ExamDBDriver:
    """
    Driver for interacting with the MongoDB database for exam data.
    """
    
    def __init__(self):
        """Initialize the MongoDB driver."""
        self.client = None
        self.db = None
        self.exams_collection = None
        self.submissions_collection = None
        
        # Get MongoDB connection string from environment variable
        self.mongo_uri = os.environ.get("MONGODB_URI")
        if not self.mongo_uri:
            logger.error("MONGODB_URI environment variable not set")
    
    def connect(self) -> bool:
        """
        Connect to the MongoDB database.
        
        Returns:
            True if connection successful, False otherwise
        """
        if not self.mongo_uri:
            logger.error("MongoDB URI not set")
            return False
            
        try:
            # Connect to MongoDB
            self.client = MongoClient(self.mongo_uri)
            
            # Check connection
            self.client.admin.command("ping")
            logger.info("Successfully connected to MongoDB")
            
            # Get database and collections
            self.db = self.client["coral-ai"]
            self.exams_collection = self.db["exams"]
            self.submissions_collection = self.db["submissions"]
            
            return True
        except ConnectionFailure as e:
            logger.error(f"MongoDB connection failed: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"Error connecting to MongoDB: {str(e)}")
            return False
    
    def get_exam_by_id(self, exam_id: str) -> Optional[Exam]:
        """
        Get an exam by its ID.
        
        Args:
            exam_id: The ID of the exam to get
            
        Returns:
            The exam if found, None otherwise
        """
        if not self.client:
            logger.error("MongoDB client not connected")
            return None
            
        try:
            # Convert string ID to ObjectId
            if not ObjectId.is_valid(exam_id):
                logger.error(f"Invalid exam ID format: {exam_id}")
                return None
                
            # Find the exam
            exam_doc = self.exams_collection.find_one({"_id": ObjectId(exam_id)})
            if not exam_doc:
                logger.error(f"Exam not found with ID: {exam_id}")
                return None
                
            # Convert to Exam object
            questions = []
            for q in exam_doc.get("questions", []):
                questions.append(ExamQuestion(
                    text=q.get("text", ""),
                    answer=q.get("answer", ""),
                    difficulty=q.get("difficulty", "medium")
                ))
                
            exam = Exam(
                exam_id=str(exam_doc["_id"]),
                name=exam_doc.get("name", "Unnamed Exam"),
                description=exam_doc.get("description", ""),
                questions=questions
            )
            
            logger.info(f"Found exam: {exam.name} with {len(exam.questions)} questions")
            return exam
            
        except PyMongoError as e:
            logger.error(f"MongoDB Error while getting exam: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Error getting exam: {str(e)}")
            return None
    
    def get_personalized_questions_from_submission(self, submission_id: str) -> List[ExamQuestion]:
        """
        Get personalized questions from a submission.
        
        Args:
            submission_id: The ID of the submission
            
        Returns:
            List of personalized questions
        """
        if not self.client:
            logger.error("MongoDB client not connected")
            return []
            
        try:
            # Convert string ID to ObjectId
            if not ObjectId.is_valid(submission_id):
                logger.error(f"Invalid submission ID format: {submission_id}")
                return []
                
            # Find the submission
            submission = self.submissions_collection.find_one({"_id": ObjectId(submission_id)})
            if not submission:
                logger.error(f"Submission not found with ID: {submission_id}")
                return []
                
            # Get personalized questions
            personalized_questions = []
            for q in submission.get("personalizedQuestions", []):
                personalized_questions.append(ExamQuestion(
                    text=q.get("text", ""),
                    answer=q.get("answer", ""),
                    difficulty=q.get("difficulty", "medium")
                ))
                
            logger.info(f"Found {len(personalized_questions)} personalized questions for submission {submission_id}")
            return personalized_questions
            
        except PyMongoError as e:
            logger.error(f"MongoDB Error while getting personalized questions: {str(e)}")
            return []
        except Exception as e:
            logger.error(f"Error getting personalized questions: {str(e)}")
            return []
    
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
            logger.info(f"Conversation has {len(conversation)} messages")
            
            # Convert the conversation to a format that MongoDB can store
            serializable_conversation = []
            for msg in conversation:
                # Ensure timestamp is a string if it's not already
                timestamp = msg.get("timestamp")
                if isinstance(timestamp, datetime.datetime):
                    timestamp = timestamp.isoformat()
                    
                serializable_conversation.append({
                    "role": msg.get("role", ""),
                    "content": msg.get("content", ""),
                    "timestamp": timestamp
                })
            
            # Log a sample of the conversation for debugging
            if len(serializable_conversation) > 0:
                logger.info(f"First message: {serializable_conversation[0]}")
                if len(serializable_conversation) > 1:
                    logger.info(f"Second message: {serializable_conversation[1]}")
            
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
                
            logger.info(f"Found submission with ID: {submission['_id']}")
            
            # Update the submission with the conversation transcript
            result = self.submissions_collection.update_one(
                {"_id": submission["_id"]},
                {
                    "$set": {
                        "submissionTranscript": serializable_conversation,
                        "status": "completed"  # Update status to completed
                    }
                }
            )
            
            logger.info(f"Update result: matched={result.matched_count}, modified={result.modified_count}")
            
            if result.matched_count > 0:
                logger.info(f"Successfully saved conversation transcript to submission {submission['_id']}")
                return True
            else:
                logger.warning(f"No submission matched when saving transcript for exam {exam_id}")
                return False
                
        except Exception as e:
            logger.error(f"MongoDB Error while saving conversation transcript: {str(e)}", exc_info=True)
            return False