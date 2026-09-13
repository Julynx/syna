"""Application entrypoint for Syna AI agent."""

import sys

from ask import ask_loop
from state import close_docker_client


def main() -> None:
    """Start the interactive agent loop."""
    ask_loop()


if __name__ == "__main__":
    try:
        main()
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
