"""Conservative, explainable constraints for text chat; no model or tool calls.

Recognize a shared explicit attribute followed by two conjunctive exclusive
literal values. This is a bounded grammar, not a general English/NLP solver.
Only user messages can resolve requirements; assistant suggestions cannot.
"""
from dataclasses import dataclass
import re

WORD = r'[A-Za-z0-9][A-Za-z0-9_-]*'
VALUE = rf'{WORD}(?:[ \t]+{WORD}){{0,2}}'
PAIR = re.compile(
    rf'(?P<prefix>[A-Za-z][A-Za-z0-9_ \t-]{{0,80}}?)\b(?:exclusively|only)[ \t]+'
    rf'(?P<left>{VALUE})[ \t]+and[ \t]+(?:exclusively|only)[ \t]+'
    rf'(?P<right>{VALUE})(?=[.!?,;\n]|$)', re.IGNORECASE)
FILLERS = set('the a an it its is are must be should to as set use write make have with and or please'.split())
NON_REQUIREMENT = re.compile(r"\b(?:not|never|cannot|if|unless|example|phrase|explain|quote|compare)\b|\bdon['’]t\b", re.IGNORECASE)


@dataclass(frozen=True)
class ExclusiveConflict:
    attribute: str
    left: str
    right: str
    evidence: str

    def question(self):
        return f'The same {self.attribute} cannot be exclusively both {self.left} and {self.right}. Which should I use?'

    def explicitly_resolved_by(self, text):
        # Accept a bare choice, or a positive imperative. Negated, hypothetical,
        # disjunctive, quoted and question forms deliberately remain unresolved.
        text = text.strip()
        if any(c in text for c in '?"`') or re.search(r'\b(?:or|if|unless|maybe|perhaps|possibly)\b', text, re.IGNORECASE): return False
        selected, rejected = set(), set()
        for value in (self.left, self.right):
            literal = re.escape(value)
            if re.fullmatch(rf'{literal}(?:\s+(?:only|exclusively))?[.!]?', text, re.IGNORECASE):
                selected.add(value.casefold())
            choice = rf'(?:^|[.!;]\s*)(?:please\s+)?(?:use|choose|pick|select)\s+(?:(?:only|exclusively)\s+)?{literal}(?:\s+(?:only|exclusively))?'
            other = re.escape(self.right if value == self.left else self.left)
            if re.search(choice+rf'(?:\s+(?:instead of|rather than)\s+{other})?(?=$|[.!;,])', text, re.IGNORECASE):
                selected.add(value.casefold())
            if re.search(rf"\b(?:(?:do\s+not|don['’]t|never|avoid)\s+(?:use|choose|pick|select)\s+|(?:not|no)\s+){literal}\b", text, re.IGNORECASE):
                rejected.add(value.casefold())
        return len(selected) == 1 and not selected.intersection(rejected)


def explicit_conflicts(text):
    result = []
    for match in PAIR.finditer(text):
        boundary = max(text.rfind(c, 0, match.start()) for c in '.,;!?\n')
        clause = text[boundary+1:match.end()]
        if any(c in clause for c in '\'"`') or NON_REQUIREMENT.search(clause): continue
        words = match['prefix'].split()
        while words and words[-1].casefold() in FILLERS: words.pop()
        if not words or words[-1].casefold() in FILLERS: continue
        attribute = words[-1]
        left, right = match['left'].strip(), match['right'].strip()
        if left.casefold() == right.casefold(): continue
        if any(w.casefold() in {'and','or','only','exclusively'} for w in (left+' '+right).split()): continue
        # Bound the rendered question to the existing 300-character plan schema.
        if len(attribute)>30 or len(left)>50 or len(right)>50: continue
        result.append(ExclusiveConflict(attribute,left,right,match[0].strip()))
    return result


def unresolved_conflicts(description, context):
    active = {}
    for text in [t['user'] for t in context]+[description]:
        active = {key:c for key,c in active.items() if not c.explicitly_resolved_by(text)}
        for conflict in explicit_conflicts(text):
            key = (conflict.attribute.casefold(), *sorted((conflict.left.casefold(),conflict.right.casefold())))
            active[key] = conflict
    return list(active.values())
