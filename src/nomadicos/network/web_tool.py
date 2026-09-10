"""Web tools (BP §13 item 14, §48): public fetch through the Network Gateway.

The tool surface is thin — all mediation lives in the Network Gateway; this
tool is what the Agent Runtime's tool registry exposes.
"""

from typing import Any

from nomadicos.core.errors import NetworkDenied, ValidationError
from nomadicos.network.gateway import NetworkGateway
from nomadicos.security.permissions import SubjectIdentity
from nomadicos.tools.base import Tool, ToolContext, ToolResult, ToolRisk, ToolSpec


class WebFetchTool(Tool):
    """NETWORK-risk tool: fetch one public document (BP §48, §268)."""

    def __init__(self, gateway: NetworkGateway) -> None:
        self._gateway = gateway
        self._spec = ToolSpec(
            name="web.fetch",
            description="fetch one public web page (GET) and return sanitized text with provenance",
            risk=ToolRisk.NETWORK,
            arguments_schema={
                "type": "object",
                "properties": {"url": {"type": "string", "minLength": 8, "maxLength": 2048}},
                "required": ["url"],
                "additionalProperties": False,
            },
            side_effects=["performs a public GET request (no private data sent)"],
        )
        self.last_identity: SubjectIdentity | None = None

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from nomadicos.tools.base import validate_against_schema

        validate_against_schema(arguments, self._spec.arguments_schema)
        url = arguments["url"]
        if not url.lower().startswith(("http://", "https://")):
            raise ValidationError("web.fetch requires an http(s) URL")
        return {"url": url}

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        identity = self.last_identity or SubjectIdentity(
            user_id=context.user_id,
            session_id=context.session_id,
            task_id=context.task_id,
            run_id=context.run_id,
            step_id=context.step_id,
        )
        try:
            document = await self._gateway.fetch_public(arguments["url"], identity)
        except NetworkDenied as exc:
            return ToolResult.failure(
                str(exc), evidence={"url": arguments["url"], "denied": True}
            )
        return ToolResult(
            success=True,
            data={
                "url": document.source_url,
                "domain": document.domain,
                "text": document.text,
                "untrusted": document.untrusted,
                "citation": document.citation(),
            },
            evidence={
                "content_hash": document.content_hash,
                "domain": document.domain,
                "provenance_recorded": True,
            },
        )


__all__ = ["WebFetchTool"]
