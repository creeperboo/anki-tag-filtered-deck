"""标签筛选牌组的纯规则逻辑。

这个文件刻意不导入 Anki 内部模块，方便在普通 Python 里跑测试。
真正的筛选牌组创建、重建和界面逻辑在 __init__.py 里。

取卡范围是三个状态的组合（未学习 / 学习中 / 复习中），用的是 Anki 原生搜索词：

    is:new                type = 0            未学习
    is:learn              type in (1, 3)      学习中（含答错后正在重学的卡）
    is:review -is:learn   type = 2            复习中（已毕业、进入间隔复习）

三者互不重复、并集覆盖全部卡片，所以统计数量相加正好等于命中总数。

关于「已暂停 / 已搁置」为什么没有做成选项：
Anki 重建筛选牌组时会把搜索式强制拼成
`<你的条件> -is:suspended -is:buried -deck:filtered`
（见 rslib/src/scheduler/filtered/mod.rs，已在 Anki 26.9.3 上实测确认），
所以暂停/搁置的卡不先解禁就永远收不进来；解禁又会改动卡片的暂停状态。
本插件不改动卡片状态，改为在预览里提示「这类卡这次收不进来多少张」。

顺序枚举值来自 Anki 26.09.3 的 proto/anki/decks.proto
（Deck.Filtered.SearchTerm.Order），顺序标签由 Anki 自己提供
（col.sched.filtered_deck_order_labels()，实现见 rslib/src/decks/filtered.rs）。
下面的中文标签只是拿不到 Anki 标签时的兜底。
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable, Mapping, Sequence

MODE_UNION = "union"
MODE_INTERSECTION = "intersection"

# 「看收不进来的卡」最多列出多少张（超出的截断，并在界面上写明）
MAX_BROWSE_CARDS = 5000
# cid: 搜索式的分块大小：一次列太多卡号会让搜索链路过长
CID_CHUNK = 400

# 老版本用过的「取卡范围」字段，现在只用来迁移旧规则
SCOPE_DUE_NEW = "due_new"
SCOPE_ALL = "all"

# 现在的三个取卡状态
STATE_NEW = "new"
STATE_LEARN = "learn"
STATE_REVIEW = "review"

STATE_CHOICES: tuple[tuple[str, str], ...] = (
    (STATE_NEW, "未学习"),
    (STATE_LEARN, "学习中"),
    (STATE_REVIEW, "复习中"),
)
# 固定顺序：界面显示、统计、搜索式都按这个顺序，结果才稳定
STATE_VALUES: tuple[str, ...] = tuple(value for value, _label in STATE_CHOICES)
STATE_LABELS: dict[str, str] = dict(STATE_CHOICES)

STATE_SEARCH: dict[str, str] = {
    STATE_NEW: "is:new",
    STATE_LEARN: "is:learn",
    STATE_REVIEW: "is:review -is:learn",
}

# 这些卡 Anki 重建筛选牌组时一定会排除，插件不会去改动它们的状态
ALWAYS_EXCLUDED_QUERY = "(is:suspended OR is:buried)"

DEFAULT_STATES: tuple[str, ...] = (STATE_NEW, STATE_LEARN, STATE_REVIEW)
# 1.5.0 起改成正向语义：True ＝ 连还没到期的也收；默认 False ＝ 只收到期
DEFAULT_INCLUDE_NOT_DUE = False
# 「只收到期的卡（加上新卡）」用的范围片段
DUE_FILTER = "(is:due OR is:new)"

DEFAULT_PARENT = "标签筛选"
DEFAULT_SCOPE = SCOPE_DUE_NEW  # 兼容旧配置；新规则用 DEFAULT_STATES / DEFAULT_INCLUDE_NOT_DUE
DEFAULT_ORDER = 6  # DUE：到期优先
DEFAULT_LIMIT = 9999
MIN_LIMIT = 1
MAX_LIMIT = 99999

# (枚举值, 兜底中文标签)。下标就是 Anki 存的值。
ORDER_CHOICES: tuple[tuple[int, str], ...] = (
    (0, "最早看过的优先"),
    (1, "随机"),
    (2, "间隔从短到长"),
    (3, "间隔从长到短"),
    (4, "遗忘次数最多"),
    (5, "添加顺序"),
    (6, "到期顺序"),
    (7, "最新添加优先"),
    (8, "提取难度从难到易（需 FSRS）"),
    (9, "提取难度从易到难（需 FSRS）"),
    (10, "相对逾期优先"),
)

# 这两项只有开启 FSRS 时才可用，Anki 自己的筛选牌组窗口也是这样取舍的
FSRS_ONLY_ORDERS: frozenset[int] = frozenset({8, 9})


def available_orders(order_count: int, fsrs_enabled: bool) -> list[int]:
    """可选的顺序值，逻辑与 Anki 内置窗口一致。"""
    return [
        order
        for order in range(max(0, int(order_count)))
        if fsrs_enabled or order not in FSRS_ONLY_ORDERS
    ]


def order_label(value: Any, labels: Sequence[str] | None = None) -> str:
    """把顺序值翻成界面文字；labels 为 Anki 提供的本地化标签时优先用它。"""
    try:
        number = int(value)
    except Exception:
        number = DEFAULT_ORDER
    if labels and 0 <= number < len(labels):
        return str(labels[number])
    for candidate, fallback in ORDER_CHOICES:
        if candidate == number:
            return fallback
    return ORDER_CHOICES[0][1]


def clean_tags(tags: Iterable[Any] | None) -> list[str]:
    """去掉空白和重复标签，保持用户选择的顺序。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        if raw is None:
            continue
        tag = str(raw).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def normalize_mode(mode: Any) -> str:
    return MODE_INTERSECTION if str(mode) == MODE_INTERSECTION else MODE_UNION


def normalize_scope(scope: Any) -> str:
    """老规则的范围字段（只用于迁移）。"""
    return SCOPE_ALL if str(scope) == SCOPE_ALL else SCOPE_DUE_NEW


# --------------------------------------------------------------------------
# 三个取卡状态
# --------------------------------------------------------------------------


def _state_values(states: Any) -> list[Any]:
    """把用户可能写成的各种形状（列表 / 逗号字符串 / 字典）摊平成一串值。"""
    if states is None:
        return []
    if isinstance(states, str):
        return states.replace(",", " ").replace("，", " ").split()
    if isinstance(states, Mapping):
        return [key for key, flag in states.items() if flag]
    try:
        return list(states)
    except TypeError:
        return [states]


def normalize_states(states: Any) -> list[str]:
    """只保留合法的状态值，按固定顺序去重返回（可能为空列表）。

    历史配置里出现过的 `suspended` / `buried` 会被当作非法值丢掉。
    """
    wanted = {
        str(item).strip().lower()
        for item in _state_values(states)
        if str(item or "").strip()
    }
    return [value for value in STATE_VALUES if value in wanted]


def state_label(value: Any) -> str:
    return STATE_LABELS.get(str(value), str(value))


def states_for_scope(scope: Any) -> list[str]:
    """老规则的 scope 迁移：两种情况都是这三个状态，只有「是否收到期」不同。"""
    return list(DEFAULT_STATES)


def include_not_due_for_scope(scope: Any) -> bool:
    """老规则的 scope 迁移：all ＝ 连没到期的也收，due_new ＝ 只收到期。"""
    return normalize_scope(scope) == SCOPE_ALL


def include_not_due_from_due_only(value: Any) -> bool:
    """1.4.0 及更早的 due_only 是反向的（True ＝ 只收到期），取反就是新字段。"""
    return not bool(value)


def _is_fully_grouped(text: str) -> bool:
    """判断整段文字是不是被一层括号完整包住（`(a) AND (b)` 不算）。"""
    if not (text.startswith("(") and text.endswith(")")):
        return False
    depth = 0
    in_quote = False
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_quote = not in_quote
            continue
        if in_quote:
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index == len(text) - 1
    return False


def _wrap(part: str) -> str:
    """给需要分组的片段套一层括号（已经完整套好的不重复套）。"""
    text = part.strip()
    if not text:
        return ""
    if _is_fully_grouped(text):
        return text
    return f"({text})"


def and_query(*parts: Any) -> str:
    pieces = [_wrap(str(part)) for part in parts if str(part or "").strip()]
    return " AND ".join(pieces)


def or_query(*parts: Any) -> str:
    pieces = [_wrap(str(part)) for part in parts if str(part or "").strip()]
    return " OR ".join(pieces)


def states_union_query(states: Any) -> str:
    """把选中的状态拼成一个并集片段（不含「是否到期」的过滤）。"""
    chosen = normalize_states(states)
    if not chosen:
        return ""
    if len(chosen) == 1:
        return _wrap(STATE_SEARCH[chosen[0]])
    return "(" + " OR ".join(STATE_SEARCH[value] for value in chosen) + ")"


def states_query(
    states: Any = DEFAULT_STATES, include_not_due: Any = DEFAULT_INCLUDE_NOT_DUE
) -> str:
    """取卡范围片段。

    * 没勾「连还没到期的也收」（include_not_due=False）时，选项里只要含
      「学习中/复习中」，就要另外满足到期条件（新卡本来就算范围内，所以拼的是
      `(is:due OR is:new)`）；
    * 选「未学习+学习中+复习中」时逐字沿用老版本写法，老规则升级后生成的
      搜索式与老版一致，不会被误判成「被 Anki 改过」。
    """
    chosen = normalize_states(states)
    if not chosen:
        return ""
    include_not_due = bool(include_not_due)

    # 兼容：老版本的两个范围（due_new / all）逐字一致
    if tuple(chosen) == DEFAULT_STATES:
        return "" if include_not_due else DUE_FILTER

    union = states_union_query(chosen)
    if include_not_due or not (STATE_LEARN in chosen or STATE_REVIEW in chosen):
        return union
    return and_query(union, DUE_FILTER)


def states_label(states: Any, include_not_due: Any = DEFAULT_INCLUDE_NOT_DUE) -> str:
    """管理窗口用的范围文字，例如「未学习+学习中+复习中·含未到期」。"""
    chosen = normalize_states(states)
    if not chosen:
        return "（没有勾选状态）"
    text = "+".join(STATE_LABELS[value] for value in chosen)
    if bool(include_not_due):
        text += "·含未到期"
    return text


def filter_tag_choices(tags: Iterable[Any] | None, keyword: Any = "") -> list[str]:
    """「已有标签」面板的过滤：大小写不敏感、包含匹配，保持传入顺序。"""
    cleaned = clean_tags(tags)
    text = str(keyword or "").strip().lower()
    if not text:
        return cleaned
    return [tag for tag in cleaned if text in tag.lower()]


# --------------------------------------------------------------------------
# 标签 → 搜索式（1.4.0 起：到期判定见上面新增的卡片级函数）
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 卡片级「到期」判定（1.4.0）
# --------------------------------------------------------------------------
#
# 为什么不能直接用 Anki 的 is:due：卡片被收进筛选牌组后，due 会被换成牌组内的
# 位置值（-100000 起），原来的到期时间挪到 odue，原牌组记在 odid。这时 is:due
# 会把位置值当成「已经到期」，于是「只收到期」和「连没到期的也收」这两个选项数
# 出来一样多（真机实测：310 张里其实有 297 张没到期）。下面这套纯函数按卡片自己
# 的到期时间算，编辑器的预览数字、管理窗口的统计和「看收不进来的卡」都用它。

CARD_TYPE_NEW = 0
CARD_TYPE_LEARN = 1
CARD_TYPE_REVIEW = 2
CARD_TYPE_RELEARN = 3
LEARNING_TYPES = (CARD_TYPE_LEARN, CARD_TYPE_RELEARN)


def _field(source: Any, *names: Any, default: Any = 0) -> Any:
    """从字典或对象里取第一个存在的字段。"""
    for name in names:
        if isinstance(source, Mapping):
            if name in source and source[name] is not None:
                return source[name]
        else:
            value = getattr(source, name, None)
            if value is not None:
                return value
    return default


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return fallback


def card_due_fields(card: Any) -> dict[str, Any]:
    """从 Anki 的卡片对象（或等价字典）里取出到期判定要用的字段。"""
    return {
        "type": _as_int(_field(card, "type"), CARD_TYPE_NEW),
        "queue": _as_int(_field(card, "queue"), -1),
        "due": _as_int(_field(card, "due"), 0),
        # did / odid 是「集中刷延迟按卡片原牌组算」（pick_home_deck）要用的：
        # 卡在筛选牌组里时看 odid，回到普通牌组后看 did。
        "did": _as_int(_field(card, "did", "deck_id"), 0),
        "odid": _as_int(_field(card, "odid", "original_deck_id"), 0),
        "odue": _as_int(_field(card, "odue", "original_due"), 0),
    }


def card_effective_due(fields: Any) -> int:
    """卡片「自己的」到期值：在筛选牌组里用 odue，否则用 due。"""
    odid = _as_int(_field(fields, "odid", "original_deck_id"), 0)
    odue = _as_int(_field(fields, "odue", "original_due"), 0)
    due = _as_int(_field(fields, "due"), 0)
    if odid and odue:
        return odue
    return due


def card_is_due(fields: Any, *, today: Any, now: Any = None) -> bool:
    """按卡片自己的到期时间判断「已经到期」；未学习的新卡一律算可以收。

    * 复习卡（type=2）的 due 是「第几天」，今天或更早就是到期；
    * 学习 / 重学卡（type=1 / 3）的 due 是 Unix 时间戳，现在或更早就是到期；
    * 未学习的新卡（type=0）不受「是否到期」限制，永远算在范围内。
    """
    if fields is None:
        return True
    ctype = _as_int(_field(fields, "type"), CARD_TYPE_NEW)
    queue = _as_int(_field(fields, "queue"), ctype)
    if ctype == CARD_TYPE_NEW or queue == 0:
        return True
    effective = card_effective_due(fields)
    now_value = _as_int(now, 0) or int(time.time())
    if ctype in LEARNING_TYPES:
        return effective <= now_value
    if ctype == CARD_TYPE_REVIEW:
        return effective <= _as_int(today, 0)
    # 兜底：拿不到 type 时按队列判
    if queue in (1, 3):
        return effective <= now_value
    return effective <= _as_int(today, 0)


def due_restriction_applies(
    states: Any, include_not_due: Any = DEFAULT_INCLUDE_NOT_DUE
) -> bool:
    """这次筛选要不要受「是否到期」限制。

    只勾「未学习」时不受影响（新卡本来就都在范围内）；勾了「学习中 / 复习中」
    又没勾「连还没到期的也收」才需要逐张看卡片自己的到期时间。
    """
    if bool(include_not_due):
        return False
    chosen = normalize_states(states)
    return STATE_LEARN in chosen or STATE_REVIEW in chosen


def due_filter_enabled(include_not_due: Any, today: Any) -> bool:
    """「只收到期」这一步这次到底要不要生效。

    参数 today 是 Anki 今天的「第几天」（复习卡的到期日就是这个口径）。读不到时
    **必须传 None**，不能用 0 顶替：新集合的第一天，这个值本身就是 0，用 0 当
    「读不到」的哨兵会让「只收到期」在第一天彻底失效（1.4.0 修的就是这个）。
    读不到今天时不按到期过滤——宁可多收几张，也不要凭空把卡挡住。
    """
    return (not bool(include_not_due)) and today is not None


def classify_cards(
    records: Iterable[Mapping[str, Any]],
    *,
    include_not_due: Any = DEFAULT_INCLUDE_NOT_DUE,
    states: Any = DEFAULT_STATES,
    today: Any = 0,
    now: Any = None,
    other_filtered_ids: Iterable[Any] | None = None,
) -> dict[str, list[int]]:
    """把一批卡分成「能收进来 / 收不进来」，四个列表互不重叠。

    * banned：暂停 / 搁置（Anki 重建筛选牌组时一定会排除）；
    * other：已经被别的筛选牌组占着（Anki 也会排除）；
    * due：卡片自己还没到期（只有没勾「连还没到期的也收」时才拦）；
    * collectible：剩下的，Anki 这次会收进来的。
    """
    restricted = due_restriction_applies(states, include_not_due)
    others = {_as_int(value) for value in (other_filtered_ids or ())}
    out: dict[str, list[int]] = {
        "collectible": [],
        "due": [],
        "banned": [],
        "other": [],
    }
    for record in records:
        card_id = _as_int(_field(record, "id"), 0)
        if not card_id:
            continue
        queue = _as_int(_field(record, "queue"), 0)
        if queue < 0:
            out["banned"].append(card_id)
            continue
        if card_id in others:
            out["other"].append(card_id)
            continue
        if restricted and not card_is_due(record, today=today, now=now):
            out["due"].append(card_id)
            continue
        out["collectible"].append(card_id)
    return out


def cid_query(card_ids: Iterable[Any]) -> str:
    """把卡号列表拼成 Anki 的 `cid:` 搜索式（分块，避免搜索链路过长）。"""
    numbers: list[int] = []
    for value in card_ids or ():
        number = _as_int(value, 0)
        if number:
            numbers.append(number)
    if not numbers:
        return ""
    parts: list[str] = []
    for start in range(0, len(numbers), CID_CHUNK):
        chunk = numbers[start : start + CID_CHUNK]
        parts.append("cid:" + ",".join(str(number) for number in chunk))
    if len(parts) == 1:
        return parts[0]
    return "(" + " OR ".join(parts) + ")"


def negate_term(term: Any) -> str:
    text = str(term or "").strip()
    if not text:
        return ""
    if text.startswith("-"):
        return text[1:].strip()
    return f"-{text}"


def quote_search_value(text: Any) -> str:
    """把可能含空格、引号、反斜杠的值放进 Anki 搜索式的引号里。"""
    raw = str(text or "")
    escaped = raw.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def tag_search_term(tag: Any) -> str:
    """生成一个标签搜索片段。

    Anki 的 tag: 搜索本身就会把子标签一起算进去，例如 tag:唐诗 也会匹配
    唐诗::格律。这里加引号是为了让包含特殊字符的标签更稳。
    """
    return f"tag:{quote_search_value(str(tag).strip())}"


def deck_search_term(name: Any) -> str:
    return f"deck:{quote_search_value(name)}"


def tag_query(tags: Iterable[Any] | None, mode: Any = MODE_UNION) -> str:
    """把标签列表变成并集或交集搜索式（不含取卡范围）。"""
    cleaned = clean_tags(tags)
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return tag_search_term(cleaned[0])
    joiner = " OR " if normalize_mode(mode) == MODE_UNION else " AND "
    return "(" + joiner.join(tag_search_term(tag) for tag in cleaned) + ")"


def scope_query(scope: Any = SCOPE_DUE_NEW) -> str:
    """老接口：把旧的 scope 值换成新的范围片段。"""
    return states_query(states_for_scope(scope), include_not_due_for_scope(scope))


def build_search_query(
    tags: Iterable[Any] | None,
    mode: Any = MODE_UNION,
    states: Any = DEFAULT_STATES,
    include_not_due: Any = DEFAULT_INCLUDE_NOT_DUE,
) -> str:
    """生成最终交给 Anki 筛选牌组的完整搜索式。"""
    base = tag_query(tags, mode)
    if not base:
        return ""
    scope_part = states_query(states, include_not_due)
    if scope_part:
        return and_query(base, scope_part)
    return base


def in_scope_query(
    tags: Iterable[Any] | None,
    mode: Any = MODE_UNION,
    states: Any = DEFAULT_STATES,
    include_not_due: Any = DEFAULT_INCLUDE_NOT_DUE,
) -> str:
    """这次要收的卡（还没减去暂停/搁置和别的筛选牌组）。"""
    return build_search_query(tags, mode, states, include_not_due)


def wanted_query(
    tags: Iterable[Any] | None, mode: Any, states: Any = DEFAULT_STATES
) -> str:
    """「勾选的状态」本身覆盖到的卡（不管是否到期）。"""
    base = tag_query(tags, mode)
    union = states_union_query(states)
    if not base or not union:
        return ""
    return and_query(base, union)


def state_count_query(tags: Iterable[Any] | None, mode: Any, state: Any) -> str:
    """统计某个状态的张数。

    三个状态互不重复，相加正好等于命中总数（已暂停/搁置属于它们各自原本的
    状态，卡片类型不会因为暂停而改变）。
    """
    value = str(state or "").strip().lower()
    term = STATE_SEARCH.get(value)
    base = tag_query(tags, mode)
    if not base or not term:
        return ""
    return and_query(base, term)


def blocked_query(
    card_ids: Iterable[Any] | None,
    *,
    limit: Any = MAX_BROWSE_CARDS,
) -> tuple[str, int]:
    """「看收不进来的卡」用的搜索式：直接把卡号列出来。

    1.4.0 起「收不进来」包含「卡片自己还没到期」这一条，而这一条没法用搜索式
    表达（筛选牌组里的卡 due 已经变成位置值），所以这里改成按卡号集合生成
    `cid:` 搜索式：张数一定和编辑器里那句「另有 K 张这次收不进来」对得上。

    返回 (搜索式, 被截断掉的张数)；没有卡号时返回 ("", 0)。
    """
    numbers: list[int] = []
    for value in card_ids or ():
        number = _as_int(value, 0)
        if number:
            numbers.append(number)
    if not numbers:
        return "", 0
    try:
        capped = max(1, int(limit))
    except Exception:
        capped = MAX_BROWSE_CARDS
    dropped = max(0, len(numbers) - capped)
    return cid_query(numbers[:capped]), dropped


# --------------------------------------------------------------------------
# 集中刷（临时把「还没到期的卡」也收进来突击刷，不改规则本身）
# --------------------------------------------------------------------------

DAY_SECS = 60 * 60 * 24
# 拿不到牌组设置时兜底用 Anki 默认的新卡步骤（1 分钟 / 10 分钟）
DEFAULT_NEW_STEP_SECS: tuple[float, ...] = (60.0, 600.0)

CRAM_MODE_PREVIEW = "preview"
CRAM_MODE_WRITEBACK = "writeback"


def maybe_round_in_days(secs: int) -> int:
    """Anki 换算「困难」间隔时用的取整：超过一天就取整天。"""
    if secs > DAY_SECS:
        return int(round(secs / DAY_SECS)) * DAY_SECS
    return secs


def new_card_steps_from_config(config: Any) -> list[float]:
    """从牌组设置里取出「新卡学习步骤」，换算成秒（拿不到就返回空列表）。

    兼容两种形状：Anki 的 JSON 字典（{"new": {"delays": [1, 10]}}）和
    protobuf 对象（.new.delays）。delays 的单位是分钟。
    """
    if config is None:
        return []
    if isinstance(config, Mapping):
        new = config.get("new")
    else:
        new = getattr(config, "new", None)
    if new is None:
        return []
    if isinstance(new, Mapping):
        delays = new.get("delays")
    else:
        delays = getattr(new, "delays", None)
    if not delays:
        return []
    out: list[float] = []
    for value in delays:
        try:
            seconds = float(value) * 60.0
        except Exception:
            continue
        if seconds > 0:
            out.append(seconds)
    return out


def cram_preview_delays(step_secs: Iterable[Any] | None = None) -> tuple[int, int, int]:
    """集中刷「不影响排期」用的三档延迟（秒）：重来 / 困难 / 良好。

    算法跟 Anki 自己的新卡按钮一致（见 rslib/src/scheduler/states/steps.rs）：

    * 重来 = 第一个学习步骤；
    * 困难 = 前两步的平均（只有一步时按第一步的 1.5 倍，最多再多一天）；
    * 良好 = 0，表示结束集中刷、带着原来的到期时间回原牌组
      （Anki 预览模式的官方语义，0 就是「送回原牌组」，简单键固定是 0）。
    """
    steps: list[float] = []
    for value in step_secs or ():
        try:
            number = float(value)
        except Exception:
            continue
        if number > 0:
            steps.append(number)
    if not steps:
        steps = list(DEFAULT_NEW_STEP_SECS)

    again = int(round(steps[0]))
    if len(steps) >= 2:
        hard = int(round((steps[0] + steps[1]) / 2.0))
    else:
        hard = int(min(steps[0] * 1.5, steps[0] + DAY_SECS))
    again = max(1, again)
    hard = max(again, maybe_round_in_days(max(1, hard)))
    return (again, hard, 0)


def cram_search_query(
    tags: Iterable[Any] | None, mode: Any = MODE_UNION, states: Any = DEFAULT_STATES
) -> str:
    """集中刷的收录条件：跟规则一样，只是临时忽略「是否到期」。"""
    return wanted_query(tags, mode, states)


def cram_blocked_query(
    tags: Iterable[Any] | None,
    mode: Any = MODE_UNION,
    states: Any = DEFAULT_STATES,
    exclude_deck_name: Any = None,
) -> str:
    """集中刷时仍然收不进来的卡：暂停/搁置的 + 已在别的筛选牌组里的。

    集中刷只顾着放宽「是否到期」，Anki 硬拼的
    `-is:suspended -is:buried -deck:filtered` 谁也绕不过去。
    """
    wanted = wanted_query(tags, mode, states)
    if not wanted:
        return ""
    other = and_query("deck:filtered", negate_term(ALWAYS_EXCLUDED_QUERY))
    if exclude_deck_name:
        other = and_query(other, negate_term(deck_search_term(exclude_deck_name)))
    return or_query(and_query(wanted, ALWAYS_EXCLUDED_QUERY), and_query(wanted, other))


# 临时集中刷牌组的名字后缀（1.4.0 起集中刷不再改原筛选牌组）
CRAM_TEMP_SUFFIX = "·集中刷"


def make_cram_session(
    writeback: Any = False,
    started_at: Any = 0,
    cram_deck_id: Any = 0,
    round_no: Any = 1,
) -> dict[str, Any]:
    """集中刷会话记录（存在集合配置里，按原牌组 ID 索引）。

    1.4.0 起集中刷用**独立的临时牌组**：cram_deck_id 记那个临时牌组的 ID，
    round 记这一次会话刷到第几轮（点「再刷一轮」就加一）。
    """
    return {
        "mode": CRAM_MODE_WRITEBACK if writeback else CRAM_MODE_PREVIEW,
        "started_at": _as_int(started_at, 0),
        "cram_deck_id": _as_int(cram_deck_id, 0),
        "round": max(1, _as_int(round_no, 1)),
    }


def normalize_cram_session(raw: Any) -> dict[str, Any] | None:
    """把历史配置里的会话记录补全；认不出来就返回 None（当作没在集中刷）。

    1.3.0 及更早的记录没有 cram_deck_id / round，这里补成 0 / 1；调用方遇到
    cram_deck_id=0 时走「兼容收尾」的老路（只按规则重收原牌组）。
    """
    if not isinstance(raw, Mapping):
        return None
    mode = str(raw.get("mode") or "")
    if mode not in (CRAM_MODE_PREVIEW, CRAM_MODE_WRITEBACK):
        return None
    return {
        "mode": mode,
        "started_at": _as_int(raw.get("started_at"), 0),
        "cram_deck_id": _as_int(raw.get("cram_deck_id"), 0),
        "round": max(1, _as_int(raw.get("round"), 1)),
    }


def cram_session_deck_id(session: Mapping[str, Any] | None) -> int:
    """这次集中刷用的临时牌组 ID；老记录或没有时返回 0。"""
    return _as_int((session or {}).get("cram_deck_id"), 0)


def cram_round(session: Mapping[str, Any] | None) -> int:
    """这一次集中刷会话已经刷到第几轮。"""
    return max(1, _as_int((session or {}).get("round"), 1))


def cram_writes_back(session: Mapping[str, Any] | None) -> bool:
    """这次集中刷会不会把作答写回原卡排期。"""
    return str((session or {}).get("mode")) == CRAM_MODE_WRITEBACK


def cram_label(session: Mapping[str, Any] | None) -> str:
    """管理窗口「状态」列里显示的集中刷文字。"""
    if not session:
        return ""
    if cram_writes_back(session):
        return "集中刷中（临时牌组·会写回排期）"
    return "集中刷中（临时牌组·不影响排期）"


def cram_temp_name(deck_name: Any) -> str:
    """临时集中刷牌组的名字：原牌组名后面加「·集中刷」，仍在同一父牌组下。"""
    text = str(deck_name or "").strip()
    if not text:
        return "集中刷"
    return text + CRAM_TEMP_SUFFIX


def cram_temp_deck_name(
    deck_name: Any,
    existing_names: Iterable[Any] | None = None,
    exclude: Any = None,
) -> str:
    """临时集中刷牌组的完整名字（重名时自动追加 (2)、(3)……）。"""
    return unique_deck_name(cram_temp_name(deck_name), existing_names, exclude=exclude)


def is_cram_temp_name(name: Any, original_name: Any) -> bool:
    """这个名字是不是某条规则的临时集中刷牌组（清理崩溃残留时用）。"""
    return str(name or "").strip() == cram_temp_name(original_name)


# --------------------------------------------------------------------------
# 集中刷「不影响排期」的延迟从哪来：按卡片自己的原牌组（1.5.0）
# --------------------------------------------------------------------------


def home_deck_id(record: Any) -> int:
    """卡片「自己的牌组」：在筛选牌组里用 odid（原牌组），否则用 did。"""
    odid = _as_int(_field(record, "odid", "original_deck_id"), 0)
    if odid:
        return odid
    return _as_int(_field(record, "did", "deck_id"), 0)


def pick_home_deck(
    records: Iterable[Any] | None,
    *,
    filtered_ids: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """从这批卡里挑出「延时按哪个牌组的设置算」——张数最多的那个原牌组。

    回归 1.5.0 修的问题：集中刷的「重来 / 困难」延迟原来读的是当前选中的牌组，
    于是卡片原牌组设定的学习步骤被忽略。按计划改成读**卡片自己的原牌组**：

    * 卡片在筛选牌组里时用 odid（原牌组），否则用 did；
    * 跳过筛选牌组（调用方通过 filtered_ids 传入牌组 ID 集合）；
    * 张数最多的胜出；张数相同时取 ID 较小的（保证结果稳定可测）；
    * 全是筛选牌组或没有卡时返回 deck_id=0，调用方据此退回 Anki 默认步骤。

    返回 {"deck_id", "count", "other_count", "other_decks"}：
    count 是来源牌组的张数，other_count 来自别的牌组的张数，
    other_decks 是别的牌组的个数（用来决定要不要提示「另有 N 张来自 M 个牌组」）。
    """
    skip = {_as_int(value, 0) for value in (filtered_ids or ())}
    skip.discard(0)
    tally: dict[int, int] = {}
    for record in records or ():
        deck_id = home_deck_id(record)
        if not deck_id or deck_id in skip:
            continue
        tally[deck_id] = tally.get(deck_id, 0) + 1
    if not tally:
        return {"deck_id": 0, "count": 0, "other_count": 0, "other_decks": 0}
    best_id = min(tally, key=lambda deck_id: (-tally[deck_id], deck_id))
    best_count = tally[best_id]
    total = sum(tally.values())
    return {
        "deck_id": int(best_id),
        "count": int(best_count),
        "other_count": int(total - best_count),
        "other_decks": int(max(0, len(tally) - 1)),
    }


# --------------------------------------------------------------------------
# 集中刷窗口：离开牌组时要不要问「还在集中刷，要结束吗」
# --------------------------------------------------------------------------

# 主窗口处在这几个状态时，才考虑「是不是已经离开集中刷那个牌组了」
CRAM_LEAVE_STATES: tuple[str, ...] = ("overview", "deckbrowser", "review")


def state_key(state: Any) -> str:
    """把 Anki 的界面状态名统一成小写字符串。

    Anki 26.09 的 MainWindowState 是字符串字面量（"deckBrowser" / "overview"…），
    这里额外兼容一下枚举或其它包装类型，避免以后上游换成枚举时静默失效。
    """
    value = getattr(state, "value", state)
    return str(value or "").strip().lower()


def should_ask_cram_end(
    new_state: Any,
    old_state: Any,
    selected_deck_id: Any,
    cram_deck_id: Any,
) -> bool:
    """离开集中刷牌组时，要不要弹「结束集中刷并恢复原样吗？」。

    判定规则（对应「关掉集中刷窗口＝结束集中刷」这套交互）：

    * 没有集中刷会话（cram_deck_id 为空）→ 不问；
    * 状态没变、或不在总览／复习／牌组列表这几类里 → 不问；
    * 回到牌组列表 → 问；
    * 停在集中刷牌组自己的总览／复习 → 不问（正常刷题）；
    * 切到别的牌组的总览／复习 → 问。
    """
    if not cram_deck_id:
        return False
    new = state_key(new_state)
    old = state_key(old_state)
    if not new or new == old:
        return False
    if new not in CRAM_LEAVE_STATES:
        return False
    if new == "deckbrowser":
        return True
    try:
        cram = int(cram_deck_id)
    except Exception:
        return False
    try:
        selected = int(selected_deck_id or 0)
    except Exception:
        return False
    if not selected:
        return False
    return selected != cram


# --------------------------------------------------------------------------
# 结束集中刷后该重画哪一页（1.5.2）
# --------------------------------------------------------------------------

# 主窗口处在这些页面时才需要显式重画；其它页面（统计、添加卡片…）不动
REDRAW_NONE = ""
REDRAW_DECK_BROWSER = "deckbrowser"
REDRAW_OVERVIEW = "overview"


def redraw_target(state: Any) -> str:
    """当前界面状态 → 结束集中刷后该重画哪一页。

    1.5.0 的坑就在这里：Anki 26.09 的状态字面量是 ``"deckBrowser"``（首字母大写），
    而插件内部统一转成小写来比对，两边永远不相等 → 牌组列表那一页从来不重画，
    于是在牌组界面上结束集中刷以后，列表里还留着已经删掉的临时牌组。

    这里统一只按小写比对（``state_key``），两种写法都判成牌组列表；认不出来的
    页面返回 ``REDRAW_NONE``，由调用方记一条日志，避免以后再静默失效。
    """
    key = state_key(state)
    if key == REDRAW_DECK_BROWSER:
        return REDRAW_DECK_BROWSER
    if key == REDRAW_OVERVIEW:
        return REDRAW_OVERVIEW
    return REDRAW_NONE


# --------------------------------------------------------------------------
# 后台操作返回值：能不能当 Anki 的 OpChanges 用（1.5.2）
# --------------------------------------------------------------------------

# 这些类型一看就不是「原生结果」：Anki 会去读 .changes，读到就抛
# AttributeError: 'list' object has no attribute 'changes'
_PLAIN_RESULT_TYPES = (
    list,
    tuple,
    set,
    frozenset,
    dict,
    str,
    bytes,
    bytearray,
    int,
    float,
    complex,
    bool,
)


def op_changes_like(value: Any) -> bool:
    """这个返回值能不能当 Anki 的 OpChanges 用。

    Anki 的 ``aqt.operations.on_op_finished()`` 会做
    ``changes = result if isinstance(result, OpChanges) else result.changes``，
    没有 ``.changes`` 又没有原生类型就直接抛异常、弹「遇到了问题」。

    合规的两种：

    * 带 ``.changes`` 的对象（``OpChangesWithCount`` / ``OpChangesWithId``…）；
    * protobuf 的 ``OpChanges`` 本体（有 ``SerializeToString`` / ``CopyFrom``）。

    ``list`` / ``tuple`` / ``int`` / ``None`` 一律判成不合规。
    """
    if value is None:
        return False
    if isinstance(value, _PLAIN_RESULT_TYPES):
        return False
    if getattr(value, "changes", None) is not None:
        return True
    return hasattr(value, "SerializeToString") or hasattr(value, "CopyFrom")


def first_op_changes(*values: Any) -> Any:
    """从一串原生结果里挑出第一个能当 OpChanges 用的；都没有就返回 None。"""
    for value in values:
        if op_changes_like(value):
            return value
    return None


# --------------------------------------------------------------------------
# crash.log：最后一次致命异常指向哪个插件
# --------------------------------------------------------------------------

# 一次「致命异常」以这样的行开头；Anki 每次崩溃都往 crash.log 追加一段栈
_CRASH_HEADER_RE = re.compile(
    r"^(?:Windows fatal exception|Fatal Python error).*$", re.MULTILINE
)
# 形如 addons21\interactive_quiz\__init__.py", line 1880 in ...
_ADDON_REF_RE = re.compile(
    r"addons21[\\/]+(?P<addon>[^\\/\"']+)[\\/]+(?P<file>[^\\/\"',]+)"
    r"[\"']?\s*,?\s*(?:line\s+(?P<line>\d+))?"
)


def crash_log_culprit(text: Any) -> dict[str, Any] | None:
    """从 crash.log 文本里解析出最后一次致命异常涉及的插件。

    只看最后一段致命异常；那一段里第一个出现的插件文件就是异常现场
    （栈是「最近调用在最前」，所以第一个插件栈帧最靠里）。
    最后一次异常里完全没有插件文件时返回 None——说明那次不是插件引起的。
    """
    content = str(text or "")
    if not content.strip():
        return None
    headers = list(_CRASH_HEADER_RE.finditer(content))
    if not headers:
        return None
    last_block = content[headers[-1].end() :]
    match = _ADDON_REF_RE.search(last_block)
    if match is None:
        return None
    try:
        line = int(match.group("line") or 0)
    except Exception:
        line = 0
    return {
        "addon": match.group("addon"),
        "file": match.group("file"),
        "line": line,
    }


def crash_culprit_label(found: Mapping[str, Any] | None, mtime: Any = 0) -> str:
    """把解析结果拼成给管理窗口看的一行字；解析不到就返回空字符串。"""
    if not found:
        return ""
    addon = str(found.get("addon") or "").strip()
    if not addon:
        return ""
    when = ""
    try:
        if mtime:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(mtime)))
    except Exception:
        when = ""
    tail = f" · {when}" if when else ""
    return f"最近一次致命异常指向 {addon}{tail}"


# --------------------------------------------------------------------------
# 牌组名
# --------------------------------------------------------------------------


def default_leaf_name(tags: Iterable[Any] | None, mode: Any = MODE_UNION) -> str:
    """根据标签生成一个可读的默认牌组名。"""
    cleaned = clean_tags(tags)
    if not cleaned:
        return "新筛选"
    if len(cleaned) == 1:
        name = cleaned[0]
    else:
        joiner = " + " if normalize_mode(mode) == MODE_UNION else " ∩ "
        name = joiner.join(cleaned)
    name = name.replace("::", "·").strip()
    if len(name) > 60:
        name = name[:57].rstrip() + "..."
    return name or "新筛选"


def normalize_parent(parent: Any) -> str:
    """把父牌组路径整理成 Anki 的点分路径。"""
    text = str(parent or "").strip()
    parts = [part.strip() for part in text.split("::") if part.strip()]
    return "::".join(parts)


def join_deck_name(parent: Any, leaf: Any) -> str:
    """拼出完整牌组名，允许叶子名本身带路径。"""
    parent_text = normalize_parent(parent)
    leaf_text = str(leaf or "").strip().strip(":")
    leaf_parts = [part.strip() for part in leaf_text.split("::") if part.strip()]
    leaf_text = "::".join(leaf_parts)
    if parent_text and leaf_text:
        if leaf_text == parent_text or leaf_text.startswith(parent_text + "::"):
            return leaf_text
        return parent_text + "::" + leaf_text
    return leaf_text or parent_text


def split_deck_name(name: Any) -> tuple[str, str]:
    """拆出父牌组路径和最后一段牌组名。"""
    parts = [part.strip() for part in str(name or "").split("::") if part.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    return "::".join(parts[:-1]), parts[-1]


def unique_deck_name(
    full_name: Any,
    existing_names: Iterable[Any] | None,
    exclude: Any = None,
) -> str:
    """重名时追加 (2)、(3)……，避免覆盖别的牌组。"""
    wanted = str(full_name or "").strip()
    excluded = "" if exclude is None else str(exclude).strip()
    existing = {
        str(name).strip()
        for name in (existing_names or [])
        if str(name).strip() and str(name).strip() != excluded
    }
    if wanted not in existing:
        return wanted

    parent, leaf = split_deck_name(wanted)
    prefix = (parent + "::") if parent else ""
    for index in range(2, 10000):
        candidate = f"{prefix}{leaf} ({index})"
        if candidate not in existing:
            return candidate
    return wanted


# --------------------------------------------------------------------------
# 规则结构
# --------------------------------------------------------------------------


def clamp_limit(value: Any) -> int:
    try:
        number = int(value)
    except Exception:
        number = DEFAULT_LIMIT
    return max(MIN_LIMIT, min(MAX_LIMIT, number))


def clamp_order(value: Any) -> int:
    try:
        number = int(value)
    except Exception:
        number = DEFAULT_ORDER
    allowed = {order for order, _label in ORDER_CHOICES}
    return number if number in allowed else DEFAULT_ORDER


def make_rule(
    *,
    deck_id: int = 0,
    tags: Iterable[Any] | None,
    mode: Any = MODE_UNION,
    states: Any = None,
    include_not_due: Any = None,
    scope: Any = None,
    parent: Any = DEFAULT_PARENT,
    leaf: Any = "",
    limit: Any = DEFAULT_LIMIT,
    order: Any = DEFAULT_ORDER,
    updated_at: int = 0,
) -> dict[str, Any]:
    """构造计划中约定保存的规则结构。

    states / include_not_due 是新字段；只给了老的 scope 时自动迁移。
    """
    cleaned = clean_tags(tags)
    mode_value = normalize_mode(mode)
    if states is None:
        if scope is not None:
            chosen = states_for_scope(scope)
            if include_not_due is None:
                include_not_due = include_not_due_for_scope(scope)
        else:
            chosen = list(DEFAULT_STATES)
    else:
        chosen = normalize_states(states) or list(DEFAULT_STATES)
    include_not_due_value = (
        DEFAULT_INCLUDE_NOT_DUE
        if include_not_due is None
        else bool(include_not_due)
    )

    parent_value = normalize_parent(parent)
    leaf_value = str(leaf or "").strip() or default_leaf_name(cleaned, mode_value)
    full_name = join_deck_name(parent_value, leaf_value)
    return {
        "version": 2,
        "deck_id": int(deck_id or 0),
        "tags": cleaned,
        "mode": mode_value,
        "states": chosen,
        "include_not_due": include_not_due_value,
        "include_subtags": True,
        "parent": parent_value,
        "leaf": leaf_value,
        "name": full_name,
        "order": clamp_order(order),
        "limit": clamp_limit(limit),
        "search": build_search_query(
            cleaned, mode_value, chosen, include_not_due_value
        ),
        "updated_at": int(updated_at or 0),
    }


def normalize_include_not_due(raw: Mapping[str, Any] | None) -> bool:
    """从规则里读出「是否连没到期的也收」，兼容老字段。

    * 1.5.0 起字段名是 include_not_due；
    * 1.4.0 及更早存的是反向的 due_only（True ＝ 只收到期），取反后迁移；
    * 两个都没有时用默认值（默认只收到期）。
    """
    if not isinstance(raw, Mapping):
        return bool(DEFAULT_INCLUDE_NOT_DUE)
    if raw.get("include_not_due") is not None:
        return bool(raw.get("include_not_due"))
    if raw.get("due_only") is not None:
        return include_not_due_from_due_only(raw.get("due_only"))
    return bool(DEFAULT_INCLUDE_NOT_DUE)


def normalize_rule(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """把历史配置补全成当前结构；字段不合法时返回 None。

    字段迁移规则：
    * 只有老 scope：due_new → 三个状态 + 只收到期；all → 三个状态 + 含未到期；
    * 只有 1.4.0 的 due_only：取反成 include_not_due（语义不变）。
    """
    if not isinstance(raw, Mapping):
        return None
    tags = clean_tags(raw.get("tags"))
    if not tags:
        return None
    try:
        deck_id_int = int(raw.get("deck_id") or 0)
    except Exception:
        deck_id_int = 0

    chosen = normalize_states(raw.get("states"))
    if chosen:
        include_not_due = normalize_include_not_due(raw)
    elif "scope" in raw:
        chosen = states_for_scope(raw.get("scope"))
        include_not_due = include_not_due_for_scope(raw.get("scope"))
    else:
        chosen = list(DEFAULT_STATES)
        include_not_due = normalize_include_not_due(raw)

    rule = make_rule(
        deck_id=deck_id_int,
        tags=tags,
        mode=raw.get("mode", MODE_UNION),
        states=chosen,
        include_not_due=include_not_due,
        parent=raw.get("parent", ""),
        leaf=raw.get("leaf", ""),
        limit=raw.get("limit", DEFAULT_LIMIT),
        order=raw.get("order", DEFAULT_ORDER),
        updated_at=raw.get("updated_at", 0),
    )
    for key in ("actual_search", "actual_limit", "actual_order", "actual_reschedule"):
        if key in raw:
            rule[key] = raw[key]
    # 「被 Anki 内置设置改过」这个标记是插件自己的状态，不属于 Anki 的字段，
    # 单独放行让它能存得进、也读得出来（否则写进配置就会被这里清掉）。
    if raw.get("externally_modified"):
        rule["externally_modified"] = True
    return rule


def rule_search(rule: Mapping[str, Any] | None) -> str:
    if not rule:
        return ""
    normalized = normalize_rule(rule)
    return str(normalized.get("search", "")) if normalized else ""


def rule_matches_actual(
    rule: Mapping[str, Any] | None,
    actual_search: Any,
    actual_limit: Any,
    actual_order: Any,
    actual_reschedule: Any,
) -> bool:
    """判断牌组当前的真实设置是否仍是插件上次写入的那套。"""
    normalized = normalize_rule(rule)
    if not normalized:
        return False

    expected_search = str(normalized.get("actual_search") or normalized.get("search") or "")
    expected_limit = int(normalized.get("actual_limit") or normalized.get("limit") or DEFAULT_LIMIT)
    expected_order = int(normalized.get("actual_order") or normalized.get("order") or DEFAULT_ORDER)
    expected_resched = bool(
        normalized.get("actual_reschedule")
        if "actual_reschedule" in normalized
        else True
    )
    return (
        str(actual_search or "") == expected_search
        and int(actual_limit or 0) == expected_limit
        and int(actual_order or 0) == expected_order
        and bool(actual_reschedule) == expected_resched
    )


def rule_summary(rule: Mapping[str, Any] | None) -> str:
    normalized = normalize_rule(rule)
    if not normalized:
        return "无效规则"
    tags = "、".join(normalized["tags"])
    mode = "并集" if normalized["mode"] == MODE_UNION else "交集"
    states = states_label(normalized["states"], normalized["include_not_due"])
    return f"{tags} · {mode} · {states}"


# --------------------------------------------------------------------------
# 命中数量与提示文案
# --------------------------------------------------------------------------


def empty_counts() -> dict[str, Any]:
    return {
        "matched": 0,
        "wanted": 0,
        "collectible": 0,
        "blocked": 0,
        "due_blocked": 0,
        "banned": 0,
        "other_filtered": 0,
        "states": {value: 0 for value in STATE_VALUES},
        "per_tag": {},
        # 「看收不进来的卡」要用的卡号列表（最多 MAX_BROWSE_CARDS 个）
        "blocked_ids": [],
        # 收不进来的总张数（可能比 blocked_ids 多，超出部分只在提示里说）
        "blocked_total": 0,
        "cram_collectible": 0,
        "cram_not_due": 0,
    }


def estimate_collectible(counts: Mapping[str, Any], limit: Any = DEFAULT_LIMIT) -> int:
    """预计能真正收进筛选牌组的张数。"""
    try:
        collectible = int(counts.get("collectible") or 0)
    except Exception:
        return 0
    return min(max(0, collectible), clamp_limit(limit))


def preview_lines(
    counts: Mapping[str, Any],
    tags: Iterable[Any] | None = None,
    mode: Any = MODE_UNION,
    states: Any = DEFAULT_STATES,
    limit: Any = DEFAULT_LIMIT,
) -> list[str]:
    """编辑器里的实时提示文字（纯函数，方便测试）。"""
    cleaned = clean_tags(tags)
    if not cleaned:
        return ["请先选择至少 1 个标签。"]

    matched = int(counts.get("matched") or 0)
    state_counts = counts.get("states") or {}
    collectible = estimate_collectible(counts, limit)

    lines: list[str] = [f"预计收录：{collectible} 张（上限 {clamp_limit(limit)}）"]
    detail = "、".join(
        f"{label} {int(state_counts.get(value) or 0)}" for value, label in STATE_CHOICES
    )
    lines.append(
        f"命中这些标签的卡共 {matched} 张（{detail}）；只有打勾的状态会被收进牌组。"
    )

    if not normalize_states(states):
        lines.append("至少要勾一个取卡范围状态（未学习／学习中／复习中）。")

    blocked = int(counts.get("blocked") or 0)
    if blocked > 0:
        parts: list[str] = []
        if int(counts.get("due_blocked") or 0):
            parts.append(f"还没到期的 {int(counts['due_blocked'])} 张")
        if int(counts.get("banned") or 0):
            parts.append(f"暂停或搁置的 {int(counts['banned'])} 张")
        if int(counts.get("other_filtered") or 0):
            parts.append(f"已在别的筛选牌组里的 {int(counts['other_filtered'])} 张")
        tail = ("：" + "、".join(parts)) if parts else "。"
        lines.append(f"另有 {blocked} 张这次收不进来{tail}")
        if int(counts.get("banned") or 0):
            lines.append(
                "（暂停/搁置的卡 Anki 不会收进筛选牌组，插件也不会改动它们的暂停状态；"
                "想学它们请先在浏览器里取消暂停或取消搁置。）"
            )

    if matched == 0:
        if len(cleaned) > 1 and normalize_mode(mode) == MODE_INTERSECTION:
            lines.append("没有卡片同时带这些标签（交集为空），牌组建出来会是空的。")
            per_tag = counts.get("per_tag") or {}
            for tag in cleaned:
                if tag in per_tag:
                    lines.append(f"　· 只有「{tag}」的卡：{int(per_tag[tag])} 张")
        else:
            lines.append("暂时没有卡片命中这些标签，牌组建出来会是空的。")
    return lines


def unused_orders() -> Sequence[tuple[int, str]]:
    """给界面用的顺序选项，返回副本避免调用方改到常量。"""
    return tuple(ORDER_CHOICES)
