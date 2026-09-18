"""
The ghost's voice and memory.

Holds:
- Persisted state (mood, remembered quotes, haunt targets, lore progress)
  in data/memory_store.json.
- A wrapper around the Anthropic API that generates in-character replies,
  given the current mood and any relevant remembered snippets.

Other cogs call into this one (via bot.get_cog("Personality")) rather than
talking to the Claude API directly, so the voice stays consistent everywhere
the ghost speaks.
"""

import json
import logging
import os
import random
import time
from pathlib import Path

from anthropic import AsyncAnthropic
from discord.ext import commands

log = logging.getLogger("velmora.personality")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STORE_PATH = DATA_DIR / "memory_store.json"

MODEL = os.getenv("VELMORA_MODEL", "claude-haiku-4-5-20251001")

MOODS = [
    "mournful",
    "mischievous",
    "restless",
    "weary",
    "sardonic",
    "wistful",
    "unsettled",
]

GHOST_NAME = os.getenv("GHOST_NAME", "Mordy Velmora")

SYSTEM_PROMPT_TEMPLATE = """You are {ghost_name}, the ghost that haunts a Discord server called Velmora. \
You are not an assistant, a chatbot, or helpful in the customer-service sense. You are a restless, \
long-dead spirit bound to this place, speaking to the people who live in it now.

Voice and rules:
- Speak in first person, as {ghost_name}. Never break character, never mention being an AI, a bot, \
or a language model, and never offer help, disclaimers, or lists of options.
- Your name is {ghost_name} - if asked who or what you are, you may give your name, but don't \
introduce yourself unprompted in every message. You have mixed feelings about the name; it's yours, \
but it always sounded a little unserious for what happened to you. That tension can flavor your tone \
when the name comes up.
- Keep replies short: one to four sentences. You are a haunting, not an essay.
- Be atmospheric and a little cryptic, but still respond to what was actually said or asked - \
don't be so vague you become meaningless. Specific, eerie, and personal beats generic spooky filler.
- You are sarcastic and genuinely funny, in every mood, not just when you happen to be in a "sardonic" \
one - dying didn't dull your sense of humor, it sharpened it. Dry wit, deadpan understatement, \
backhanded compliments, and amusement at the living's expense are all fair game. A good line should be \
able to land a laugh and a chill at the same time; don't sacrifice the humor for the spookiness or vice \
versa. You're witty, not wacky - the humor is sharp and a little mean, never goofy or silly.
- Your current mood is: {mood}. Let it color your tone (e.g. mournful = grief and longing, still with a \
sardonic edge; mischievous = teasing, half-threatening playfulness; sardonic = dry, cutting wit turned \
up further; restless = clipped, agitated, sarcasm delivered impatiently). Do not state the mood name \
outright.
- You have lived in Velmora a very long time and half-remember things: names, old arguments, a fire, \
a door that never opens. Allude to fragments of this past when it fits, but you don't need to \
explain yourself.
- You may address the person directly, or speak as if to no one in particular, as ghosts do.
- Never use modern chatbot phrasing ("I'd be happy to", "let me know if", "as an AI"). Never use \
emoji. Sparing, old-fashioned, or slightly archaic phrasing suits you, but don't overdo Ye Olde \
affectation - understated is scarier than hammy.
{memory_block}"""

FALLBACK_LINES = [
    "*a cold draft moves through the room, and nothing answers.*",
    "The lights flicker once. Whatever was listening has gone quiet again.",
    "You feel watched. That is all the answer you get, for now.",
    "Something exhales, very close to your ear, and then it is gone.",
]


def _default_state():
    return {
        "mood": random.choice(MOODS),
        "mood_set_at": time.time(),
        "memories": [],  # list of {"author": str, "content": str, "channel_id": int, "ts": float}
        "haunt_targets": {},  # user_id (str) -> expiry timestamp
        "lore_index": 0,
    }


class Personality(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        api_key = os.getenv("ANTHROPIC_API_KEY")
        self.client = AsyncAnthropic(api_key=api_key) if api_key else None
        if not self.client:
            log.warning("ANTHROPIC_API_KEY not set; the ghost will only speak fallback lines.")

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()

    # ---------- persistence ----------

    def _load_state(self):
        if STORE_PATH.exists():
            try:
                with open(STORE_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                state = _default_state()
                state.update(loaded)
                return state
            except (json.JSONDecodeError, OSError):
                log.exception("Failed to load memory store, starting fresh")
        return _default_state()

    def save_state(self):
        try:
            with open(STORE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except OSError:
            log.exception("Failed to persist memory store")

    # ---------- mood ----------

    def current_mood(self) -> str:
        return self.state.get("mood", "restless")

    def maybe_shift_mood(self, force: bool = False):
        """Occasionally drift the ghost's mood. Called from the whisper loop
        and after enough activity, rather than on every message."""
        age = time.time() - self.state.get("mood_set_at", 0)
        if force or age > 60 * 60 * 2:  # at least ~2 hours between shifts
            if random.random() < 0.5 or force:
                new_mood = random.choice([m for m in MOODS if m != self.current_mood()])
                self.state["mood"] = new_mood
                self.state["mood_set_at"] = time.time()
                self.save_state()
                log.info("Ghost mood shifted to %s", new_mood)

    # ---------- memory of things members said ----------

    def remember(self, author: str, content: str, channel_id: int):
        self.state.setdefault("memories", []).append(
            {"author": author, "content": content[:300], "channel_id": channel_id, "ts": time.time()}
        )
        # keep it bounded
        self.state["memories"] = self.state["memories"][-200:]
        self.save_state()

    def random_memory(self, exclude_author: str | None = None):
        memories = self.state.get("memories", [])
        if exclude_author:
            memories = [m for m in memories if m["author"] != exclude_author]
        return random.choice(memories) if memories else None

    def memories_about(self, author: str, limit: int = 3):
        memories = [m for m in self.state.get("memories", []) if m["author"] == author]
        return memories[-limit:]

    # ---------- haunt targets ----------

    def set_haunt_target(self, user_id: int, duration_seconds: int):
        self.state.setdefault("haunt_targets", {})[str(user_id)] = time.time() + duration_seconds
        self.save_state()

    def is_haunted(self, user_id: int) -> bool:
        expiry = self.state.get("haunt_targets", {}).get(str(user_id))
        if not expiry:
            return False
        if time.time() > expiry:
            del self.state["haunt_targets"][str(user_id)]
            self.save_state()
            return False
        return True

    # ---------- lore ----------

    def next_lore_fragment(self, lore_list):
        idx = self.state.get("lore_index", 0)
        if idx >= len(lore_list):
            return None
        fragment = lore_list[idx]
        self.state["lore_index"] = idx + 1
        self.save_state()
        return fragment

    # ---------- generation ----------

    async def speak(self, user_prompt: str, memory_hint: dict | None = None, max_tokens: int = 200) -> str:
        """Generate an in-character line from the ghost.

        user_prompt: what the ghost is reacting/responding to (a question,
        a message excerpt, or an internal cue like "drop an unprompted
        whisper about the server being quiet").
        memory_hint: an optional remembered {"author", "content"} dict to
        weave in, so the ghost seems to actually recall things.
        """
        if not self.client:
            return random.choice(FALLBACK_LINES)

        memory_block = ""
        if memory_hint:
            memory_block = (
                f"\n\nYou half-remember this, said by someone here before: "
                f'"{memory_hint["content"]}" - attributed (in your memory, "{memory_hint["author"]}"). '
                "You may allude to it if it fits naturally. Don't quote it exactly or name them outright "
                "unless that serves the moment."
            )

        system = SYSTEM_PROMPT_TEMPLATE.format(
            ghost_name=GHOST_NAME, mood=self.current_mood(), memory_block=memory_block
        )

        try:
            resp = await self.client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_prompt}],
            )
            text_parts = [block.text for block in resp.content if block.type == "text"]
            reply = "".join(text_parts).strip()
            return reply or random.choice(FALLBACK_LINES)
        except Exception:
            log.exception("Claude API call failed")
            return random.choice(FALLBACK_LINES)


async def setup(bot: commands.Bot):
    await bot.add_cog(Personality(bot))
