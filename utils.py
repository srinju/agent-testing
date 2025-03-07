import logging
import asyncio
from transcript import save_transcript

logger = logging.getLogger("voice-agent")

def setup_participant_handlers(ctx, agent, exam_state, db_driver):
    """
    Sets up handlers for participant connection and disconnection events.
    
    Args:
        ctx: Job context
        agent: The voice pipeline agent
        exam_state: Current state of the exam
        db_driver: Database driver instance
    """
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant):
        logger.info(f"Participant connected: {participant.identity}")

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant):
        logger.info(f"Participant disconnected: {participant.identity}")
        
        # Save the conversation transcript when the participant disconnects
        asyncio.create_task(save_transcript(db_driver, exam_state.exam.exam_id, agent))

async def wait_for_data(agent, exam_state):
    """
    Periodically checks if exam data has been received.
    
    Args:
        agent: The voice pipeline agent
        exam_state: Current state of the exam
    """
    check_interval = 5  # seconds
    max_checks = 12  # 60 seconds total
    checks = 0
    
    while checks < max_checks and not exam_state.data_received:
        await asyncio.sleep(check_interval)
        checks += 1
        logger.info(f"Data check {checks}/{max_checks}: Data received: {exam_state.data_received}")
        
        if checks == 3 and not exam_state.data_received:  # After 15 seconds
            await agent.say("Waiting for exam data. Please ensure it has been sent to the room.", allow_interruptions=False)