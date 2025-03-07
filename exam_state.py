class ExamState:
    """
    Manages the state of an exam session including current question, progress tracking,
    and flags for controlling the flow of the exam.
    """
    def __init__(self):
        self.exam = None
        self.current_question_idx = 0
        self.questions_asked = 0
        self.data_received = False
        self.exam_completed = False
        self.needs_another_chance = False
        self.waiting_for_another_chance_response = False
        self.waiting_for_next_question_confirmation = False
        self.current_question = None