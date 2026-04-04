import asyncio
import json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

class BrowserMCPClient:
    def __init__(self):
        self.session = None
        self._client = None

    async def __aenter__(self):
        server_params = StdioServerParameters(
            command="npx",
            args=["@playwright/mcp@latest", "--headless"],
        )
        self._client = stdio_client(server_params)
        read, write = await self._client.__aenter__()
        self.session = ClientSession(read, write)
        await self.session.__aenter__()
        await self.session.initialize()
        return self

    async def __aexit__(self, *args):
        await self.session.__aexit__(*args)
        await self._client.__aexit__(*args)

    async def get_tools(self):
        result = await self.session.list_tools()
        return result.tools

    async def call_tool(self, name: str, arguments: dict):
        result = await self.session.call_tool(name, arguments)
        texts = []
        for block in result.content:
            if hasattr(block, 'text'):
                texts.append(block.text)
        return "\n".join(texts)