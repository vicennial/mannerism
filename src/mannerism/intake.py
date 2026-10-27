"""What happens to the units one read produced before they are stored: exclusion rules, then machine-typed screening.

`Intake` is built once per sync (it loads the rules and the user's "it's me" decisions) and used for every location:

1. a unit that matches an exclusion rule (`exclude.py`) is dropped, and its stored copy, if there is one, is deleted;
2. every other unit is scored (`humanness.py`): below LOW it is dropped and counted as filtered, between LOW and
   REVIEW it is stored but held, and above REVIEW it is stored normally. A unit in a group the user confirmed
   ("it's me") is never dropped or held, whatever it scores: it is stored as normal text;
3. after the write, stored units whose template or session crossed a threshold of the score are scored again
   (`after_write`). That can hold a stored unit; it never drops or deletes one.

Nothing here writes; `after_write` is the only method that touches the store, and only to update scores.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import exclude, groups, humanness


@dataclass
class Intaken:
    units: list = field(default_factory=list)  # to store (scored; `held` set)
    excluded_refs: list[str] = field(default_factory=list)  # source_refs of excluded units: stored copies are deleted
    excluded: int = 0
    filtered: int = 0  # machine-typed below LOW: not stored
    held: int = 0  # stored, but held for review


class Intake:
    def __init__(self, con: sqlite3.Connection | None, cfg):
        self.rules, self.rule_errors = exclude.all_rules_checked(con, cfg)
        self.kept = groups.kept_groups(con) if con is not None else set()
        self.screener = humanness.Screener(con, self.kept)

    def apply(self, units: list) -> Intaken:
        out = Intaken()
        for u in units:  # group rules may name a template cluster, so the template is known before matching
            u.template = humanness.template_of(u.text)[0]
        rest = []
        for u in units:
            facts = exclude.facts_of_unit(u)
            matched = exclude.first_match(self.rules, facts) if self.rules else None
            if matched:
                out.excluded += 1
                # A timeout excludes new text, but it cannot authorize deletion of a stored copy. Confirm the same
                # rule with destructive matching's fail-open timeout policy before returning its source reference.
                if matched.matches(facts, timeout_result=False):
                    out.excluded_refs.append(u.source_ref)
            else:
                rest.append(u)
        self.screener.reset()
        self.screener.apply(rest, self._group_ids)
        for u in rest:
            dropped = humanness.is_dropped(u.human_score)
            kept = self.screener.is_kept(u, self._group_ids)
            if dropped and u.unit_id not in self.screener.stored_ids and not kept:
                out.filtered += 1
                continue
            if dropped and not kept:
                u.held = True
            out.held += int(u.held)
            out.units.append(u)
        return out

    @staticmethod
    def _group_ids(u) -> tuple[str, ...]:
        return groups.group_ids(u.source, u.account, u.project, u.session_kind, u.template)

    def after_write(self, con: sqlite3.Connection, units: list | None = None) -> set[str]:
        """Score stored units again where the batches applied since the last call moved a template or a session over a
        threshold of the score (and only then: a write that moves no threshold changes no score). Returns the registers
        whose bank must be rebuilt (units entered or left `held`)."""
        templates, sessions = self.screener.crossed_templates, self.screener.crossed_sessions
        self.screener.crossed_templates, self.screener.crossed_sessions = set(), set()
        if not templates and not sessions:
            return set()
        return humanness.rescore_after_write(con, templates, sessions, self.kept, groups.group_ids_of_row)
