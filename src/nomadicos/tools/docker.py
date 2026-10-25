"""Docker tool (SPEC §22-23) — typed capability for container operations.

The Docker tool is a typed capability in front of the Docker Engine API.
It runs ONLY after the existing pipeline produced an AuthorizedAction:
validated, typed arguments, dispatches to docker, returns honest structured
ToolOutcomes with bounded evidence. It makes no permission decisions.
"""

from __future__ import annotations

import json
import time
from typing import Any, ClassVar

from pydantic import BaseModel, Field, ValidationError

from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.kernel.errors import Failure
from nomadicos.tools.base import Tool, ToolOutcome, error_text
from nomadicos.tools.context import ExecutionContext

MAX_OUTPUT_CHARS = 10000
MAX_CONTAINERS = 50
MAX_IMAGES = 50


class _Args(BaseModel):
    """Base for typed docker arguments; unknown fields fail closed."""

    model_config = {"extra": "forbid"}


class RunArgs(_Args):
    """Run a container (docker run)."""

    image: str = Field(min_length=1, max_length=256)
    command: list[str] = Field(default_factory=list)
    detach: bool = True
    name: str | None = Field(default=None, max_length=128)
    env: dict[str, str] = Field(default_factory=dict)
    ports: dict[str, int] = Field(default_factory=dict)  # host_port -> container_port
    volumes: dict[str, dict[str, str]] = Field(default_factory=dict)  # host_path -> {bind, mode}
    working_dir: str | None = Field(default=None, max_length=512)
    remove: bool = False
    network: str | None = Field(default=None, max_length=64)
    labels: dict[str, str] = Field(default_factory=dict)


class PsArgs(_Args):
    """List containers (docker ps)."""

    all: bool = False
    limit: int = Field(default=MAX_CONTAINERS, ge=1, le=MAX_CONTAINERS)
    filters: dict[str, str] = Field(default_factory=dict)


class ImagesArgs(_Args):
    """List images (docker images)."""

    all: bool = False
    limit: int = Field(default=MAX_IMAGES, ge=1, le=MAX_IMAGES)
    filter: str | None = None


class InspectArgs(_Args):
    """Inspect a container/image (docker inspect)."""

    target: str = Field(min_length=1, max_length=256)  # container name/id or image name
    type: str = Field(default="container")  # container or image


class LogsArgs(_Args):
    """Get container logs (docker logs)."""

    container: str = Field(min_length=1, max_length=256)
    tail: int = Field(default=100, ge=1, le=10000)
    follow: bool = False
    timestamps: bool = False


class StopArgs(_Args):
    """Stop a container (docker stop)."""

    container: str = Field(min_length=1, max_length=256)
    timeout: int = Field(default=10, ge=1, le=300)


class RemoveArgs(_Args):
    """Remove a container (docker rm)."""

    container: str = Field(min_length=1, max_length=256)
    force: bool = False
    volumes: bool = False


class PullArgs(_Args):
    """Pull an image (docker pull)."""

    image: str = Field(min_length=1, max_length=256)
    tag: str = Field(default="latest", max_length=128)


class BuildArgs(_Args):
    """Build an image (docker build)."""

    path: str = Field(min_length=1, max_length=512)
    tag: str | None = Field(default=None, max_length=256)
    dockerfile: str | None = Field(default=None, max_length=256)
    build_args: dict[str, str] = Field(default_factory=dict)
    no_cache: bool = False


class DockerTool(Tool):
    """Docker capability in front of the Docker Engine API (SPEC §3 Lego)."""

    name = "docker"
    ops: ClassVar[dict[str, type[BaseModel]]] = {
        "run": RunArgs,
        "ps": PsArgs,
        "images": ImagesArgs,
        "inspect": InspectArgs,
        "logs": LogsArgs,
        "stop": StopArgs,
        "remove": RemoveArgs,
        "pull": PullArgs,
        "build": BuildArgs,
    }

    def __init__(self, client: Any = None, DockerException: type[Exception] | None = None) -> None:
        self.client = client
        self._DockerException = DockerException

    def _exc(self) -> type[Exception]:
        if self._DockerException is not None:
            return self._DockerException
        try:
            from docker.errors import DockerException as DE

            return DE
        except ModuleNotFoundError:
            return Exception

    def _api_error(self) -> type[Exception]:
        try:
            from docker.errors import APIError

            return APIError
        except ModuleNotFoundError:
            return Exception

    def _not_found(self) -> type[Exception]:
        try:
            from docker.errors import NotFound

            return NotFound
        except ModuleNotFoundError:
            return Exception

    async def run(self, operation: str, args: dict[str, Any], ctx: ExecutionContext) -> ToolOutcome:
        model = self.ops.get(operation)
        if model is None:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"{self.name} has no operation {operation!r}",
            )
        try:
            typed = model.model_validate(args)
        except ValidationError as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"docker.{operation} args: {error_text(exc)}",
            )
        started = time.monotonic()
        try:
            outcome = self._dispatch(operation, typed, ctx)
        except self._exc() as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"docker error: {exc}",
                duration_s=round(time.monotonic() - started, 3),
            )
        return outcome.model_copy(update={"duration_s": round(time.monotonic() - started, 3)})

    def _dispatch(self, operation: str, typed: BaseModel, ctx: ExecutionContext) -> ToolOutcome:
        if operation == "run" and isinstance(typed, RunArgs):
            return self._run(typed, ctx)
        if operation == "ps" and isinstance(typed, PsArgs):
            return self._ps(typed, ctx)
        if operation == "images" and isinstance(typed, ImagesArgs):
            return self._images(typed, ctx)
        if operation == "inspect" and isinstance(typed, InspectArgs):
            return self._inspect(typed, ctx)
        if operation == "logs" and isinstance(typed, LogsArgs):
            return self._logs(typed, ctx)
        if operation == "stop" and isinstance(typed, StopArgs):
            return self._stop(typed, ctx)
        if operation == "remove" and isinstance(typed, RemoveArgs):
            return self._remove(typed, ctx)
        if operation == "pull" and isinstance(typed, PullArgs):
            return self._pull(typed, ctx)
        if operation == "build" and isinstance(typed, BuildArgs):
            return self._build(typed, ctx)
        raise ValueError(f"unknown dispatch for {operation}")

    def _run(self, a: RunArgs, ctx: ExecutionContext) -> ToolOutcome:
        kwargs: dict[str, Any] = {
            "image": a.image,
            "command": a.command or None,
            "detach": a.detach,
            "name": a.name,
            "environment": a.env or None,
            "ports": {f"{k}/tcp": v for k, v in a.ports.items()} if a.ports else None,
            "volumes": a.volumes or None,
            "working_dir": a.working_dir,
            "remove": a.remove,
            "network": a.network,
            "labels": a.labels or None,
        }
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:
            container = self.client.containers.run(**kwargs)
            return ToolOutcome(
                status=ExecutionStatus.SUCCEEDED,
                evidence={
                    "container_id": container.id[:12],
                    "container_name": container.name,
                    "image": a.image,
                    "detached": a.detach,
                },
                message=f"started container {container.name} ({container.id[:12]})",
            )
        except self._api_error() as exc:
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=f"docker run failed: {exc}",
            )

    def _ps(self, a: PsArgs, ctx: ExecutionContext) -> ToolOutcome:
        try:
            containers = self.client.containers.list(all=a.all, limit=a.limit, filters=a.filters)
            items = [
                {
                    "id": c.id[:12],
                    "name": c.name,
                    "image": c.image.tags[0] if c.image.tags else c.image.id[:12],
                    "status": c.status,
                    "ports": c.ports,
                }
                for c in containers
            ]
            return ToolOutcome(
                status=ExecutionStatus.SUCCEEDED,
                evidence={"count": len(items), "containers": items},
                message=f"listed {len(items)} containers",
            )
        except self._exc() as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"docker ps failed: {exc}",
            )

    def _images(self, a: ImagesArgs, ctx: ExecutionContext) -> ToolOutcome:
        try:
            flt = {"reference": a.filter} if a.filter else None
            images = self.client.images.list(all=a.all, limit=a.limit, filters=flt)
            items = [
                {
                    "id": img.id[7:19],
                    "tags": img.tags,
                    "size": img.attrs.get("Size", 0),
                }
                for img in images
            ]
            return ToolOutcome(
                status=ExecutionStatus.SUCCEEDED,
                evidence={"count": len(items), "images": items},
                message=f"listed {len(items)} images",
            )
        except self._exc() as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"docker images failed: {exc}",
            )

    def _inspect(self, a: InspectArgs, ctx: ExecutionContext) -> ToolOutcome:
        try:
            if a.type == "container":
                obj = self.client.containers.get(a.target)
                data = obj.attrs
            else:
                obj = self.client.images.get(a.target)
                data = obj.attrs
            # bounded output
            summary = json.dumps(data, default=str)[:MAX_OUTPUT_CHARS]
            return ToolOutcome(
                status=ExecutionStatus.SUCCEEDED,
                evidence={"target": a.target, "type": a.type, "summary": summary},
                message=f"inspected {a.type} {a.target}",
            )
        except self._not_found():
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=f"{a.type} not found: {a.target}",
            )
        except self._exc() as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"docker inspect failed: {exc}",
            )

    def _logs(self, a: LogsArgs, ctx: ExecutionContext) -> ToolOutcome:
        try:
            container = self.client.containers.get(a.container)
            logs = container.logs(tail=a.tail, follow=a.follow, timestamps=a.timestamps)
            text = logs.decode("utf-8", errors="replace") if isinstance(logs, bytes) else str(logs)
            preview = text[:MAX_OUTPUT_CHARS]
            return ToolOutcome(
                status=ExecutionStatus.SUCCEEDED,
                evidence={
                    "container": a.container,
                    "lines": len(text.splitlines()),
                    "preview": preview,
                },
                message=f"retrieved logs for {a.container}",
            )
        except self._not_found():
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=f"container not found: {a.container}",
            )
        except self._exc() as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"docker logs failed: {exc}",
            )

    def _stop(self, a: StopArgs, ctx: ExecutionContext) -> ToolOutcome:
        try:
            container = self.client.containers.get(a.container)
            container.stop(timeout=a.timeout)
            return ToolOutcome(
                status=ExecutionStatus.SUCCEEDED,
                evidence={"container": a.container, "stopped": True},
                message=f"stopped container {a.container}",
            )
        except self._not_found():
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=f"container not found: {a.container}",
            )
        except self._exc() as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"docker stop failed: {exc}",
            )

    def _remove(self, a: RemoveArgs, ctx: ExecutionContext) -> ToolOutcome:
        try:
            container = self.client.containers.get(a.container)
            container.remove(force=a.force, v=a.volumes)
            return ToolOutcome(
                status=ExecutionStatus.SUCCEEDED,
                evidence={"container": a.container, "removed": True},
                message=f"removed container {a.container}",
            )
        except self._not_found():
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=f"container not found: {a.container}",
            )
        except self._exc() as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"docker rm failed: {exc}",
            )

    def _pull(self, a: PullArgs, ctx: ExecutionContext) -> ToolOutcome:
        try:
            image = self.client.images.pull(a.image, tag=a.tag)
            return ToolOutcome(
                status=ExecutionStatus.SUCCEEDED,
                evidence={"image": f"{a.image}:{a.tag}", "id": image.id[7:19]},
                message=f"pulled {a.image}:{a.tag}",
            )
        except self._exc() as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"docker pull failed: {exc}",
            )

    def _build(self, a: BuildArgs, ctx: ExecutionContext) -> ToolOutcome:
        try:
            image, logs = self.client.images.build(
                path=a.path,
                tag=a.tag,
                dockerfile=a.dockerfile,
                buildargs=a.build_args or None,
                nocache=a.no_cache,
            )
            log_text = "".join(log.get("stream", "") for log in logs if "stream" in log)
            preview = log_text[:MAX_OUTPUT_CHARS]
            return ToolOutcome(
                status=ExecutionStatus.SUCCEEDED,
                evidence={"image_id": image.id[7:19], "tag": a.tag, "log_preview": preview},
                message=f"built image {a.tag or image.id[7:19]}",
            )
        except self._exc() as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"docker build failed: {exc}",
            )


class UnavailableDockerTool(DockerTool):
    """Honest absence: raises a structured unavailability error."""

    def __init__(self) -> None:
        # Don't call parent __init__ - we have no client
        self.client = None
        self._DockerException = None

    async def run(self, operation: str, args: dict[str, Any], ctx: ExecutionContext) -> ToolOutcome:
        return ToolOutcome(
            status=ExecutionStatus.ERRORED,
            failure=Failure.RESOURCE_UNAVAILABLE,
            message="docker unavailable: Docker daemon not accessible",
        )


def _docker_client():
    """Lazy import and connection test for Docker."""
    try:
        import docker
        from docker.errors import DockerException as DE
    except ModuleNotFoundError:
        return None, None
    try:
        client = docker.from_env()
        client.ping()
        return client, DE
    except DE:
        return None, DE


def docker_tool() -> DockerTool:
    """Platform-selected adapter: real Docker client if available, honest absence otherwise."""
    client, DE = _docker_client()
    if client is None:
        return UnavailableDockerTool()
    return DockerTool(client, DE)
