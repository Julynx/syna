from state import get_container


def execute_command(command: str, arguments: list[str]):
    """
    Execute a shell command and get its output.

    The shell command will be executed inside a linux machine you have full control
    over. You may install packages, create and run scripts, and take any action you need
    to perform your assigned task.

    Almost any task can be completed through a linux terminal in one way or another, so
    do not give up until you reach your stated goal.
    """
    container = get_container()
    response = container.exec([command, *arguments])
    return response


def finish(answer):
    """
    Finish the task by sending your final answer to the user. This will pass priority
    for him to input your next task to solve.
    """
    return answer
