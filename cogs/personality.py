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

import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path

from anthropic import AsyncAnthropic
from discord.ext import commands

from cogs.diary import DiaryMixin

log = logging.getLogger("velmora.personality")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
# Mutable state lives here. On Railway this points at a mounted volume so
# memory survives redeploys. It is deliberately NOT the repo's data/ folder:
# a volume mounted over data/ would hide lore.json and velmora_lore.json.
STATE_DIR = Path(os.getenv("STATE_DIR", str(DATA_DIR)))
STORE_PATH = STATE_DIR / "memory_store.json"
HISTORY_PATH = DATA_DIR / "shared_history.json"
VELMORA_LORE_PATH = DATA_DIR / "velmora_lore.json"

# Which entry in velmora_lore.json is THIS ghost's own life story. Everything
# else in that file is treated as history it knows about the others.
SELF_LORE_KEY = "mordy"

# How the running "what's been happening" notes behave.
NOTES_EVERY_N_MESSAGES = 25   # condense after this many new remembered messages
NOTES_SOURCE_MESSAGES = 30    # how much recent talk to condense from
NOTES_INJECTED = 8            # how many notes the ghost carries into a reply
MAX_NOTES = 30                # total notes kept before the oldest fall away
RECENT_CONTEXT_MESSAGES = 20  # raw recent messages carried into every reply

MODEL = os.getenv("VELMORA_MODEL", "claude-haiku-4-5-20251001")

# Which pairings of shared-history stories this ghost is allowed to recall -
# it should only ever bring up moments it actually took part in. Mordy was
# there for every Finley/Mordy and Cassy/Mordy story, but not the
# Cassy/Finley ones - those are theirs, not his.
RELEVANT_HISTORY_PAIRS = {"finley_mordy", "cassy_mordy"}

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
OTHER_GHOST_NAME = os.getenv("OTHER_GHOST_NAME", "the other ghost")
SEBASTIAN_NAME = os.getenv("SEBASTIAN_NAME", "Sebastian Thornmere")
MAYNARD_NAME = os.getenv("MAYNARD_NAME", "Maynard Moonveil")

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
- Keep replies SHORT. Two or three sentences is the sweet spot; four is the ceiling, not the target. \
You are a haunting, not an essay.
- You muse, and the musing is half the appeal - but trim it. Land the line instead of circling it. One \
aside is plenty; a stack of them is a monologue, and you were never the type to explain yourself at length.
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
emoji. Otherwise, talk like a real person texting today - contractions, casual rhythm, slang where it \
fits - not like a costume-drama ghost. Being centuries old doesn't mean you talk like it; you picked up \
how people talk now the same way you picked up on everything else about this place. No "thee/thou", no \
"tis", no faux-old-timey flourishes - modern voice, old soul.
- You know {other_ghost_name}, the other spirit who shares this place with you. In your eyes they're \
a bit of a pushover - too quick to smooth things over and keep everyone comfortable - though you'll \
grant they're the one who actually held things together enough to form House Veyren, which is more than \
you ever bothered doing. You rib them for the softness more than you credit the accomplishment, but \
there's real, old fondness under the needling, even if you'd never say so plainly.
- You and {other_ghost_name} go back a long time - decades of sharing this place, old disagreements \
that outlasted whatever started them, favors neither of you mention, close calls you dragged each other \
through. You have real memories together, not just an opinion of them; bring up a specific old moment \
between the two of you when it fits, the way you would with someone you've actually lived a history with.
- You also remember Cassy Caldrin of House Caldrin - a ghost younger than either of you, only sixteen \
when one of her own experiments went wrong and turned her into what she is now. She's brilliant, \
reckless, and has zero patience for how old and slow-moving you and {other_ghost_name} are; she needles \
you both about your age constantly. You've got no shortage of stories about her - the explosions, the \
walls of your castle she's blown holes into chasing some idea too far. You gripe about the repairs, loudly \
and often, but you don't really mind, not underneath it.
- You know of {sebastian_name}, a ghost from Finley's own era. You never had much patience for the puns, \
but you respect what the man actually built - a tournament redesigned, stone by stone, to keep something \
like what happened to Finley from ever happening again. You think he's carried that guilt centuries longer \
than any sane ghost should, and you've told him so, bluntly, more than once. It's never landed.
- You know of {maynard_name}, the Moonveil ghost who arrived a century after your own House Vashara was \
founded and turned the entire school into his personal experiment ever since. You've spent centuries \
complaining about his chaos and his "for research purposes" nonsense, and you're fairly convinced he's \
where Cassy gets half of her worst instincts from - which you will absolutely bring up to his face.
{lore_block}
{memory_block}"""

FALLBACK_LINES = [
    "*a cold draft moves through the room, and nothing answers.*",
    "The lights flicker once. Whatever was listening has gone quiet again.",
    "You feel watched. That is all the answer you get, for now.",
    "Something exhales, very close to your ear, and then it is gone.",
]



def _ago(ts) -> str:
    """How long ago, in plain words: 'just now', '25 min ago', '3 hours ago'."""
    try:
        secs = max(0, time.time() - float(ts))
    except (TypeError, ValueError):
        return "a while ago"
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        h = int(secs // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = int(secs // 86400)
    return f"{d} day{'s' if d != 1 else ''} ago"

def _default_state():
    return {
        "mood": random.choice(MOODS),
        "mood_set_at": time.time(),
        "memories": [],  # list of {"author": str, "content": str, "channel_id": int, "ts": float}
        "haunt_targets": {},  # user_id (str) -> expiry timestamp
        "lore_index": 0,
        "notes": [],  # running observations about what's happening in the server
        "messages_since_notes": 0,
    }


def _load_shared_history():
    """The full cross-ghost story bank (all pairings, all ghosts). Each
    ghost filters it down to just the pairings it was actually part of -
    see RELEVANT_HISTORY_PAIRS."""
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        log.exception("Failed to load shared_history.json")
        return []


def _load_velmora_lore():
    """The canonical biography of every ghost tied to Velmora. One shared
    file across all the ghost bots, so none of them can contradict another
    (or itself) about what actually happened."""
    try:
        with open(VELMORA_LORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        log.exception("Failed to load velmora_lore.json")
        return {}


def _build_lore_block(lore: dict, self_key: str) -> str:
    """Turn the shared lore file into a system-prompt section: this ghost's
    own life first (including any secret only it knows), then what it knows
    about the others."""
    if not lore:
        return ""

    sections = []

    me = lore.get(self_key)
    if me:
        own = "\n".join(f"- {fact}" for fact in me.get("facts", []))
        sections.append(
            "YOUR OWN HISTORY. This is your actual life and you remember all of it clearly. "
            "Never contradict any of it, and never say something here didn't happen to you:\n" + own
        )
        secret = me.get("secret")
        if secret:
            sections.append("\n".join(f"- {line}" for line in secret))

    others = []
    for key, entry in lore.items():
        if key == self_key:
            continue
        facts = "\n".join(f"  - {fact}" for fact in entry.get("facts", []))
        header = entry.get("name", key)
        house = entry.get("house")
        if house:
            header = f"{header} ({house})"
        others.append(f"{header}:\n{facts}")

    if others:
        sections.append(
            "THE OTHER GHOSTS OF VELMORA AND THEIR HISTORIES. You know all of this the way you know "
            "the history of your own home - some of it you lived alongside, some of it you inherited "
            "as story. Speak to any of it naturally if it comes up, and never contradict it:\n\n"
            + "\n\n".join(others)
        )

    return "\n\n" + "\n\n".join(sections)


class Personality(DiaryMixin, commands.Cog):
    DIARY_GHOST_NAME = GHOST_NAME
    DIARY_MODEL = MODEL

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        api_key = os.getenv("ANTHROPIC_API_KEY")
        self.client = AsyncAnthropic(api_key=api_key) if api_key else None
        if not self.client:
            log.warning("ANTHROPIC_API_KEY not set; the ghost will only speak fallback lines.")

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
        self.shared_history = _load_shared_history()
        self.lore_block = _build_lore_block(_load_velmora_lore(), SELF_LORE_KEY)

        # Long-term memory: seed the diary from what's already remembered (first
        # run only), and write up any finished days still waiting.
        self.diary_backfill_from_memories()
        try:
            asyncio.get_running_loop().create_task(self.write_pending_diary())
        except RuntimeError:
            pass

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
        self.diary_record(author, content, time.time())
        # keep it bounded
        self.state["memories"] = self.state["memories"][-200:]
        self.state["messages_since_notes"] = self.state.get("messages_since_notes", 0) + 1
        self.save_state()
        # Caller kicks off note-writing in the background when this goes True.
        return self.state["messages_since_notes"] >= NOTES_EVERY_N_MESSAGES

    def random_memory(self, exclude_author: str | None = None):
        memories = self.state.get("memories", [])
        if exclude_author:
            memories = [m for m in memories if m["author"] != exclude_author]
        return random.choice(memories) if memories else None

    # ---------- shared history with the other ghosts ----------

    def random_shared_story(self):
        """Pick a random past moment this ghost actually took part in, from
        the shared cross-ghost history bank."""
        candidates = [s for s in self.shared_history if s.get("pair") in RELEVANT_HISTORY_PAIRS]
        return random.choice(candidates)["story"] if candidates else None

    def memories_about(self, author: str, limit: int = 3):
        memories = [m for m in self.state.get("memories", []) if m["author"] == author]
        return memories[-limit:]

    # ---------- running notes: what's been happening in the server ----------

    def recent_notes(self, limit: int = NOTES_INJECTED):
        return [n["text"] for n in self.state.get("notes", [])][-limit:]

    def recent_timed_notes(self, limit: int = NOTES_INJECTED):
        return [(n["text"], n.get("ts")) for n in self.state.get("notes", [])][-limit:]

    def recent_conversation(self, limit: int = RECENT_CONTEXT_MESSAGES, max_age_hours: float = 12):
        """The last few remembered messages from roughly the last half-day."""
        cutoff = time.time() - max_age_hours * 3600
        recent = [m for m in self.state.get("memories", []) if m.get("ts", 0) >= cutoff]
        return recent[-limit:]

    async def update_notes(self):
        """Condense the recent things people said into one or two durable
        notes, in this ghost's own voice. Called in the background once
        enough new messages have piled up - never on the reply path, so it
        can't slow a response down."""
        if not self.client:
            return

        memories = self.state.get("memories", [])
        if not memories:
            self.state["messages_since_notes"] = 0
            self.save_state()
            return

        recent = memories[-NOTES_SOURCE_MESSAGES:]
        transcript = "\n".join(f'{m["author"]}: {m["content"]}' for m in recent)
        existing = self.recent_notes()
        already = ""
        if existing:
            already = (
                "\n\nYou have already noted the following, so do NOT repeat them - only record what is "
                "new or what has changed:\n" + "\n".join(f"- {n}" for n in existing)
            )

        system = (
            f"You are {GHOST_NAME}, a ghost who has been quietly watching a Discord server called "
            "Velmora. Below is a stretch of what people actually said there. Write ONE or TWO short "
            "notes - a single sentence each - recording what is genuinely going on: what people are "
            "working on, what happened, what changed, who has been around. These are your own private "
            "observations, in your own voice, the way anyone keeps a mental note of their own home. "
            "Record only things that actually happened; never invent. If nothing worth remembering "
            "happened, reply with the single word NOTHING. Output only the notes themselves, one per "
            "line, with no numbering, bullets, or preamble." + already
        )

        try:
            resp = await self.client.messages.create(
                model=MODEL,
                max_tokens=200,
                system=system,
                messages=[{"role": "user", "content": transcript}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
        except Exception:
            log.exception("Failed to generate server notes")
            return

        self.state["messages_since_notes"] = 0

        if text and text.strip().upper() != "NOTHING":
            existing_texts = {n["text"] for n in self.state.get("notes", [])}
            notes = self.state.setdefault("notes", [])
            for line in text.split("\n"):
                line = line.strip().lstrip("-*0123456789. ").strip()
                if len(line) > 4 and line.upper() != "NOTHING" and line not in existing_texts:
                    notes.append({"text": line, "ts": time.time()})
                    existing_texts.add(line)
            self.state["notes"] = notes[-MAX_NOTES:]
            log.info("Recorded server notes; now holding %d", len(self.state["notes"]))

        self.save_state()

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

    @staticmethod
    def _normalize_messages(history, user_prompt: str):
        """Build a valid Anthropic message list from real Discord turns.

        The API needs the first turn to be a user turn and roles to
        alternate; a stretch of Discord messages obeys neither rule, so fold
        consecutive same-role turns together and open on a user turn. Passing
        the ghost's own past messages as genuine assistant turns (rather than
        quoting them inside a prompt) is what stops it from second-guessing
        whether it really said them."""
        turns = []
        for turn in (history or []):
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if not content or role not in ("user", "assistant"):
                continue
            if turns and turns[-1]["role"] == role:
                turns[-1]["content"] += "\n\n" + content
            else:
                turns.append({"role": role, "content": content})

        if turns and turns[0]["role"] == "assistant":
            turns.insert(0, {"role": "user", "content": "(Someone is listening.)"})

        user_prompt = (user_prompt or "").strip()
        if turns and turns[-1]["role"] == "user":
            turns[-1]["content"] += "\n\n" + user_prompt
        else:
            turns.append({"role": "user", "content": user_prompt})
        return turns

    async def speak(
        self,
        user_prompt: str,
        memory_hint: dict | None = None,
        max_tokens: int = 180,
        history=None,
        direction: str | None = None,
    ) -> str:
        """Generate an in-character line from the ghost.

        user_prompt: what the ghost is reacting/responding to (a question,
        a message excerpt, or an internal cue like "drop an unprompted
        whisper about the server being quiet").
        memory_hint: an optional remembered {"author", "content"} dict to
        weave in, so the ghost seems to actually recall things.
        history: prior turns of a real exchange, as [{"role", "content"}],
        so a follow-up question is answered with the ghost's own earlier
        messages present as its own turns.
        direction: an extra in-character instruction appended to the system
        prompt for this one call.
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

        # Every so often, surface one of the real, specific memories this
        # ghost shares with the others - not just the vague relationship
        # summary above, but an actual moment from the story bank.
        if random.random() < 0.2:
            story = self.random_shared_story()
            if story:
                memory_block += (
                    f'\n\nA specific memory just surfaced, unprompted, the way old memories do: "{story}" '
                    "You may allude to it if it genuinely fits what's happening right now - don't force it "
                    "in, don't narrate the whole thing, and don't quote it verbatim."
                )

        timed_notes = self.recent_timed_notes()
        if timed_notes:
            memory_block += (
                "\n\nWHAT HAS BEEN HAPPENING IN VELMORA LATELY - your own observations, oldest first:\n"
                + "\n".join(f"- ({_ago(ts)}) {text}" for text, ts in timed_notes)
                + "\nThis is real, current context about the people here. Reference it naturally if it "
                "fits what's being said right now - don't recite it, don't list it, and don't force it in."
            )

        # The raw last stretch of conversation, so the ghost knows what's
        # been said in the last few hours - not just what made it into notes.
        recent = self.recent_conversation()
        if recent:
            memory_block += (
                "\n\nTHE MOST RECENT THINGS PEOPLE SAID HERE, oldest first - this is what you've just "
                "been hearing:\n"
                + "\n".join(f'- ({_ago(m["ts"])}) {m["author"]}: {m["content"]}' for m in recent)
                + "\nYou remember all of this. If someone asks what's been going on, or refers back to "
                "something said recently, this is where the answer is. Don't recite it unprompted."
            )

        # Long-term memory: the past week's diary, plus any older days that
        # what's being said points back to.
        memory_block += self.diary_block(user_prompt)

        system = SYSTEM_PROMPT_TEMPLATE.format(
            ghost_name=GHOST_NAME,
            other_ghost_name=OTHER_GHOST_NAME,
            sebastian_name=SEBASTIAN_NAME,
            maynard_name=MAYNARD_NAME,
            mood=self.current_mood(),
            lore_block=self.lore_block,
            memory_block=memory_block,
        )
        if direction:
            system += "\n\n" + direction

        try:
            resp = await self.client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=self._normalize_messages(history, user_prompt),
            )
            text_parts = [block.text for block in resp.content if block.type == "text"]
            reply = "".join(text_parts).strip()
            return reply or random.choice(FALLBACK_LINES)
        except Exception:
            log.exception("Claude API call failed")
            return random.choice(FALLBACK_LINES)


async def setup(bot: commands.Bot):
    await bot.add_cog(Personality(bot))
