import logging
import json
import asyncio
import datetime
from dotenv import load_dotenv
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    llm,
)
from livekit.agents.pipeline import VoicePipelineAgent
from livekit.plugins import openai, deepgram, silero, turn_detector
from livekit import rtc
from exam_db_driver import ExamDBDriver, Exam, ExamQuestion
from prompts import INSTRUCTIONS, WELCOME_MESSAGE

load_dotenv(dotenv_path=".env.local")
logger = logging.getLogger("voice-agent")

def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["db"] = ExamDBDriver()  # Initialize ExamDBDriver 

async def entrypoint(ctx: JobContext):
    initial_ctx = llm.ChatContext().append(
        role="system",
        text=(
            "You are an oral exam instructor. Your role is to:"
            "1. Ask questions from the exam one at a time"
            "2. Listen to the student's response, dig deeper once if needed"
            "3. Move to the next question after receiving the response"
            "4. Do not provide answers or hints"
            "5. End the exam with a completion message"
            "6. If the student says they don't know the answer (using phrases like 'I don't know', 'not sure', 'no idea', etc.), ask if they would like another chance to answer this question"
            "7. If they want another chance (they say 'yes', 'sure', 'okay', etc.), repeat the current question"
            "8. If they don't want another chance (they say 'no', 'next', etc.), move to the next question"
            "Do not ask questions until you receive the exam data."
        ),
    )

    logger.info(f"Connecting to room {ctx.room.name}")
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    class ExamState:
        def __init__(self):
            self.exam = None
            self.current_question_idx = 0
            self.questions_asked = 0
            self.data_received = False
            self.exam_completed = False
            self.needs_another_chance = False
            self.waiting_for_another_chance_response = False
            self.current_question = None

    exam_state = ExamState()
    db_driver = ctx.proc.userdata["db"]

    # Define the agent 
    agent = VoicePipelineAgent(
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(),
        llm=openai.LLM(model="gpt-4o-mini"),
        tts=deepgram.TTS(),
        turn_detector=turn_detector.EOUModel(),
        min_endpointing_delay=1.0, #increased from 0.5 to 1.0 to make the turn detection between the agent and the student more accurant and robust
        max_endpointing_delay=8.0,
        chat_ctx=initial_ctx,
    )



    async def ask_next_question():
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
            try:
                # Format the conversation from the chat context
                conversation = []
                current_time = datetime.datetime.now()
                
                for message in agent.chat_ctx.messages:
                    # Skip system messages and empty messages
                    if message.role == "system" or not message.text.strip():
                        continue
                        
                    # Map assistant role to agent
                    role = "agent" if message.role == "assistant" else "user"
                    
                    # Ensure each message has a timestamp
                    timestamp = getattr(message, "timestamp", None)
                    if not timestamp:
                        timestamp = current_time
                        current_time += datetime.timedelta(milliseconds=1)  # Ensure unique timestamps
                    
                    conversation.append({
                        "role": role,
                        "content": message.text.strip(),
                        "timestamp": timestamp
                    })
                    
                # Save to database
                if db_driver.save_conversation_transcript(exam_state.exam.exam_id, conversation):
                    logger.info(f"Successfully saved conversation transcript to submission for exam {exam_state.exam.exam_id}")
                else:
                    logger.error(f"Failed to save conversation transcript for exam {exam_state.exam.exam_id}")
                    
            except Exception as e:
                logger.error(f"Error saving conversation transcript: {str(e)}", exc_info=True)
                
            await agent.say(
                f"Thank you for completing the {exam_state.exam.name} exam. This concludes our session. You've answered all {len(exam_state.exam.questions)} questions. Good luck with your results!",
                allow_interruptions=False
            )




    async def handle_data_received(data: rtc.DataPacket):
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
                            from exam_db_driver import Exam, ExamQuestion
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
                    await ask_next_question()
                else:
                    logger.warning(f"Received unknown message type: {message_json.get('type')}")

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON data: {e}")
            except Exception as e:
                logger.error(f"Error processing data: {e}", exc_info=True)




    # Register this handler BEFORE connecting participants
    ctx.room.on("data_received", lambda data: asyncio.create_task(handle_data_received(data)))
    
    # Wait for participant after setting up event handlers
    participant = await ctx.wait_for_participant()
    logger.info(f"Starting voice assistant for participant {participant.identity}")
    logger.info(f"Agent's own identity: {ctx.room.local_participant.identity}")

    # Custom handler for when participant speaks and finishes speaking
    async def on_user_speech_committed():
        logger.info("User speech committed")
        
        # Get the last user message from the chat context
        last_user_message = None
        for message in reversed(agent.chat_ctx.messages):
            if message.role == "user":
                last_user_message = message.text
                break
        
        logger.info(f"Last user message: {last_user_message}")
        
        # Check if we're waiting for a response about another chance
        if exam_state.waiting_for_another_chance_response and last_user_message:
            exam_state.waiting_for_another_chance_response = False
            
            # Check if the user wants another chance
            if any(phrase in last_user_message.lower() for phrase in ["yes", "yeah", "sure", "okay", "please", "give me another chance"]):
                logger.info("User wants another chance")
                exam_state.needs_another_chance = True
                await asyncio.sleep(1.0)
                await ask_next_question()
                return
            else:
                logger.info("User doesn't want another chance, moving to next question")
                exam_state.needs_another_chance = False
                # Continue to ask the next question
        
        # Check if the user's response indicates they don't know the answer
        elif last_user_message and any(phrase in last_user_message.lower() for phrase in ["i don't know", "don't know", "no idea", "not sure", "i'm not sure", "i am not sure", "i have no idea"]):
            logger.info("User indicated they don't know the answer")
            
            # Ask if they want another chance
            exam_state.waiting_for_another_chance_response = True
            await agent.say("Would you like another chance to answer this question?", allow_interruptions=True)
            return
        
        # Add delay to give a more natural conversation flow
        await asyncio.sleep(1.5)
        if not exam_state.exam_completed:
            await ask_next_question()

    agent.on("user_speech_committed", lambda _: asyncio.create_task(on_user_speech_committed()))



    
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant):
        logger.info(f"Participant connected: {participant.identity}")
        # Send a brief greeting when participant connects
        #asyncio.create_task(agent.say("Welcome to Coral AI Exam Platform. I'll be your exam proctor today. Please wait while I load your exam.", allow_interruptions=False))
        pass
    # Start the agent
    agent.start(ctx.room, participant)

    # Periodic check to see if we've received data
    check_interval = 5  # seconds
    max_checks = 12  # 60 seconds total
    checks = 0
    
    while checks < max_checks and not exam_state.data_received:
        await asyncio.sleep(check_interval)
        checks += 1
        logger.info(f"Data check {checks}/{max_checks}: Data received: {exam_state.data_received}")
        
        if checks == 3 and not exam_state.data_received:  # After 15 seconds
            await agent.say("Waiting for exam data. Please ensure it has been sent to the room.", allow_interruptions=False)



if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        ),
    )