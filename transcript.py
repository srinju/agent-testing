import datetime
import logging

logger = logging.getLogger("voice-agent")

def extract_conversation_transcript(agent):
    """
    Extracts a formatted conversation transcript from the agent's chat context.
    
    Args:
        agent: The voice pipeline agent with chat context
        
    Returns:
        list: A list of conversation messages with role, content, and timestamp
    """
    conversation = []
    current_time = datetime.datetime.now()
    
    for message in agent.chat_ctx.messages:
        # Skip system messages and empty messages
        if message.role == "system":
            continue
            
        # Get the message content
        content = ""
        if hasattr(message, "content"):
            content = message.content
        elif hasattr(message, "text"):
            content = message.text
            
        if not content.strip():
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
            "content": content.strip(),
            "timestamp": timestamp
        })
        
    return conversation

async def save_transcript(db_driver, exam_id, agent):
    """
    Saves the conversation transcript to the database.
    
    Args:
        db_driver: Database driver instance
        exam_id: ID of the exam
        agent: The voice pipeline agent with chat context
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        conversation = extract_conversation_transcript(agent)
            
        # Save to database
        if db_driver.save_conversation_transcript(exam_id, conversation):
            logger.info(f"Successfully saved conversation transcript to submission for exam {exam_id}")
            return True
        else:
            logger.error(f"Failed to save conversation transcript for exam {exam_id}")
            return False
            
    except Exception as e:
        logger.error(f"Error saving conversation transcript: {str(e)}", exc_info=True)
        return False