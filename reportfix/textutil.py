"""Text helpers: normalisation, fuzzy phrase matching (typo tolerant)."""
import difflib
import functools
import re
from collections import defaultdict

NUMWORDS = {
    "one": 1, "ek": 1, "two": 2, "do": 2, "three": 3, "teen": 3, "tin": 3,
    "four": 4, "char": 4, "chaar": 4, "five": 5, "panch": 5, "paanch": 5,
    "six": 6, "chhe": 6, "seven": 7, "saat": 7, "eight": 8, "aath": 8,
    "nine": 9, "nau": 9, "ten": 10, "das": 10,
}
_NUMW = "|".join(sorted(NUMWORDS, key=len, reverse=True))


def mostly_upper(text):
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 4:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) > 0.7


def pre_norm(text):
    """Cheap fixes applied to raw text before splitting into clauses."""
    t = text
    if not mostly_upper(t):
        # "IS lab 2" (Information Systems) vs the English verb "is"
        t = re.sub(r"\bIS\b(?=\s+(?:lab|labs|classroom|room|block|dept|department|office|building)\b)", "isdept", t)
    t = re.sub(r"\ba\s*/\s*c\b", "ac", t, flags=re.I)
    t = re.sub(r"wi\s*-\s*fi|wi\s+fi", "wifi", t, flags=re.I)
    return t


def norm_text(text):
    t = pre_norm(text).lower()
    t = re.sub(r"computer\s+science(?:\s+and\s+engineering)?", " cse ", t)
    t = re.sub(r"electrical\s+engineering", " ee ", t)
    t = re.sub(r"information\s+systems?|info\s?sys\w*", " isdept ", t)
    t = re.sub(r"[_\-]+", " ", t)
    t = re.sub(r"(lab|labs|room|classroom|hall|block|floor|no|num|number|building|cafeteria|library)(\d)", r"\1 \2", t)
    t = re.sub(r"[^\w\s#]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(
        r"\b(lab|labs|room|classroom|hall|block|no|num|number|nbr)\s+(?:no\s+|num\s+|number\s+)?(%s)\b" % _NUMW,
        lambda m: "%s %d" % (m.group(1), NUMWORDS[m.group(2)]), t)
    return t


@functools.lru_cache(maxsize=50000)
def _ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def tok_match(t, p, thr=0.86):
    """Token t matches phrase word p exactly, or fuzzily when p is long enough."""
    if t == p:
        return True
    if len(p) < 6 or len(t) < 5 or abs(len(t) - len(p)) > 2 or t[0] != p[0]:
        return False
    if t.isdigit() or p.isdigit():
        return False
    return _ratio(t, p) >= thr


class Matcher:
    """Finds phrases (word sequences) in a token list, typo tolerant."""

    def __init__(self, phrases):
        # phrases: iterable of (phrase_string, payload)
        self.exact = defaultdict(list)
        self.fuzzy_first = defaultdict(list)
        for phrase, payload in phrases:
            pt = phrase.split()
            self.exact[pt[0]].append((pt, payload))
            if len(pt[0]) >= 6:
                self.fuzzy_first[pt[0]].append((pt, payload))

    def hits(self, tokens):
        out = []
        for i, t in enumerate(tokens):
            cands = list(self.exact.get(t, ()))
            if len(t) >= 5:
                for w, lst in self.fuzzy_first.items():
                    if w != t and tok_match(t, w):
                        cands.extend(lst)
            for pt, payload in cands:
                n = len(pt)
                if i + n <= len(tokens) and all(tok_match(tokens[i + j], pt[j]) for j in range(n)):
                    out.append((i, i + n, payload))
        return out


def has_word_fuzzy(tokens, word, thr=0.8):
    """Looser fuzzy check used for place names (library, cafeteria...)."""
    for t in tokens:
        if t == word:
            return True
        if len(t) >= 5 and abs(len(t) - len(word)) <= 2 and t[0] == word[0] and _ratio(t, word) >= thr:
            return True
    return False
