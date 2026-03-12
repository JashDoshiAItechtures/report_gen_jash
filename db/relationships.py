"""Relationship discovery between database tables.

Detects relationships via:
1. Explicit foreign-key constraints
2. Matching column names across tables
3. ID-like suffix patterns (*_id, *_key)
4. Fuzzy name matching (cust_id ≈ customer_id)
"""

from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import text

from db.connection import get_engine
from db.schema import get_schema


@dataclass
class Relationship:
    table_a: str
    column_a: str
    table_b: str
    column_b: str
    confidence: float        # 0.0 – 1.0
    source: str              # "fk", "exact_match", "id_pattern", "fuzzy"


def discover_relationships() -> list[Relationship]:
    """Return all discovered relationships across public tables."""
    rels: list[Relationship] = []
    rels.extend(_fk_relationships())
    rels.extend(_implicit_relationships())
    return _deduplicate(rels)


# ── Explicit FK relationships ───────────────────────────────────────────────

def _fk_relationships() -> list[Relationship]:
    query = text("""
        SELECT
            tc.table_name       AS source_table,
            kcu.column_name     AS source_column,
            ccu.table_name      AS target_table,
            ccu.column_name     AS target_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema  = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
            ON ccu.constraint_name = tc.constraint_name
            AND ccu.table_schema   = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema    = 'public'
    """)

    rels: list[Relationship] = []
    with get_engine().connect() as conn:
        for row in conn.execute(query).fetchall():
            rels.append(Relationship(
                table_a=row[0], column_a=row[1],
                table_b=row[2], column_b=row[3],
                confidence=1.0, source="fk",
            ))
    return rels


# ── Implicit relationships ──────────────────────────────────────────────────

def _implicit_relationships() -> list[Relationship]:
    schema = get_schema()
    tables = list(schema.keys())
    rels: list[Relationship] = []

    for i, t1 in enumerate(tables):
        cols1 = {c["column_name"] for c in schema[t1]}
        for t2 in tables[i + 1:]:
            cols2 = {c["column_name"] for c in schema[t2]}

            # 1. Exact column-name matches
            common = cols1 & cols2
            for col in common:
                rels.append(Relationship(
                    table_a=t1, column_a=col,
                    table_b=t2, column_b=col,
                    confidence=0.85, source="exact_match",
                ))

            # 2. ID-pattern matching  (e.g. "id" in t1 ↔ "t1_id" in t2)
            for c1 in cols1:
                if not c1.endswith(("_id", "_key", "id")):
                    continue
                for c2 in cols2:
                    if not c2.endswith(("_id", "_key", "id")):
                        continue
                    if c1 == c2:
                        continue  # already caught above
                    base1 = c1.rsplit("_", 1)[0] if "_" in c1 else c1
                    base2 = c2.rsplit("_", 1)[0] if "_" in c2 else c2
                    if base1 == base2:
                        rels.append(Relationship(
                            table_a=t1, column_a=c1,
                            table_b=t2, column_b=c2,
                            confidence=0.75, source="id_pattern",
                        ))

            # 3. Fuzzy matching for remaining column pairs
            for c1 in cols1:
                for c2 in cols2:
                    if c1 == c2:
                        continue
                    ratio = SequenceMatcher(None, c1, c2).ratio()
                    if ratio >= 0.75:
                        rels.append(Relationship(
                            table_a=t1, column_a=c1,
                            table_b=t2, column_b=c2,
                            confidence=round(ratio * 0.8, 2),
                            source="fuzzy",
                        ))

    return rels


def _deduplicate(rels: list[Relationship]) -> list[Relationship]:
    """Keep the highest-confidence relationship for each column pair."""
    best: dict[tuple, Relationship] = {}
    for r in rels:
        key = tuple(sorted([(r.table_a, r.column_a), (r.table_b, r.column_b)]))
        if key not in best or r.confidence > best[key].confidence:
            best[key] = r
    return list(best.values())


def format_relationships(rels: list[Relationship] | None = None) -> str:
    """Format relationships as a readable string for prompt injection."""
    if rels is None:
        rels = discover_relationships()

    if not rels:
        return "No explicit or inferred relationships found between tables."

    lines: list[str] = []
    for r in sorted(rels, key=lambda x: -x.confidence):
        lines.append(
            f"{r.table_a}.{r.column_a} <-> {r.table_b}.{r.column_b}  "
            f"(confidence: {r.confidence:.0%}, source: {r.source})"
        )
    return "\n".join(lines)
