"""Classify an article into one of the 17 IPTC Media Topics top-level subjects.

IPTC Media Topics vocabulary © IPTC (https://iptc.org/), used under CC BY 4.0.
Names and definitions below are the level-1 nodes of the NewsCodes Media Topic
scheme; the slugs are Pintxøs' own short, stable identifiers for them.
"""

from __future__ import annotations

import logging

from pintxos import llm
from pintxos.config import get_setting

log = logging.getLogger("pintxos")

# (slug, name, definition) for the 17 top-level IPTC Media Topics.
TOPICS: list[tuple[str, str, str]] = [
    (
        "arts",
        "arts, culture, entertainment and media",
        "All forms of arts, entertainment, cultural heritage and media",
    ),
    (
        "conflict",
        "conflict, war and peace",
        "Acts of socially or politically motivated protest or violence, military "
        "activities, geopolitical conflicts, as well as resolution efforts",
    ),
    (
        "crime",
        "crime, law and justice",
        "The establishment and/or statement of the rules of behaviour in society, the "
        "enforcement of these rules, breaches of the rules, the punishment of offenders "
        "and the organisations and bodies involved in these activities",
    ),
    (
        "disaster",
        "disaster, accident and emergency incident",
        "Man made or natural event resulting in loss of life or injury to living "
        "creatures and/or damage to inanimate objects or property",
    ),
    (
        "economy",
        "economy, business and finance",
        "All matters concerning the planning, production and exchange of wealth.",
    ),
    (
        "education",
        "education",
        "All aspects of furthering knowledge, formally or informally",
    ),
    (
        "environment",
        "environment",
        "The protection, damage, and condition of the ecosystem of the planet Earth and "
        "its surroundings",
    ),
    (
        "health",
        "health",
        "All aspects of physical and mental well-being",
    ),
    (
        "human_interest",
        "human interest",
        "Item that discusses individuals, groups, animals, plants or other objects in an "
        "emotional way",
    ),
    (
        "labour",
        "labour",
        "Social aspects, organisations, rules and conditions affecting the employment of "
        "human effort for the generation of wealth or provision of services and the "
        "economic support of the unemployed.",
    ),
    (
        "lifestyle",
        "lifestyle and leisure",
        "Activities undertaken for pleasure, relaxation or recreation outside paid "
        "employment, including eating and travel.",
    ),
    (
        "politics",
        "politics and government",
        "Local, regional, national and international exercise of power, the day-to-day "
        "running of government, and the relationships between governing bodies and states.",
    ),
    (
        "religion",
        "religion",
        "Belief systems, institutions and people who provide moral guidance to followers",
    ),
    (
        "science",
        "science and technology",
        "All aspects pertaining to human understanding of, as well as methodical study and "
        "research of natural, formal and social sciences, such as astronomy, linguistics "
        "or economics",
    ),
    (
        "society",
        "society",
        "The concerns, issues, affairs and institutions relevant to human social "
        "interactions, problems and welfare, such as poverty, human rights and family "
        "planning",
    ),
    (
        "sport",
        "sport",
        "Competitive activity or skill that involves physical and/or mental effort and "
        "organisations and bodies involved in these activities",
    ),
    (
        "weather",
        "weather",
        "The study, prediction and reporting of meteorological phenomena",
    ),
]

TOPIC_SLUGS: frozenset[str] = frozenset(slug for slug, _name, _definition in TOPICS)

# Characters a model likes to wrap or end its one-word answer with.
_STRIP_CHARS = " \t\r\n\"'`“”‘’.,;:!?*_()[]<>"


def build_prompt(title: str, labels: list[str], lead: str) -> tuple[str, str]:
    """Return (system, user) prompts for classifying one article.

    Deterministic: the same arguments always produce byte-identical prompts.
    """
    catalogue = "\n".join(f"{slug}: {name} - {definition}" for slug, name, definition in TOPICS)
    system = (
        "You classify news articles into exactly one IPTC Media Topics top-level "
        "subject. The topics are:\n"
        f"{catalogue}\n"
        "Choose the single topic that best fits the article's primary subject. "
        "Reply with exactly one slug from the list above and nothing else: no "
        "punctuation, no quotes, no explanation."
    )

    lines = [f"Title: {title}"]
    if labels:
        lines.append("Labels: " + ", ".join(labels))
    if lead:
        lines.append(f"Lead: {lead}")
    return system, "\n".join(lines)


def parse_topic(raw: str) -> str | None:
    """Return the slug the model answered with, or None if it is not a known slug."""
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().lower().strip(_STRIP_CHARS)
    return candidate if candidate in TOPIC_SLUGS else None


def classify_topic(
    title: str, labels: list[str], lead: str, model: str | None = None
) -> str | None:
    """Return the IPTC topic slug for an article, or None when classification fails.

    Fails open: an API error or an answer that is not one of the known slugs yields
    None (logged as a warning) rather than an exception, so a failed classification
    never mutes an item or breaks a poll. MissingApiKey propagates, as for summarize().
    """
    system, user_message = build_prompt(title, labels, lead)

    model = model or get_setting("PINTXOS_MODEL")
    try:
        # 200 is a cap, not a spend: the answer is one slug, but a reasoning model
        # needs room to think before it emits that slug or it returns nothing.
        raw = llm.complete(system, user_message, 200, model)
    except llm.MissingApiKey:
        raise
    except llm.LLMError as e:
        log.warning("topic classification failed for %r: %s", title, e)
        return None

    topic = parse_topic(raw)
    if topic is None:
        log.warning("topic classification returned an unknown topic %r for %r", raw, title)
    return topic
