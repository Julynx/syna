"""Application entrypoint for Syna AI agent."""

import sys

from .ask import ask_loop
from .state import close_docker_client

def main():
    from .tools import read_file
    result = read_file("/etc/os-release")
    print(result)

def main_() -> None:
    """Start the interactive agent loop."""
    try:
        ask_loop()
    except KeyboardInterrupt:
        print("\n  + (Received CTRL+C)")
        sys.exit(0)
    finally:
        try:
            print("  + (Cleaning up container and shutting down, please wait...)")
            close_docker_client()
            print("  + (Cleanup finished. Goodbye!)\n")
        except KeyboardInterrupt:
            pass
