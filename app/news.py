"""Keyless market headlines, beside the technical read.

The desk says plainly that it is *technical only*. A trader looking at a chart
still wants to know what is in the tape, so this layer fetches recent headlines
from public, keyless RSS feeds and shows them next to the analysis.

Honesty rules, because a news layer is where a trading tool usually starts to
lie:

  * **Headlines only, and that is all they are.** RSS gives a title, a source and
    a link -- no article body. Nothing here claims to have *read* the story, and
    no field is called "sentiment" or "impact". The word "sentiment" does not
    appear on any surface; a test asserts it.
  * **The source is named per item.** A headline is shown with the feed it came
    from, so a reader can judge it, not just trust it.
  * **A feed that fails is reported, not hidden.** The result carries per-feed
    status; an empty list because every feed 404'd must not read as "no news".
  * **No key, by design.** These are public feeds; adding a paid news API would
    put a secret and a bill where neither is needed.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

import httpx
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

# Public, keyless RSS. Each is labelled with the outlet it actually is -- the
# earlier lesson that a feed's URL can disagree with its name. Reuters' old
# feeds.reuters.com host is dead (DNS), so it is not listed; a feed that only
# ever fails is noise, not coverage.
FEEDS = [
    {
        "name": "CNBC Markets",
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    },
    {"name": "Investing.com Forex", "url": "https://www.investing.com/rss/news_1.rss"},
    {"name": "Investing.com Commodities", "url": "https://www.investing.com/rss/news_11.rss"},
    {"name": "MarketWatch", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
]

UA = "AnalysisDesk/1.1 (+https://github.com/msy898931-boop/analysisdesk)"
_CACHE_TTL = 600  # 10 minutes; headlines do not need to be second-fresh
_cache: dict[str, tuple[float, NewsResult]] = {}


@dataclass
class Headline:
    title: str
    source: str
    link: str
    published: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class NewsResult:
    headlines: list[Headline] = field(default_factory=list)
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: list[dict] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """True when at least one feed answered -- not when the list is empty."""
        return bool(self.sources_ok)

    def as_dict(self) -> dict:
        return {
            "headlines": [h.as_dict() for h in self.headlines],
            "sources_ok": self.sources_ok,
            "sources_failed": self.sources_failed,
            "usable": self.usable,
            "note": (
                "headlines only — the desk has not read the articles and "
                "makes no claim about their effect on price"
            ),
        }


def _text(el: ET.Element | None) -> str:
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def parse_feed(xml_text: str, source: str) -> list[Headline]:
    """Parse RSS 2.0 or Atom into headlines. Pure; no network, no state.

    Returns [] on anything unparseable rather than raising, so one malformed
    feed cannot take down the whole read. Parsing goes through defusedxml, which
    refuses entity-expansion ("billion laughs") and external-entity payloads in
    a feed we did not write.
    """
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, DefusedXmlException):
        return []

    out: list[Headline] = []
    # RSS 2.0: rss/channel/item ; Atom: feed/entry
    items = root.findall(".//item")
    atom = False
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        atom = True

    for it in items:
        if atom:
            title = _text(it.find("{http://www.w3.org/2005/Atom}title"))
            link_el = it.find("{http://www.w3.org/2005/Atom}link")
            link = link_el.get("href", "") if link_el is not None else ""
            published = _text(it.find("{http://www.w3.org/2005/Atom}updated")) or _text(
                it.find("{http://www.w3.org/2005/Atom}published")
            )
        else:
            title = _text(it.find("title"))
            link = _text(it.find("link"))
            published = _text(it.find("pubDate"))
        if title:
            out.append(Headline(title=title, source=source, link=link, published=published))
    return out


async def _fetch_feed(client: httpx.AsyncClient, feed: dict) -> tuple[list[Headline], str | None]:
    try:
        r = await client.get(feed["url"])
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}"
        return parse_feed(r.text, feed["name"]), None
    except (httpx.HTTPError, ValueError) as exc:
        return [], f"{type(exc).__name__}: {exc}"


async def fetch_news(*, fresh: bool = False, limit: int = 12) -> NewsResult:
    """Fetch and merge headlines from every feed. Cache for `_CACHE_TTL` seconds.

    A cached read is only reused when at least one feed succeeded; a total
    failure is not cached, so the next call retries instead of serving silence.
    """
    now = time.time()
    hit = _cache.get("news")
    if hit and not fresh and now - hit[0] < _CACHE_TTL:
        return hit[1]

    result = NewsResult()
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}, follow_redirects=True) as client:
        for feed in FEEDS:
            heads, err = await _fetch_feed(client, feed)
            if err:
                result.sources_failed.append({"name": feed["name"], "error": err})
            else:
                result.sources_ok.append(feed["name"])
                result.headlines.extend(heads)

    result.headlines = result.headlines[:limit]
    # Cache only a read that worked. A total failure is not cached, so the next
    # call retries rather than serving silence as if it were "no news".
    if result.sources_ok:
        _cache["news"] = (now, result)
    return result
