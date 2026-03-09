# Install linters
# pip install pylint ruff black isort

# Run linters
pylint cogs config database services utils bot.py moderation.py
black --check cogs config database services utils bot.py moderation.py
isort --check cogs config database services utils bot.py moderation.py

