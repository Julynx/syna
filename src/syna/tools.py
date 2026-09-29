from pathlib import Path

from .config import truncate_command_output
from .editor import read_file as e_read_file
from .state import get_container


def read_file(file_path):
    temp_path = Path(".syna-tmp")
    try:
        container = get_container()
        container.copy_from(file_path, temp_path)
        output = e_read_file(Path(temp_path, Path(file_path).name))
    finally:
        try:
            temp_path.unlink()
        except PermissionError:
            pass
    return output


def execute_command(command: str, arguments: list[str]):
    """
    Execute a shell command and get its output.

    The shell command will be executed inside a debian machine you have full control
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
    Do not include a call to this tool in the same message as other tool calls.
    If you are still working on a task, only call this tool when you have finished.
    """
    return text
