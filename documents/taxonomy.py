"""Suggested subject tree, extended by labels on visible uploaded documents."""
from .constants import SUBJECT_TOPICS


def subject_groups(pairs):
    groups = {subject: list(topics) for subject, topics in SUBJECT_TOPICS.items()}
    subjects = {s.casefold(): s for s in groups}
    for subject, topic in pairs:
        subject, topic = subject.strip(), topic.strip()
        if not subject or subject.casefold() == "all":
            continue
        key = subjects.setdefault(subject.casefold(), subject)
        topics = groups.setdefault(key, [])
        if topic and topic.casefold() not in {t.casefold() for t in topics}:
            topics.append(topic)
    return [{"subject": subject, "topics": topics} for subject, topics in groups.items()]


def normalise_subject_topic(subject, topic):
    subject, topic = subject.strip(), topic.strip()
    canonical = next((s for s in SUBJECT_TOPICS if s.casefold() == subject.casefold()), subject)
    options = SUBJECT_TOPICS.get(canonical, [])
    topic = next((t for t in options if t.casefold() == topic.casefold()), topic)
    return canonical, topic
