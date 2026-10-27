"""Evaluation helpers: Wilson interval for the blind test and Burrows' Delta for a non-LLM style metric."""
from __future__ import annotations

import math
import re

# About 150 very common English function words. Burrows' Delta compares how often two texts use them.
FUNCTION_WORDS = tuple(dict.fromkeys(["the", "of", "and", "to", "a", "in", "that", "is", "was", "it", "for", "as", "with", "he", "she", "they", "i", "you", "we", "his", "her", "their", "my", "our", "your", "this", "these", "those", "on", "at", "by", "from", "be", "have", "has", "had", "do", "does", "did", "will", "would", "can", "could", "should", "may", "might", "must", "not", "no", "but", "or", "if", "then", "than", "so", "because", "while", "when", "where", "which", "who", "whom", "whose", "what", "how", "why", "all", "any", "each", "every", "some", "such", "more", "most", "other", "another", "both", "few", "many", "much", "own", "same", "only", "very", "just", "also", "too", "even", "still", "yet", "again", "once", "there", "here", "now", "about", "above", "after", "before", "between", "into", "through", "during", "over", "under", "up", "down", "out", "off", "against", "among", "around", "until", "since", "without", "within", "upon", "toward", "towards", "across", "along", "behind", "beside", "beyond", "despite", "except", "like", "near", "per", "via", "am", "are", "were", "been", "being", "me", "him", "us", "them", "mine", "ours", "yours", "theirs", "myself", "yourself", "himself", "herself", "itself", "themselves", "ourselves", "one", "two", "first"]))
_WORD = re.compile(r"[a-z']+")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k successes out of n (95% by default)."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _freqs(text: str) -> list[float]:
    words = _WORD.findall(text.lower())
    total = len(words) or 1
    counts = {w: 0 for w in FUNCTION_WORDS}
    for w in words:
        if w in counts:
            counts[w] += 1
    return [counts[w] / total for w in FUNCTION_WORDS]


def burrows_delta(texts_a: list[str], texts_b: list[str], chunk_words: int = 100) -> float:
    """Burrows' Delta between two sets of texts: mean |z_A - z_B| over the function words.

    z-scores use the mean and standard deviation of each word's relative frequency across ~100-word
    chunks of both sets pooled (the reference). Lower means more similar function-word habits."""
    def chunks(texts: list[str]) -> list[str]:
        words = " ".join(texts).split()
        return [" ".join(words[i:i + chunk_words]) for i in range(0, max(len(words), 1), chunk_words)]

    ref = [_freqs(c) for c in chunks(texts_a) + chunks(texts_b)]
    fa, fb = _freqs(" ".join(texts_a)), _freqs(" ".join(texts_b))
    total, used = 0.0, 0
    for i in range(len(FUNCTION_WORDS)):
        col = [r[i] for r in ref]
        mean = sum(col) / len(col)
        std = math.sqrt(sum((v - mean) ** 2 for v in col) / len(col))
        if std < 1e-9:
            continue
        total += abs((fa[i] - mean) / std - (fb[i] - mean) / std)
        used += 1
    return total / used if used else 0.0
