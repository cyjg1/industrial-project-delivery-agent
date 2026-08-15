from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import replace
from typing import Any

from agent.schemas import PeopleStructure, Person, PersonAssignment


def normalize_people_structure(structure: PeopleStructure, project_id: str) -> PeopleStructure:
    """Assign stable identities without guessing that equal display names are equal people."""
    name_counts = Counter(person.name.strip() for person in structure.people if person.name.strip())
    grouped: dict[str, list[Person]] = {}
    order: list[str] = []
    fingerprint_occurrences: Counter[tuple[str, str, str, str]] = Counter()

    for person in structure.people:
        name = person.name.strip()
        if not name:
            continue
        if person.person_id:
            person_id = person.person_id
        elif name_counts[name] == 1:
            person_id = _stable_id("person", project_id, name)
        else:
            fingerprint = (name, person.path, person.group, person.role)
            occurrence = fingerprint_occurrences[fingerprint]
            fingerprint_occurrences[fingerprint] += 1
            person_id = _stable_id(
                "person",
                project_id,
                name,
                person.path,
                person.group,
                person.role,
                str(occurrence),
            )
        if person_id not in grouped:
            grouped[person_id] = []
            order.append(person_id)
        grouped[person_id].append(person)

    normalized_people: list[Person] = []
    for person_id in order:
        records = grouped[person_id]
        first = records[0]
        assignments: list[PersonAssignment] = []
        seen_assignment_ids: set[str] = set()
        for record in records:
            source_assignments = record.assignments or [
                PersonAssignment(
                    assignment_id="",
                    group=record.group,
                    role=record.role,
                    path=record.path,
                    responsibility_note=record.responsibility_note,
                )
            ]
            for assignment in source_assignments:
                assignment_id = assignment.assignment_id or _stable_id(
                    "assignment",
                    project_id,
                    person_id,
                    assignment.group,
                    assignment.role,
                    assignment.path,
                )
                if assignment_id in seen_assignment_ids:
                    continue
                seen_assignment_ids.add(assignment_id)
                assignments.append(replace(assignment, assignment_id=assignment_id))

        ambiguous_name = name_counts[first.name.strip()] > 1 and not all(record.person_id for record in records)
        identity_status = "needs_merge" if ambiguous_name else (
            first.identity_status if first.identity_status in {"confirmed", "needs_merge"} else "confirmed"
        )
        normalized_people.append(
            Person(
                name=first.name,
                group=first.group,
                role=first.role,
                path=first.path,
                responsibility_note=first.responsibility_note,
                person_id=person_id,
                identity_status=identity_status,
                assignments=assignments,
                source_id=first.source_id,
            )
        )

    return PeopleStructure(
        root_title=structure.root_title,
        groups=list(structure.groups),
        people=normalized_people,
        scenarios=list(structure.scenarios),
    )


def sync_people_directory(
    store: Any,
    structure: PeopleStructure,
    *,
    org_id: str,
    project_id: str,
    source_id: str,
    author_id: str,
    sensitivity: str,
) -> None:
    entities: list[dict[str, Any]] = []
    assignments: list[dict[str, Any]] = []
    for person in structure.people:
        entities.append(
            {
                "person_id": person.person_id,
                "name": person.name,
                "identity_status": person.identity_status,
                "source_id": source_id,
                "author_id": author_id,
                "sensitivity": sensitivity,
            }
        )
        for assignment in person.assignments:
            assignments.append(
                {
                    "assignment_id": assignment.assignment_id,
                    "person_id": person.person_id,
                    "group": assignment.group,
                    "role": assignment.role,
                    "path": assignment.path,
                    "responsibility_note": assignment.responsibility_note,
                    "source_id": assignment.source_id or source_id,
                    "author_id": author_id,
                    "sensitivity": sensitivity,
                }
            )
    store.replace_people_directory(
        org_id=org_id,
        project_id=project_id,
        entities=entities,
        assignments=assignments,
    )


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(str(part).strip() for part in parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
