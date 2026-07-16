from __future__ import annotations

import asyncio
import logging

from mewcode.config import MCPServerConfig
from mewcode.mcp.client import MCPClient
from mewcode.mcp.tool_wrapper import MCPToolWrapper
from mewcode.tools import ToolRegistry

logger = logging.getLogger(__name__)

MCP_CONNECT_TIMEOUT_SECONDS = 30
MCP_DISCOVERY_TIMEOUT_SECONDS = 30
MCP_CLOSE_TIMEOUT_SECONDS = 10


class MCPManager:


    def __init__(self) -> None:
        self._configs: dict[str, MCPServerConfig] = {}
        self._clients: dict[str, MCPClient] = {}


    def load_configs(self, configs: list[MCPServerConfig]) -> None:
        for cfg in configs:
            self._configs[cfg.name] = cfg


    async def register_all_tools(self, registry: ToolRegistry) -> list[str]:
        errors: list[str] = []
        for name, config in self._configs.items():
            stage = "connection"
            stage_timeout = MCP_CONNECT_TIMEOUT_SECONDS
            client: MCPClient | None = None
            try:
                client = MCPClient(config)
                await asyncio.wait_for(
                    client.connect(),
                    timeout=stage_timeout,
                )
                self._clients[name] = client

                stage = "tool discovery"
                stage_timeout = MCP_DISCOVERY_TIMEOUT_SECONDS
                tools = await asyncio.wait_for(
                    client.list_tools(),
                    timeout=stage_timeout,
                )
                for tool_def in tools:
                    wrapper = MCPToolWrapper(name, tool_def, client)
                    registry.register(wrapper)
                    logger.info("Registered MCP tool: %s", wrapper.name)

            except asyncio.TimeoutError:
                msg = (
                    f"MCP server '{name}' {stage} timed out "
                    f"after {stage_timeout}s"
                )
                logger.error(msg, exc_info=True)
                errors.append(msg)
                await self._discard_failed_client(name, client)
            except Exception as e:
                msg = f"MCP server '{name}': {e}"
                logger.error(msg, exc_info=True)
                errors.append(msg)
                await self._discard_failed_client(name, client)

        return errors


    async def _discard_failed_client(
        self,
        name: str,
        client: MCPClient | None,
    ) -> None:
        self._clients.pop(name, None)
        if client is None:
            return
        try:
            await asyncio.wait_for(
                client.close(),
                timeout=MCP_CLOSE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.error(
                "Failed to clean up MCP client after registration failure: "
                "name=%s reason=%s",
                name,
                exc,
                exc_info=True,
            )


    async def get_client(self, name: str) -> MCPClient | None:
        client = self._clients.get(name)
        if client is None:
            config = self._configs.get(name)
            if config is None:
                return None
            client = MCPClient(config)
            await asyncio.wait_for(
                client.connect(),
                timeout=MCP_CONNECT_TIMEOUT_SECONDS,
            )
            self._clients[name] = client
            return client

        if not client.is_alive:
            logger.info("Reconnecting MCP server '%s'", name)
            await client.close()
            client = MCPClient(self._configs[name])
            await asyncio.wait_for(
                client.connect(),
                timeout=MCP_CONNECT_TIMEOUT_SECONDS,
            )
            self._clients[name] = client

        return client


    async def shutdown(self) -> None:
        for name, client in self._clients.items():
            try:
                await asyncio.wait_for(
                    client.close(),
                    timeout=MCP_CLOSE_TIMEOUT_SECONDS,
                )
                logger.info("MCP server '%s' closed", name)
            except Exception as exc:
                logger.error(
                    "Failed to close MCP server: name=%s reason=%s",
                    name,
                    exc,
                    exc_info=True,
                )
        self._clients.clear()
