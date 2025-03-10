import asyncio
import logging
import json
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

# Import our modules
from exam_db_driver import ExamDBDriver
from exam_state import ExamState
from handlers import ask_next_question, handle_data_received, on_user_speech_committed
from transcript import save_transcript
from utils import setup_participant_handlers, wait_for_data

logger = logging.getLogger("voice-agent")

def prewarm(proc: JobProcess):
    """Initialize resources before the agent starts."""
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["db"] = ExamDBDriver()  # Initialize ExamDBDriver 

async def entrypoint(ctx: JobContext):
    """Main entry point for the voice agent."""
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

    # Initialize exam state
    exam_state = ExamState()
    db_driver = ctx.proc.userdata["db"]
    last_user_message = None

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

    # Register data received handler BEFORE connecting participants
    ctx.room.on("data_received", lambda data: asyncio.create_task(handle_data_received(data, agent, exam_state, db_driver)))
    
    # Set up participant handlers
    setup_participant_handlers(ctx, agent, exam_state, db_driver)
    
    # Wait for participant after setting up event handlers
    participant = await ctx.wait_for_participant()
    logger.info(f"Starting voice assistant for participant {participant.identity}")
    logger.info(f"Agent's own identity: {ctx.room.local_participant.identity}")

    # Register user speech handler
    agent.on("user_speech_committed", lambda _: asyncio.create_task(
        on_user_speech_committed(agent, exam_state, db_driver, last_user_message)
    ))

    # Start the agent
    agent.start(ctx.room, participant)
    
    # Wait for exam data
    await wait_for_data(agent, exam_state)
    
    try:
        # Instead of wait_until_done, use a different approach to keep the agent running
        # Create an event that will be set when the exam is completed
        exam_completed_event = asyncio.Event()
        
        # Add a handler to monitor when the exam is completed
        async def monitor_exam_completion():
            while not exam_state.exam_completed:
                await asyncio.sleep(1)
            # When exam is completed, set the event
            logger.info("Exam completion detected, setting event")
            exam_completed_event.set()
        
        # Start the monitoring task
        monitor_task = asyncio.create_task(monitor_exam_completion())
        
        # Wait for the exam to complete
        await exam_completed_event.wait()
        
        # Add a small delay to ensure the final message is delivered
        logger.info("Exam completed, waiting for final message delivery")
        await asyncio.sleep(2)
        
        # Save the conversation transcript when the exam is completed or when we exit
        logger.info("Saving transcript")
        await save_transcript(db_driver, exam_state.exam.exam_id, agent)
        
        # Stop the agent
        logger.info("Stopping agent")
        agent.stop()
        
        # Disconnect from the room
        logger.info("Disconnecting from room")
        await ctx.disconnect()
        
        logger.info("Successfully disconnected from room")
    except Exception as e:
        logger.error(f"Error during exam completion: {e}", exc_info=True)
        # Try to disconnect even if there was an error
        try:
            logger.info("Attempting to disconnect after error")
            agent.stop()
            await ctx.disconnect()
        except Exception as disconnect_error:
            logger.error(f"Error during disconnect: {disconnect_error}", exc_info=True)

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        ),
    )