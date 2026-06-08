# selene_brain/mood_observer.py
import re
import os
import json
import time
import threading
from typing import Dict, List, Tuple, Optional, Any

MOODLETS = [
    'happy', 'sad', 'angry', 'anxious', 'excited',
    'affectionate', 'annoyed', 'confident', 'playful', 'melancholy'
]

NEUTRAL = 0.5

EXPRESSION_MAP = {
    'happy':       'happy',
    'sad':         'sad',
    'angry':       'angry',
    'anxious':     'scared',
    'excited':     'surprised',
    'affectionate': 'happy',
    'annoyed':     'disinterested',
    'confident':   'confident',
    'playful':     'playful',
    'melancholy':  'tired',
}

PHRASE_PATTERNS: List[Tuple[str, str, float]] = [
    ("i love you", "affectionate", +0.12),
    ("i missed you", "affectionate", +0.10),
    ("i'm proud of you", "happy", +0.10),
    ("proud of you", "happy", +0.09),
    ("you're amazing", "happy", +0.10),
    ("you're the best", "affectionate", +0.09),
    ("thank you so much", "happy", +0.07),
    ("means a lot", "affectionate", +0.08),
    ("i appreciate", "happy", +0.06),
    ("good job", "happy", +0.06),
    ("well done", "happy", +0.06),
    ("nice work", "happy", +0.06),
    ("you make me", "affectionate", +0.05),
    ("just kidding", "playful", +0.08),
    ("haha", "playful", +0.05),
    ("lol", "playful", +0.04),
    ("lmao", "playful", +0.06),
    ("that's hilarious", "playful", +0.08),
    ("you're funny", "playful", +0.07),
    ("wanna play", "playful", +0.07),
    ("let's play", "playful", +0.07),
    ("bet you can't", "playful", +0.06),
    ("i'm sad", "sad", +0.10),
    ("i feel sad", "sad", +0.10),
    ("i'm depressed", "sad", +0.12),
    ("i miss", "melancholy", +0.08),
    ("it hurts", "sad", +0.09),
    ("i'm lonely", "sad", +0.10),
    ("feel alone", "sad", +0.09),
    ("i lost", "sad", +0.08),
    ("passed away", "sad", +0.12),
    ("broke up", "sad", +0.10),
    ("i can't do this", "sad", +0.08),
    ("give up", "melancholy", +0.07),
    ("what's the point", "melancholy", +0.09),
    ("never mind", "melancholy", +0.05),
    ("i hate", "angry", +0.10),
    ("piss me off", "angry", +0.12),
    ("so annoying", "annoyed", +0.09),
    ("that's bullshit", "angry", +0.10),
    ("are you kidding me", "annoyed", +0.08),
    ("what the hell", "angry", +0.08),
    ("shut up", "angry", +0.10),
    ("stop it", "annoyed", +0.07),
    ("you always", "annoyed", +0.06),
    ("you never", "annoyed", +0.07),
    ("sick of", "annoyed", +0.08),
    ("tired of", "annoyed", +0.07),
    ("leave me alone", "angry", +0.09),
    ("go away", "angry", +0.08),
    ("i'm scared", "anxious", +0.10),
    ("i'm worried", "anxious", +0.09),
    ("i'm nervous", "anxious", +0.08),
    ("what if", "anxious", +0.05),
    ("i'm afraid", "anxious", +0.09),
    ("freaking out", "anxious", +0.10),
    ("panic", "anxious", +0.10),
    ("can't breathe", "anxious", +0.12),
    ("something's wrong", "anxious", +0.07),
    ("i don't know what to do", "anxious", +0.08),
    ("oh my god", "excited", +0.08),
    ("no way", "excited", +0.07),
    ("that's awesome", "excited", +0.09),
    ("let's go", "excited", +0.07),
    ("i can't wait", "excited", +0.09),
    ("so cool", "excited", +0.07),
    ("holy shit", "excited", +0.08),
    ("guess what", "excited", +0.06),
    ("you won't believe", "excited", +0.07),
    ("i got this", "confident", +0.08),
    ("watch me", "confident", +0.07),
    ("easy", "confident", +0.05),
    ("no problem", "confident", +0.05),
    ("i know", "confident", +0.04),
    ("obviously", "confident", +0.05),
    ("of course", "confident", +0.04),
    ("trust me", "confident", +0.06),
]

WORD_PATTERNS: Dict[str, List[Tuple[str, float]]] = {
    'love':       [('affectionate', +0.07)],
    'adore':      [('affectionate', +0.08)],
    'cute':       [('affectionate', +0.05), ('playful', +0.03)],
    'beautiful':  [('affectionate', +0.06), ('happy', +0.03)],
    'sweet':      [('affectionate', +0.05)],
    'happy':      [('happy', +0.06)],
    'glad':       [('happy', +0.05)],
    'great':      [('happy', +0.05)],
    'wonderful':  [('happy', +0.06)],
    'awesome':    [('excited', +0.06)],
    'perfect':    [('happy', +0.05), ('confident', +0.03)],
    'yay':        [('excited', +0.06), ('happy', +0.04)],
    'exciting':   [('excited', +0.07)],
    'amazing':    [('excited', +0.07), ('happy', +0.04)],
    'incredible': [('excited', +0.07)],
    'fun':        [('playful', +0.06)],
    'funny':      [('playful', +0.06)],
    'silly':      [('playful', +0.05)],
    'tease':      [('playful', +0.05)],
    'joke':       [('playful', +0.05)],
    'sad':        [('sad', +0.07)],
    'cry':        [('sad', +0.08)],
    'crying':     [('sad', +0.09)],
    'tears':      [('sad', +0.07)],
    'heartbroken':[('sad', +0.10)],
    'depressed':  [('sad', +0.09), ('melancholy', +0.05)],
    'lonely':     [('sad', +0.07), ('melancholy', +0.04)],
    'miss':       [('melancholy', +0.06)],
    'lost':       [('melancholy', +0.05), ('sad', +0.04)],
    'empty':      [('melancholy', +0.06)],
    'numb':       [('melancholy', +0.07)],
    'hopeless':   [('melancholy', +0.08), ('sad', +0.05)],
    'angry':      [('angry', +0.08)],
    'furious':    [('angry', +0.10)],
    'mad':        [('angry', +0.07)],
    'hate':       [('angry', +0.08)],
    'rage':       [('angry', +0.10)],
    'pissed':     [('angry', +0.09)],
    'annoyed':    [('annoyed', +0.07)],
    'annoying':   [('annoyed', +0.07)],
    'irritating': [('annoyed', +0.06)],
    'frustrating':[('annoyed', +0.07), ('angry', +0.03)],
    'stupid':     [('annoyed', +0.05), ('angry', +0.03)],
    'ugh':        [('annoyed', +0.05)],
    'scared':     [('anxious', +0.08)],
    'afraid':     [('anxious', +0.07)],
    'worried':    [('anxious', +0.07)],
    'nervous':    [('anxious', +0.06)],
    'anxious':    [('anxious', +0.07)],
    'terrified':  [('anxious', +0.10)],
    'stress':     [('anxious', +0.06)],
    'stressed':   [('anxious', +0.07)],
    'overwhelmed':[('anxious', +0.07)],
    'confident':  [('confident', +0.06)],
    'strong':     [('confident', +0.05)],
    'proud':      [('confident', +0.06), ('happy', +0.03)],
    'brave':      [('confident', +0.06)],
    'unstoppable':[('confident', +0.08)],
    'sure':       [('confident', +0.04)],
    'sorry':      [('sad', +0.04), ('anxious', +0.03)],
    'please':     [('anxious', +0.02)],
    'help':       [('anxious', +0.03)],
    'goodbye':    [('melancholy', +0.05), ('sad', +0.03)],
    'bye':        [('melancholy', +0.03)],
    'whatever':   [('annoyed', +0.05), ('melancholy', +0.03)],
    'fine':       [('annoyed', +0.03)],
    'okay':       [('annoyed', +0.02)],
    'boring':     [('annoyed', +0.05)],
    'bored':      [('annoyed', +0.05)],
    'tired':      [('melancholy', +0.05)],
    'exhausted':  [('melancholy', +0.07)],
}

NEGATION_WORDS = {'not', "n't", 'no', 'never', 'neither', 'nor', 'hardly', 'barely', 'without'}

def _score_punctuation(text: str) -> Dict[str, float]:
    deltas: Dict[str, float] = {}
    exclaim_count = len(re.findall(r'!{2,}', text))
    if exclaim_count > 0:
        deltas['excited'] = deltas.get('excited', 0) + min(exclaim_count * 0.04, 0.10)
    ellipsis_count = len(re.findall(r'\.{3,}', text))
    if ellipsis_count > 0:
        deltas['melancholy'] = deltas.get('melancholy', 0) + min(ellipsis_count * 0.03, 0.08)
    caps_words = re.findall(r'\b[A-Z]{3,}\b', text)
    if len(caps_words) >= 2:
        deltas['angry']   = deltas.get('angry',   0) + min(len(caps_words) * 0.03, 0.08)
        deltas['excited'] = deltas.get('excited', 0) + min(len(caps_words) * 0.02, 0.06)
    question_count = len(re.findall(r'\?{2,}', text))
    if question_count > 0:
        deltas['anxious'] = deltas.get('anxious', 0) + min(question_count * 0.03, 0.07)
    return deltas

def score_text(text: str) -> Dict[str, float]:
    deltas: Dict[str, float] = {m: 0.0 for m in MOODLETS}
    lower = text.lower()
    words = re.findall(r"[\w']+", lower)
    matched_ranges: List[Tuple[int, int]] = []

    for phrase, moodlet, delta in PHRASE_PATTERNS:
        idx = lower.find(phrase)
        if idx >= 0:
            matched_ranges.append((idx, idx + len(phrase)))
            prefix = lower[max(0, idx - 8):idx].strip()
            prefix_words = prefix.split()
            negated = any(nw in NEGATION_WORDS for nw in prefix_words[-2:]) if prefix_words else False
            effective_delta = -delta * 0.6 if negated else delta
            deltas[moodlet] += effective_delta

    for i, word in enumerate(words):
        word_pos = lower.find(word, sum(len(w) + 1 for w in words[:i]) if i > 0 else 0)
        in_phrase = any(start <= word_pos < end for start, end in matched_ranges)
        if in_phrase:
            continue
        if word in WORD_PATTERNS:
            negated = False
            for j in range(max(0, i - 2), i):
                if words[j] in NEGATION_WORDS or words[j].endswith("n't"):
                    negated = True
                    break
            for moodlet, delta in WORD_PATTERNS[word]:
                effective_delta = -delta * 0.6 if negated else delta
                deltas[moodlet] += effective_delta

    punct_deltas = _score_punctuation(text)
    for moodlet, delta in punct_deltas.items():
        deltas[moodlet] += delta
    return deltas

class MoodObserver:
    def __init__(self, decay_rate: float = 0.03, clamp_normal: float = 0.15,
                 clamp_momentum: float = 0.25, momentum_threshold: int = 2,
                 neutral_threshold: float = 0.15):
        self.moodlets: Dict[str, float] = {m: NEUTRAL for m in MOODLETS}
        self.momentum: Dict[str, int] = {m: 0 for m in MOODLETS}
        self.last_prompt_feeling: Optional[str] = None
        self.last_prompt_deltas: Dict[str, float] = {}
        self.decay_rate = decay_rate
        self.clamp_normal = clamp_normal
        self.clamp_momentum = clamp_momentum
        self.momentum_threshold = momentum_threshold
        self.neutral_threshold = neutral_threshold
        self.last_applied: Dict[str, float] = {m: 0.0 for m in MOODLETS}

    def _apply_decay(self):
        for m in MOODLETS:
            diff = self.moodlets[m] - NEUTRAL
            if abs(diff) < 0.01:
                self.moodlets[m] = NEUTRAL
            else:
                self.moodlets[m] -= diff * self.decay_rate

    def _apply_deltas(self, deltas: Dict[str, float]):
        self.last_applied = {m: 0.0 for m in MOODLETS}
        for m in MOODLETS:
            raw = deltas.get(m, 0.0)
            if abs(raw) < 0.001:
                self.momentum[m] = max(0, self.momentum[m] - 1)
                continue
            if self.momentum[m] >= self.momentum_threshold:
                clamp = self.clamp_momentum
            else:
                clamp = self.clamp_normal
            clamped = max(-clamp, min(clamp, raw))
            before = self.moodlets[m]
            self.moodlets[m] = max(0.0, min(1.0, before + clamped))
            self.last_applied[m] = self.moodlets[m] - before
            if raw > 0:
                self.momentum[m] += 1
            else:
                self.momentum[m] = max(0, self.momentum[m] - 1)

    def observe_prompt(self, user_msg: str) -> Dict[str, float]:
        self._apply_decay()
        deltas = score_text(user_msg)
        self._apply_deltas(deltas)
        self.last_prompt_deltas = deltas
        abs_deltas = {m: abs(d) for m, d in deltas.items() if abs(d) > 0.01}
        self.last_prompt_feeling = max(abs_deltas, key=lambda m: abs_deltas[m]) if abs_deltas else None
        return deltas

    def observe_response(self, assistant_msg: str) -> Dict[str, float]:
        deltas = score_text(assistant_msg)
        scaled = {m: d * 0.4 for m, d in deltas.items()}
        self._apply_deltas(scaled)
        return scaled

    def get_dominant_mood(self) -> Tuple[str, float]:
        best_moodlet = None
        best_distance = 0.0
        for m in MOODLETS:
            dist = abs(self.moodlets[m] - NEUTRAL)
            if dist > best_distance:
                best_distance = dist
                best_moodlet = m
        intensity = best_distance * 2.0
        if intensity < self.neutral_threshold or best_moodlet is None:
            return ("neutral", 0.0)
        return (EXPRESSION_MAP.get(best_moodlet, "neutral"), intensity)

    def get_mood_description(self) -> str:
        dominant_expr, intensity = self.get_dominant_mood()
        feeling = self.last_prompt_feeling
        if dominant_expr == "neutral" and feeling is None:
            return "You're feeling neutral and calm right now."
        parts = []
        if dominant_expr != "neutral":
            dominant_moodlet = next((m for m in MOODLETS if EXPRESSION_MAP.get(m) == dominant_expr), None)
            if dominant_moodlet:
                parts.append(f"You're feeling {'quite' if intensity > 0.6 else 'somewhat' if intensity > 0.3 else 'a little'} {dominant_moodlet}")
        if feeling and abs(self.last_prompt_deltas.get(feeling, 0.0)) > 0.03:
            fd = abs(self.last_prompt_deltas.get(feeling, 0.0))
            parts.append(f"This message made you feel {'strongly' if fd > 0.08 else 'stirred some' if fd > 0.05 else 'a hint of'} {feeling}")
        return ". ".join(parts) + "." if parts else "You're feeling neutral and calm right now."

# ── Model-Agnostic Emotion Classifier Interface ─────────────────────────────
class EmotionClassifier:
    """
    Model-Agnostic Emotion Classifier Interface.
    Enables swapping background classification engines (e.g. LLM calls, tiny local embeddings, or keyword fallback).
    """
    def __init__(self, agent_name: str, llm_caller: Any = None):
        self.agent_name = agent_name
        self.llm_caller = llm_caller
        self.mood_observer = MoodObserver()
        
    def classify_text(self, text: str, is_thought: bool = False) -> Tuple[str, float]:
        """
        Classifies a block of text and returns a dominant (state, intensity 0-1) tuple.
        This wrapper is fully model-agnostic: by default it utilizes our high-performance,
        zero-latency phrase analyzer, but can easily swap to a local ONNX model or LLM async pass.
        """
        # If an LLM is connected and we wish to leverage semantic LLM inference (background thread):
        # We can run a small background classification call. For extreme zero-latency standard runs,
        # we utilize our Adapted Phrase Analyzer:
        deltas = score_text(text)
        abs_deltas = {m: abs(d) for m, d in deltas.items() if abs(d) > 0.01}
        if not abs_deltas:
            return ("neutral", 0.0)
        
        dominant = max(abs_deltas, key=lambda m: abs_deltas[m])
        # Scale to 0-1 intensity based on max keyword impact
        intensity = min(1.0, abs_deltas[dominant] * 8.0)
        return (dominant, intensity)
