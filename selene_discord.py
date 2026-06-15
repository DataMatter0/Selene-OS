"""
selene_discord.py — Direct Discord bot integration for Selene OS
────────────────────────────────────────────────────────────────
Connects Selene directly to Discord via discord.py.
Loads credentials automatically from project .env or fallback Hermes .env.
Manages isolated conversations per channel/DM so she doesn't clutter UI sessions.
"""

import asyncio
import os
import sys
import logging
import discord
from dotenv import load_dotenv
import random as _random

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("selene_discord")

# Load project environment
load_dotenv()

# Fallback to Hermes environment if keys are missing in project
if not os.environ.get("DISCORD_BOT_TOKEN"):
    hermes_env = os.path.expanduser(r"~\AppData\Local\hermes\.env")
    if os.path.exists(hermes_env):
        logger.info(f"[Discord Bot]: Loading credentials from fallback Hermes .env → {hermes_env}")
        load_dotenv(hermes_env)

# Resolve Configs
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()

# Parse Allowed Users
ALLOWED_USERS = []
raw_users = os.environ.get("DISCORD_ALLOWED_USERS", "")
if raw_users:
    ALLOWED_USERS = [int(u.strip()) for u in raw_users.split(",") if u.strip().isdigit()]

# Parse Allowed Channels
ALLOWED_CHANNELS = []
raw_channels = os.environ.get("DISCORD_HOME_CHANNEL", "")
if raw_channels:
    ALLOWED_CHANNELS = [int(c.strip()) for c in raw_channels.split(",") if c.strip().isdigit()]

# Add default hardcoded owner ID for safety if env parsing was empty
if not ALLOWED_USERS:
    ALLOWED_USERS = [466372090531151882] # Ghost's ID

# Global bot instance
discord_client = None
discord_loop = None

def split_message(text: str, limit: int = 1950) -> list:
    """
    Splits a long response into Discord-friendly chunks that preserve paragraph
    and list coherence.  Rules:
      1. Collapse multiple blank lines into one.
      2. Accumulate lines into a chunk until adding the next line would exceed
         the limit — then flush and start a new chunk.
      3. If a single line exceeds the limit, hard-split it.
    This prevents every bullet point from becoming its own Discord message.
    """
    import re as _re
    # Normalise: collapse 3+ blank lines → 2, strip trailing whitespace per line
    text = _re.sub(r'\n{3,}', '\n\n', text).rstrip()
    lines = text.split("\n")

    chunks: list = []
    current: list = []
    current_len: int = 0

    def flush():
        nonlocal current, current_len
        block = "\n".join(current).strip()
        if block:
            chunks.append(block)
        current = []
        current_len = 0

    for line in lines:
        # Hard-split any single line that exceeds the limit
        while len(line) > limit:
            piece = line[:limit]
            # Try to break at a word boundary
            split_at = piece.rfind(" ")
            if split_at < limit // 2:
                split_at = limit
            if current:
                flush()
            chunks.append(line[:split_at].strip())
            line = line[split_at:].strip()

        # +1 for the newline separator
        needed = len(line) + (1 if current else 0)
        if current_len + needed > limit:
            flush()

        current.append(line)
        current_len += needed

    flush()
    return chunks if chunks else [text[:limit]]

def is_complex_prompt(user_input: str) -> bool:
    """
    Determines whether the prompt is complex (requires tools, planning, or is a larger prompt).
    """
    cleaned = user_input.strip().lower()
    
    # 1. Check for larger prompt: 15 words or more, or 90 characters or more
    words = cleaned.split()
    if len(words) >= 15 or len(cleaned) >= 90:
        return True
        
    # 2. Check for tool/planning keywords
    complex_keywords = {
        "manifest", "task", "search", "todo", "notion", "find", "list", 
        "edit", "add", "create", "delete", "update", "run", "check", 
        "tool", "plan", "game", "play", "status", "write", "read", 
        "file", "code", "debug", "git", "github", "help", "solve", 
        "calculate", "analyze", "compaction", "memory", "profile"
    }
    
    # Check if any word in the input starts with/contains a keyword or if keyword is in cleaned
    for word in words:
        w = "".join(ch for ch in word if ch.isalnum())
        if w in complex_keywords:
            return True
            
    for kw in complex_keywords:
        if kw in cleaned:
            return True
            
    return False

def parse_thoughts(content: str) -> tuple:
    """
    Parses <think>...</think> tags from content.
    Returns a tuple of (thoughts, clean_content).
    """
    import re
    # Extract all think blocks
    think_blocks = re.findall(r'<think>([\s\S]*?)</think>', content, re.DOTALL | re.IGNORECASE)
    thoughts = "\n".join(t.strip() for t in think_blocks if t.strip())
    clean_content = re.sub(r'<think>[\s\S]*?</think>', '', content, flags=re.DOTALL | re.IGNORECASE).strip()
    return thoughts, clean_content

def execute_discord_turn(selene, process_message_fn, update_memory_fn, conv_id, display_name, user_input):
    """
    Executes a single Discord chat turn synchronously inside Selene's re-entrant threading lock.
    Runs entirely on the executor thread to prevent threading/asyncio deadlocks.
    """
    with selene.lock:
        # 1. Capture current active UI session ID to restore later
        prev_active_id = selene.active_conversation_id

        # ── Remote command dispatch ───────────────────────────────────────────
        cleaned_input = user_input.strip().lower()

        # /new — reset the Discord conversation session
        if cleaned_input in ("/new", "!new", "clear", "/clear", "!clear", "reset", "!reset"):
            selene.load_conversation(conv_id)
            selene.delete_conversation(conv_id)
            selene.new_conversation()
            selene.active_conversation_id   = conv_id
            selene.active_conversation_name = f"Discord Chat ({display_name})"
            selene.save_current_conversation()
            if prev_active_id:
                selene.load_conversation(prev_active_id)
            else:
                selene.active_conversation_id   = None
                selene.active_conversation_name = "New Conversation"
                selene.working_memory           = []
            return f"Fresh start — history cleared. What's on your mind, {display_name}?"

        # /status — quick system state snapshot
        if cleaned_input in ("/status", "!status"):
            lines = ["**Selene Status**"]
            lines.append(f"Model: `{getattr(selene, 'model_name', 'unknown')}`")
            lines.append(f"Energy: `{getattr(selene, 'creative_energy', '?')}/100`")
            active = getattr(selene, "active_agent_name", None)
            lines.append(f"Active agent: `{active or 'selene'}`")
            tool_names = list(getattr(selene.tool_router, "tools", {}).keys())
            lines.append(f"Tools loaded: `{len(tool_names)}` — {', '.join(tool_names[:8])}" +
                         (f" + {len(tool_names)-8} more" if len(tool_names) > 8 else ""))
            mem_turns = len(getattr(selene, "working_memory", []))
            lines.append(f"Working memory: `{mem_turns}` turns (this session)")
            if prev_active_id:
                selene.load_conversation(prev_active_id)
            else:
                selene.active_conversation_id   = None
                selene.active_conversation_name = "New Conversation"
                selene.working_memory           = []
            return "\n".join(lines)

        # /todo — dump the todo list via tool
        if cleaned_input in ("/todo", "!todo", "/tasks", "!tasks"):
            try:
                todo_tool = selene.tool_router.tools.get("todo")
                if todo_tool:
                    result = todo_tool.execute({"action": "list"})
                    data   = result.get("data", {})
                    items  = data.get("items", []) if isinstance(data, dict) else []
                    if not items:
                        reply = "No active tasks in the todo list."
                    else:
                        lines = ["**Todo List**"]
                        for it in items:
                            status = "✅" if it.get("done") else "⬜"
                            lines.append(f"{status} {it.get('text', str(it))}")
                        reply = "\n".join(lines)
                else:
                    reply = "Todo tool not loaded."
            except Exception as e:
                reply = f"Error fetching todo list: {e}"
            if prev_active_id:
                selene.load_conversation(prev_active_id)
            else:
                selene.active_conversation_id   = None
                selene.active_conversation_name = "New Conversation"
                selene.working_memory           = []
            return reply

        # /tool <name> [args] — direct tool execution
        # Usage: /tool todo {"action":"add","text":"buy milk"}
        #        /tool manifest {"action":"get"}
        if cleaned_input.startswith("/tool ") or cleaned_input.startswith("!tool "):
            import json as _json
            raw_args = user_input.strip()[6:].strip()  # strip "/tool "
            parts    = raw_args.split(None, 1)
            tool_name = parts[0].lower() if parts else ""
            tool_args_raw = parts[1].strip() if len(parts) > 1 else "{}"
            try:
                tool_args = _json.loads(tool_args_raw)
            except Exception:
                tool_args = {"query": tool_args_raw}
            try:
                tool = selene.tool_router.tools.get(tool_name)
                if tool is None:
                    reply = (f"Unknown tool `{tool_name}`. "
                             f"Available: {', '.join(selene.tool_router.tools.keys())}")
                else:
                    result    = selene.tool_router.route_and_execute(tool_name, tool_args)
                    from server.utils import _format_tool_data
                    data_str  = _format_tool_data(result.get("data", ""))
                    status    = result.get("status", "?")
                    reply     = f"**Tool: {tool_name}** ({status})\n```\n{data_str[:1800]}\n```"
            except Exception as e:
                reply = f"Tool execution error: {e}"
            if prev_active_id:
                selene.load_conversation(prev_active_id)
            else:
                selene.active_conversation_id   = None
                selene.active_conversation_name = "New Conversation"
                selene.working_memory           = []
            return reply

        # 2. Load the Discord conversation (load or create new)
        loaded = selene.load_conversation(conv_id)
        if not loaded:
            selene.new_conversation()
            selene.active_conversation_id = conv_id
            selene.active_conversation_name = f"Discord Chat ({display_name})"
            selene.save_current_conversation()

        # Determine if this is a simple chat or complex prompt
        is_complex = is_complex_prompt(user_input)
        disable_tools = not is_complex

        # 3. Run presence/choice layer — full capacity on Discord
        choice = selene.run_choice_layer(user_input)
        gating       = (choice.get("gating") or "RESPOND").upper()
        response_mode = choice.get("type") or "CONVERSATIONAL"

        if gating == "IGNORE":
            response = "*— (no response) —*"
            # Restore session and return early — don't call the LLM
            if prev_active_id:
                selene.load_conversation(prev_active_id)
            else:
                selene.active_conversation_id   = None
                selene.active_conversation_name = "New Conversation"
                selene.working_memory           = []
            return response

        if gating == "OBSERVE":
            response = "*— (observing) —*"
            # Still commit the user turn to memory so context isn't lost
            update_memory_fn(user_input, "")
            selene.save_current_conversation()
            if prev_active_id:
                selene.load_conversation(prev_active_id)
            else:
                selene.active_conversation_id   = None
                selene.active_conversation_name = "New Conversation"
                selene.working_memory           = []
            return response

        # 4. Call process_message pipeline with presence-chosen response mode
        try:
            response = process_message_fn(user_input, disable_tools=disable_tools, response_mode=response_mode)
        except TypeError:
            try:
                response = process_message_fn(user_input, disable_tools=disable_tools)
            except TypeError:
                response = process_message_fn(user_input)

        if response:
            # 5. Commit turn to Discord session working memory & creative energy
            update_memory_fn(user_input, response, response_mode=response_mode)

            # 6. Save final Discord conversation state
            selene.save_current_conversation()

            # 7. Extract memories from this turn in the background
            selene.maybe_extract_memory(user_input, response, reflective_turn=(response_mode.upper() == "REFLECT"))

        # 7. Restore previous active UI session state
        if prev_active_id:
            selene.load_conversation(prev_active_id)
        else:
            selene.active_conversation_id = None
            selene.active_conversation_name = "New Conversation"
            selene.working_memory = []

        return response

class SeleneDiscordClient(discord.Client):
    def __init__(self, selene_chat, process_message_fn, update_memory_fn,
                 broadcast_fn=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.selene = selene_chat
        self.process_message_fn = process_message_fn
        self.update_memory_fn = update_memory_fn
        self.broadcast_fn = broadcast_fn   # async fn(dict) → broadcasts to all UI clients
        logger.info("[Discord Bot]: Client initialized.")

    async def on_ready(self):
        logger.info("+" + "-"*38 + "+")
        logger.info("|   S E L E N E   D I S C O R D   *    |")
        user_info = f"{self.user.name}#{self.user.discriminator}" if self.user else "Unknown"
        padded_user = f"Connected as: {user_info}".center(36)
        logger.info(f"| {padded_user} |")
        logger.info("+" + "-"*38 + "+")

        # Notify the UI so the Discord indicator lights up immediately
        if self.broadcast_fn:
            is_online = self.is_ready()
            latency   = round(self.latency * 1000, 1) if is_online else None
            guilds_list = [{"id": str(g.id), "name": g.name} for g in self.guilds]
            bot_name  = user_info if is_online else "Offline"
            try:
                await self.broadcast_fn({
                    "type": "discord_status",
                    "data": {
                        "online":           is_online,
                        "bot_name":         bot_name,
                        "latency":          latency,
                        "guilds":           guilds_list,
                        "allowed_channels": ALLOWED_CHANNELS,
                        "allowed_users":    ALLOWED_USERS,
                        "token_exists":     bool(DISCORD_BOT_TOKEN),
                    }
                })
            except Exception as e:
                logger.warning(f"[Discord Bot]: Could not broadcast ready status — {e}")

    async def on_message(self, message):
        # Ignore messages from the bot itself
        if message.author == self.user:
            return

        is_dm = message.channel.type == discord.ChannelType.private
        author_id = message.author.id
        channel_id = message.channel.id
        display_name = message.author.display_name or message.author.name

        # ── Access Security ──────────────────────────────────────────────────
        # Enforce allowed users
        if ALLOWED_USERS and author_id not in ALLOWED_USERS:
            logger.info(f"[Discord Bot]: Ignored message from unauthorized user {message.author} ({author_id})")
            return

        # Enforce allowed channels for server messages (DMs from allowed users pass implicitly)
        if not is_dm and ALLOWED_CHANNELS and channel_id not in ALLOWED_CHANNELS:
            # Let the bot respond if it is directly mentioned in the allowed channel, otherwise skip.
            if self.user and self.user.mentioned_in(message):
                pass
            else:
                return

        # Clean user input (strip bot mention if any)
        user_input = message.content
        if self.user and self.user.mentioned_in(message):
            mention_str = f"<@{self.user.id}>"
            mention_nickname_str = f"<@!{self.user.id}>"
            user_input = user_input.replace(mention_str, "").replace(mention_nickname_str, "").strip()

        if not user_input:
            # Voice messages and file-only sends arrive with empty content + attachments
            if message.attachments:
                attach_types = [a.content_type or "" for a in message.attachments]
                is_audio = any("audio" in t for t in attach_types)
                if is_audio:
                    await message.channel.send(
                        "I caught a voice message — text only for now. Type it out and I'm here."
                    )
                else:
                    await message.channel.send(
                        "I see a file, but I can only process text right now. Describe what you need and I'll help."
                    )
            return

        logger.info(f"[Discord Bot]: Message from {display_name} in {'DM' if is_dm else f'#{message.channel.name}'}: '{user_input}'")

        # ── Session Routing ──────────────────────────────────────────────────
        # Distinct session ID to isolate Discord chats from active Electron sessions
        conv_id = f"discord_{channel_id if not is_dm else author_id}"

        # Trigger typing indicator while Selene is processing
        async with message.channel.typing():
            try:
                loop = asyncio.get_event_loop()

                # Execute the entire turn safely inside the executor thread under the threading lock
                # to prevent threading/asyncio deadlocks.
                response = await loop.run_in_executor(
                    None,
                    execute_discord_turn,
                    self.selene,
                    self.process_message_fn,
                    self.update_memory_fn,
                    conv_id,
                    display_name,
                    user_input
                )

                if not response:
                    response = "I heard you, but I couldn't formulate a reply."

                # ── Message Dispatch ─────────────────────────────────────────
                from server.utils import split_response_chunks
                is_complex = is_complex_prompt(user_input)
                thoughts, clean_reply = parse_thoughts(response)

                # Split using the same sentence-grouped logic as the UI
                sentence_chunks = split_response_chunks(clean_reply)
                # Safety: enforce Discord 1950-char limit on each chunk
                clean_chunks = []
                for sc in sentence_chunks:
                    if len(sc) > 1950:
                        clean_chunks.extend(split_message(sc))
                    else:
                        clean_chunks.append(sc)

                if thoughts and is_complex:
                    thoughts_truncated = thoughts[:1200] + ("\n... [truncated]" if len(thoughts) > 1200 else "")
                    thoughts_msg = f"||🧠 **Thinking:**\n{thoughts_truncated}||"
                    first_clean = clean_chunks[0] if clean_chunks else ""
                    combined = f"{thoughts_msg}\n\n{first_clean}"
                    if len(combined) <= 1950:
                        chunks = [combined] + clean_chunks[1:]
                    else:
                        chunks = [thoughts_msg] + clean_chunks
                else:
                    chunks = clean_chunks

                # Send the first chunk as a reply
                if chunks:
                    await message.reply(chunks[0])

                # Subsequent chunks: same jitter delay as UI (1.2–2.8s base + 8ms/char)
                for chunk in chunks[1:]:
                    async with message.channel.typing():
                        base_delay = _random.uniform(1.2, 2.8)
                        char_delay = len(chunk) * 0.008
                        delay = min(4.5, base_delay + char_delay)
                        await asyncio.sleep(delay)
                        await message.channel.send(chunk)

            except Exception as e:
                logger.error(f"[Discord Bot Error]: Exception in message loop — {e}", exc_info=True)
                err_str = f"{type(e).__name__}: {e}"
                # Save failed turn to working memory so it stays in context
                if self.selene:
                    import time as _time
                    _ts = _time.time()
                    with self.selene.lock:
                        self.selene.working_memory.append({"role": "user", "content": user_input, "ts": _ts})
                        self.selene.working_memory.append({"role": "assistant", "content": f"[ERROR] {err_str}", "ts": _ts})
                await message.reply(f"I ran into an internal error while processing that, Ghost: `{err_str}`")

async def start_discord_bot(selene_chat, process_message_fn, update_memory_fn,
                           broadcast_fn=None):
    """Initializes and runs the Discord client in a separate, dedicated thread to prevent blocking the main server lifecycle."""
    global discord_client, discord_loop

    if not DISCORD_BOT_TOKEN:
        logger.warning("[Discord Bot]: No DISCORD_BOT_TOKEN found in environment. Bot is disabled.")
        return

    logger.info("[Discord Bot]: Booting client in a separate thread...")
    
    # Get reference to the main event loop
    try:
        main_loop = asyncio.get_running_loop()
    except RuntimeError:
        main_loop = None

    import threading

    def thread_entry():
        global discord_client, discord_loop
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        discord_loop = new_loop

        # Thread-safe async broadcast helper
        async def async_thread_safe_broadcast(message):
            if broadcast_fn and main_loop and main_loop.is_running():
                asyncio.run_coroutine_threadsafe(broadcast_fn(message), main_loop)

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        discord_client = SeleneDiscordClient(
            selene_chat=selene_chat,
            process_message_fn=process_message_fn,
            update_memory_fn=update_memory_fn,
            broadcast_fn=async_thread_safe_broadcast,
            intents=intents,
        )
        new_loop.run_until_complete(discord_client.start(DISCORD_BOT_TOKEN))

    discord_thread = threading.Thread(target=thread_entry, daemon=True)
    discord_thread.start()
    logger.info("[Discord Bot]: Thread started.")


async def stop_discord_bot():
    """Gracefully closes the Discord client and its event loop."""
    global discord_client, discord_loop
    if discord_client:
        try:
            if discord_loop and discord_loop.is_running():
                asyncio.run_coroutine_threadsafe(discord_client.close(), discord_loop)
            else:
                await discord_client.close()
        except Exception as e:
            logger.warning(f"[Discord Bot]: Error during shutdown — {e}")
    logger.info("[Discord Bot]: Stopped.")
