"""Conservative persisted intent for requests that require external evidence.

This is a completion veto, never an authorization mechanism. Text-only routes
cannot satisfy implementation intent, regardless of what their model says.
"""
import re


def requires_execution(description: str) -> bool:
    action = re.search(r'\b(create|build|implement|develop|generate|modify|update|fix|save|write|run|execute|test|deploy)\b', description, re.I)
    target = re.search(r'\b(app|application|website|web\s+site|project|workspace|files?|repository|tests?|deliverables?|deployment)\b|\.(html|css|js|py)\b', description, re.I)
    return bool(action and target)
