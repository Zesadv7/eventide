"""Deterministic natural-language memory intake over trusted user originals.

This is a deliberately finite grammar, not free-language understanding. Only the
exact patterns below are recognised; every other message returns ``None`` and is
never remembered:

- the direct language preference "以后这个项目都用中文" and its declared
  Chinese/English variants are explicit project preferences; other future facts
  do not match;
- otherwise intent must open the message: optional marker/scope words, then ``记住``/``remember``;
- explicit long-term save requires a long-term marker word (以后/永久/长期/每次/
  from now on/permanently/always) or the structured ``记住：…：key=content`` form
  handled by ``PreferenceMemory.parse``;
- a bare ``记住 …`` without a marker is only an observation candidate that may
  later become a pending suggestion after exact repetition;
- ``全局``/``global`` wording must appear in the original text for global scope,
  otherwise the project scope is defaulted and surfaced in the receipt;
- ``这次``/``本次``/``this time`` phrasing, negated remembering
  (别记/不要记住/don't remember) and quoted/backticked/bracketed text are never
  auto-memorised (temporary and negated intents return ``None`` silently, a
  remember-phrase containing a temporary marker returns a rejected receipt).

Recognition itself grants no authority: callers may only feed ``message.user``
facts authored by the real user from the trusted receive path; there is no
``trusted`` flag for models or tools to set.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

QUOTES = '`"“”「」<>'
TEMPORARY = re.compile(r"这次|本次|就这回|this time|just this (?:one|time)", re.I)
NEGATION = re.compile(
    r"(?:别|不要|不用|无需|勿|禁止|不想)\s*(?:再|在)?\s*[记注]"
    r"|(?:don't|do not|never)\s+(?:bother\s+)?remember",
    re.I,
)
LONG_TERM = ("以后", "永久", "长期", "每次", "fromnowon", "permanently", "always")
GLOBAL_WORDS = ("全局", "整个安装", "所有项目", "global")
REMEMBER = ("记住", "remember")
TOKEN = re.compile(
    r"以后|永久|长期|每次|from\s+now\s+on|permanently|always|"
    r"全局|整个安装|所有项目|global|记住|remember",
    re.I,
)
LANGUAGE_PREFERENCE = re.compile(
    r"(?:(?:这个|本|所有)项目)?(?:都|请|一律)?(?:用|使用)(?:中文|英文)"
    r"(?:回答|回复|交流)?[。！.!]?"
)
DIRECT_LANGUAGE = re.compile(r"^(?:以后|今后)(?P<content>.+)$")
SEPARATOR = re.compile(r"[\s，,、：:]*")


def natural_key(scope: str, content: str) -> str:
    """Known language preferences share a topic; other text uses a stable digest."""
    if LANGUAGE_PREFERENCE.fullmatch(content):
        return "response_language"
    return "nl_" + hashlib.sha256(f"{scope}\0{content}".encode()).hexdigest()[:12]


def classify(text: str) -> dict[str, Any] | None:
    """Classify one single-line trusted user original; ``None`` means no intent."""
    stripped = text.strip()
    if "\n" in stripped or any(c in stripped for c in QUOTES) or NEGATION.search(stripped):
        return None
    lead = len(text) - len(text.lstrip())
    body = text[lead:].rstrip()
    pos, saw_remember, long_term, global_scope, steps = 0, False, False, False, 0
    while steps < 8:
        steps += 1
        sep = SEPARATOR.match(body, pos)
        if sep is None:
            break
        pos = sep.end()
        token = TOKEN.match(body, pos)
        if token is None:
            break
        word = re.sub(r"\s+", "", token.group(0)).lower()
        if word in GLOBAL_WORDS:
            after = SEPARATOR.match(body, token.end())
            if after is None or after.end() == token.end():
                break  # Global wording fused with following text is not an explicit scope.
        pos = token.end()
        if word in LONG_TERM:
            long_term = True
        elif word in GLOBAL_WORDS:
            global_scope = True
        else:
            saw_remember = True
    if not saw_remember:
        directive = DIRECT_LANGUAGE.fullmatch(body)
        if directive is None or not LANGUAGE_PREFERENCE.fullmatch(directive["content"]):
            return None
        pos = directive.start("content")
        long_term = True
        global_scope = directive["content"].startswith("所有项目")
    if pos >= len(body):
        return None
    if TEMPORARY.search(body):
        return {
            "kind": "rejected",
            "reason": "“这次”之类的临时要求不会自动保存为长期记忆；如需长期生效请明确说明。",
        }
    content = body[pos:]
    pad = len(content) - len(content.lstrip())
    start = lead + pos + pad
    end = lead + len(body) - (len(content) - len(content.rstrip()))
    value = body[pos:].strip()
    if not 1 <= len(value) <= 500:
        return None
    return {
        "kind": "intake",
        "explicit": long_term,
        "scope": "global" if global_scope else "project",
        "scope_word": global_scope,
        "key": None,
        "content": value,
        "span": {"start": start, "end": end},
    }
