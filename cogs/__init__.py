from pathlib import Path

"""Cogs package for wareraNL-bot."""


__all__ = []

# Dynamically load all cog modules
cogs_dir = Path(__file__).parent
for cog_file in cogs_dir.glob("*.py"):
    if cog_file.name != "__init__.py":
        module_name = cog_file.stem
        __all__.append(module_name)