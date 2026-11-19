"""Screening-question topics recognised in a posting sentence (normalised text:
lowercase, no accents, no punctuation). Shared by the knock-out check and the
match, which leaves these sentences to the knock-out questions."""

from __future__ import annotations

import re

from ..scoring.relevance import normalize_text

TOPIC_PATTERNS = {
    "right_to_work": re.compile(
        r"permesso di soggiorno|right to work|work permit|eligib\w* to work|authori[sz]ed to work"
        r"|autorizzat\w* a lavorare|cittadinanza (?:ue|europea|italiana)|visa sponsorship|\bsponsorship\b"),
    "relocation": re.compile(r"relocat\w*|trasferimento di residenza|disponibil\w* a trasferirsi|trasferirsi a"),
    "location": re.compile(
        r"domicil\w*|residen\w* (?:in|a|nei pressi|nelle vicinanze|in zona)|nelle vicinanze di|in zona"
        r"|within \d+ ?km|commut\w*|presenza in sede|lavoro in sede|on site|living (?:in|near)"),
    "availability": re.compile(
        r"disponibilit\w* immediata|immediate(?:ly)? (?:start|availability|available)|start date|data di inizio"
        r"|inizio immediato|preavviso|notice period|\basap\b|da subito|available immediately"),
    "salary": re.compile(r"\bral\b|retribuzion\w* (?:richiest|attes)|salary expectation|aspettative economiche"
                         r"|pretese economiche|expected salary|desired salary"),
    "shifts": re.compile(r"\bturni\b|\bturnazion\w*|shift work|\bshifts?\b|ciclo continuo|\bweekend\b|festivi"
                         r"|notturn\w*|night shift"),
}


def topic_of(sentence: str) -> str | None:
    """The screening topic of a sentence, or None."""
    norm = normalize_text(sentence)
    for topic, pattern in TOPIC_PATTERNS.items():
        if pattern.search(norm):
            return topic
    return None
