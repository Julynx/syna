from ask import ask_loop
from state import close_docker_client


def main() -> None:
    try:
        ask_loop()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            close_docker_client()
        except KeyboardInterrupt:
            pass
