import json
import asyncio
import logging
from exam_db_driver import Exam, ExamQuestion
from transcript import save_transcript
from prompts import INSTRUCTIONS, WELCOME_MESSAGE

logger = logging.getLogger("voice-agent")

async def ask_next_question(agent, exam_state, db_driver):
    """
    Asks the next question in the exam or handles special cases like giving another chance.
    
    Args:
        agent: The voice pipeline agent
        exam_state: Current state of the exam
        db_driver: Database driver instance
    """
    if not exam_state.data_received:
        logger.warning("Attempted to ask next question but no exam data received")
        return
        
    if exam_state.exam is None:
        logger.error("Exam data was invalid")
        await agent.say("Exam data was invalid. Please try again.", allow_interruptions=False)
        return
        
    if exam_state.exam_completed:
        logger.info("Exam already completed")
        return
    
    # If we're waiting for a response about another chance, don't ask a new question
    if exam_state.waiting_for_another_chance_response:
        logger.info("Waiting for user to decide if they want another chance")
        return
        
    # If we need to give the user another chance, ask the current question again
    if exam_state.needs_another_chance:
        logger.info(f"Giving user another chance for question {exam_state.current_question_idx}")
        await agent.say(exam_state.current_question, allow_interruptions=True)
        exam_state.needs_another_chance = False
        return
        
    if exam_state.current_question_idx < len(exam_state.exam.questions):
        question = exam_state.exam.questions[exam_state.current_question_idx].text
        logger.info(f"Asking question {exam_state.current_question_idx + 1}: {question[:50]}...")
        
        exam_state.questions_asked += 1
        question_prompt = f"Question {exam_state.current_question_idx + 1}: {question}"
        exam_state.current_question = question_prompt
        await agent.say(question_prompt, allow_interruptions=True)
        exam_state.current_question_idx += 1
    else:
        logger.info("All questions completed, ending exam")
        exam_state.exam_completed = True
        
        # Save the conversation transcript to the submission
        await save_transcript(db_driver, exam_state.exam.exam_id, agent)

        # send message to the frontend to end the exam
        await agent.data_channel.send_message(json.dumps({
            "type": "EXAM_COMPLETED",
            "data": {
                "examId": exam_state.exam.exam_id,
                "endCall" : True #call ended 
            }
        }))
            
        await agent.say(
            f"Thank you for completing the {exam_state.exam.name} exam. This concludes our session. You've answered all {len(exam_state.exam.questions)} questions. Good luck with your results!",
            allow_interruptions=False
        )

async def handle_data_received(data, agent, exam_state, db_driver):
    """
    Handles data received from the frontend, such as exam questions.
    
    Args:
        data: Data packet received
        agent: The voice pipeline agent
        exam_state: Current state of the exam
        db_driver: Database driver instance
    """
    logger.info(f"Data received handler triggered")
    if data.data:
        try:
            message = data.data.decode("utf-8")
            logger.info(f"Decoded message: {message}")
            message_json = json.loads(message)

            logger.info(f"Received data: {message_json}")
            exam_state.data_received = True

            if message_json.get("type") == "QUESTIONS":
                data_obj = message_json.get("data", {})
                exam_id = data_obj.get("examId")
                questions_from_frontend = data_obj.get("questions", [])
                name = data_obj.get("name", "Unnamed Exam") #name of the exam
                student_name = data_obj.get("studentName", "Student")  # student name
                is_improvized = data_obj.get("isImprovized", False)
                
                logger.info(f"Received exam data - ID: {exam_id}, Name: {name}, Student: {student_name}, Questions: {len(questions_from_frontend)}")

                
                # Try to get exam from MongoDB first
                if exam_id:
                    logger.info(f"Looking up exam with ID: {exam_id}")
                    try:
                        # Check if this is a personalized exam
                        if is_improvized:
                            logger.info(f"This is a personalized exam, fetching questions from submissions")
                            # First get the exam for metadata (name, duration, etc.)
                            exam_metadata = db_driver.get_exam_by_id(exam_id)
                            
                            if exam_metadata:
                                # Then get personalized questions from submission
                                personalized_questions = db_driver.get_personalized_questions_from_submission(exam_id)
                                
                                if personalized_questions:
                                    logger.info(f"Found {len(personalized_questions)} personalized questions in submission")
                                    # Create a new Exam object with personalized questions
                                    exam_state.exam = Exam(
                                        exam_id=exam_id,
                                        name=exam_metadata.name,
                                        questions=personalized_questions,
                                        duration=exam_metadata.duration,
                                        difficulty=exam_metadata.difficulty
                                    )
                                    logger.info(f"Successfully loaded personalized exam: {exam_state.exam.name}")
                                else:
                                    logger.info(f"No personalized questions found in submission, falling back to exam questions")
                                    exam_state.exam = exam_metadata
                            else:
                                logger.info(f"Exam metadata not found, will use frontend data")
                                exam_state.exam = None
                        else:
                            # Regular exam, fetch from exams collection
                            exam_state.exam = db_driver.get_exam_by_id(exam_id)
                            if exam_state.exam:
                                logger.info(f"Successfully loaded exam from MongoDB: {exam_state.exam.name}")
                            else:
                                logger.info(f"Exam not found in MongoDB, will use frontend data")
                    except Exception as e:
                        logger.error(f"Error fetching exam from MongoDB: {e}", exc_info=True)
                        exam_state.exam = None
                
                # If MongoDB fetch failed or no examId, use questions from frontend
                if exam_state.exam is None:
                    logger.info(f"Using questions from frontend as fallback")
                    if questions_from_frontend:
                        # Create an Exam object from frontend data
                        exam_state.exam = Exam(
                            exam_id=exam_id or "frontend-exam",
                            name=name,
                            questions=[ExamQuestion(text=q.get("text", "")) for q in questions_from_frontend],
                            duration=data_obj.get("duration", 30),
                            difficulty=data_obj.get("difficulty", "Medium")
                        )
                        logger.info(f"Created exam from frontend data: {exam_state.exam.name} with {len(exam_state.exam.questions)} questions")
                    else:
                        logger.error("No questions available from frontend or MongoDB")
                        await agent.say("Sorry, I couldn't load any questions for this exam. Please try again.", allow_interruptions=False)
                        return

                logger.info(f"Successfully loaded exam: {exam_state.exam.name} with {len(exam_state.exam.questions)} questions")
                for i, q in enumerate(exam_state.exam.questions):
                    logger.info(f"Question {i+1}: {q.text[:50]}...")

                # Update the agent's context with exam-specific instructions
                question_texts = [q.text for q in exam_state.exam.questions]
                formatted_questions = "\n".join([f"{i+1}. {q}" for i, q in enumerate(question_texts)])
                
                agent.chat_ctx.append(
                    role="system",
                    text=INSTRUCTIONS.format(exam_questions=formatted_questions)
                )

                # Format welcome message with exam details
                welcome_msg = WELCOME_MESSAGE.format(
                    student_name=student_name,
                    exam_name=exam_state.exam.name
                )
                
                logger.info(f"Sending welcome message: {welcome_msg}")
                await agent.say(welcome_msg, allow_interruptions=False)

                # Add a slight delay before first question
                await asyncio.sleep(2)
                await ask_next_question(agent, exam_state, db_driver)
            else:
                logger.warning(f"Received unknown message type: {message_json.get('type')}")

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON data: {e}")
        except Exception as e:
            logger.error(f"Error processing data: {e}", exc_info=True)

async def on_user_speech_committed(agent, exam_state, db_driver, last_user_message):
    """
    Handles when a user finishes speaking.
    
    Args:
        agent: The voice pipeline agent
        exam_state: Current state of the exam
        db_driver: Database driver instance
        last_user_message: The last message from the user
    """
    # Get the last user message from the chat context
    for message in reversed(agent.chat_ctx.messages):
        if message.role == "user":
            # Use content instead of text for ChatMessage objects
            last_user_message = message.content if hasattr(message, "content") else message.text
            break
            
    logger.info("User speech committed")
    
    # If the user says "end exam" or similar, end the exam
    if last_user_message and any(phrase in last_user_message.lower() for phrase in ["end exam", "finish exam", "stop exam", "exit exam", "quit exam", "terminate exam"]):
        await agent.say("Thank you for completing the exam. I'll save your responses now.", allow_interruptions=False)
        exam_state.exam_completed = True
        
        # Save the conversation transcript when the exam is explicitly ended
        await save_transcript(db_driver, exam_state.exam.exam_id, agent)
        return
        
    # If we're waiting for the user to confirm they're ready for the next question
    if exam_state.waiting_for_next_question_confirmation:
        if last_user_message and any(phrase in last_user_message.lower() for phrase in ["yes", "yeah", "sure", "okay", "ok", "ready", "next"]):
            exam_state.waiting_for_next_question_confirmation = False
            await ask_next_question(agent, exam_state, db_driver)
        else:
            await agent.say("Let me know when you're ready to continue to the next question.", allow_interruptions=True)
        return