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
    "tools": "🛠️",
    "logpages": "📝",
    "joinsprint": "📖",
    "update": "🔄",
    "leavesprint": "🚪",
    "bargraph": "📊",
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


def partial(bot: discord.Client, name: str):
    """The same emoji as a PartialEmoji (what a button's `emoji=` needs), falling back to unicode."""
    found = getattr(bot, "app_emojis", {}).get(name)
    if found:
        return discord.PartialEmoji(name=found.name, id=found.id, animated=getattr(found, "animated", False))
    fallback = _FALLBACKS.get(name)
    return discord.PartialEmoji(name=fallback) if fallback else None
