import asyncio
import datetime
import json
import logging
import os
import random
import re
import time
from typing import Dict, List, Optional, Tuple, Union

import livekit.agents
from livekit.agents import Agent, AgentConfig
from livekit.plugins.turn_detector import TurnDetectorConfig

from exam_db_driver import ExamDBDriver
from exam_models import Exam, ExamQuestion, ExamState

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("voice-agent")

# Initialize the MongoDB driver
db_driver = ExamDBDriver()

# Initialize the agent
agent_config = AgentConfig(
    name="Coral",
    system_prompt="""You are Coral, a conversational voice AI designed to administer oral exams.

Your goal is to ask questions from a provided exam and evaluate the student's responses.

Follow these guidelines:
1. Be friendly and conversational, but maintain a professional tone.
2. Ask one question at a time and wait for the student's response.
3. If the student says they don't know the answer, ask if they want another chance.
4. If they still don't know, move on to the next question.
5. After all questions are asked, thank the student for completing the exam.

Remember, you're evaluating their knowledge, not teaching or providing answers.""",
    voice_id="alloy",
    turn_detector_config=TurnDetectorConfig(
        eou_threshold=0.5,
        min_time_threshold_secs=1.0,
    ),
)

agent = Agent(agent_config)

# Initialize exam state
exam_state = ExamState()


async def entrypoint(room_name: str, identity: str):
    """
    Main entrypoint for the agent.
    
    Args:
        room_name: The name of the LiveKit room to join
        identity: The identity to use when joining the room
    """
    logger.info(f"Starting agent with room_name={room_name}, identity={identity}")
    
    # Connect to MongoDB
    if not db_driver.connect():
        logger.error("Failed to connect to MongoDB")
        return
    
    # Extract exam ID from room name
    # Room name format: exam_{exam_id}_{student_id}
    match = re.match(r"exam_([a-f0-9]+)_.*", room_name)
    if not match:
        logger.error(f"Invalid room name format: {room_name}")
        return
        
    exam_id = match.group(1)
    logger.info(f"Extracted exam ID: {exam_id}")
    
    # Load exam data from MongoDB
    exam = db_driver.get_exam_by_id(exam_id)
    if not exam:
        logger.error(f"Failed to load exam with ID: {exam_id}")
        return
        
    logger.info(f"Successfully loaded exam from MongoDB: {exam.name}")
    
    # Set up exam state
    exam_state.exam = exam
    exam_state.data_received = True
    
    # Log exam details
    logger.info(f"Successfully loaded exam: {exam.name} with {len(exam.questions)} questions")
    for i, question in enumerate(exam.questions):
        logger.info(f"Question {i+1}: {question.text[:50]}...")
    
    # Register event handlers
    agent.on_user_speech_committed(on_user_speech_committed)
    
    # Join the room
    await agent.join_room(room_name, identity)
    
    # Send welcome message
    student_name = identity.replace("_", " ")
    welcome_message = f"Hi {student_name}, great to e-meet you! I'm Coral, a conversational voice AI. Let's chat naturally. If I need you to clarify something I'll just ask. Ready to get started with {exam.name}?"
    logger.info(f"Sending welcome message: {welcome_message}")
    await agent.say(welcome_message, allow_interruptions=True)
    
    # Wait a moment before asking the first question
    await asyncio.sleep(1.5)
    
    # Start asking questions
    await ask_next_question()
    
    # Start data check loop
    asyncio.create_task(check_data_received())
    
    # Keep the agent running
    while True:
        await asyncio.sleep(1)


async def check_data_received():
    """
    Periodically check if exam data has been received.
    If not, log a warning.
    """
    check_count = 0
    while True:
        check_count += 1
        logger.info(f"Data check {check_count}/12: Data received: {exam_state.data_received}")
        
        if check_count >= 12 and not exam_state.data_received:
            logger.error("Exam data not received after 12 checks, exiting")
            break
            
        await asyncio.sleep(4)


async def ask_next_question():
    """
    Ask the next question in the exam.
    If all questions have been asked, end the exam.
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
        # Call the end_exam function instead of handling it inline
        await end_exam()


async def end_exam():
    """End the exam and save the conversation transcript."""
    logger.info("All questions completed, ending exam")
    
    # Set exam_completed flag to prevent multiple calls
    if exam_state.exam_completed:
        logger.info("Exam already marked as completed")
        return
        
    exam_state.exam_completed = True
    
    # Save the conversation transcript to the submission
    try:
        # Format the conversation from the chat context
        conversation = []
        
        # Log the chat context for debugging
        logger.info(f"Chat context has {len(agent.chat_ctx.messages)} messages")
        
        for i, message in enumerate(agent.chat_ctx.messages):
            # Skip system messages
            if message.role == "system":
                continue
                
            # Map assistant role to agent
            role = "agent" if message.role == "assistant" else message.role
            
            # Get the message content
            content = ""
            if hasattr(message, "content"):
                content = message.content
            elif hasattr(message, "text"):
                content = message.text
                
            # Skip empty messages
            if not content:
                continue
                
            # Log each message for debugging
            logger.info(f"Message {i}: role={role}, content={content[:50]}...")
            
            conversation.append({
                "role": role,
                "content": content,
                "timestamp": datetime.datetime.now().isoformat()
            })
            
        logger.info(f"Prepared conversation with {len(conversation)} messages to save to database")
        
        # Only save if we have messages
        if len(conversation) > 0:
            # Save to database
            if db_driver.save_conversation_transcript(exam_state.exam.exam_id, conversation):
                logger.info(f"Successfully saved conversation transcript to submission for exam {exam_state.exam.exam_id}")
            else:
                logger.error(f"Failed to save conversation transcript for exam {exam_state.exam.exam_id}")
        else:
            logger.error("No conversation messages to save")
    except Exception as e:
        logger.error(f"Error saving conversation transcript: {str(e)}", exc_info=True)
        
    # Thank the user for completing the exam
    await agent.say(
        f"Thank you for completing the {exam_state.exam.name} exam. This concludes our session. You've answered all {len(exam_state.exam.questions)} questions. Good luck with your results!",
        allow_interruptions=False
    )


async def on_user_speech_committed():
    """
    Handle user speech committed event.
    This is called when the user finishes speaking.
    """
    logger.info("User speech committed")
    
    # Get the last user message from the chat context
    last_user_message = None
    for message in reversed(agent.chat_ctx.messages):
        if message.role == "user":
            # Check if the message has 'content' attribute
            if hasattr(message, "content"):
                last_user_message = message.content
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
        
        # Ask if they want another chance - only ask once
        if not exam_state.waiting_for_another_chance_response:
            exam_state.waiting_for_another_chance_response = True
            await agent.say("Would you like another chance to answer this question?", allow_interruptions=True)
            return
    
    # Add delay to give a more natural conversation flow
    await asyncio.sleep(1.5)
    
    # Check if we've reached the end of the exam
    if exam_state.current_question_idx >= len(exam_state.exam.questions) and not exam_state.exam_completed:
        await end_exam()
    elif not exam_state.exam_completed:
        await ask_next_question()


if __name__ == "__main__":
    # Get room name and identity from environment variables
    room_name = os.environ.get("ROOM_NAME")
    identity = os.environ.get("IDENTITY")
    
    if not room_name or not identity:
        logger.error("ROOM_NAME and IDENTITY environment variables must be set")
        exit(1)
    
    # Run the agent
    livekit.agents.run(entrypoint(room_name, identity))