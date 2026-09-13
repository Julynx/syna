import logging
from contextlib import ExitStack

from bollard import DockerClient

logging.getLogger("bollard").setLevel(logging.WARNING)

_exit_stack: ExitStack | None = None
client = None
container = None


def get_container():
    global client, container
    if client is None:
        init_docker_client()
    if container is not None:
        return container
    container = client.run_container("alpine:latest", command="sleep infinity")
    return container


def init_docker_client(*args, **kwargs):
    """Initializes the context manager globally."""
    global _exit_stack, client

    if _exit_stack is not None:
        raise RuntimeError("Docker client is already initialized.")

    _exit_stack = ExitStack()
    client = _exit_stack.enter_context(DockerClient(*args, **kwargs))


def close_docker_client():
    """Manually closes the context manager."""
    global _exit_stack, client, container

    if container is not None:
        container.stop()
        container.remove(force=True)

    if _exit_stack is not None:
        _exit_stack.close()
        _exit_stack = None
        client = None


def get_docker_client():
    """Safely retrieves the docker client."""
    if client is None:
        raise RuntimeError(
            "Docker client has not been initialized. Call init_docker_client() first."
        )
    return client
