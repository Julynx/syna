import shlex
import sys
import tkinter as tk
from tkinter import filedialog

from bollard import Container
from state import get_container


def send(dest_path):
    src_path = _select_file_or_folder()
    if src_path is None:
        return "No input file or folder selected"

    container: Container = get_container()
    container.copy_to(src_path, dest_path)
    return f"Directory '{src_path}' copied from machine to '{dest_path}'"


def receive(src_path):
    dest_path = _select_folder()
    if dest_path is None:
        return "No output folder selected"

    container: Container = get_container()
    container.copy_from(src_path, dest_path)
    return f"Directory '{src_path}' copied from pod to '{dest_path}'"


def bye():
    sys.exit(0)


def show_help():
    return get_command_help()


def parse_and_execute_command(command_str: str) -> str:
    command = shlex.split(command_str.strip())

    # Ensure the command is registered
    error_msg_unreg = f"Unregistered command {command_str}"
    try:
        command_name = command[0].strip().removeprefix("/")
    except IndexError as index_error:
        raise ValueError(error_msg_unreg) from index_error
    if command_name not in registered_commands:
        raise ValueError(error_msg_unreg)
    reg_cmd = registered_commands[command_name]

    # Ensure enough arguments are provided
    reg_cmd_n_args = reg_cmd["n_args"]
    error_msg_n_args = (
        f"Not enough arguments for command {command_str}"
        f" (Takes {reg_cmd_n_args} arguments)."
    )
    if len(command) - 1 != reg_cmd_n_args:
        raise ValueError(error_msg_n_args)

    # Call the command and get a response
    args = command[1:]
    try:
        result = reg_cmd["callback"](*args)
    except Exception as exc:
        raise ValueError(f"Error invoking callback '{command_str}': {exc}") from exc
    return result


def get_command_help():
    text = ["The following commands are supported:"]
    for command, params in registered_commands.items():
        text.append(" " * 2 + f"/{command}: {params['description']}")
    return "\n".join(text)


def _select_folder():
    root = tk.Tk()
    root.withdraw()
    file_path = filedialog.askdirectory()
    return file_path


def _select_file_or_folder() -> str | None:
    result = {"path": None}

    def select_file():
        path = filedialog.askopenfilename(title="Select a file", parent=root)
        if path:
            result["path"] = path
            root.destroy()

    def select_folder():
        path = filedialog.askdirectory(title="Select a folder", parent=root)
        if path:
            result["path"] = path
            root.destroy()

    root = tk.Tk()
    root.title("Select File or Folder")

    tk.Button(
        root,
        text="Select File",
        command=select_file,
        width=20,
    ).pack(padx=20, pady=10)
    tk.Button(
        root,
        text="Select Folder",
        command=select_folder,
        width=20,
    ).pack(padx=20, pady=(0, 20))

    root.mainloop()

    return result["path"]


registered_commands = {
    "send": {
        "description": "Send a file or a directory to Syna's pod.",
        "n_args": 1,
        "callback": send,
    },
    "receive": {
        "description": "Get a file or a directory from Syna's pod.",
        "n_args": 1,
        "callback": receive,
    },
    "help": {
        "description": "Show this help message.",
        "n_args": 0,
        "callback": show_help,
    },
    "exit": {
        "description": "End the chat with Syna.",
        "n_args": 0,
        "callback": bye,
    },
}
