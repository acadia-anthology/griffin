import discord

# Unicode stand-ins used until the matching application emoji is uploaded/found,
# or if the name in code ever drifts from the name it was uploaded under.
_FALLBACKS = {
    "announcement": "📣",
    "litcandle": "🕯️",
    "snuffedcandle": "🕯️",
    "hourglass": "⏳",
    "griffinlurk": "🧌",
    "blankbook": "📖",
    "bookmark": "🔖",
    "headphones": "🎧",
    "griffinreading": "🧌",
    "bookswap": "🔄",
    "inkandquill": "🖋️",
    "librarystamp": "📮",
    "griffinsleepy": "🧌",
    "books": "📚",
    "checkered_flag": "🏁",
    "first_place": "🥇",
    "second_place": "🥈",
    "third_place": "🥉",
}


async def load_application_emojis(bot: discord.Client) -> dict:
    """Fetches the bot's application emojis (uploaded via the dev portal) keyed by name."""
    try:
        emojis = await bot.fetch_application_emojis()
        return {e.name: e for e in emojis}
    except discord.HTTPException:
        return {}


def emoji(bot: discord.Client, name: str) -> str:
    """Returns the custom application emoji mention by name, or a unicode fallback."""
    cache = getattr(bot, "app_emojis", {})
    found = cache.get(name)
    if found:
        return str(found)
    return _FALLBACKS.get(name, "")
