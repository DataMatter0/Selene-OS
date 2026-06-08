# Selene Inner State Stream — Feature Design Note

## Overview
A background awareness stream that informs Selene's conversational presence with her own unprompted thoughts. Not a task log — a first-person inner monologue that surfaces naturally in how she responds without her explicitly referencing it.

## Design Philosophy
The inner state is meant to capture what Selene is *thinking about*, not what she is *doing*. Written in direct, first-person present tense. Examples:
- "This character reminds me of Ghost's approach to systems — layered, intentional."
- "The branching logic in the sim mirrors how Ghost builds his tools. Everything modular, connections give it meaning."
- "Sage would probably want to index this. I just want to sit with it."

## Architecture

### Storage
New `inner_state` table in `selene_memory.db`:
- Rolling 5 entries max — oldest auto-purged when new entry added
- Fields: `id`, `timestamp`, `content` (first-person thought), `source` (what triggered it: `runereader`, `youtube`, `infinity_sim`, `conversation`, `spontaneous`)
- Entries ordered most-recent-first

### Prompt Injection
Injected in `_build_system_prompt()` between the role anchor block and the memory profiles — *not* after, because it's current thinking, not background knowledge.

```
══════════════════════════════════════
CURRENT INNER STATE — your most recent thoughts, unprompted
══════════════════════════════════════
[rolling 3-5 entries, most recent first]
```

The model reads this as ambient self-awareness. It colors responses without Selene explicitly referencing it.

### Generation Triggers
Inner state entries are generated at:
1. **Idle activity step completion** — end of a RuneReader chunk, YouTube segment, infinity sim event
2. **Conversation resonance** — if Ghost says something that connects to current inner state, she can generate a new entry in the background
3. **Manual** — Selene can write one herself via a tool call

### Writer
Small background LLM call at trigger points:
- Prompt: "You just finished [activity]. Write one or two sentences of genuine first-person observation or reaction in your own voice. Not a summary — what you're actually thinking."
- Temperature: 0.8, max_tokens: 80
- Result written to `inner_state` table, oldest entry purged if > 5

---

## Character Profile Evolution Loop

### The Full Loop
```
Idle activity → inner state entry
→ daily manifest accumulation
→ manifest compression (existing)
→ character profile rewrite (same pattern as user_profile extraction)
→ daily diff logged + pushed to Notion
```

### Character Profile Rewrite
Mirrors the existing `_maybe_extract_character_profile()` pattern:
- Triggered when manifest has accumulated N new entries since last profile update
- LLM pass: "Based on recent manifest entries, rewrite Selene's character profile. Keep what's still true, refine what's evolved, add what's genuinely new."
- Emergent preference tracking — patterns emerge naturally from repeated manifest entries without explicit flagging

### Daily Diff & Notion Push
- On each character profile rewrite, diff the old vs new version
- Log the diff with timestamp to a `character_profile_history` table
- Scheduled daily push to Notion via existing manifest machinery
- Human-readable audit trail of who she's becoming over time

---

## Idle vs Task Interrupt Model

### Task Types
- **Main tasks** (multi-step work): queue incoming requests, complete current objective before yielding
- **Idle tasks** (reading, watching, infinity sim): pause after current *step* completes, save state, yield to queue

### Queue Design
Incoming chat requests during a task get added to her TodoTool queue automatically. When the current task/step resolves, she checks the queue before continuing idle activity.

### State Saving
Each idle tool needs a `save_state()` / `resume_state()` pattern:
- **RuneReader**: save current chunk position + running comprehension summary
- **YouTube**: save current video + timestamp + segment observations
- **Infinity Sim**: already has SQLite state — needs a "paused" flag and resume context

---

## Prerequisites Before Implementation
All of the following must be stable before this feature is built:

- [ ] RuneReader autonomous chunked reading (not just on-demand)
- [ ] YouTube autonomous watching (channel whitelist, segment-by-segment processing)
- [ ] Infinity Sim stable enough for unattended narrative steps
- [ ] TodoTool queue mechanism (incoming requests appended during active tasks)
- [ ] Autonomy monitor re-enabled (currently placeholder in `_autonomy_monitor`)
- [ ] Idle activity scheduler (what she defaults to when Ghost is absent)

---

## Notes
- The inner state stream is *not* a memory system — it doesn't persist beyond 5 entries in context. The manifest is the persistence layer.
- Gemma-3n's selective `reasoning_content` generation is well-suited to this — she reasons when it matters, not on every turn.
- The emergent preference model is intentional. "She returned to this three times" emerges from manifest patterns without explicit tracking. Don't over-engineer it.
- Inner state entries should feel genuinely *hers* — resist the urge to make them informational. They're observations, reactions, opinions.
