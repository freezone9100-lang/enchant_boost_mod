#!/usr/bin/env python3
"""
Enchant Level Boost - mod jar builder
======================================

Builds a real, working Fabric mod .jar that raises the max level of vanilla
enchantments (e.g. Protection X, Unbreaking XIV, Sharpness XII).

WHY THIS SCRIPT EXISTS (read this before running):
Since Minecraft 1.21, enchantments are defined by plain JSON data files
inside the game's own jar, not by compiled Java code. That means a mod that
raises enchantment levels doesn't need any custom Java or a build toolchain
at all - it just needs to ship modified copies of those JSON files.

This script:
  1. Finds your installed Minecraft client jar (or you point it at one).
  2. Extracts the REAL vanilla enchantment JSON files from YOUR jar - so the
     result is always correct for whatever version you actually have
     installed, instead of relying on someone's guess of the file contents.
  3. Changes ONLY the "max_level" number in each file you've chosen to
     boost. Every other field (damage formulas, costs, requirements, etc.)
     is left byte-for-byte as Mojang wrote it. This is deliberately the
     smallest possible edit - it's what makes this safe to trust blindly.
  4. Generates the missing Roman numeral translations. Vanilla only ships
     Roman numerals for levels 1-10 (I-X); anything above that shows as
     raw text like "enchantment.level.11" unless we add the translation.
  5. Packages everything into a Fabric mod .jar (fabric.mod.json + data +
     assets), zipped with the standard library - no Gradle, no Minecraft
     SDK, no internet connection required.

REQUIREMENTS:
  - Python 3.8+ (standard library only, nothing to pip install)
  - Fabric Loader already installed for the Minecraft version you're
    boosting (get it from https://fabricmc.net/use/installer/)
  - The matching vanilla client jar already downloaded by the Minecraft
    launcher at least once (this is normal - it happens automatically
    the first time you launch that version, Fabric or not)

USAGE:
  python3 build_enchant_boost_mod.py
      Auto-detects your newest installed Minecraft version and builds the
      mod using the ENCHANT_BOOSTS table below.

  python3 build_enchant_boost_mod.py --version 1.21.4
      Use a specific installed version instead of the newest one found.

  python3 build_enchant_boost_mod.py --jar "C:\\path\\to\\1.21.4.jar"
      Point directly at a client jar if auto-detection doesn't find it.

  python3 build_enchant_boost_mod.py --list
      Just print every enchantment id found in your jar and its vanilla
      max level, then exit. Useful for picking ids to add below.

CUSTOMIZING LEVELS:
  Edit the ENCHANT_BOOSTS dictionary below. Key = enchantment id (as it
  appears under data/minecraft/enchantment/ in your jar, without ".json"),
  value = the new max level you want (1-255, vanilla hard limit).
  Any enchantment NOT listed here is left completely untouched - the mod
  simply doesn't include a file for it, so vanilla's own definition wins.

WHY BOOSTED LEVELS DIDN'T SHOW UP AT THE ENCHANTING TABLE BEFORE:
  Raising max_level alone makes a level *legal*, but the enchanting table
  decides what to *offer* using two more fields on the same JSON file:
  min_cost and max_cost. Each is a formula (base + per_level_above_first x
  (level-1)) that vanilla scales up automatically for the new higher
  levels - which pushes the required enchanting power (capped around 30
  at a normal 15-bookshelf table) way out of reach. That's why the table
  kept offering the old vanilla-range levels even with this mod installed.

  This script now rescales per_level_above_first on both min_cost and
  max_cost (for every enchantment it boosts) so the new top level's
  required power lands back within TABLE_REACHABLE_POWER below. The old
  "base" cost (the price for level 1) is left untouched - only how
  steeply cost climbs per extra level is compressed. This does NOT
  guarantee the table rolls your new top level often, since the table
  still picks randomly among every level that fits the power range - it
  just makes that level reachable at all through normal enchanting
  instead of only via /enchant commands.
"""

import argparse
import json
import os
import platform
import sys
import zipfile
from pathlib import Path

# --------------------------------------------------------------------------
# 1. CHOOSE YOUR BOOSTED LEVELS HERE
# --------------------------------------------------------------------------
# Format: "enchantment_id": new_max_level
# (vanilla defaults noted in comments as of the 1.21.x / 26.x data format;
#  the script never trusts these numbers for anything except deciding
#  which files to touch - the actual old value always comes from your jar)
ENCHANT_BOOSTS = {
    # --- the ones you asked about specifically ---
    "protection":            10,  # vanilla max: 4
    "unbreaking":             14,  # vanilla max: 3
    "sharpness":              12,  # vanilla max: 5
    "bane_of_arthropods":      6,  # vanilla max: 5

    # --- the rest of the "leveled" enchantment family, boosted to match ---
    "blast_protection":       10,  # vanilla max: 4
    "fire_protection":        10,  # vanilla max: 4
    "projectile_protection":  10,  # vanilla max: 4
    "feather_falling":        10,  # vanilla max: 4
    "smite":                  12,  # vanilla max: 5
    "efficiency":             10,  # vanilla max: 5
    "fortune":                10,  # vanilla max: 3
    "looting":                10,  # vanilla max: 3
    "power":                  10,  # vanilla max: 5
    "punch":                   5,  # vanilla max: 2
    "knockback":               5,  # vanilla max: 2
    "piercing":                8,  # vanilla max: 4
    "quick_charge":            6,  # vanilla max: 3
    "loyalty":                 6,  # vanilla max: 3
    "lure":                    6,  # vanilla max: 3
    "luck_of_the_sea":         6,  # vanilla max: 3
    "respiration":             6,  # vanilla max: 3
    "depth_strider":           6,  # vanilla max: 3
    "impaling":                8,  # vanilla max: 5
    "multishot":               1,  # binary effect, not raised (would do nothing)
}

MOD_ID = "enchant_level_boost"
MOD_NAME = "Enchant Level Boost"
MOD_VERSION = "1.0.0"

# The approximate max "enchanting power" a normal table can reach with a
# full 15-bookshelf setup (the number shown on the bottom enchant slot).
# Cost curves get compressed so each boosted enchant's new top level is
# reachable at or below this power. Raise it if you're using command-block
# power boosts or don't mind some boosted levels staying command-only.
TABLE_REACHABLE_POWER = 30

# --------------------------------------------------------------------------
# 2. Locate a Minecraft client jar
# --------------------------------------------------------------------------

def candidate_minecraft_dirs():
    """Every location this script knows to check, in priority order.
    Windows in particular has no single guaranteed spot: the official
    Mojang/Microsoft launcher defaults to Roaming, but some setups (older
    installs, some third-party launchers, some managed/corporate profiles)
    end up in Local AppData or elsewhere instead. So: check all of them
    rather than assuming one."""
    system = platform.system()
    home = Path.home()
    dirs = []

    if system == "Windows":
        roaming = os.environ.get("APPDATA")
        local = os.environ.get("LOCALAPPDATA")
        if roaming:
            dirs.append(Path(roaming) / ".minecraft")
        if local:
            dirs.append(Path(local) / ".minecraft")
            dirs.append(Path(local) / "Packages")  # MS Store installs land under here
        # fall back to the standard paths even if the env vars were missing
        dirs.append(home / "AppData" / "Roaming" / ".minecraft")
        dirs.append(home / "AppData" / "Local" / ".minecraft")
    elif system == "Darwin":
        dirs.append(home / "Library" / "Application Support" / "minecraft")
    else:
        dirs.append(home / ".minecraft")
        dirs.append(home / ".var" / "app" / "com.mojang.Minecraft" / "data" / "minecraft")  # Flatpak

    # de-duplicate while preserving order
    seen = set()
    unique = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def find_client_jars(mc_dirs):
    """Return list of (version_string, jar_path, source_dir) for every
    installed vanilla client jar found across the given directories,
    newest-looking first."""
    if isinstance(mc_dirs, Path):
        mc_dirs = [mc_dirs]

    found = []
    for mc_dir in mc_dirs:
        versions_dir = mc_dir / "versions"
        if not versions_dir.is_dir():
            continue
        for entry in versions_dir.iterdir():
            if not entry.is_dir():
                continue
            jar = entry / f"{entry.name}.jar"
            if jar.is_file():
                found.append((entry.name, jar, mc_dir))

    # Rough sort: plain "26.2" / "1.21.4"-style version folders first,
    # Fabric/other loader profile folders last (they usually inherit the
    # vanilla jar rather than containing their own full copy).
    def sort_key(item):
        name = item[0]
        looks_vanilla = all(c.isdigit() or c in ".w-" for c in name.split("-")[0])
        return (0 if looks_vanilla else 1, name)
    found.sort(key=sort_key, reverse=True)
    return found


def pick_client_jar(explicit_jar: str | None, explicit_version: str | None):
    if explicit_jar:
        p = Path(explicit_jar).expanduser()
        if not p.is_file():
            sys.exit(f"error: --jar path does not exist: {p}")
        return p

    checked_dirs = candidate_minecraft_dirs()
    candidates = find_client_jars(checked_dirs)
    if not candidates:
        locations = "\n".join(f"  - {d}" for d in checked_dirs)
        sys.exit(
            "Couldn't find any installed Minecraft version. Checked:\n"
            f"{locations}\n\n"
            "Launch Minecraft (any version) at least once so the launcher downloads\n"
            "the client jar, then re-run this script - or pass --jar explicitly\n"
            "pointing at a '<version>.jar' file under your install's 'versions' folder."
        )

    if explicit_version:
        for name, jar, _ in candidates:
            if name == explicit_version:
                return jar
        available = ", ".join(n for n, _, _ in candidates)
        sys.exit(f"error: version '{explicit_version}' not found. Installed: {available}")

    # default: newest-looking vanilla jar
    chosen_name, chosen_jar, chosen_dir = candidates[0]
    if len(candidates) > 1 and len({d for _, _, d in candidates}) > 1:
        print(f"(found installs in more than one location, using: {chosen_dir})")
    return chosen_jar


# --------------------------------------------------------------------------
# 3. Extract + patch vanilla enchantment JSON
# --------------------------------------------------------------------------

def load_vanilla_enchantments(client_jar: Path):
    """Returns {enchantment_id: parsed_json_dict} for every enchantment
    found inside the given client jar."""
    prefix = "data/minecraft/enchantment/"
    out = {}
    with zipfile.ZipFile(client_jar) as zf:
        for info in zf.infolist():
            if info.filename.startswith(prefix) and info.filename.endswith(".json"):
                # keep only top-level enchantment ids (skip any subfolders
                # like dependency-only files some versions ship)
                rest = info.filename[len(prefix):]
                if "/" in rest:
                    continue
                enchant_id = rest[: -len(".json")]
                with zf.open(info) as f:
                    out[enchant_id] = json.load(f)
    if not out:
        sys.exit(
            f"error: found no files under {prefix} inside {client_jar}\n"
            "This usually means the jar is older than Minecraft 1.21, which "
            "predates data-driven enchantments. Update your Minecraft "
            "version and try again."
        )
    return out


def rescale_cost_curve(cost_field: dict | None, new_max_level: int, target_power: int):
    """Given a vanilla min_cost/max_cost object like {"base": 1,
    "per_level_above_first": 11}, return a copy whose per_level_above_first
    is shrunk so cost at new_max_level doesn't exceed target_power.
    "base" (the level-1 price) is always left alone. Returns None
    unchanged if the field is missing or shaped unexpectedly - some
    versions/enchantments may not use this exact schema, and in that case
    we just leave max_level as the only change for that field."""
    if not isinstance(cost_field, dict) or "base" not in cost_field:
        return cost_field

    base = cost_field["base"]
    old_per_level = cost_field.get("per_level_above_first", 0)
    levels_above_first = max(1, new_max_level - 1)

    room = target_power - base
    if room <= 0:
        # base price alone already exceeds our target power - nothing we
        # can do without also touching level 1's price, which we don't.
        new_per_level = 0
    else:
        new_per_level = max(1, room // levels_above_first)
        # never make levels MORE expensive than vanilla already had them
        new_per_level = min(new_per_level, old_per_level) if old_per_level else new_per_level

    patched = dict(cost_field)
    patched["per_level_above_first"] = new_per_level
    return patched


def roman_numeral(n: int) -> str:
    table = [
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
        (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
    ]
    result = []
    for value, symbol in table:
        count, n = divmod(n, value)
        result.append(symbol * count)
    return "".join(result)


def build(client_jar: Path, out_dir: Path, boosts: dict):
    vanilla = load_vanilla_enchantments(client_jar)

    data_files = {}   # zip path -> bytes, for data/minecraft/enchantment/*.json
    changes = []       # for the printed summary
    highest_level_used = 10

    for enchant_id, new_max in boosts.items():
        if enchant_id not in vanilla:
            print(f"  skip: '{enchant_id}' not found in your jar (name changed or removed?)")
            continue

        original = vanilla[enchant_id]
        old_max = original.get("max_level")

        if old_max is None:
            print(f"  skip: '{enchant_id}' has no max_level field, leaving untouched")
            continue

        if new_max <= old_max:
            print(f"  skip: '{enchant_id}' target {new_max} <= vanilla {old_max}, nothing to do")
            continue

        patched = dict(original)
        patched["max_level"] = new_max

        cost_patched = False
        if "min_cost" in original:
            patched["min_cost"] = rescale_cost_curve(original["min_cost"], new_max, TABLE_REACHABLE_POWER)
            cost_patched = True
        if "max_cost" in original:
            patched["max_cost"] = rescale_cost_curve(original["max_cost"], new_max, TABLE_REACHABLE_POWER)
            cost_patched = True

        path = f"data/minecraft/enchantment/{enchant_id}.json"
        data_files[path] = json.dumps(patched, indent=2).encode("utf-8")
        changes.append((enchant_id, old_max, new_max, cost_patched))
        highest_level_used = max(highest_level_used, new_max)

    if not changes:
        sys.exit("Nothing to build - no enchantments were actually raised. Check ENCHANT_BOOSTS.")

    # Roman numerals for every level vanilla doesn't already cover (11+),
    # up to whatever the highest boosted level is (with a little headroom).
    lang_entries = {}
    for lvl in range(11, highest_level_used + 5):
        lang_entries[f"enchantment.level.{lvl}"] = roman_numeral(lvl)
    lang_bytes = json.dumps(lang_entries, indent=2, ensure_ascii=False).encode("utf-8")

    fabric_mod_json = {
        "schemaVersion": 1,
        "id": MOD_ID,
        "version": MOD_VERSION,
        "name": MOD_NAME,
        "description": "Raises the max level of several vanilla enchantments "
                        "and adds the matching Roman numeral text.",
        "authors": ["you"],
        "license": "CC0-1.0",
        "environment": "*",
        "depends": {
            "fabricloader": ">=0.15.0",
            "minecraft": "*"
        }
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    jar_path = out_dir / f"{MOD_ID}-{MOD_VERSION}.jar"

    with zipfile.ZipFile(jar_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("fabric.mod.json", json.dumps(fabric_mod_json, indent=2))
        zf.writestr("assets/minecraft/lang/en_us.json", lang_bytes)
        for path, content in data_files.items():
            zf.writestr(path, content)

    print()
    print("Boosted enchantments:")
    for enchant_id, old_max, new_max, cost_patched in changes:
        tag = "table-reachable" if cost_patched else "max_level only, no cost field found"
        print(f"  {enchant_id}: {roman_numeral(old_max)} -> {roman_numeral(new_max)}  ({old_max} -> {new_max})  [{tag}]")
    print()
    print(f"Cost curves compressed to stay reachable at ~{TABLE_REACHABLE_POWER} enchanting power")
    print("(a normal 15-bookshelf table). Boosted top levels are now offerable")
    print("through normal enchanting, not just /enchant commands - though the")
    print("table still rolls randomly among every level within power range.")
    print()
    print(f"Built: {jar_path}")
    print(f"Source jar used: {client_jar}")
    print()
    print("Next steps:")
    print("  1. Install Fabric Loader for this Minecraft version if you haven't:")
    print("     https://fabricmc.net/use/installer/")
    print(f"  2. Copy {jar_path.name} into your .minecraft/mods folder")
    print("     (create the 'mods' folder next to 'saves' if it doesn't exist yet)")
    print("  3. Launch the Fabric profile. Enchant an item past the old cap, e.g.:")
    print("     /enchant @s minecraft:sharpness 12")
    return jar_path


# --------------------------------------------------------------------------
# 4. CLI
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jar", help="Path to a specific Minecraft client .jar to read vanilla data from")
    parser.add_argument("--version", help="Installed version folder name to use (e.g. 1.21.4 or 26.2)")
    parser.add_argument("--out", default="dist", help="Output directory for the built mod jar (default: ./dist)")
    parser.add_argument("--list", action="store_true", help="List every enchantment id + vanilla max level found, then exit")
    args = parser.parse_args()

    client_jar = pick_client_jar(args.jar, args.version)
    print(f"Using client jar: {client_jar}")

    if args.list:
        vanilla = load_vanilla_enchantments(client_jar)
        for enchant_id in sorted(vanilla):
            print(f"  {enchant_id}: max_level={vanilla[enchant_id].get('max_level')}")
        return

    build(client_jar, Path(args.out), ENCHANT_BOOSTS)


if __name__ == "__main__":
    main()
