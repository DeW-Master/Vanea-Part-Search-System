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


def norm_zgs(value) -> str:
    """Normalize a ZGS cell: trim and strip leading zeros for pure-digit
    values so that '005' and '5' compare equal across data sources."""
    v = norm(value)
    if v.isdigit():
        return str(int(v))
    return v


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
    # 同一阶段文件内同 PN 多行时，该 PN 出现过的全部 ZGS 取值集合
    zgs_values: Optional[set] = None
    # None -> PN not present in ENIGMA; dict of sets -> present (possibly empty values)
    enigma_values: Optional[Dict[str, set]] = None
    # ENIGMA 主表完整参考记录 (单条, 同 PN 多行已合并), 纯参考不参与对比
    enigma_record: Optional[dict] = None

    def merge_zgs(self, other: 'Part') -> None:
        """合并同阶段同 PN 另一条记录的 ZGS 取值（多行多 ZGS 场景）。"""
        merged = (self.zgs_values or {self.zgs}) | (other.zgs_values or {other.zgs})
        merged.discard('')
        self.zgs_values = merged
        if merged:
            zgs_joined = ','.join(sorted(merged, key=lambda z: (len(z), z)))
            self.zgs = zgs_joined
            # 同步写回 data，保证字段级 diff 看到的是合并后的 ZGS
            if business_column('zgs') in self.data or business_column('zgs') in other.data:
                self.data[business_column('zgs')] = zgs_joined

    def value(self, business_key: str) -> str:
        """业务字段取值: BOM 原始数据优先, 没有则回退到 ENIGMA 参考记录。

        取值优先级 (一旦取到非空值就返回):
        1. ``self.data`` — BOM 原始列 (保证阶段数据的真实性)
        2. ``self.enigma_record`` — ENIGMA 主表完整参考记录 (包含 status 等所有字段)
        3. ``self.enigma_values`` — ENIGMA 多值索引 (ec/kem/fav/soma 的 set, 取首个稳定值)
        4. 空串

        这些参考值仅用于 KPI 统计和摘要展示, **不参与阶段数据对比**。
        """
        col_name = business_column(business_key)
        # 1) BOM 原始数据
        raw = norm(self.data.get(col_name))
        if raw:
            return raw
        # 2) ENIGMA 完整参考记录 (字段名 = business_column 映射后的统一列名)
        if self.enigma_record is not None:
            v = norm(self.enigma_record.get(col_name))
            if v:
                return v
        # 3) ENIGMA 多值索引 (业务键直接匹配, 用于 ec/kem/fav/soma)
        if self.enigma_values is not None:
            s = self.enigma_values.get(business_key)
            if s:
                return sorted(s)[0]
        return ''

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
    def soma_values(self) -> set:
        return self._enigma_set('soma')

    @property
    def soma_ja(self) -> bool:
        return any(v.lower() == 'ja' for v in self._enigma_set('soma'))

    @classmethod
    def from_row(cls, row_id, file_id, pn, data, stage='', enigma_values=None,
                 enigma_record=None) -> 'Part':
        data = data or {}
        zgs = norm_zgs(data.get(business_column('zgs')))
        return cls(
            pn=norm(pn),
            zgs=zgs,
            stage=stage,
            file_id=file_id,
            row_id=row_id,
            data=data,
            zgs_values={zgs} if zgs else set(),
            enigma_values=enigma_values,
            enigma_record=enigma_record,
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
            if from_part and field_name not in from_part.data and to_part and field_name not in to_part.data:
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

    def compare(self, other: Optional['Part']) -> dict:
        """逐字段对比两个 Part 的真实数据，零 hardcoding。

        只比较 ``self.data`` 和 ``other.data`` 中各自真正存在的字段，
        不做任何富化、补全或配置驱动的映射。适合下钻展示原始差异。

        返回结构::

            {
                "from_field_count": int,       # 本侧字段数
                "to_field_count": int,         # 对侧字段数
                "common": [                    # 两边都有的字段 (按字段名排序)
                    {"field": str, "from_value": str, "to_value": str, "is_different": bool}
                ],
                "only_in_from": [              # 只在本侧有的字段
                    {"field": str, "value": str}
                ],
                "only_in_to": [                # 只在对侧有的字段
                    {"field": str, "value": str}
                ],
                "diff_count": int,             # common 中有差异的数量 + 单边字段数
            }

        用法 (同一个 PN 在不同阶段的对比)::

            from_catalog.get(pn).compare(to_catalog.get(pn))
        """
        my_data = self.data or {}
        other_data = (other.data or {}) if other else {}

        my_keys = set(my_data.keys())
        other_keys = set(other_data.keys())

        common_keys = sorted(my_keys & other_keys)
        only_my_keys = sorted(my_keys - other_keys)
        only_other_keys = sorted(other_keys - my_keys)

        common = []
        diff_count = 0
        for k in common_keys:
            v1 = my_data.get(k)
            v2 = other_data.get(k)
            # 统一用字符串比较，避免 None / '' / 数字类型混淆
            s1 = '' if v1 is None else str(v1).strip()
            s2 = '' if v2 is None else str(v2).strip()
            is_diff = s1 != s2
            if is_diff:
                diff_count += 1
            common.append({
                "field": k,
                "from_value": v1 if v1 is not None else '',
                "to_value": v2 if v2 is not None else '',
                "is_different": is_diff,
            })

        only_in_from = [{"field": k, "value": my_data.get(k) if my_data.get(k) is not None else ''}
                        for k in only_my_keys]
        only_in_to = [{"field": k, "value": other_data.get(k) if other_data.get(k) is not None else ''}
                      for k in only_other_keys]

        diff_count += len(only_my_keys) + len(only_other_keys)

        return {
            "from_field_count": len(my_keys),
            "to_field_count": len(other_keys),
            "common": common,
            "only_in_from": only_in_from,
            "only_in_to": only_in_to,
            "diff_count": diff_count,
        }

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
                # 同 PN 多行 ZGS 已合并为集合：两侧 ZGS 集合有交集即视为该 PN 在两阶段间无 ZGS 变更
                from_zs = from_part.zgs_values or ({from_part.zgs} if from_part.zgs else set())
                to_zs = to_part.zgs_values or ({to_part.zgs} if to_part.zgs else set())
                if from_zs & to_zs:
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
