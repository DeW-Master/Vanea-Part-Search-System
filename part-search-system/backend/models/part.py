"""Part domain model.

Thin domain layer over the JSON-blob storage. All field access goes through
business keys mapped in ``config.DELTA_BUSINESS_FIELDS`` so that any source
table (any header language) can be normalized once during import and then
compared/abstracted without further hardcoding.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional

from config import DELTA_BUSINESS_FIELDS


def norm(value) -> str:
    """Normalize a raw cell value: None -> '', trimmed string otherwise."""
    if value is None:
        return ''
    return str(value).strip()


# Raw status values that are placeholders / data-entry errors, not categories.
#  - 'null'/'none'/'nan': source cells literally contain the word "null" (export artifact)
#  - values containing '|': a few rows have pipe-joined IDs/step codes mis-entered into
#    the status column (e.g. 'A0009904362 | A0009913700 | ...' in FAV Status Short,
#    'AG1_PRO1_Fuz | AG1_PRO2_Fuz' in ProzessStatusDetail) — these are not statuses.
_STATUS_INVALID = {'', 'null', 'none', 'nan', '-'}


def _valid_status(value: str) -> bool:
    """A status cell is only a real category if non-empty, not a literal 'null'
    placeholder and not a pipe-joined aggregate (data-entry error)."""
    v = norm(value)
    return v.lower() not in _STATUS_INVALID and '|' not in v


# Display labels (English) for source status values. The ENIGMA export keeps
# EC status (ProzessStatusDetail) in German; ZEUS/FAV statuses use internal
# codes plus two German words. Everything is normalized to English labels so
# the dashboard shows source categories in English regardless of source locale.
STATUS_LABELS = {
    # EC / ProzessStatusDetail (German workflow phase -> English)
    'Umsetzung': 'Implementation',
    'Abgeschlossen': 'Completed',
    'Bewertung': 'Evaluation',
    'Detaillierung': 'Detailing',
    'Entscheidung': 'Decision',
    'Verteilung': 'Distribution',
    # ZEUS / FAV Status Short
    'Erledigt': 'Resolved',
    'Abgebrochen': 'Cancelled',
}


def status_label(business_key: str, raw_value: str) -> str:
    """Map a raw source status value to its English display label.
    Known German words/codes get explicit English labels; language-neutral
    internal codes (AE/OWP/BE/UA/KÄ/ÜW...) are returned unchanged."""
    v = norm(raw_value)
    if v in STATUS_LABELS:
        return STATUS_LABELS[v]
    return v


def business_column(business_key: str) -> str:
    """Map a business key to the unified (English) column name."""
    return DELTA_BUSINESS_FIELDS.get(business_key, business_key)


def determine_change_type(business_key: str, old_value, new_value) -> str:
    """Classify a value pair. ZGS (business_key == 'zgs') compares as ints
    (upgrade semantics); everything else uses added/removed/changed."""
    old = norm(old_value)
    new = norm(new_value)
    if old == new:
        return 'persisted' if old else 'unchanged'
    if business_key == 'zgs' and old and new:
        try:
            return 'upgraded' if int(new) > int(old) else 'changed'
        except (TypeError, ValueError):
            pass  # fall through to the generic rules
    if not old:
        return 'added'
    if not new:
        return 'removed'
    return 'changed'


@dataclass(slots=True)
class FieldChange:
    field: str
    business: str
    priority: int
    old_value: str
    new_value: str
    change_type: str

    def to_dict(self) -> dict:
        return {
            'field': self.field,
            'business': self.business,
            'priority': self.priority,
            'old_value': self.old_value,
            'new_value': self.new_value,
            'change_type': self.change_type,
        }


@dataclass(slots=True)
class Part:
    """One part row. ``data`` holds unified column names; access via value()."""
    pn: str
    zgs: str
    stage: str
    file_id: int
    row_id: int
    data: dict
    # None -> PN not present in ENIGMA; dict of sets -> present (possibly empty values)
    enigma_values: Optional[Dict[str, set]] = None

    def value(self, business_key: str) -> str:
        """Single access point: business key -> config mapping -> normalized value."""
        return norm(self.data.get(business_column(business_key)))

    @property
    def ec(self) -> str:
        return self.value('ec')

    @property
    def ec_status(self) -> str:
        return self.value('ec_status')

    @property
    def fav(self) -> str:
        return self.value('fav')

    @property
    def fav_status(self) -> str:
        return self.value('fav_status')

    @property
    def kem(self) -> str:
        return self.value('kem')

    @property
    def soma(self) -> str:
        return self.value('soma')

    @property
    def has_ec(self) -> bool:
        return bool(self.ec)

    @property
    def in_enigma(self) -> bool:
        return self.enigma_values is not None

    def _enigma_set(self, key: str) -> set:
        if not self.enigma_values:
            return set()
        return self.enigma_values.get(key) or set()

    @property
    def ec_values(self) -> set:
        return self._enigma_set('ec')

    @property
    def kem_values(self) -> set:
        return self._enigma_set('kem')

    @property
    def fav_values(self) -> set:
        return self._enigma_set('fav')

    @property
    def soma_ja(self) -> bool:
        return any(v.lower() == 'ja' for v in self._enigma_set('soma'))

    @classmethod
    def from_row(cls, row_id, file_id, pn, data, stage='', enigma_values=None) -> 'Part':
        data = data or {}
        return cls(
            pn=norm(pn),
            zgs=norm(data.get(business_column('zgs'))),
            stage=stage,
            file_id=file_id,
            row_id=row_id,
            data=data,
            enigma_values=enigma_values,
        )

    @staticmethod
    def diff_parts(from_part: Optional['Part'], to_part: Optional['Part'],
                   field_config: List[dict], col_names: Optional[set] = None) -> List[FieldChange]:
        """Compare two parts over ``field_config`` (config.DELTA_FIELD_CONFIG).

        ``col_names`` (columns available in the underlying table) decides
        whether a tracked field is 'unavailable' for this comparison.
        """
        changes: List[FieldChange] = []
        for cfg in field_config:
            field_name = cfg['field']
            if col_names is not None and field_name not in col_names:
                if cfg.get('track'):
                    changes.append(FieldChange(
                        field=field_name, business=cfg['business'],
                        priority=cfg['priority'], old_value='', new_value='',
                        change_type='unavailable',
                    ))
                continue
            old = norm(from_part.data.get(field_name)) if from_part else ''
            new = norm(to_part.data.get(field_name)) if to_part else ''
            change_type = determine_change_type(cfg.get('key') or '', old, new)
            changes.append(FieldChange(
                field=field_name, business=cfg['business'],
                priority=cfg['priority'], old_value=old, new_value=new,
                change_type=change_type,
            ))
        changes.sort(key=lambda c: (c.priority, 0 if c.change_type != 'unchanged' else 1))
        return changes

    def to_dict(self) -> dict:
        return {
            'part_number': self.pn,
            'zgs': self.zgs,
            'stage': self.stage,
            'file_id': self.file_id,
            'row_id': self.row_id,
            'ec': self.ec,
            'fav': self.fav,
            'kem': self.kem,
            'in_enigma': self.in_enigma,
        }


@dataclass(slots=True)
class DeltaPair:
    pn: str
    match_type: str  # 'zgs_upgraded' | 'new_part' | 'discontinued_part'
    from_part: Optional[Part] = None
    to_part: Optional[Part] = None


class StageCatalog:
    """{pn: Part} catalog of one stage with set operations."""

    def __init__(self, stage: str, parts: Optional[Dict[str, Part]] = None):
        self.stage = stage
        self.parts: Dict[str, Part] = parts or {}

    def __len__(self) -> int:
        return len(self.parts)

    def __bool__(self) -> bool:
        return bool(self.parts)

    def __contains__(self, pn: str) -> bool:
        return pn in self.parts

    def __iter__(self) -> Iterator[Part]:
        return iter(self.parts.values())

    def get(self, pn: str) -> Optional[Part]:
        return self.parts.get(pn)

    @property
    def pns(self) -> set:
        return set(self.parts)

    def add(self, part: Part) -> None:
        self.parts[part.pn] = part

    def delta_pairs(self, other: 'StageCatalog') -> List[DeltaPair]:
        """Delta from self (earlier stage) to other (later stage).

        Same PN + same ZGS -> identical, skipped; same PN + different ZGS ->
        'zgs_upgraded'; only in other -> 'new_part'; only in self ->
        'discontinued_part'.
        """
        pairs: List[DeltaPair] = []
        for pn in sorted(self.pns | other.pns):
            from_part = self.get(pn)
            to_part = other.get(pn)
            if from_part and to_part:
                if from_part.zgs == to_part.zgs:
                    continue
                match_type = 'zgs_upgraded'
            elif to_part:
                match_type = 'new_part'
            else:
                match_type = 'discontinued_part'
            pairs.append(DeltaPair(pn=pn, match_type=match_type,
                                   from_part=from_part, to_part=to_part))
        return pairs

    def stats(self) -> dict:
        """Stage KPI counters (ENIGMA-backed, distinct-value sets).

        Keys are part of the dashboard API contract.
        """
        ec_values_all, ec_pns = set(), set()
        fav_values_all, fav_pns = set(), set()
        kem_values_all = set()
        soma_ja = 0
        for part in self.parts.values():
            if part.ec_values:
                ec_values_all |= part.ec_values
                ec_pns.add(part.pn)
            if part.fav_values:
                fav_values_all |= part.fav_values
                fav_pns.add(part.pn)
            if part.kem_values:
                kem_values_all |= part.kem_values
            if part.soma_ja:
                soma_ja += 1
        return {
            'total_records': len(self.parts),
            'unique_pn': len(self.parts),
            'ec_count': len(ec_values_all),
            'ec_pn': len(ec_pns),
            'fav_count': len(fav_values_all),
            'fav_pn': len(fav_pns),
            'kem_count': len(kem_values_all),
            'soma_ja': soma_ja,
        }

    def status_distribution(self, business_key: str) -> List[dict]:
        """Value distribution of a status field (pie charts).

        Placeholder cells (literal 'null'), empty cells and pipe-joined
        data-entry errors are skipped; raw source values are mapped to
        English display labels. Sorted by count descending.
        API shape: [{name, value}].
        """
        counts: Dict[str, int] = {}
        for part in self.parts.values():
            raw = part.value(business_key)
            if not _valid_status(raw):
                continue
            label = status_label(business_key, raw)
            counts[label] = counts.get(label, 0) + 1
        items = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        return [{'name': n, 'value': c} for n, c in items]
