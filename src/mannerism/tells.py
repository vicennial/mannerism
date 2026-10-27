"""Seeded list of common AI-writing tells.

Each tell has an id, a category, a regex, a short `why` and a card `label`. The list is only a
starting point: `lint` fires a tell only when the user's OWN rate of it in the register is low
(see `stats.tell_rates`), so someone who writes em dashes or "robust" is never nagged about them.

Attribution. Entries marked origin="humanizer" adapt the pattern catalogue of blader/humanizer
(SKILL.md), MIT licence, Copyright (c) 2025 Siqi Chen, https://github.com/blader/humanizer.
Entries marked origin="slop-forensics" adapt the over-represented vocabulary found by
sam-paech/slop-forensics (essay-domain slop list), MIT licence, Copyright (c) 2025 Sam Paech,
https://github.com/sam-paech/slop-forensics. Both licences were fetched from GitHub on
2026-10-21 and confirmed as MIT; the licence text is reproduced in NOTICE. Entries marked
origin="original" were written for this project. Wikipedia's "Signs of AI writing" was used only
as a checklist of what to cover; none of its text is copied.

`strong` marks tells that are rare in human writing in any register (chatbot residue, "delve");
those still fire for a user with too little data to calibrate. The others need calibration data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_I = re.IGNORECASE | re.MULTILINE
_Q = "['’]"  # straight or curly apostrophe


@dataclass(frozen=True)
class Tell:
    id: str
    category: str  # vocab | phrase | structure | formatting | chatbot | sycophancy
    pattern: re.Pattern
    label: str  # short name for "Never uses: ..." in cards
    why: str
    strong: bool = False
    origin: str = "original"

    def count(self, text: str) -> int:
        return len(self.pattern.findall(text))


def _t(id: str, category: str, rx: str, label: str, why: str, *, strong: bool = False, origin: str = "original",
       flags: int = _I) -> Tell:
    return Tell(id, category, re.compile(rx.replace("{Q}", _Q), flags), label, why, strong, origin)


H, S, O = "humanizer", "slop-forensics", "original"  # noqa: E741

TELLS: tuple[Tell, ...] = (
    # --- vocabulary: words models use far more often than people -------------------------------------------------
    _t("vocab.delve", "vocab", r"\bdelv(?:e|es|ed|ing)\b", "'delve'", "A classic model word; people rarely write it.",
       strong=True, origin=S),
    _t("vocab.tapestry", "vocab", r"\btapestr(?:y|ies)\b", "'tapestry'", "Stock metaphor for 'mix of things'.",
       strong=True, origin=S),
    _t("vocab.testament", "vocab", r"\b(?:a|the|is a|stands as a|serves as a) testament\b", "'testament'",
       "Inflated way to say 'shows'.", strong=True, origin=H),
    _t("vocab.intricate", "vocab", r"\bintricat(?:e|ely|acies|acy)\b", "'intricate'", "Decorative adjective.", origin=S),
    _t("vocab.multifaceted", "vocab", r"\bmulti-?faceted\b", "'multifaceted'", "Vague praise word.", origin=S),
    _t("vocab.pivotal", "vocab", r"\bpivotal\b", "'pivotal'", "Inflates ordinary things into turning points.",
       origin=H),
    _t("vocab.underscore", "vocab", r"\bunderscor(?:es|ed|ing)\b|\bunderscore the\b", "'underscore' (verb)",
       "Model verb for 'shows'.", origin=H),
    _t("vocab.meticulous", "vocab", r"\bmeticulous(?:ly)?\b", "'meticulous'", "Decorative adjective.", origin=S),
    _t("vocab.bolster", "vocab", r"\bbolster(?:s|ed|ing)?\b", "'bolster'", "Model verb for 'support'.", origin=S),
    _t("vocab.foster", "vocab", r"\bfoster(?:s|ed|ing)\b", "'foster'", "Model verb for 'encourage'.", origin=S),
    _t("vocab.leverage", "vocab", r"\bleverag(?:e|es|ed|ing)\b", "'leverage'", "Corporate verb for 'use'.", origin=S),
    _t("vocab.seamless", "vocab", r"\bseamless(?:ly)?\b", "'seamless'", "Sales word.", origin=S),
    _t("vocab.holistic", "vocab", r"\bholistic(?:ally)?\b", "'holistic'", "Vague abstraction.", origin=S),
    _t("vocab.robust", "vocab", r"\brobust\b", "'robust'", "Used figuratively it is filler.", origin=H),
    _t("vocab.navigate", "vocab", r"\bnavigat(?:e|es|ing) (?:the|this|these|your|today)\b", "'navigate the ...'",
       "Metaphor for 'deal with'.", origin=S),
    _t("vocab.realm", "vocab", r"\b(?:in|within) the realm of\b|\brealm of\b", "'realm'", "Stock phrase.", origin=O),
    _t("vocab.landscape", "vocab", r"\b(?:evolving|ever-changing|digital|competitive|modern|shifting) landscape\b",
       "'... landscape'", "Abstract 'landscape' is a filler noun.", origin=H),
    _t("vocab.ever_evolving", "vocab", r"\bever[- ](?:evolving|changing|growing)\b", "'ever-evolving'",
       "Stock opener.", strong=True, origin=O),
    _t("vocab.vibrant", "vocab", r"\bvibrant\b", "'vibrant'", "Sales word.", origin=S),
    _t("vocab.showcase", "vocab", r"\bshowcas(?:e|es|ed|ing)\b", "'showcase'", "Model verb for 'show'.", origin=H),
    _t("vocab.elevate", "vocab", r"\belevat(?:e|es|ed|ing) (?:your|the|our|this|its)\b", "'elevate your ...'",
       "Sales verb.", origin=O),
    _t("vocab.unlock", "vocab", r"\bunlock(?:s|ed|ing)? (?:the|your|new|its|their)\b", "'unlock the ...'",
       "Sales verb.", origin=O),
    _t("vocab.cutting_edge", "vocab", r"\bcutting[- ]edge\b|\bgame[- ]chang(?:er|ing)\b|\bstate[- ]of[- ]the[- ]art\b",
       "'cutting-edge' / 'game-changer'", "Hype words.", origin=O),
    _t("vocab.streamline", "vocab", r"\bstreamlin(?:e|es|ed|ing)\b", "'streamline'", "Corporate verb.", origin=S),
    _t("vocab.harness", "vocab", r"\bharness(?:es|ed|ing)? (?:the|its|their|your)\b", "'harness the ...'",
       "Hype verb.", origin=O),
    _t("vocab.comprehensive", "vocab", r"\bcomprehensive\b", "'comprehensive'", "Filler adjective.", origin=S),
    _t("vocab.nuanced", "vocab", r"\bnuanced?\b", "'nuanced'", "Vague praise word.", origin=S),
    _t("vocab.resonate", "vocab", r"\bresonat(?:e|es|ed|ing) with\b", "'resonate with'", "Stock phrase.", origin=S),
    _t("vocab.crucial", "vocab", r"\bcrucial\b", "'crucial'", "Inflates importance.", origin=H),
    _t("vocab.deep_dive", "vocab", r"\bdeep dive\b|\bdive (?:in|into)\b", "'dive in'", "Stock phrase.", origin=H),
    _t("vocab.embark", "vocab", r"\bembark(?:s|ed|ing)? (?:on|upon)\b|\bjourney (?:of|to|toward)\b", "'embark on'",
       "Stock phrase.", origin=O),
    _t("vocab.interplay", "vocab", r"\binterplay\b|\bparadigm shift\b", "'interplay'", "Abstract filler.", origin=S),
    # --- phrases and constructions ------------------------------------------------------------------------------
    _t("phrase.worth_noting", "phrase", r"\bit(?:{Q}s| is) worth (?:noting|mentioning|pointing out)\b",
       "'it's worth noting'", "Announces a point instead of making it.", strong=True, origin=O),
    _t("phrase.important_to_note", "phrase",
       r"\bit(?:{Q}s| is) (?:important|crucial|essential|vital) to (?:note|remember|understand|recognize)\b"
       r"|\bimportant to note\b", "'it's important to note'", "Announces a point instead of making it.",
       strong=True, origin=O),
    _t("phrase.todays_fast_paced", "phrase",
       r"\bin today{Q}s (?:fast[- ]paced|digital|modern|rapidly|competitive|world|landscape|age)\b",
       "'in today's fast-paced ...'", "Stock opener.", strong=True, origin=H),
    _t("phrase.not_just_but", "structure",
       r"\bnot (?:just|only|merely|simply) [^.!?\n]{1,60}?,? but (?:also )?", "'not just X but Y'",
       "Stages a contrast nobody claimed to sound bigger.", origin=H),
    _t("phrase.not_x_but_y", "structure", r"\bnot [^.!?\n,;]{1,40}, but [^.!?\n]{1,40}", "'not X, but Y'",
       "Stages a contrast nobody claimed to sound bigger.", origin=H),
    _t("phrase.its_not_its", "structure",
       r"\b(?:it|this|that){Q}?s not (?:about )?[^.!?\n]{1,50}[,;.] (?:it|this|that){Q}?s\b", "'it's not X, it's Y'",
       "Stages a contrast nobody claimed to sound bigger.", origin=H),
    _t("phrase.isnt_dash_its", "structure", r"\b(?:isn|wasn|aren|doesn|don){Q}t [^.!?\n]{1,40}\s*[—–-]{1,2}\s*(?:it|this|that|they){Q}?s?\b",
       "'isn't X - it's Y'", "Same staged contrast, with a dash.", origin=H),
    _t("phrase.lets_dive", "phrase", r"\blet{Q}?s (?:dive|explore|break (?:this|it) down|unpack|get started|delve)\b",
       "'let's dive in'", "Staged run-up before the point.", origin=H),
    _t("phrase.heres_the_thing", "phrase", r"\bhere{Q}?s (?:the|what) (?:thing|kicker|catch|deal|truth)\b",
       "'here's the thing'", "Staged candor before a routine claim.", origin=H),
    _t("phrase.to_be_clear", "phrase", r"\bto be clear\b|\bdon{Q}?t get me wrong\b|\bi{Q}?m not saying\b",
       "'to be clear'", "Argues with a position nobody took.", origin=H),
    _t("phrase.end_of_day", "phrase", r"\bat the end of the day\b|\bwhen all is said and done\b",
       "'at the end of the day'", "Stock closer.", origin=H),
    _t("phrase.in_conclusion", "phrase", r"\bin (?:conclusion|summary)\b|^[ \t]*overall,", "'in conclusion'",
       "Summary signpost; fine in essays, a tell in short messages.", origin=H),
    _t("phrase.real_question", "phrase", r"\bthe real (?:question|issue|challenge|win|magic) is\b|\bwhat really matters\b",
       "'the real question is'", "Sounds deep, adds nothing.", origin=H),
    _t("phrase.key_role", "phrase", r"\b(?:plays?|played) an? (?:crucial|pivotal|key|vital|significant|important) role\b",
       "'plays a key role'", "Inflated significance.", origin=H),
    _t("phrase.serves_as", "phrase", r"\b(?:stands|serves|acts|functions) as an? ", "'serves as a'",
       "Avoids a plain 'is'.", origin=H),
    _t("phrase.future_bright", "phrase", r"\bthe future (?:looks|is) bright\b|\bexciting times ahead\b",
       "'the future looks bright'", "Stock send-off.", origin=H),
    _t("phrase.ing_rider", "structure",
       r",\s+(?:highlighting|underscoring|emphasizing|showcasing|fostering|cultivating|reflecting|ensuring|"
       r"contributing to)\s", "', highlighting ...'", "Shallow trailing clause that restates the point.", origin=H),
    _t("phrase.rule_of_three", "structure", r"\b[a-z][\w-]+, [a-z][\w-]+,? (?:and|or) [a-z][\w-]+\b",
       "tidy lists of three", "Ideas arrive in threes to sound complete.", origin=H),
    _t("phrase.whether_youre", "phrase", r"\bwhether you{Q}?re (?:an? )?[^.!?\n]{1,30} or (?:an? )?[^.!?\n]{1,30}",
       "'whether you're X or Y'", "Brochure construction.", origin=O),
    _t("phrase.a_reminder", "phrase", r"\ba (?:powerful |stark |timely |gentle )?reminder (?:that|of)\b",
       "'a reminder that'", "Moral tacked onto a fact.", origin=H),
    _t("phrase.fragment_row", "structure", r"(?:^|[.!?]\s)(?:[A-Z][a-z]+(?: [a-z]+){0,2}\.\s+){3,}",
       "rows of short fragments", "Dramatic fragments instead of a sentence with a claim.", origin=H, flags=re.MULTILINE),
    # --- chatbot residue and sycophancy ----------------------------------------------------------------------------
    _t("chat.hope_helps", "chatbot", r"\bi hope (?:this|that) (?:helps|finds you well|is helpful)\b",
       "'I hope this helps'", "Assistant sign-off.", strong=True, origin=H),
    # Demoted (R3): common enough in ordinary human writing that flagging it as "strong" (fires even
    # uncalibrated, with too little data) produced noisy false positives; it still fires once calibrated.
    _t("chat.let_me_know", "chatbot", r"\blet me know if you (?:have|need|want) (?:any|anything|more|further)\b",
       "'let me know if you have any questions'", "Assistant sign-off.", strong=False, origin=H),
    _t("chat.feel_free", "chatbot", r"\bfeel free to (?:reach out|ask|let me know|contact)\b", "'feel free to ...'",
       "Assistant sign-off.", origin=H),
    _t("chat.happy_to_help", "chatbot", r"\b(?:i{Q}?m|i am|happy|glad) (?:happy|glad) to (?:help|assist)\b",
       "'happy to help'", "Assistant filler.", origin=H),
    _t("chat.as_an_ai", "chatbot", r"\bas an? (?:ai|a\.i\.|artificial intelligence|language model|llm)\b"
       r"|\bi{Q}?m (?:just )?an ai\b", "'as an AI'", "Model self-reference.", strong=True, origin=H),
    _t("chat.no_opinions", "chatbot", r"\bi don{Q}?t have (?:personal )?(?:opinions|feelings|experiences)\b",
       "'I don't have personal opinions'", "Model self-reference.", strong=True, origin=H),
    _t("chat.knowledge_cutoff", "chatbot",
       r"\bas of my (?:last|latest) (?:update|training)|\bknowledge cutoff\b|\bup to my last training\b",
       "'as of my last update'", "Model self-reference.", strong=True, origin=H),
    _t("chat.would_you_like", "chatbot", r"\b(?:would you like me to|want me to|shall i|should i continue)\b[^.!?\n]{0,60}\?",
       "'would you like me to ...?'", "Assistant follow-up offer.", origin=H),
    _t("chat.heres_a_breakdown", "chatbot",
       r"^[ \t]*here{Q}?s (?:a|an|the|your) (?:quick |detailed |brief |comprehensive )?(?:breakdown|summary|overview|rundown|list)",
       "'here's a breakdown'", "Assistant preamble.", origin=H),
    _t("chat.thanks_patience", "chatbot", r"\bthank you for (?:your )?(?:patience|understanding)\b",
       "'thank you for your patience'", "Support-bot filler.", origin=O),
    _t("syco.great_question", "sycophancy", r"^[ \t]*(?:great|good|excellent|fantastic|wonderful|fascinating) (?:question|point|idea|catch)\b",
       "'great question'", "Praise opener.", strong=True, origin=H),
    # Demoted (R3): "Absolutely!" and "you're totally right" are common in ordinary agreement too.
    _t("syco.certainly", "sycophancy", r"^[ \t]*(?:certainly|absolutely|of course|sure thing|definitely)[!,.]",
       "'Certainly!'", "Eager opener.", strong=False, origin=H),
    _t("syco.absolutely_right", "sycophancy", r"\byou{Q}?re (?:absolutely|completely|totally|entirely) (?:right|correct)\b",
       "'you're absolutely right'", "Sycophantic agreement.", strong=False, origin=H),
    # --- formatting -----------------------------------------------------------------------------------------------
    _t("fmt.em_dash", "formatting", r"—", "em dashes", "Models reach for the em dash as a universal connector.",
       origin=H),
    _t("fmt.dash_spaced", "formatting", r"\s--\s|\s–\s", "spaced dashes",
       "A dash standing in for choosing how clauses relate.", origin=H),
    _t("fmt.emoji_bullets", "formatting", r"^[ \t]*[☀-➿\U0001F300-\U0001FAFF][️‍]*\s+\S",
       "emoji bullets", "Emoji used as list markers.", origin=O),
    _t("fmt.title_case_heading", "formatting",
       r"^(?:#{1,6}\s*|\*\*)?[A-Z][\w'-]*(?:\s+(?:[A-Z][\w'-]*|and|of|the|for|to|in|a|an|with|on|vs\.?)){2,}:?\*{0,2}$",
       "Title Case Headings", "Headings in Title Case.", origin=H, flags=re.MULTILINE),
    _t("fmt.md_header", "formatting", r"^#{1,6}\s+\S", "markdown headings", "Headings in a message.", origin=H),
    _t("fmt.bold", "formatting", r"\*\*[^*\n]{2,60}\*\*", "bold text", "Bold used as decoration.", origin=H),
    _t("fmt.bullets", "formatting", r"(?:^[ \t]*[-*•][ \t]+\S.*\n){2,}[ \t]*[-*•][ \t]+\S", "bullet lists",
       "A bulleted list where prose would do.", origin=O, flags=re.MULTILINE),
    _t("fmt.numbered_bold", "formatting", r"^\s*\d+\.\s+\*\*[^*\n]+\*\*", "bold numbered lead-ins",
       "Numbered list with bold lead-ins.", origin=H, flags=re.MULTILINE),
    _t("fmt.curly_quotes", "formatting", r"[“”]", "curly quotes", "Typographic quotes typed by a model.", origin=H),
)

BY_ID: dict[str, Tell] = {t.id: t for t in TELLS}
assert len(BY_ID) == len(TELLS), "duplicate tell id"


def count_all(text: str) -> dict[str, int]:
    """Occurrences of every tell in `text` (only tells that occur)."""
    out = {}
    for t in TELLS:
        n = t.count(text)
        if n:
            out[t.id] = n
    return out
