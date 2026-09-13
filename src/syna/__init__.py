from ask import ask_loop
import sys
from state import close_docker_client


def main() -> None:
    ask_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\n\n  + (Received CTRL+C:"
            " Cleaning up and shutting down, please wait...)\n"
        )
        sys.exit(0)
    finally:
        try:
            close_docker_client()
        except KeyboardInterrupt:
            pass
