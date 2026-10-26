"""Original OOXML text retrieval after PostgreSQL publication authority filtering.

This is lexical retrieval, not validated BGE-M3 semantic retrieval. No page numbers
are inferred from Word's layout-independent XML. Appendices are never normative.
"""

import hashlib
import re
from datetime import datetime
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from database.models import PolicyUpload, PolicyVersion
from domain.compliance import SourceEvidence
from rag.retrieval.pgvector import scope_rows

SOURCE_SHA256 = "65043838143a8d043130881b8fda35fd2573ed7fcd8716f1a33fc72f32bf1a98"
HOTEL_2020_2030_SHA256 = "ef13e53dce3bb7efa4a3703df20a2dcbd73b746efaf514e1090cb2d6c190af10"
SOURCE_INTERVALS = {
    SOURCE_SHA256: ("2026-11-01", "2027-01-01"),
    HOTEL_2020_2030_SHA256: ("2020-01-01", "2031-01-01"),
}
NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def xml_text(element):
    return "".join(node.text or "" for node in element.findall(".//w:t", NS))


def original_chunks(raw, *, document_id, policy_version_id, effective_from, effective_to):
    sha = hashlib.sha256(raw).hexdigest()
    if sha not in SOURCE_INTERVALS:
        raise ValueError("P4B_SOURCE_SHA_MISMATCH")
    with ZipFile(BytesIO(raw)) as archive:
        body = ET.fromstring(archive.read("word/document.xml")).find("w:body", NS)
    section, section_number, table_number = "", 0, 0
    chunks = []

    def append(text, clause, position):
        key = hashlib.sha256((policy_version_id + str(position) + text).encode()).hexdigest()
        chunks.append(
            SourceEvidence(
                document_id=document_id,
                document_sha256=sha,
                policy_version_id=policy_version_id,
                section=section,
                clause_id=clause,
                chunk_id=f"P4B-{key}",
                source_text=text,
                source_position={"part": "word/document.xml", **position},
                effective_from=effective_from,
                effective_to=effective_to,
            )
        )

    for body_index, item in enumerate(body):
        if item.tag.endswith("}p"):
            text = xml_text(item)
            if re.match(r"^附录\s*[A-DＡ-Ｄ]", text):
                section, section_number = "", 0
            if re.fullmatch(r"第[一二三四五六七八九十]+章\s+[^｜|]+", text):
                section = text
                section_number += 1
            if not section or not text:
                continue
            clause = re.match(r"^(第[一二三四五六七八九十百]+条)", text)
            append(
                text,
                clause[1] if clause else f"SECTION-{section_number}",
                {"body_index": body_index, "paragraph": True},
            )
        elif item.tag.endswith("}tbl"):
            table_number += 1
            if not section:
                continue
            rows = [
                [xml_text(cell) for cell in row.findall("w:tc", NS)]
                for row in item.findall("w:tr", NS)
            ]
            if not rows:
                continue
            for row_index, row in enumerate(rows[1:], 1):
                text = " | ".join(rows[0]) + "\n" + " | ".join(row)
                append(
                    text,
                    f"TABLE-{table_number}-ROW-{row_index}",
                    {
                        "body_index": body_index,
                        "table_index": table_number,
                        "header_row": rows[0],
                        "row_index": row_index,
                        "cells": row,
                        "columns": list(range(len(row))),
                    },
                )
    return chunks


def extraction(chunks):
    """Source-derived candidates require explicit scope confirmation, never auto-publish."""
    allowances, entertainment = {}, None
    rates = None
    for chunk in chunks:
        p, text = chunk.source_position, chunk.source_text
        row, header = p.get("cells", []), p.get("header_row", [])
        if header and "基础补助" in " ".join(header) and len(row) >= 2:
            tier = {"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV"}.get(row[0][0])
            if tier:
                allowances[tier] = {
                    "amount": re.search(r"\d+", row[1])[0],
                    "chunk_id": chunk.chunk_id,
                }
        if row and row[0] == "一般客户/供应商工作餐" and len(row) >= 3:
            entertainment = {
                "per_person": re.search(r"[\d,]+", row[1])[0].replace(",", ""),
                "per_event": re.search(r"[\d,]+", row[2])[0].replace(",", ""),
                "chunk_id": chunk.chunk_id,
            }
        if chunk.clause_id == "第二十六条":
            rate = re.search(r"(\d+)%", text)
            if rate and "分别扣减" in text and "早餐不扣减" in text:
                rates = {
                    "lunch": rate[1],
                    "dinner": rate[1],
                    "breakfast": "0",
                    "chunk_id": chunk.chunk_id,
                }
    if len(allowances) != 4 or entertainment is None or rates is None:
        raise ValueError("P4B_SOURCE_RULES_INCOMPLETE")
    result = {"allowances": allowances, "entertainment": entertainment, "meal_rates": rates}
    import json

    result["extraction_digest"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return result


def rank_chunks(chunks, query, *, expense_type, limit=30):
    # Baseline applicable chapters are always available, including evidence/authority clauses.
    chapters = {"第一章", "第二章", "第三章", "第十三章", "第十四章", "第十六章", "第十七章"}
    chapters.update(
        {
            "HOTEL": {"第四章", "第五章"},
            "ALLOWANCE": {"第四章", "第八章"},
            "ENTERTAINMENT": {"第九章"},
            "TRANSPORT": {"第六章", "第七章"},
            "MEETING": {"第十章"},
        }.get(expense_type, {c.section.split()[0] for c in chunks})
    )
    terms = set(re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{2}", query))
    applicable = [c for c in chunks if c.section.split()[0] in chapters]

    def score(chunk):
        return sum(t in chunk.source_text for t in terms) + (
            5
            if expense_type != "GENERAL"
            and chunk.section.split()[0]
            in chapters
            - {"第一章", "第二章", "第三章", "第十三章", "第十四章", "第十六章", "第十七章"}
            else 0
        )

    ranked = sorted(
        applicable,
        key=lambda c: (
            -score(c),
            c.source_position["body_index"],
            c.source_position.get("row_index", 0),
        ),
    )
    # Money-heavy chapters must not crowd out proof, independent authorization,
    # anomaly and version governance needed for the semantic review.
    mandatory = {
        "第三十八条",
        "第三十九条",
        "第四十条",
        "第四十一条",
        "第四十三条",
        "第四十四条",
        "第四十五条",
        "第五十一条",
        "第五十二条",
        "第五十三条",
        "第五十四条",
        "第五十六条",
        "第五十七条",
    }
    pinned = (
        [c for c in applicable if c.clause_id in mandatory] if expense_type != "GENERAL" else []
    )
    return list({c.chunk_id: c for c in [*pinned, *ranked]}.values())[:limit]


class ComplianceRetriever:
    def __init__(self, engine):
        self.engine = engine

    def read(self, **filters):
        try:
            return self._read(**filters)
        except OperationalError:
            raise ConnectionError("P4B_POLICY_DEPENDENCY_FAILURE") from None

    def _read(self, *, company, city, incurred_on, started: datetime, expected_id=None):
        with Session(self.engine.execution_options(isolation_level="REPEATABLE READ")) as session:
            rows = scope_rows(
                session,
                extra=(PolicyVersion.authority_level >= 100,),
                legal_entity=company,
                category="HOTEL",
                city=city,
                incurred_on=incurred_on,
                audit_started_at=started,
            )
            if len(rows) != 1:
                raise ValueError("POLICY_CONFLICT" if rows else "NO_ACTIVE_P4B_POLICY")
            policy = rows[0]
            if expected_id is not None and expected_id != policy.policy_version_id:
                raise ValueError("RULE_RAG_VERSION_CONFLICT")
            rules = policy.authoritative_rules
            if rules.get("rule_type") != "HOTEL_LIMIT_MATRIX":
                raise ValueError("P4B_MATRIX_SOURCE_REQUIRED")
            source = session.get(PolicyUpload, rules.get("upload_id"))
            if (not source or not source.original or source.sha256 not in SOURCE_INTERVALS
                    or hashlib.sha256(source.original).hexdigest() != source.sha256
                    or rules.get("file_sha256") != source.sha256):
                raise ValueError("P4B_SOURCE_SHA_MISMATCH")
            if (
                policy.effective_from.isoformat() != SOURCE_INTERVALS[source.sha256][0]
                or not policy.effective_to
                or policy.effective_to.isoformat() != SOURCE_INTERVALS[source.sha256][1]
            ):
                raise ValueError("P4B_SOURCE_EFFECTIVE_INTERVAL_MISMATCH")
            chunks = original_chunks(
                source.original,
                document_id=source.upload_id,
                policy_version_id=policy.policy_version_id,
                effective_from=policy.effective_from,
                effective_to=policy.effective_to,
            )
            return {
                "policy_version_id": policy.policy_version_id,
                "title": policy.title,
                "version": policy.version,
                "chunks": chunks,
                "rules": extraction(chunks),
                "policy_rule_type": rules.get("rule_type"),
            }
