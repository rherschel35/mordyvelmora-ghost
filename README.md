# Mordy Velmora

A Discord bot that plays Mordy Velmora, a restless spirit haunting the
server "Velmora." It speaks in character via the Claude API (dynamic, not
canned lines), drops unprompted whispers, reacts to keywords, remembers
things members say and brings them up later, and answers direct questions
through `/seance`. It can also fixate on a member for a while with `/haunt`,
and slowly reveals Velmora's backstory (including its own name) through
lore drops.

The name is configurable via `GHOST_NAME` in `.env` if you ever want to
rename it — it's woven into the system prompt, the bot's Discord presence,
and `/mood`.

## Setup

1. Create a Discord application + bot at https://discord.com/developers/applications
   - Enable the **Message Content Intent** under Bot settings.
   - Invite it to your server with the `bot` and `applications.commands` scopes,
     and at least: View Channels, Send Messages, Read Message History.
2. `cp .env.example .env` and fill in `DISCORD_TOKEN` and `ANTHROPIC_API_KEY`.
3. `pip install -r requirements.txt`
4. `python bot.py`

Slash commands sync automatically on startup (guild-instant if you set
`DEV_GUILD_ID` in `.env`, otherwise global sync which can take up to an hour
the first time).

## Commands

- `/seance question:<text>` — ask the ghost something; it answers in character,
  cryptically.
- `/haunt user:<@member>` — the ghost starts randomly slipping into that
  member's conversations for the next while.
- `/lore` — request the next unrevealed fragment of Velmora's backstory.
- `/mood` — (admin) peek at the ghost's current mood, for debugging.

## Structure

```
bot.py                 # entrypoint, client setup, background whisper loop
cogs/
  personality.py       # Claude API wrapper + ghost voice/mood/memory
  haunting.py           # passive behaviors: whispers, keyword reactions, memory recall
  commands.py           # /seance, /haunt, /lore, /mood
data/
  memory_store.json     # persisted member quotes + mood + haunt targets (runtime-created)
  lore.json              # Velmora backstory fragments, revealed in order
```

## Notes

- All ghost dialogue is generated at request time by Claude (haiku/sonnet,
  configurable in `cogs/personality.py`) using a system prompt that defines
  its voice, current mood, and any remembered snippets — nothing is hardcoded
  canned text, though there are graceful fallback lines if the API call fails.
- State (mood, memories, haunt targets, lore progress) is persisted to a
  small JSON file in `data/` so it survives restarts.
