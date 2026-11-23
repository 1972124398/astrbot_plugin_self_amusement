"""Read-only AstrBot search and bounded RSS/Atom browsing."""

from __future__ import annotations

import asyncio
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urlsplit

import aiohttp


SEARCH_TOOLS = {
    "tavily": "web_search_tavily", "bocha": "web_search_bocha",
    "brave": "web_search_brave", "firecrawl": "web_search_firecrawl",
    "baidu_ai_search": "web_search_baidu", "exa": "web_search_exa",
    "anysearch": "web_search_anysearch", "deepseek": "web_search_deepseek",
    "serpapi": "web_search_serpapi",
}


def http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.username is None and parsed.password is None
    except ValueError:
        return False


def plain_text(value: str, limit=500) -> str:
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())[:limit]


def parse_feed(data: bytes, *, now: float, max_age_hours: float) -> list[dict]:
    root = ET.fromstring(data)
    results = []
    for item in root.iter():
        if item.tag.split("}")[-1] not in {"item", "entry"}:
            continue
        fields = {}
        link = ""
        for child in item:
            key = child.tag.split("}")[-1]
            fields[key] = child.text or ""
            if key == "link" and child.get("rel", "alternate") == "alternate":
                link = child.get("href") or child.text or ""
        if not http_url(link.strip()):
            continue
        published = fields.get("pubDate") or fields.get("published") or fields.get("updated") or fields.get("date") or ""
        if published:
            try:
                stamp = parsedate_to_datetime(published).timestamp()
            except (ValueError, TypeError, OverflowError):
                try:
                    stamp = datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    stamp = None
            if stamp is not None and (now - stamp > max_age_hours * 3600 or stamp > now + 86400):
                continue
        results.append({
            "title": plain_text(fields.get("title", ""), 180),
            "url": link.strip(),
            "snippet": plain_text(fields.get("description") or fields.get("summary") or fields.get("content") or ""),
            "published": published[:100],
        })
    return results[:12]


class Browser:
    def __init__(self, context):
        self.context = context

    async def search(self, event, query: str, feeds: list[str], *, timeout: float, max_age_hours: float):
        config = self.context.get_config(umo=event.unified_msg_origin)
        settings = config.get("provider_settings", {})
        provider = settings.get("websearch_provider", "tavily")
        if settings.get("web_search", False) and provider in SEARCH_TOOLS:
            try:
                from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext

                tool = self.context.get_llm_tool_manager().get_builtin_tool(SEARCH_TOOLS[provider])
                wrapper = AgentContextWrapper(context=AstrAgentContext(context=self.context, event=event))
                async with asyncio.timeout(timeout):
                    result = await tool.call(wrapper, query=query)
                data = json.loads(result) if isinstance(result, str) else result
                entries = data.get("results", []) if isinstance(data, dict) else []
                results = [
                    {"title": plain_text(str(row.get("title", "")), 180),
                     "url": row["url"], "snippet": plain_text(str(row.get("snippet", ""))),
                     "published": str(row.get("published", ""))[:100]}
                    for row in entries if isinstance(row, dict) and http_url(str(row.get("url", "")))
                ]
                if results:
                    return {"mode": "search", "results": results[:6]}
            except Exception:
                # A configured search service can be unavailable while subscriptions still work.
                pass
        urls = [url.strip() for url in feeds if isinstance(url, str) and http_url(url.strip())][:8]
        if not urls:
            return {"mode": "unavailable", "results": []}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async def read_feed(url):
                try:
                    async with session.get(url, headers={"User-Agent": "AstrBot-Self-Amusement/0.1"}) as response:
                        response.raise_for_status()
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(32768):
                            data.extend(chunk)
                            if len(data) > 1024 * 1024:
                                raise ValueError("Feed is too large")
                    return parse_feed(bytes(data), now=time.time(), max_age_hours=max_age_hours)
                except (aiohttp.ClientError, TimeoutError, ValueError, ET.ParseError):
                    return []

            batches = await asyncio.gather(*(read_feed(url) for url in urls))
        unique = {}
        for batch in batches:
            for entry in batch:
                unique.setdefault(entry["url"], entry)
        return {"mode": "rss", "results": list(unique.values())[:8]}
