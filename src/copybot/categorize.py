"""Inferencia de categoría desde el slug + pregunta del mercado.

Polymarket no siempre devuelve `category`. Este módulo aplica reglas
basadas en keywords para clasificar mercados sin etiqueta.
"""
from __future__ import annotations

import re
from typing import Iterable

# Orden importa: el primer match gana (de más específico a más genérico)
# Las claves son la categoría canónica.
RULES: list[tuple[str, list[str]]] = [
    ("crypto", [
        "bitcoin", "btc", "ethereum", " eth ", "solana", " sol ", "dogecoin",
        "doge", "shiba", "shib", "xrp", "cardano", "ada", "binance", " bnb ",
        "polkadot", "avalanche", "chainlink", "polygon", "matic", "litecoin",
        "ltc", "stablecoin", "usdt", "usdc", "tether", "stellar", "monero",
        "halving", "etf-bitcoin", "spot-etf", "altcoin", "memecoin", "pepe",
        "dogwifhat", "wif", "bonk", "hyperliquid", "blockchain", "defi",
        "nft", " dex ", "uniswap", "coinbase", "kraken", "mt-gox", "okex",
        "tron", "ftx", "sam-bankman", "ripple",
    ]),
    ("politics-us", [
        "trump", "biden", "harris", "pelosi", "mcconnell", "schumer", "obama",
        "vance", "newsom", "desantis", "kennedy", "rfk", "ramaswamy",
        "president-2024", "president-2025", "president-2028", "election",
        "primary", "republican", "democrat", "gop", "senator", "congress",
        "house-speaker", "vice-president", "vp-pick", "running-mate",
        "supreme-court", "scotus", "cabinet", "secretary-of", "attorney-general",
        "impeach", "indict", "convict", "filibuster", "shutdown", "debt-ceiling",
        "midterm", "swing-state", "electoral-college", "popular-vote",
        "white-house", "oval-office", "state-of-the-union", "executive-order",
    ]),
    ("politics-world", [
        "putin", "zelensky", "ukraine", "russia", "nato", "xi-jinping",
        "china", "taiwan", "hong-kong", "north-korea", "kim-jong",
        "iran", "israel", "netanyahu", "hamas", "gaza", "hezbollah",
        "syria", "assad", "saudi", "uae", "macron", "merkel", "starmer",
        "milei", "lula", "argentina", "brazil", "mexico", "canada",
        "trudeau", "modi", "india", "pakistan", "europe", "european-union",
        "brexit", "uk-elect", "germany-elect", "france-elect", "italy-elect",
        "venezuela", "maduro", "korea", "japan", "kishida",
    ]),
    ("war-conflict", [
        "war", "ceasefire", "invasion", "missile", "nuclear", "drone-strike",
        "airstrike", "casualt", "troops", "military", "putin-vs", "ukraine-war",
        "israel-war", "world-war",
    ]),
    ("sports-nfl", [
        "nfl", "super-bowl", "superbowl", "patriots", "chiefs", "eagles",
        "cowboys", "packers", "ravens", "bengals", "bills", "lions",
        "49ers", "giants", "jets", "dolphins", "steelers", "broncos",
        "vikings", "saints", "rams", "seahawks", "panthers", "colts",
        "texans", "browns", "titans", "raiders", "chargers", "buccaneers",
        "falcons", "cardinals", "jaguars", "commanders",
        "mahomes", "lamar-jackson", "josh-allen", "burrow", "rodgers",
        "afc-champ", "nfc-champ", "draft", "heisman",
    ]),
    ("sports-nba", [
        "nba", "lakers", "celtics", "warriors", "nuggets", "heat", "bucks",
        "76ers", "knicks", "clippers", "suns", "mavericks", "thunder",
        "rockets", "spurs", "pelicans", "grizzlies", "jazz", "kings",
        "trailblazers", "timberwolves", "nets", "raptors", "wizards",
        "magic", "hawks", "hornets", "pistons", "pacers", "cavaliers",
        "bulls", "lebron", "stephen-curry", "luka-doncic", "jokic",
        "embiid", "giannis", "tatum", "durant", "morant", "wembanyama",
        "mvp", "all-star",
    ]),
    ("sports-soccer", [
        "premier-league", "champions-league", "la-liga", "bundesliga",
        "serie-a", "ligue-1", "world-cup", "messi", "ronaldo", "mbappe",
        "haaland", "neymar", "real-madrid", "barcelona", "manchester",
        "liverpool", "arsenal", "chelsea", "psg", "bayern", "juventus",
        "milan", "uefa", "fifa", "copa-america", "euro-202",
    ]),
    ("sports-mlb", [
        "mlb", "world-series", "yankees", "dodgers", "red-sox", "mets",
        "phillies", "cubs", "cardinals", "braves", "astros", "rangers",
        "diamondbacks", "ohtani", "judge", "all-star-mlb", "mlb-mvp",
        "cy-young",
    ]),
    ("sports-other", [
        "ufc", "mma", " boxing ", " tennis ", "wimbledon", "us-open",
        "australian-open", "french-open", "roland-garros", "djokovic",
        "alcaraz", "sinner", "swiatek", "f1", "formula-1", "verstappen",
        "hamilton", "ferrari", "mclaren", "olympics", "olympic-games",
        "nhl", "stanley-cup", "ncaa", "ncaab", "march-madness", "pga",
        "golf", "masters-tournament", "ryder-cup", "horse-racing",
        "kentucky-derby",
    ]),
    ("entertainment", [
        "oscar", "academy-award", "best-picture", "grammy", "emmy", "tony-award",
        "billboard", "taylor-swift", "kanye", "drake", "beyonce", "rihanna",
        "kardashian", "weeknd", "song-of-the-year", "album-of-the-year",
        "movie", "movies", "box-office", "rotten-tomatoes", "marvel",
        "disney", "netflix", "youtube", "tiktok", "instagram-follower",
        "celeb", "wedding", "engaged", "divorce", "baby", "pregnant",
        "billionaire-list",
    ]),
    ("tech-companies", [
        "apple", "google", "alphabet", "microsoft", "tesla", "spacex",
        "starlink", "amazon", "meta", "facebook", "openai", "chatgpt",
        "gpt-", "claude", "anthropic", "gemini", "llama", "nvidia",
        "intel", "amd", "ipo", "elon-musk", "zuckerberg", "tim-cook",
        "sam-altman", "sundar-pichai", "satya-nadella", "twitter", " x ",
        "x-corp", "xai", "layoff", "acquisition", "merger",
    ]),
    ("science-space", [
        "spacex", "starship", "falcon-9", "nasa", "moon-landing", "mars",
        "satellite", "asteroid", "comet", "iss", "international-space",
        "telescope", "exoplanet", "rocket-launch",
    ]),
    ("ai-tech", [
        " ai ", " agi ", " asi ", "artificial-intelligence", "neural",
        "machine-learning", "language-model", "llm", "robot", "humanoid",
        "self-driving", "autonomous-vehicle", "deepfake",
    ]),
    ("economy", [
        "fed-rate", "fed-cut", "fed-hike", "fomc", "powell", "interest-rate",
        "inflation", "cpi", "ppi", "unemployment", "nonfarm-payroll",
        "jobs-report", "gdp", "recession", "stock-market", "s&p-500",
        "nasdaq", "dow-jones", "vix", "yield-curve", "treasury",
        "ten-year", "oil-price", "wti", "brent", "gold-price",
    ]),
    ("weather-climate", [
        "hurricane", "tropical-storm", "tornado", "snow", "snowfall",
        "temperature", "heatwave", "wildfire", "earthquake", "tsunami",
        "weather", "climate", "el-nino", "la-nina",
    ]),
    ("health", [
        "covid", "vaccine", "pandemic", "cdc", "fda", "ozempic",
        "drug-approval", "biden-health", "trump-health",
    ]),
]


# Patrones de slugs ultra-cortos: ruido natural del mid > edge esperable.
# - Crypto updown 5m/15m/1h/4h: el mid se mueve 1-3% por ticks aleatorios.
#   Con SL 20% disparamos en falso. Slugs: btc-updown-5m-..., eth-updown-15m-...
# - Esports live (CS2/LoL/Valorant/Dota/CSGO): match in-play, mid vola fuerte.
# - Sports daily (NFL/NBA/MLB/NHL/UFC) DENTRO de 6h: assumed live game.
_ULTRASHORT_CRYPTO_RX = re.compile(
    r"^(btc|eth|sol|xrp|bnb|hype|doge)-updown-(5m|15m|1h|4h)-"
)
_ULTRASHORT_ESPORTS_RX = re.compile(r"(?:^|-)(cs2|lol|valorant|dota|csgo)-")
_ULTRASHORT_SPORTS_RX = re.compile(
    r"^(?:ufc-sea\d|nfl|nba|mlb|nhl)-.+-2026-"
)
# Si el match cae dentro de las próximas N segundos se considera "live game".
_ULTRASHORT_SPORTS_LIVE_HORIZON_S = 6 * 3600


def is_ultrashort_market(slug: str | None, end_date_ts: int | None = None) -> bool:
    """True si el slug pertenece a una categoría ultra-corta donde el ruido del
    mid supera el edge medible.

    Reglas:
    - Crypto updown 5m/15m/1h/4h → siempre True
    - Esports CS2/LoL/Valorant/Dota/CSGO → siempre True
    - Sport daily (NFL/NBA/MLB/NHL/UFC sea) AND end_date dentro de 6h → True
      (asume game en curso o por arrancar; los closing futuros lejanos sí
       valen — son markets sobre temporada/torneo).
    - end_date_ts None / desconocido → no se aplica el filtro de sports
      (pero crypto/esports igual disparan por slug).
    """
    if not slug:
        return False
    s = slug.lower()
    if _ULTRASHORT_CRYPTO_RX.search(s):
        return True
    if _ULTRASHORT_ESPORTS_RX.search(s):
        return True
    if _ULTRASHORT_SPORTS_RX.search(s):
        # Sports markets necesitan el horizon check para no bloquear apuestas
        # sobre torneo entero (que pueden cerrar en semanas).
        if end_date_ts is None:
            return False
        import time as _time
        dt = end_date_ts - int(_time.time())
        if 0 < dt <= _ULTRASHORT_SPORTS_LIVE_HORIZON_S:
            return True
    return False


def infer(slug: str | None, question: str | None = None) -> str | None:
    """Devuelve la categoría inferida o None si no hay match."""
    haystack_parts: list[str] = []
    if slug:
        haystack_parts.append(slug.lower())
    if question:
        # Reemplazar espacios y signos por guiones para que keywords como
        # "super-bowl" matcheen también con "Super Bowl" en la question
        q = re.sub(r"[^a-z0-9]+", "-", question.lower())
        haystack_parts.append(q)
    if not haystack_parts:
        return None
    haystack = " ".join(haystack_parts)
    haystack = f" {haystack} "  # padding para keywords " ai "

    for category, keywords in RULES:
        for kw in keywords:
            if kw in haystack:
                return category
    return None


def backfill_markets(only_missing: bool = True) -> dict:
    """Recorre la tabla `markets` y completa `category` con la inferencia."""
    from src.db.schema import db, tx

    where = "WHERE category IS NULL OR category=''" if only_missing else ""
    with db() as conn:
        rows = conn.execute(
            f"SELECT condition_id, slug, question, category FROM markets {where}"
        ).fetchall()

    updates: list[tuple] = []
    counts: dict[str, int] = {}
    for r in rows:
        cat = infer(r["slug"], r["question"])
        if cat:
            updates.append((cat, r["condition_id"]))
            counts[cat] = counts.get(cat, 0) + 1

    if updates:
        with tx() as conn:
            conn.executemany(
                "UPDATE markets SET category=? WHERE condition_id=?",
                updates,
            )
    return {
        "scanned": len(rows),
        "updated": len(updates),
        "by_category": counts,
    }
