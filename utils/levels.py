# Goblin Grade curve ("The Refined Ledger"): quick climb through 20, a short
# ramp to 30, then a flat 25,000 GG/level for the rest, landing on exactly
# 2,000,000 cumulative at level 100.

LEVEL_THRESHOLDS = {
    0: 0, 1: 500, 2: 1500, 3: 3000, 4: 5000, 5: 7000, 6: 9500, 7: 12000, 8: 15000,
    9: 18000, 10: 21500, 11: 25000, 12: 29000, 13: 33000, 14: 37500, 15: 42000,
    16: 47000, 17: 52000, 18: 57500, 19: 63500, 20: 70000,
    21: 80000, 22: 92000, 23: 106000, 24: 122000, 25: 140000, 26: 160000,
    27: 181000, 28: 203000, 29: 226000, 30: 250000,
}
MAX_LEVEL = 100
FLAT_RATE = 25_000
FLAT_START_LEVEL = 30

TIER_NAMES = [
    (1, 4, "Wandering Patron"),
    (5, 9, "Registered Patron"),
    (10, 19, "Cozy Resident"),
    (20, 29, "Novice Scribe"),
    (30, 39, "Archive Apprentice"),
    (40, 49, "Shelf Inspector"),
    (50, 59, "Scholar of the Stacks"),
    (60, 69, "Manuscript Curator"),
    (70, 79, "Ink Weaver"),
    (80, 89, "Master Archivist"),
    (90, 99, "Elder of the Stacks"),
    (100, 100, "Legendary Lorekeeper"),
]


def cumulative_for_level(level: int) -> int:
    level = max(0, min(level, MAX_LEVEL))
    if level <= FLAT_START_LEVEL:
        return LEVEL_THRESHOLDS[level]
    return LEVEL_THRESHOLDS[FLAT_START_LEVEL] + (level - FLAT_START_LEVEL) * FLAT_RATE


def get_level(total_gg: int) -> int:
    if total_gg >= cumulative_for_level(MAX_LEVEL):
        return MAX_LEVEL
    level = 0
    while cumulative_for_level(level + 1) <= total_gg:
        level += 1
    return level


def get_progress(total_gg: int):
    """Returns (level, gg_into_level, gg_needed_for_next_level). gg_needed is 0 at max level."""
    level = get_level(total_gg)
    if level >= MAX_LEVEL:
        return level, total_gg - cumulative_for_level(MAX_LEVEL), 0
    floor = cumulative_for_level(level)
    ceiling = cumulative_for_level(level + 1)
    return level, total_gg - floor, ceiling - floor


def get_tier_name(level: int) -> str:
    for lo, hi, name in TIER_NAMES:
        if lo <= level <= hi:
            return name
    return "Wandering Patron"
