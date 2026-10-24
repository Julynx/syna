"""Application entrypoint for Syna AI agent."""

import sys

from .conversation.ask import ask_loop
from .state import close_docker_client


def main() -> None:
    """Start the interactive agent loop."""
    try:
        ask_loop()
    except ValueError as exc:
        print(f"\n  ! Configuration error: {exc}")
        print("\n    Please run 'syna' or 'syna-setup' to complete first-time setup.\n")
        sys.exit(1)
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
