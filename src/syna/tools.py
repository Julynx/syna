from .config import truncate_command_output
from .state import get_container


def execute_command(command: str, arguments: list[str]):
    """
    Execute a shell command and get its output.

    The shell command will be executed inside a linux machine you have full control
    over. You may install packages, create and run scripts, and take any action you need
    to perform your assigned task.

    Almost any task can be completed through a linux terminal in one way or another.
    """
    container = get_container()
    response = container.exec([command, *arguments])
    return truncate_command_output(response)


def respond(text: str):
    """
    Send a message to the user. This will end the task and pass priority to him.
    Never include a call to this tool in the same response as other tool calls.
    If you are still working on a task, only call this tool when you have finished.
    """
    return text
