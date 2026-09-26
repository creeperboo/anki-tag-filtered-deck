"""隔离集成测试探针（只用于开发验证，不随插件分发给用户）。

由 工具\run_anki_probe.py 复制到一个临时用户配置的 addons21\zz_probe\__init__.py，
再用 Anki.exe -b <临时配置> 启动。探针在真实 Anki 里跑一整套流程，把每步的断言结果
写成 JSON（路径由环境变量 TFD_PROBE_OUT 指定），然后关闭窗口。

它验证的是「插件代码 + 本机 Anki 26.09 的筛选牌组接口」配合起来的行为，
所以直接调用插件里的函数（_prepare_deck / collect_counts / on_state_did_change /
confirm_and_delete 等），而不是另写一套等价逻辑。

这一版的取卡范围是三个状态（未学习 / 学习中 / 复习中）加一个「连还没到期的也收」开关。
Anki 重建筛选牌组时会把搜索式强制拼成
`<你的条件> -is:suspended -is:buried -deck:filtered`，所以暂停 / 搁置的卡一定收不进来。
插件的承诺是**绝不改动这些卡的暂停 / 搁置状态**；本探针用「运行前后暂停集合、搁置集合、
逐张卡的 queue 快照三者都要一模一样」来守住这条承诺（这是本版最重要的一条回归）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from typing import Any, Callable

from anki.decks import DeckId, FilteredDeckConfig
from aqt import gui_hooks, mw
from aqt.qt import QMenu, QTimer

OUT = os.environ.get("TFD_PROBE_OUT") or os.path.join(
    tempfile.gettempdir(), "tfd_probe.json"
)

RESULT: dict[str, Any] = {"steps": [], "errors": [], "ok": False}
STATE: dict[str, Any] = {"step": "启动"}

# 样例卡片：名字 → 标签。建造过程见 step_sample_data。
SAMPLES: tuple[tuple[str, list[str]], ...] = (
    ("A-唐诗", ["唐诗"]),
    ("B-唐诗子标签", ["唐诗::格律"]),
    ("C-宋词", ["宋词"]),
    ("D-两标签", ["唐诗", "宋词"]),
    ("E-三标签", ["唐诗", "宋词", "重点"]),
    ("F-暂停", ["宋词"]),
    ("G-搁置", ["唐诗"]),
    ("H-学习中", ["宋词"]),
    ("I-复习未到期", ["宋词"]),
    ("J-复习到期", ["宋词"]),
    ("K-别的筛选", ["宋词", "别处"]),
)


# --------------------------------------------------------------------------
# 记录、断言、步骤调度
# --------------------------------------------------------------------------


def write_result() -> None:
    try:
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(RESULT, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def record(name: str, payload: dict[str, Any] | None = None) -> None:
    entry: dict[str, Any] = {}
    if payload:
        entry.update(payload)
    entry["name"] = name
    entry["step"] = STATE["step"]
    RESULT["steps"].append(entry)
    write_result()


def note(key: str, value: Any) -> None:
    STATE[key] = value


def check(name: str, condition: Any, detail: str = "") -> bool:
    RESULT["checks"] = int(RESULT.get("checks", 0)) + 1
    if condition:
        return True
    RESULT["errors"].append(
        f"[{STATE['step']}] {name}：{detail if detail else '断言不成立'}"
    )
    return False


STEP_QUEUE: list[tuple[str, Callable[[], None], int]] = []


def run_step(name: str, fn: Callable[[], None]) -> None:
    STATE["step"] = name
    try:
        fn()
    except Exception:
        RESULT["errors"].append(f"[{name}] 执行出错：{traceback.format_exc()}")
    write_result()
    if STEP_QUEUE:
        next_name, next_fn, next_delay = STEP_QUEUE.pop(0)
        QTimer.singleShot(next_delay, lambda: run_step(next_name, next_fn))
    else:
        finish()


def finish() -> None:
    RESULT["ok"] = not RESULT["errors"]
    RESULT["finished"] = True
    RESULT["anki_version"] = getattr(getattr(mw, "pm", None), "anki_version", "")
    write_result()
    QTimer.singleShot(300, mw.close)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def tfd() -> Any:
    module = sys.modules.get("tag_filtered_deck")
    if module is None:
        module = __import__("tag_filtered_deck")
    return module


def rl() -> Any:
    module = sys.modules.get("tag_filtered_deck.rule_logic")
    if module is None:
        from tag_filtered_deck import rule_logic

        module = rule_logic
    return module


def build_deck(col: Any, rule: dict) -> int:
    """和插件 apply_rule 的写入部分一样的调用序列（同步版）。"""
    module = tfd()
    deck = col.sched.get_or_create_filtered_deck(DeckId(int(rule["deck_id"])))
    module._prepare_deck(deck, rule)
    deck_id, _changes = module._write_filtered_deck(col, deck)
    actual = module.read_actual(col, deck_id)
    stored = dict(rule)
    stored["deck_id"] = deck_id
    if actual:
        stored["name"] = actual["name"]
        stored["actual_search"] = actual["search"]
        stored["actual_limit"] = actual["limit"]
        stored["actual_order"] = actual["order"]
        stored["actual_reschedule"] = actual["reschedule"]
    module.put_rule(stored, col)
    return deck_id


def cards_of(col: Any, deck_id: int) -> list[int]:
    return sorted(int(cid) for cid in col.decks.cids(DeckId(int(deck_id))))


def install_auto_spies() -> None:
    """只给探针用：记录自动重收这条后台链路到底走到了哪一步。"""
    module = tfd()
    if getattr(module, "_probe_spied", False):
        return
    orig_plan = module._on_auto_plan
    orig_err = module._error_handler
    orig_rebuild = module._do_rebuild

    def spy_plan(deck_id: int, result: Any) -> Any:
        record(
            "auto_plan_done",
            {
                "deck_id": int(deck_id),
                "action": str((result or {}).get("action")),
                "elapsed": round(time.time() - float(STATE.get("auto_t0") or time.time()), 2),
            },
        )
        return orig_plan(deck_id, result)

    def spy_err(parent: Any, on_error: Any) -> Any:
        inner = orig_err(parent, on_error)

        def handle(exc: Exception) -> None:
            record("auto_error", {"exc": repr(exc)})
            return inner(exc)

        return handle

    def spy_rebuild(collection: Any, deck_id: int) -> Any:
        out = orig_rebuild(collection, deck_id)
        record("auto_rebuild_call", {"deck_id": int(deck_id), "count": out})
        return out

    module._on_auto_plan = spy_plan
    module._error_handler = spy_err
    module._do_rebuild = spy_rebuild
    module._probe_spied = True


def install_op_result_spy() -> dict[str, Any]:
    """1.5.2：盯住每一个后台操作交给 Anki 的返回值。

    Anki 的 ``aqt.operations.on_op_finished()`` 会直接读 ``result.changes``：
    操作本身成功了，但返回 ``list`` / ``tuple`` / ``int`` 时它会抛
    ``AttributeError: 'list' object has no attribute 'changes'``，然后在界面上弹
    「遇到了问题…可能是由某个插件引起」。这里把那个函数包一层，先判定合不合规、
    记下来，再交给原函数（原函数照旧会抛，好让探针能观察到真实行为）。
    """
    from aqt import operations as aqt_operations

    existing = getattr(aqt_operations, "_probe_op_spy", None)
    if existing is not None:
        return existing
    spy: dict[str, Any] = {"checked": 0, "violations": [], "kinds": {}}
    original = aqt_operations.on_op_finished

    def wrapper(main_window: Any, result: Any, initiator: Any) -> None:
        spy["checked"] = int(spy["checked"]) + 1
        kind = type(result).__name__
        spy["kinds"][kind] = int(spy["kinds"].get(kind, 0)) + 1
        if not rl().op_changes_like(result):
            spy["violations"].append(
                {
                    "step": STATE.get("step"),
                    "type": kind,
                    "value": repr(result)[:200],
                }
            )
            record("op_result_violation", spy["violations"][-1])
        return original(main_window, result, initiator)

    aqt_operations.on_op_finished = wrapper
    aqt_operations._probe_op_spy = spy
    return spy


def baseline_queryop(label: str) -> None:
    """对照组：跑一个「只读一下卡数」的后台查询，量这个环境里后台操作本身要多久。

    探针环境（新配置、窗口可能没被激活）里后台操作偶尔会整体延迟十几到二十几秒，
    连这种空查询也一样慢；有了这个对照，才能证明慢的是环境而不是插件的链路。
    """
    from aqt.operations import QueryOp

    started = time.time()

    def op(collection: Any) -> int:
        return int(collection.card_count())

    def done(count: int) -> None:
        record(
            "queryop_baseline",
            {"label": label, "count": count, "elapsed": round(time.time() - started, 2)},
        )

    QueryOp(parent=mw, op=op, success=done).run_in_background()


def cid(text: str) -> int:
    return int(STATE["cards"][text])


def ids_of(*texts: str) -> list[int]:
    return sorted(cid(text) for text in texts)


def card_snapshot(col: Any, card_id: int) -> dict[str, Any]:
    """一张卡的关键原始状态；用来证明插件没有偷偷改动暂停 / 搁置状态。"""
    card = col.get_card(int(card_id))
    return {
        "type": int(card.type),
        "queue": int(getattr(card, "queue", -99)),
        "due": int(card.due),
        "ivl": int(card.ivl),
        "factor": int(getattr(card, "factor", 0)),
        "reps": int(card.reps),
        "did": int(card.did),
        "odid": int(card.odid),
    }


def answer_card(col: Any, card_id: int, ease: int) -> None:
    """模拟界面上的作答：先 getCard（记录开始时间）再 answerCard。

    ease：1=重来、2=困难、3=良好、4=简单（Anki 26.09 的 answerCard 就是按这个映射）。
    """
    card = col.get_card(int(card_id))
    if hasattr(card, "start_timer"):
        card.start_timer()
    else:  # pragma: no cover - 兼容老接口
        card.timer_started = time.time()
    col.sched.answerCard(card, ease)


def force_due_in_past(col: Any, card_id: int, seconds: int = 10) -> None:
    """把学习队列里的到期时间挪到过去，让这张卡确定算「已到期」。

    学习队列的 due 是 Unix 时间戳；界面上等一分钟自然也会到期，这里只是不等它。
    """
    card = col.get_card(int(card_id))
    card.due = int(time.time()) - int(seconds)
    col.update_card(card)


def make_review_card(col: Any, card_id: int) -> None:
    """把一张新卡变成「复习中」的卡（答「简单」通常会直接毕业，必要时再补一次）。"""
    for _ in range(3):
        if int(col.get_card(int(card_id)).type) == 2:
            break
        answer_card(col, card_id, 4)


def hook_names(hook: Any) -> list[str] | None:
    for attr in ("_hooks", "hooks", "_functions", "funcs", "_callbacks"):
        value = getattr(hook, attr, None)
        if isinstance(value, list):
            return [getattr(fn, "__name__", "?") for fn in value]
    if isinstance(hook, list):
        return [getattr(fn, "__name__", "?") for fn in hook]
    return None


def make_notetype(col: Any, name: str = "TFD探针") -> Any:
    """建一个字段名可控的模板，避免依赖界面语言下默认模板的名字。"""
    existing = col.models.by_name(name)
    if existing is not None:
        return existing
    notetype = col.models.new(name)
    col.models.add_field(notetype, col.models.new_field("正面"))
    col.models.add_field(notetype, col.models.new_field("背面"))
    template = col.models.new_template("卡片 1")
    template["qfmt"] = "{{正面}}"
    template["afmt"] = "{{背面}}"
    col.models.add_template(notetype, template)
    col.models.add(notetype)
    return col.models.by_name(name)


# --------------------------------------------------------------------------
# 各步骤
# --------------------------------------------------------------------------


def step_import() -> None:
    module = tfd()
    logic = rl()
    record(
        "import",
        {
            "module_file": getattr(module, "__file__", ""),
            "version": getattr(module, "__version__", ""),
            "logic_file": getattr(logic, "__file__", ""),
        },
    )
    check("插件模块可导入", bool(getattr(module, "__file__", "")))
    # 环境基线：开场先量一次「只读卡数」的后台查询，后面遇到慢操作时有对照
    baseline_queryop("startup")
    # 1.5.2：从这一刻起，每个后台操作交给 Anki 的返回值都过一遍检查
    spy = install_op_result_spy()
    record("op_result_spy_installed", {"checked": spy["checked"]})

    action = getattr(mw, "_tag_filtered_deck_menu", None)
    check("工具菜单已注册", action is not None, "mw._tag_filtered_deck_menu 为空")
    if action is not None:
        check("工具菜单文字正确", action.text() == "标签筛选牌组…", action.text())

    wanted = {
        "main_window_did_init": "install_menu",
        "profile_did_open": "on_profile_did_open",
        "state_did_change": "on_state_did_change",
        "deck_browser_will_show_options_menu": "on_deck_browser_menu",
        "browser_sidebar_will_show_context_menu": "on_sidebar_menu",
    }
    seen: dict[str, Any] = {}
    for hook_name, func_name in wanted.items():
        hook = getattr(gui_hooks, hook_name, None)
        if hook is None:
            check(f"钩子 {hook_name} 存在", False, "gui_hooks 上没有这个钩子")
            continue
        names = hook_names(hook)
        if names is None:
            seen[hook_name] = f"内部列表不可见，但已能注册（{func_name}）"
            continue
        seen[hook_name] = names
        check(f"{hook_name} 已挂上 {func_name}", func_name in names, str(names))
    record("hooks", seen)

    # 三状态口径 + 旧解禁机制必须彻底消失（本轮的核心约束）
    check(
        "取卡范围只有三个状态",
        tuple(logic.STATE_VALUES) == ("new", "learn", "review"),
        str(getattr(logic, "STATE_VALUES", None)),
    )
    legacy = [
        name
        for name in (
            "unban_for_rule",
            "restore_ids",
            "restore_rule_unbanned",
            "plan_unban",
            "restorable_ids",
            "unban_candidate_query",
            "_bury_manually",
        )
        if hasattr(module, name)
    ]
    check("旧解禁机制已彻底移除", not legacy, str(legacy))


def step_sample_data() -> None:
    col = mw.col
    logic = rl()
    note("source_deck_id", int(col.decks.id("源牌组")))
    src = STATE["source_deck_id"]
    # 默认模板的名字会随界面语言变化，所以自己建一个字段名确定的模板
    notetype = make_notetype(col)
    check("找到可用的模板", notetype is not None)
    fields = [str(f["name"]) for f in (notetype or {}).get("flds", [])]
    record("notetype", {"name": (notetype or {}).get("name"), "fields": fields})
    check("模板至少有 2 个字段", len(fields) >= 2, str(fields))
    note("fields", fields)

    note("cards", {})
    for text, tags in SAMPLES:
        new_note = col.new_note(notetype)
        for field in fields:
            new_note[field] = text
        new_note.tags = list(tags)
        col.add_note(new_note, DeckId(src))
        ids = new_note.card_ids()
        check(f"{text} 有 1 张卡", len(ids) == 1, str(list(ids)))
        STATE["cards"][text] = int(ids[0])

    # 暂停 / 搁置：这两类卡 Anki 一定不会收进筛选牌组，插件也不许动它们
    col.sched.suspend_cards([cid("F-暂停")])
    col.sched.bury_cards([cid("G-搁置")], manual=True)

    # 学习中且已到期（重来一次 → 学习队列，再把到期时间挪到过去）
    answer_card(col, cid("H-学习中"), 1)
    force_due_in_past(col, cid("H-学习中"))

    # 复习中：I 未到期、J 今天到期（都是官方接口把卡变成复习卡）
    make_review_card(col, cid("I-复习未到期"))
    col.sched.set_due_date([cid("I-复习未到期")], "3")
    make_review_card(col, cid("J-复习到期"))
    col.sched.set_due_date([cid("J-复习到期")], "0")

    # 已经在别的筛选牌组里的卡：K。它带着「宋词」标签，但 Anki 不会让两张筛选牌组抢同一张卡。
    other = col.sched.get_or_create_filtered_deck(DeckId(0))
    other.name = "别的筛选"
    other.allow_empty = True
    other.config.reschedule = False
    del other.config.search_terms[:]
    other.config.search_terms.append(
        FilteredDeckConfig.SearchTerm(
            search=logic.tag_search_term("别处"), limit=10, order=0
        )
    )
    other_id = int(col.sched.add_or_update_filtered_deck(other).id)
    col.sched.rebuild_filtered_deck(DeckId(other_id))
    note("other_deck_id", other_id)

    # 逐张卡的原始状态 + 各状态口径下的命中情况，出问题时能一眼看出是哪一步不对
    facts: dict[str, Any] = {}
    for text, _tags in SAMPLES:
        card_id = cid(text)
        snap = card_snapshot(col, card_id)
        snap.update(
            {
                "is:new": card_id in col.find_cards("is:new"),
                "is:learn": card_id in col.find_cards("is:learn"),
                "is:review -is:learn": card_id
                in col.find_cards("is:review -is:learn"),
                "is:due": card_id in col.find_cards("is:due"),
                "is:suspended": card_id in col.find_cards("is:suspended"),
                "is:buried": card_id in col.find_cards("is:buried"),
                "deck:filtered": card_id in col.find_cards("deck:filtered"),
            }
        )
        facts[text] = snap
    record("card_facts", facts)
    note("facts", facts)

    check("F 是暂停的", facts["F-暂停"]["is:suspended"] is True, str(facts["F-暂停"]))
    check("G 是搁置的", facts["G-搁置"]["is:buried"] is True, str(facts["G-搁置"]))
    check(
        "H 是学习中且已到期",
        facts["H-学习中"]["is:learn"] is True and facts["H-学习中"]["is:due"] is True,
        str(facts["H-学习中"]),
    )
    check(
        "I 是复习中且未到期",
        facts["I-复习未到期"]["is:review -is:learn"] is True
        and facts["I-复习未到期"]["is:due"] is False,
        str(facts["I-复习未到期"]),
    )
    check(
        "J 是复习中且已到期",
        facts["J-复习到期"]["is:review -is:learn"] is True
        and facts["J-复习到期"]["is:due"] is True,
        str(facts["J-复习到期"]),
    )
    check("K 已经在别的筛选牌组里", facts["K-别的筛选"]["deck:filtered"] is True)
    check(
        "暂停/搁置没有改变卡片类型（F、G 仍是未学习）",
        facts["F-暂停"]["is:new"] is True and facts["G-搁置"]["is:new"] is True,
    )

    note("queue_snapshot", {text: card_snapshot(col, cid(text)) for text, _t in SAMPLES})
    note("suspended_snapshot", sorted(int(x) for x in col.find_cards("is:suspended")))
    note("buried_snapshot", sorted(int(x) for x in col.find_cards("is:buried")))
    note("total_cards", col.card_count())

    # 并集 + 默认范围（三个状态都勾、只收到期的）的统计
    counts = rl_counts()
    note("union_counts", counts)
    record("sample_data", {"counts": counts, "total_cards": STATE["total_cards"]})

    check("并集命中 11 张", counts["matched"] == 11, str(counts["matched"]))
    check(
        "三个状态的张数相加正好等于命中总数",
        sum(int(counts["states"][key]) for key in ("new", "learn", "review"))
        == counts["matched"],
        str(counts["states"]),
    )
    check(
        "各状态张数（未学习 8、学习中 1、复习中 2）",
        counts["states"] == {"new": 8, "learn": 1, "review": 2},
        str(counts["states"]),
    )
    check(
        "暂停的 F 计在「未学习」里（暂停不改变卡片类型）",
        counts["states"]["new"] == 8,
        str(counts["states"]),
    )
    check("范围内 10 张（未到期的 I 被排除）", counts["wanted"] == 11 and counts["due_blocked"] == 1, str(counts))
    check("暂停/搁置 2 张收不进来", counts["banned"] == 2, str(counts))
    check("别的筛选牌组占着 1 张", counts["other_filtered"] == 1, str(counts))
    check("预计可收 7 张", counts["collectible"] == 7, str(counts))
    check("一共 4 张收不进来", counts["blocked"] == 4, str(counts))
    check(
        "估算函数与统计一致",
        logic.estimate_collectible(counts, 9999) == 7,
        str(logic.estimate_collectible(counts, 9999)),
    )
    check(
        "带唐诗的卡 5 张（含搁置的那张）",
        counts["per_tag"].get("唐诗") == 5,
        str(counts["per_tag"]),
    )


def rl_counts(states: Any = None, include_not_due: Any = False, tags: Any = None) -> dict:
    """默认按「唐诗 OR 宋词 + 三个状态 + 只收到期」统计。"""
    module = tfd()
    logic = rl()
    return module.collect_counts(
        mw.col,
        ["唐诗", "宋词"] if tags is None else tags,
        logic.MODE_UNION,
        logic.DEFAULT_STATES if states is None else states,
        include_not_due,
        None,
    )


def step_include_not_due_switch() -> None:
    """「连还没到期的也收」开关 + 单选某个状态时的收录范围。"""
    col = mw.col
    module = tfd()
    logic = rl()
    tags = ["宋词"]
    states = list(logic.DEFAULT_STATES)

    # 1.5.0：第一个布尔参数是正向的 include_not_due（True＝连没到期的也收）
    strict = module.collect_counts(col, tags, logic.MODE_UNION, states, False, None)
    loose = module.collect_counts(col, tags, logic.MODE_UNION, states, True, None)
    strict_q = logic.in_scope_query(tags, logic.MODE_UNION, states, False)
    loose_q = logic.in_scope_query(tags, logic.MODE_UNION, states, True)
    i_hit = col.find_cards(strict_q)
    l_hit = col.find_cards(loose_q)
    record(
        "include_not_due_switch",
        {
            "strict": strict,
            "loose": loose,
            "strict_search": strict_q,
            "loose_search": loose_q,
            "i_due_kept": cid("I-复习未到期") in i_hit,
            "i_loose_kept": cid("I-复习未到期") in l_hit,
        },
    )
    check("不勾「未到期」时，未到期的复习卡收不进来（预计 5 张）", strict["collectible"] == 5, str(strict))
    check("勾上「未到期」后它也能收（预计 6 张）", loose["collectible"] == 6, str(loose))
    check("不勾时未到期的卡算「收不进来」1 张", strict["due_blocked"] == 1, str(strict))
    check("勾上后就没有「未到期」这个原因了", loose["due_blocked"] == 0, str(loose))
    check("未到期的 I 在「不勾」时被排除", cid("I-复习未到期") not in i_hit, strict_q)
    check("未到期的 I 在「勾上」时被包含", cid("I-复习未到期") in l_hit, loose_q)
    check(
        "已到期的 J 两边都在范围内",
        cid("J-复习到期") in i_hit and cid("J-复习到期") in l_hit,
    )

    # 只勾一个状态时的搜索式
    new_only = module.collect_counts(
        col, tags, logic.MODE_UNION, [logic.STATE_NEW], False, None
    )
    learn_only = module.collect_counts(
        col, tags, logic.MODE_UNION, [logic.STATE_LEARN], False, None
    )
    review_only = module.collect_counts(
        col, tags, logic.MODE_UNION, [logic.STATE_REVIEW], False, None
    )
    review_loose = module.collect_counts(
        col, tags, logic.MODE_UNION, [logic.STATE_REVIEW], True, None
    )
    record(
        "single_state_scopes",
        {
            "new_only": new_only,
            "learn_only": learn_only,
            "review_only": review_only,
            "review_loose": review_loose,
        },
    )
    check("只勾「未学习」：5 张新卡，减掉暂停和别的筛选牌组 → 3 张", new_only["collectible"] == 3, str(new_only))
    check("只勾「未学习」时不受到期开关影响", logic.states_query([logic.STATE_NEW], True) == "(is:new)", logic.states_query([logic.STATE_NEW], True))
    check("只勾「学习中」：已到期的 H 能收进来", learn_only["collectible"] == 1, str(learn_only))
    check(
        "只勾「复习中」+ 只收到期：只收 J",
        review_only["collectible"] == 1 and review_only["due_blocked"] == 1,
        str(review_only),
    )
    check(
        "只勾「复习中」+ 含未到期：I、J 都收",
        review_loose["collectible"] == 2,
        str(review_loose),
    )


def step_union_deck() -> None:
    col = mw.col
    module = tfd()
    logic = rl()
    rule = logic.make_rule(
        deck_id=0,
        tags=["唐诗", "宋词"],
        mode=logic.MODE_UNION,
        states=list(logic.DEFAULT_STATES),
        include_not_due=False,
        parent="标签筛选",
        leaf="",
        limit=9999,
        order=logic.DEFAULT_ORDER,
    )
    deck_id = build_deck(col, rule)
    note("union_deck_id", deck_id)
    cards = cards_of(col, deck_id)
    expected = ids_of(
        "A-唐诗", "B-唐诗子标签", "C-宋词", "D-两标签", "E-三标签", "H-学习中", "J-复习到期"
    )
    actual = module.read_actual(col, deck_id)
    record(
        "union_deck",
        {
            "deck_id": deck_id,
            "deck_name": module._deck_name(col, deck_id),
            "cards": cards,
            "expected": expected,
            "actual": actual,
            "search": rule["search"],
            "excluded": {
                "F-暂停": cid("F-暂停") in cards,
                "G-搁置": cid("G-搁置") in cards,
                "I-复习未到期": cid("I-复习未到期") in cards,
                "K-别的筛选": cid("K-别的筛选") in cards,
            },
        },
    )
    check("并集牌组已建", deck_id != 0 and module._is_filtered(col, deck_id))
    check(
        "牌组名按标签生成",
        module._deck_name(col, deck_id) == "标签筛选::唐诗 + 宋词",
        module._deck_name(col, deck_id),
    )
    check("收录 7 张（含学习中、已到期复习卡）", cards == expected, f"{cards} != {expected}")
    check("暂停的卡收不进来", cid("F-暂停") not in cards, str(cards))
    check("搁置的卡收不进来", cid("G-搁置") not in cards, str(cards))
    check("未到期的复习卡收不进来", cid("I-复习未到期") not in cards, str(cards))
    check("已在别的筛选牌组里的卡收不进来", cid("K-别的筛选") not in cards, str(cards))
    check(
        "写进 Anki 的搜索式和生成的等价",
        bool(actual)
        and set(col.find_cards(actual["search"])) == set(col.find_cards(rule["search"])),
        f"{actual} vs {rule}",
    )
    record(
        "search_written",
        {
            "generated": rule["search"],
            "stored_by_anki": (actual or {}).get("search"),
        },
    )
    check("顺序=到期(6)", bool(actual) and actual["order"] == 6, str(actual))
    check("上限=9999", bool(actual) and actual["limit"] == 9999, str(actual))
    check(
        "作答影响原卡已开启",
        bool(actual) and actual["reschedule"] is True,
        str(actual),
    )
    check("只有一个搜索条件", bool(actual) and actual["terms"] == 1, str(actual))
    check(
        "规则与牌组一致",
        module.rule_matches_actual(
            module.get_rule(deck_id, col),
            actual["search"],
            actual["limit"],
            actual["order"],
            actual["reschedule"],
        ),
        str(module.get_rule(deck_id, col)),
    )

    menu = QMenu()
    module.on_deck_browser_menu(menu, deck_id)
    labels = [action.text() for action in menu.actions() if action.text()]
    record("menu_managed", {"labels": labels})
    check("菜单含编辑", "编辑筛选规则…" in labels, str(labels))
    check("菜单含重建", "立即重建" in labels, str(labels))
    check("菜单含删除", "删除筛选（保留卡片）" in labels, str(labels))

    plain_menu = QMenu()
    module.on_deck_browser_menu(plain_menu, STATE["source_deck_id"])
    plain_labels = [a.text() for a in plain_menu.actions() if a.text()]
    record("menu_plain_deck", {"labels": plain_labels})
    check("普通牌组没有插件菜单", not plain_labels, str(plain_labels))

    other_menu = QMenu()
    module.on_deck_browser_menu(other_menu, int(STATE["other_deck_id"]))
    other_labels = [a.text() for a in other_menu.actions() if a.text()]
    record("menu_unmanaged", {"labels": other_labels})
    check(
        "未托管筛选牌组显示接管入口",
        other_labels == ["用标签规则接管这个筛选牌组…"],
        str(other_labels),
    )

    try:
        from aqt.browser.sidebar.item import SidebarItem, SidebarItemType

        item = SidebarItem("唐诗", "", item_type=SidebarItemType.TAG)
        sidebar_menu = QMenu()
        module.on_sidebar_menu(None, sidebar_menu, item, None)
        sidebar_labels = [a.text() for a in sidebar_menu.actions() if a.text()]
        record("menu_sidebar", {"labels": sidebar_labels})
        check(
            "标签右键有创建入口",
            sidebar_labels == ["用它创建标签筛选牌组…"],
            str(sidebar_labels),
        )
    except Exception as exc:
        record("menu_sidebar", {"error": repr(exc)})
        check("侧边栏标签右键入口", False, repr(exc))


def step_other_deck_cleanup() -> None:
    """把「别的筛选」牌组撤掉，让 K 回到源牌组，后面的步骤都在全部 11 张卡上跑。"""
    col = mw.col
    other_id = int(STATE["other_deck_id"])
    col.decks.remove([DeckId(other_id)])
    k = cid("K-别的筛选")
    record(
        "other_deck_cleanup",
        {
            "deck_gone": not bool(col.decks.get(DeckId(other_id), default=False)),
            "k_did": int(col.get_card(k).did),
            "source_deck_id": STATE["source_deck_id"],
        },
    )
    check("别的筛选牌组已撤掉", not bool(col.decks.get(DeckId(other_id), default=False)))
    check("K 回到源牌组", int(col.get_card(k).did) == STATE["source_deck_id"])


def step_dialogs() -> None:
    """两个窗口只做「能构造、能填表」的冒烟测试，不真的显示出来。"""
    module = tfd()
    logic = rl()

    class _EnumLike:
        value = "overview"

    record(
        "state_names",
        {
            "overview": module._state_name("overview"),
            "deckBrowser": module._state_name("deckBrowser"),
            "enum_like": module._state_name(_EnumLike()),
        },
    )
    check("状态名大小写兼容", module._state_name("DeckBrowser") == "deckbrowser")
    check("状态名兼容枚举包装", module._state_name(_EnumLike()) == "overview")

    editor = None
    picker = None
    manager = None
    try:
        editor = module.RuleEditor(mw, preset_tags=["唐诗"])
        tags = editor._tags()
        parent = editor._parent()
        built = editor._build_rule()
        record(
            "editor",
            {
                "tags": tags,
                "parent": parent,
                "leaf": editor._leaf(),
                "name": built["name"],
                "search": built["search"],
                "states": editor._states(),
                "include_not_due": editor._include_not_due(),
                "order": built["order"],
                "limit": built["limit"],
                "order_values": list(editor._order_values),
                "state_boxes": sorted(editor.state_boxes),
            },
        )
        check("编辑器预选了标签", tags == ["唐诗"], str(tags))
        check("编辑器默认父牌组", parent == "标签筛选", parent)
        check("编辑器生成搜索式", "tag:" in str(built["search"]), str(built["search"]))
        check(
            "编辑器默认勾选三个状态",
            editor._states() == ["new", "learn", "review"],
            str(editor._states()),
        )
        check("编辑器默认只收到期", editor._include_not_due() is False)
        check("编辑器没有暂停/搁置选项", "suspended" not in editor.state_boxes and "buried" not in editor.state_boxes, str(sorted(editor.state_boxes)))
        check("编辑器默认顺序=到期(6)", built["order"] == 6, str(built["order"]))
        check("编辑器默认上限=9999", built["limit"] == 9999, str(built["limit"]))
        check("顺序列表非空", bool(editor._order_values), str(editor._order_values))

        # 已选标签面板：搜索过滤 + 一键加入
        editor.avail_search.setText("唐诗")
        filtered = [
            editor.avail_list.item(row).text()
            for row in range(editor.avail_list.count())
        ]
        record("tag_panel_filter", {"keyword": "唐诗", "items": filtered})
        check(
            "已有标签面板按关键字过滤",
            filtered == ["唐诗", "唐诗::格律"],
            str(filtered),
        )

        picker = module.RuleEditor(mw)
        total = picker.avail_list.count()
        picker.avail_search.setText("宋词")
        picker.avail_list.setCurrentRow(0)
        picker._on_avail_add_clicked()
        record(
            "tag_panel_add",
            {
                "all_tags": total,
                "after_filter": picker.avail_list.count(),
                "tags": picker._tags(),
                "filter_fn": logic.filter_tag_choices(["唐诗", "宋词"], "宋"),
            },
        )
        check("已有标签面板列出了全部标签", total >= 5, str(total))
        check("点「加入所选」后标签进了已选列表", picker._tags() == ["宋词"], str(picker._tags()))
    except Exception:
        RESULT["errors"].append(f"[dialogs] 编辑器出错：{traceback.format_exc()}")

    try:
        deck_id = STATE["union_deck_id"]
        manager = module.ManagerDialog(mw)
        manager._populate(
            [
                {
                    "deck_id": deck_id,
                    "name": module._deck_name(mw.col, deck_id),
                    "rule": module.get_rule(deck_id, mw.col) or {},
                    "exists": True,
                    "filtered": True,
                    "managed": True,
                    "cards": len(cards_of(mw.col, deck_id)),
                }
            ]
        )
        row = manager.tree.topLevelItem(0)
        record(
            "manager",
            {
                "rows": manager.tree.topLevelItemCount(),
                "columns": manager.tree.columnCount(),
                "scope_column": row.text(2) if row else "",
                "status": row.text(6) if row else "",
            },
        )
        check("管理窗口能填表", manager.tree.topLevelItemCount() == 1)
        check("管理窗口 7 列", manager.tree.columnCount() == 7)
        check("管理窗口显示取卡范围", bool(row) and row.text(2) == "未学习+学习中+复习中", row.text(2) if row else "")
        check(
            "托管状态的牌组显示正常",
            bool(row) and row.text(6) == "正常",
            row.text(6) if row else "",
        )
        record(
            "manager_options",
            {
                "cram_button": manager.cram_button.text(),
                "auto_rebuild": bool(manager.auto_rebuild_check.isChecked()),
                "update_check": bool(manager.update_check_check.isChecked()),
                "anki_update": bool(manager.anki_update_check.isChecked()),
                "update_button": manager.update_button.text(),
                "rebuild_button": manager.rebuild_button.text(),
            },
        )
        check(
            "管理窗口有「集中刷…」按钮",
            manager.cram_button.text() == "集中刷…",
            manager.cram_button.text(),
        )
        check(
            "管理窗口有自动重收开关（默认开）",
            manager.auto_rebuild_check.isChecked() is True,
            str(manager.auto_rebuild_check.isChecked()),
        )
        check(
            "管理窗口有「启动时自动检查更新」开关",
            "启动时自动检查更新" in manager.update_check_check.text(),
            manager.update_check_check.text(),
        )
        check(
            "管理窗口有「让 Anki 也自动检查插件更新」开关",
            "让 Anki" in manager.anki_update_check.text(),
            manager.anki_update_check.text(),
        )
    except Exception:
        RESULT["errors"].append(f"[dialogs] 管理窗口出错：{traceback.format_exc()}")

    for dialog in (editor, picker, manager):
        try:
            if dialog is not None:
                dialog.reject()
        except Exception:
            pass


def step_subtag_deck() -> None:
    col = mw.col
    module = tfd()
    logic = rl()
    # 先把并集牌组清空：Anki 不会把已经在别的筛选牌组里的卡再收一次
    col.sched.empty_filtered_deck(DeckId(STATE["union_deck_id"]))
    rule = logic.make_rule(
        deck_id=0,
        tags=["唐诗"],
        mode=logic.MODE_UNION,
        states=list(logic.DEFAULT_STATES),
        include_not_due=True,
        parent="标签筛选",
        leaf="唐诗-全部",
        limit=9999,
        order=logic.DEFAULT_ORDER,
    )
    deck_id = build_deck(col, rule)
    note("subtag_deck_id", deck_id)
    cards = cards_of(col, deck_id)
    expected = ids_of("A-唐诗", "B-唐诗子标签", "D-两标签", "E-三标签")
    record(
        "subtag_deck",
        {
            "deck_id": deck_id,
            "cards": cards,
            "expected": expected,
            "search": rule["search"],
        },
    )
    check("含未到期的搜索式不含 is:due", "is:due" not in rule["search"], rule["search"])
    check("层级标签自动包含子标签", cid("B-唐诗子标签") in cards, str(cards))
    check("所有匹配卡范围收录 4 张（搁置被排除）", cards == expected, str(cards))


def step_intersection_deck() -> None:
    col = mw.col
    module = tfd()
    logic = rl()
    col.sched.empty_filtered_deck(DeckId(STATE["subtag_deck_id"]))
    counts = module.collect_counts(
        col,
        ["唐诗", "宋词"],
        logic.MODE_INTERSECTION,
        logic.DEFAULT_STATES,
        False,
        None,
    )
    rule = logic.make_rule(
        deck_id=0,
        tags=["唐诗", "宋词"],
        mode=logic.MODE_INTERSECTION,
        states=list(logic.DEFAULT_STATES),
        include_not_due=True,
        parent="标签筛选",
        leaf="",
        limit=9999,
        order=logic.DEFAULT_ORDER,
    )
    deck_id = build_deck(col, rule)
    note("intersection_deck_id", deck_id)
    cards = cards_of(col, deck_id)
    expected = ids_of("D-两标签", "E-三标签")
    record(
        "intersection_deck",
        {
            "deck_id": deck_id,
            "deck_name": module._deck_name(col, deck_id),
            "cards": cards,
            "expected": expected,
            "counts": counts,
            "search": rule["search"],
        },
    )
    check("交集搜索式含 AND", " AND " in rule["search"], rule["search"])
    check(
        "交集牌组名用 ∩",
        module._deck_name(col, deck_id) == "标签筛选::唐诗 ∩ 宋词",
        module._deck_name(col, deck_id),
    )
    check("交集收录 2 张", cards == expected, f"{cards} != {expected}")
    check("交集命中 2 张", counts["matched"] == 2, str(counts["matched"]))

    empty = logic.empty_counts()
    empty["per_tag"] = {"唐诗": 5, "不存在的标签": 0}
    lines = logic.preview_lines(
        empty,
        ["唐诗", "不存在的标签"],
        logic.MODE_INTERSECTION,
        logic.DEFAULT_STATES,
        9999,
    )
    record("empty_intersection", {"lines": lines})
    check("空交集有提示", any("交集为空" in line for line in lines), str(lines))


def step_answer() -> None:
    col = mw.col
    # 把交集牌组清空，再把并集牌组收满，然后在筛选牌组里作答
    col.sched.empty_filtered_deck(DeckId(STATE["intersection_deck_id"]))
    col.sched.rebuild_filtered_deck(DeckId(STATE["union_deck_id"]))
    before_cards = cards_of(col, STATE["union_deck_id"])
    record("union_before_answer", {"cards": before_cards})
    check(
        "重建后并集牌组有 8 张（K 已经回到源牌组）",
        before_cards
        == ids_of(
            "A-唐诗",
            "B-唐诗子标签",
            "C-宋词",
            "D-两标签",
            "E-三标签",
            "H-学习中",
            "J-复习到期",
            "K-别的筛选",
        ),
        str(before_cards),
    )

    target = cid("A-唐诗")
    before = col.get_card(target)
    snap = {
        "id": int(before.id),
        "nid": int(before.nid),
        "did": int(before.did),
        "odid": int(before.odid),
        "type": int(before.type),
        "due": int(before.due),
        "ivl": int(before.ivl),
        "reps": int(before.reps),
    }
    tag_before = len(col.find_cards('tag:"唐诗"'))
    total_before = col.card_count()
    revlog_before = int(col.db.scalar("select count() from revlog") or 0)
    error = ""
    tb = ""
    try:
        answer_card(col, target, 4)
    except Exception as exc:
        error = repr(exc)
        tb = traceback.format_exc()
    after = col.get_card(target)
    snap_after = {
        "id": int(after.id),
        "nid": int(after.nid),
        "did": int(after.did),
        "odid": int(after.odid),
        "type": int(after.type),
        "due": int(after.due),
        "ivl": int(after.ivl),
        "reps": int(after.reps),
    }
    record(
        "answer_in_filtered_deck",
        {
            "before": snap,
            "after": snap_after,
            "error": error,
            "tag_before": tag_before,
            "tag_after": len(col.find_cards('tag:"唐诗"')),
            "total_before": total_before,
            "total_after": col.card_count(),
            "revlog_before": revlog_before,
            "revlog_after": int(col.db.scalar("select count() from revlog") or 0),
            "deck_cards_after": cards_of(col, STATE["union_deck_id"]),
            "traceback": tb,
        },
    )
    check("作答没有报错", not error, tb or error)
    check("还是同一张卡", snap_after["id"] == snap["id"])
    check("还挂在同一张笔记上", snap_after["nid"] == snap["nid"])
    check("复习次数 +1", snap_after["reps"] == snap["reps"] + 1, str(snap_after))
    check("到期时间被改动", snap_after["due"] != snap["due"], f"{snap} -> {snap_after}")
    check(
        "写回了原卡（间隔/类型变化）",
        snap_after["ivl"] != snap["ivl"] or snap_after["type"] != snap["type"],
        f"{snap} -> {snap_after}",
    )
    check("卡片总数不变", col.card_count() == total_before)
    check(
        "这次作答记进了这张卡自己的复习日志",
        int(col.db.scalar("select count() from revlog") or 0) == revlog_before + 1,
        f"{revlog_before} -> {col.db.scalar('select count() from revlog')}",
    )
    # Anki 原生行为：筛选牌组开着「按作答重排原卡」时，答完的卡当场送回原牌组，
    # 不再挂在筛选牌组里（所以筛选牌组会随着学习变空，靠重建再收）。
    check(
        "作答后卡片回到源牌组",
        snap_after["did"] == STATE["source_deck_id"],
        str(snap_after),
    )
    check("不再挂在筛选牌组里", snap_after["odid"] == 0, str(snap_after))
    check("答完的卡已从筛选牌组里移走", target not in cards_of(col, STATE["union_deck_id"]))
    check(
        "刚学完的卡掉出「只收待学和新卡」范围",
        target
        not in col.find_cards(
            rl().in_scope_query(
                ["唐诗", "宋词"],
                "union",
                rl().DEFAULT_STATES,
                rl().DEFAULT_INCLUDE_NOT_DUE,
            )
        ),
    )


def step_auto_rebuild() -> None:
    col = mw.col
    deck_id = STATE["union_deck_id"]
    module = tfd()
    install_auto_spies()
    # 别的筛选牌组也清空，免得它们的卡被 Anki 排除在外
    for key in ("subtag_deck_id", "intersection_deck_id"):
        other = STATE.get(key)
        if other:
            col.sched.empty_filtered_deck(DeckId(int(other)))
    col.sched.empty_filtered_deck(DeckId(deck_id))
    note("auto_before", cards_of(col, deck_id))
    rule = module.get_rule(deck_id, col) or {}
    diag: dict[str, Any] = {
        "auto_enabled": module.auto_rebuild_enabled(),
        "selected": int(col.decks.selected()),
        "deck_id": int(deck_id),
        "rule_present": bool(rule),
        "cram_active": module.cram_active(deck_id, col),
        "external_flag": bool(rule.get("externally_modified")),
        "rule_search": rule.get("search"),
        "rule_actual_search": rule.get("actual_search"),
        "rule_actual_limit": rule.get("actual_limit"),
    }
    try:
        diag["plan"] = dict(module._auto_plan(col, deck_id, rule)) if rule else None
    except Exception as exc:
        diag["plan_error"] = repr(exc)
    note("auto_t0", time.time())
    baseline_queryop("auto_rebuild")
    col.decks.select(DeckId(deck_id))
    diag["selected_after"] = int(col.decks.selected())
    record("auto_diag", diag)
    tfd().on_state_did_change("overview", "deckBrowser")


def step_auto_rebuild_check() -> None:
    col = mw.col
    deck_id = STATE["union_deck_id"]
    cards = cards_of(col, deck_id)
    attempt = int(STATE.get("auto_rebuild_attempt", 0)) + 1
    STATE["auto_rebuild_attempt"] = attempt
    record(
        "auto_rebuild",
        {"attempt": attempt, "before": STATE.get("auto_before"), "after": cards},
    )
    expected = ids_of(
        "B-唐诗子标签", "C-宋词", "D-两标签", "E-三标签", "H-学习中", "J-复习到期", "K-别的筛选"
    )
    if cards != expected and attempt < 120:
        # 后台的「判断 → 重建」是两段异步操作；探针环境里后台操作本身就可能延迟
        # 十几到二十几秒（空查询也一样慢，见 queryop_baseline），所以这里轮询等它。
        # 上限放到 120 次（约 3 分钟）：成品包那一轮里也出现过后台操作要 1 分多钟才
        # 落地的情况，40 次会误报成失败。
        STEP_QUEUE.insert(0, ("auto_rebuild_check", step_auto_rebuild_check, 1500))
        return
    check("进入牌组时自动重收（空牌组被填回）", cards == expected, str(cards))
    check("刚学完的卡没有回来", cid("A-唐诗") not in cards, str(cards))


def step_no_rebuild_from_review() -> None:
    """从学习界面返回总览页不应该触发重收（计划里明确要求）。"""
    col = mw.col
    deck_id = STATE["union_deck_id"]
    col.sched.empty_filtered_deck(DeckId(deck_id))
    note("review_return_before", cards_of(col, deck_id))
    col.decks.select(DeckId(deck_id))
    tfd().on_state_did_change("overview", "review")


def step_no_rebuild_from_review_check() -> None:
    col = mw.col
    deck_id = STATE["union_deck_id"]
    cards = cards_of(col, deck_id)
    record(
        "no_rebuild_from_review",
        {"before": STATE.get("review_return_before"), "after": cards},
    )
    check("从学习界面返回总览不会重收", cards == [], str(cards))


def step_external_change() -> None:
    col = mw.col
    deck_id = STATE["union_deck_id"]
    deck = col.sched.get_or_create_filtered_deck(DeckId(deck_id))
    deck.config.search_terms[0].limit = 5
    col.sched.add_or_update_filtered_deck(deck)
    col.sched.empty_filtered_deck(DeckId(deck_id))
    note("external_before", cards_of(col, deck_id))
    col.decks.select(DeckId(deck_id))
    tfd().on_state_did_change("overview", "deckBrowser")


def step_external_change_check() -> None:
    col = mw.col
    module = tfd()
    deck_id = STATE["union_deck_id"]
    cards = cards_of(col, deck_id)
    rule = module.get_rule(deck_id, col) or {}
    actual = module.read_actual(col, deck_id) or {}
    attempt = int(STATE.get("external_attempt", 0)) + 1
    note("external_attempt", attempt)
    record(
        "external_change",
        {
            "attempt": attempt,
            "before": STATE.get("external_before"),
            "after": cards,
            "externally_modified": bool(rule.get("externally_modified")),
            "limit_now": actual.get("limit"),
            "search_now": actual.get("search"),
            "stored_actual_limit": rule.get("actual_limit"),
            "stored_actual_search": rule.get("actual_search"),
        },
    )
    settled = bool(rule.get("externally_modified")) or bool(cards)
    if not settled and attempt < 60:
        # 后台判断还没回来，再等一轮（成品包那一轮里后台操作曾慢到 1 分多钟）
        STEP_QUEUE.insert(
            0, ("external_change_check", step_external_change_check, 1500)
        )
        return
    check("被改过就不自动重收", cards == [], str(cards))
    check("规则被标记为外部修改", bool(rule.get("externally_modified")), str(rule))
    check("没有偷偷覆盖用户的改动", actual.get("limit") == 5, str(actual))


def step_move() -> None:
    """移动 = 把牌组重命名到新的「父牌组::子牌组」路径。"""
    col = mw.col
    module = tfd()
    deck_id = STATE["union_deck_id"]
    rule = dict(module.get_rule(deck_id, col) or {})
    rule["deck_id"] = deck_id
    rule["parent"] = "标签筛选::里层"
    rule["leaf"] = "唐诗 + 宋词"
    rule["name"] = "标签筛选::里层::唐诗 + 宋词"
    note("cards_before_move", cards_of(col, deck_id))
    note("names_before_move", module._deck_names(col, include_filtered=True))
    module.apply_rule(rule, parent=mw)


def step_move_check() -> None:
    col = mw.col
    module = tfd()
    deck_id = STATE["union_deck_id"]
    name = module._deck_name(col, deck_id)
    cards = cards_of(col, deck_id)
    rule = module.get_rule(deck_id, col) or {}
    names = module._deck_names(col, include_filtered=True)
    record(
        "move",
        {
            "name": name,
            "stored_name": rule.get("name"),
            "stored_parent": rule.get("parent"),
            "cards": cards,
            "old_name_gone": "标签筛选::唐诗 + 宋词" not in names,
            "new_name_count": names.count("标签筛选::里层::唐诗 + 宋词"),
        },
    )
    expected = ids_of(
        "B-唐诗子标签", "C-宋词", "D-两标签", "E-三标签", "H-学习中", "J-复习到期", "K-别的筛选"
    )
    check(
        "牌组被移到新路径",
        name == "标签筛选::里层::唐诗 + 宋词",
        name,
    )
    check(
        "没有留下重复牌组（是改名不是新建）",
        "标签筛选::唐诗 + 宋词" not in names and names.count(name) == 1,
        str(names),
    )
    check("规则里的名字跟着更新", rule.get("name") == name, str(rule))
    check("移动后卡照旧按规则收回来", cards == expected, str(cards))


def step_delete() -> None:
    col = mw.col
    module = tfd()
    calls: dict[str, list[str]] = {"warning": [], "info": [], "ask": []}
    note(
        "dialogs",
        {
            "showWarning": module.showWarning,
            "showInfo": module.showInfo,
            "askUser": module.askUser,
            "tooltip": module.tooltip,
        },
    )
    module.showWarning = lambda text, **kw: calls["warning"].append(str(text))
    module.showInfo = lambda text, **kw: calls["info"].append(str(text))
    module.tooltip = lambda *a, **kw: None

    module.askUser = lambda *a, **kw: True
    module.confirm_and_delete(STATE["source_deck_id"], parent=mw)
    record(
        "delete_protection_normal_deck",
        {
            "warning": calls["warning"],
            "deck_still_there": bool(
                col.decks.get(DeckId(STATE["source_deck_id"]), default=False)
            ),
        },
    )
    check("删普通牌组被拒绝", bool(calls["warning"]), str(calls["warning"]))
    check(
        "普通牌组还在",
        bool(col.decks.get(DeckId(STATE["source_deck_id"]), default=False)),
    )

    module.askUser = lambda *a, **kw: False
    module.confirm_and_delete(STATE["union_deck_id"], parent=mw)
    check(
        "点取消就不删",
        bool(col.decks.get(DeckId(STATE["union_deck_id"]), default=False)),
    )

    note("cards_before_delete", col.card_count())
    module.askUser = lambda text, **kw: (calls["ask"].append(str(text)) or True)
    module.confirm_and_delete(STATE["union_deck_id"], parent=mw)
    record("delete_requested", {"ask": calls["ask"]})
    last = calls["ask"][-1] if calls["ask"] else ""
    check("确认框写明了只删筛选", "只删除这次筛选" in last, last)
    check("确认框不再提「恢复暂停/搁置」（解禁机制已删除）", "恢复" not in last and "取消暂停" not in last, last)


def step_delete_check() -> None:
    col = mw.col
    module = tfd()
    logic = rl()
    for key, value in (STATE.get("dialogs") or {}).items():
        setattr(module, key, value)

    deck_id = STATE["union_deck_id"]
    sample = [col.get_card(c) for c in STATE["cards"].values()]
    record(
        "delete_result",
        {
            "deck_gone": not bool(col.decks.get(DeckId(deck_id), default=False)),
            "rule_gone": module.get_rule(deck_id, col) is None,
            "cards_before": STATE.get("cards_before_delete"),
            "cards_after": col.card_count(),
            "card_decks": sorted({int(c.did) for c in sample}),
            "odids": sorted({int(c.odid) for c in sample}),
        },
    )
    check("筛选牌组被删掉", not bool(col.decks.get(DeckId(deck_id), default=False)))
    check("规则被清掉", module.get_rule(deck_id, col) is None)
    check(
        "卡片总数不变",
        col.card_count() == STATE.get("cards_before_delete"),
        f"{STATE.get('cards_before_delete')} -> {col.card_count()}",
    )
    check(
        "卡片都回到源牌组",
        all(int(c.did) == STATE["source_deck_id"] for c in sample),
        str(sorted({int(c.did) for c in sample})),
    )
    check(
        "odid 已清空",
        all(int(c.odid) == 0 for c in sample),
        str(sorted({int(c.odid) for c in sample})),
    )

    module.put_rule(
        {
            "version": 1,
            "deck_id": 987654321,
            "tags": ["不存在的"],
            "mode": "union",
            "scope": "due_new",
            "parent": "",
            "leaf": "不存在的",
            "name": "不存在的牌组",
            "order": 6,
            "limit": 9999,
            "search": 'tag:"不存在的"',
            "updated_at": 0,
        },
        col,
    )
    removed = module.cleanup_stale_rules(col)
    record("cleanup_stale", {"removed": removed})
    check("失效规则被清理", bool(removed), str(removed))
    check("失效规则确实没了", module.get_rule(987654321, col) is None)

    # 老规则（只有 scope 字段）读回来要自动迁移成新字段，且搜索式与老版本逐字一致
    module.put_rule(
        {
            "version": 1,
            "deck_id": 987654322,
            "tags": ["诗"],
            "mode": "union",
            "scope": "all",
            "parent": "",
            "leaf": "老规则",
            "name": "老规则",
            "order": 6,
            "limit": 9999,
            "search": 'tag:"诗"',
            "updated_at": 0,
        },
        col,
    )
    migrated = module.get_rule(987654322, col) or {}
    record(
        "legacy_scope_migration",
        {
            "states": migrated.get("states"),
            "include_not_due": migrated.get("include_not_due"),
            "search": migrated.get("search"),
        },
    )
    check(
        "老 scope=all 迁移成三个状态 + 含未到期",
        migrated.get("states") == ["new", "learn", "review"]
        and migrated.get("include_not_due") is True,
        str(migrated),
    )
    module.drop_rule(987654322, col)
    check("迁移用例的临时规则已清掉", module.get_rule(987654322, col) is None)

    # 1.4.0 存的是反向的 due_only：升级后行为必须一模一样，
    # 而且旧搜索式要与新版逐字一致（否则真机上老规则会被误判成「被 Anki 改过」）
    legacy_cases = (
        # (老的 due_only, 期望 include_not_due, 期望搜索式)
        (True, False, '(tag:"诗") AND (is:due OR is:new)'),
        (False, True, 'tag:"诗"'),
    )
    migration_facts: dict[str, Any] = {}
    for index, (old_flag, want_flag, want_search) in enumerate(legacy_cases):
        deck_key = 987654330 + index
        module.put_rule(
            {
                "version": 2,
                "deck_id": deck_key,
                "tags": ["诗"],
                "mode": "union",
                "states": list(logic.DEFAULT_STATES),
                "due_only": old_flag,
                "parent": "",
                "leaf": "老规则",
                "name": "老规则",
                "order": 6,
                "limit": 9999,
                "search": want_search,
                "updated_at": 0,
            },
            col,
        )
        row = module.get_rule(deck_key, col) or {}
        matched = logic.rule_matches_actual(
            row,
            want_search,
            int(row.get("limit") or 9999),
            int(row.get("order") or 6),
            True,
        )
        migration_facts[f"due_only={old_flag}"] = {
            "include_not_due": row.get("include_not_due"),
            "search": row.get("search"),
            "matches_actual": matched,
        }
        check(
            f"老 due_only={old_flag} 迁移后行为不变",
            row.get("include_not_due") is want_flag and row.get("search") == want_search,
            str(row),
        )
        check(
            f"老 due_only={old_flag} 不会被误判成「被 Anki 改过」",
            bool(matched),
            str(migration_facts[f"due_only={old_flag}"]),
        )
        module.drop_rule(deck_key, col)
    record("due_only_migration", migration_facts)


def step_search_normalization() -> None:
    """直接用 Anki 的接口写一条会命中的搜索式，看它保存/重建时的写法变化。

    结论决定插件怎么判断「规则有没有被外部改过」：如果 Anki 重建时会把搜索式
    改写成另一种写法，就不能拿插件生成的字符串直接和它比。
    """
    col = mw.col
    module = tfd()
    logic = rl()

    # 用一个匹配不到卡片的搜索，避免这个探针牌组抢走后面测试要用的卡
    raw = logic.tag_search_term("不存在的标签")
    deck = col.sched.get_or_create_filtered_deck(DeckId(0))
    deck.name = "标签筛选::探针-规范化"
    deck.allow_empty = True
    deck.config.reschedule = True
    del deck.config.search_terms[:]
    deck.config.search_terms.append(
        FilteredDeckConfig.SearchTerm(search=raw, limit=9999, order=6)
    )
    deck_id = int(col.sched.add_or_update_filtered_deck(deck).id)

    written = (module.read_actual(col, deck_id) or {}).get("search", "")
    col.sched.rebuild_filtered_deck(DeckId(deck_id))
    rebuilt = (module.read_actual(col, deck_id) or {}).get("search", "")
    col.sched.rebuild_filtered_deck(DeckId(deck_id))
    rebuilt_twice = (module.read_actual(col, deck_id) or {}).get("search", "")
    record(
        "search_normalization",
        {
            "generated": raw,
            "after_write": written,
            "after_rebuild": rebuilt,
            "after_second_rebuild": rebuilt_twice,
            "rewritten_by_anki": written != rebuilt,
            "stable_after_first_rebuild": rebuilt == rebuilt_twice,
        },
    )
    check(
        "重建后搜索式稳定（再重建不会再变）",
        rebuilt == rebuilt_twice,
        f"{rebuilt!r} -> {rebuilt_twice!r}",
    )
    check(
        "重建后的搜索式仍能命中同样多的卡",
        len(col.find_cards(rebuilt)) == len(col.find_cards(raw)),
        f"{rebuilt!r} vs {raw!r}",
    )
    col.decks.remove([DeckId(deck_id)])
    check(
        "探针牌组已清理",
        not bool(col.decks.get(DeckId(deck_id), default=False)),
    )


def step_editor_live_counts() -> None:
    """建一个真的编辑器，看它的「实时数量」链路能不能跑通。

    这条链路是：改动 → 250ms 防抖定时器 → QueryOp 统计 → 预览文字。
    纯逻辑测试只覆盖了文字格式化，这里补上端到端的部分。
    """
    module = tfd()
    note("editor_dialog", module.RuleEditor(mw, preset_tags=["唐诗"]))


def step_editor_live_counts_check() -> None:
    editor = STATE.get("editor_dialog")
    if editor is None:
        check("编辑器能建起来", False, "编辑器没建起来")
        return
    counts = editor._counts
    preview = editor.preview_label.text()
    record(
        "editor_live_counts",
        {"counts": counts, "preview": preview, "search": editor.search_label.text()},
    )
    check(
        "编辑器实时算出了命中数",
        isinstance(counts, dict) and int((counts or {}).get("matched") or 0) == 5,
        str(counts),
    )
    check(
        "三个状态的张数相加等于命中总数",
        isinstance(counts, dict)
        and sum(
            int(((counts or {}).get("states") or {}).get(key) or 0)
            for key in ("new", "learn", "review")
        )
        == int((counts or {}).get("matched") or 0),
        str(counts),
    )
    check(
        "预览给出了预计收录张数",
        "预计收录：3 张" in preview,
        preview,
    )
    check("预览解释了收不进来的原因", "暂停或搁置的 1 张" in preview, preview)
    check("预览说明插件不改动暂停状态", "不会改动它们的暂停状态" in preview, preview)
    check("预览显示了搜索式", "tag:" in editor.search_label.text(), editor.search_label.text())
    try:
        editor.reject()
    except Exception:
        pass


def step_no_state_mutation() -> None:
    """本版最重要的一条回归：整个流程跑完，暂停 / 搁置状态必须原封不动。"""
    col = mw.col
    before = dict(STATE.get("queue_snapshot") or {})
    after: dict[str, Any] = {}
    for text in before:
        try:
            after[text] = card_snapshot(col, cid(text))
        except Exception as exc:
            after[text] = {"error": repr(exc)}
    suspended_after = sorted(int(x) for x in col.find_cards("is:suspended"))
    buried_after = sorted(int(x) for x in col.find_cards("is:buried"))
    record(
        "no_state_mutation",
        {
            "before": before,
            "after": after,
            "suspended_before": STATE.get("suspended_snapshot"),
            "suspended_after": suspended_after,
            "buried_before": STATE.get("buried_snapshot"),
            "buried_after": buried_after,
        },
    )
    for text in ("F-暂停", "G-搁置"):
        check(
            f"{text} 的原始状态（含 queue）一点没变",
            before.get(text) == after.get(text),
            f"{before.get(text)} -> {after.get(text)}",
        )
    check(
        "暂停卡还是那 1 张",
        suspended_after == STATE.get("suspended_snapshot"),
        f"{STATE.get('suspended_snapshot')} -> {suspended_after}",
    )
    check(
        "搁置卡还是那 1 张",
        buried_after == STATE.get("buried_snapshot"),
        f"{STATE.get('buried_snapshot')} -> {buried_after}",
    )
    check("F 现在仍然是暂停状态", cid("F-暂停") in suspended_after)
    check("G 现在仍然是搁置状态", cid("G-搁置") in buried_after)


# --------------------------------------------------------------------------
# 「看收不进来的卡」修正：浏览器里看到的必须正好是「另有 K 张收不进来」
# --------------------------------------------------------------------------


def step_blocked_browse() -> None:
    """编辑器里那个按钮查出来的集合，必须正好是「收不进来」的那批卡。

    老版本这里写反了：取的是「命中 ∧ 到期 ∧ 未暂停未搁置 ∧ 不在别的筛选牌组」，
    也就是**能**收进来的那批，于是浏览器里的张数永远和预览里的 K 对不上。
    本步先造一个三种原因都有的局面（没到期 / 暂停搁置 / 在别的筛选牌组），
    再把插件查出来的集合与预览数字逐一对齐。
    """
    col = mw.col
    module = tfd()
    logic = rl()
    deck_id = STATE["union_deck_id"]

    # 造局面：先把并集牌组清空让卡回源牌组，再把「唐诗-全部」收满，
    # 这样 A/B/D/E 被它占住（Anki 不让两张筛选牌组抢同一张卡）。
    col.sched.empty_filtered_deck(DeckId(deck_id))
    col.sched.rebuild_filtered_deck(DeckId(int(STATE["subtag_deck_id"])))
    col.sched.rebuild_filtered_deck(DeckId(deck_id))

    rule = module.get_rule(deck_id, col) or {}
    name = module._deck_name(col, deck_id)
    tags = rule.get("tags")
    mode = rule.get("mode")
    states = rule.get("states")
    include_not_due = bool(rule.get("include_not_due"))

    counts = module.collect_counts(col, tags, mode, states, include_not_due, name)

    wanted_ids = {
        int(x) for x in col.find_cards(logic.wanted_query(tags, mode, states))
    }
    scoped_ids = {
        int(x)
        for x in col.find_cards(
            logic.in_scope_query(tags, mode, states, include_not_due)
        )
    }
    banned_ids = {
        int(x)
        for x in col.find_cards(
            logic.and_query(
                logic.wanted_query(tags, mode, states),
                logic.ALWAYS_EXCLUDED_QUERY,
            )
        )
    }
    other_ids = {
        int(x)
        for x in col.find_cards(
            logic.and_query(
                logic.wanted_query(tags, mode, states),
                logic.negate_term(logic.ALWAYS_EXCLUDED_QUERY),
                "deck:filtered",
                logic.negate_term(logic.deck_search_term(name)),
            )
        )
    }
    collectible_ids = scoped_ids - banned_ids - other_ids
    # 「还没到期」这一条没法用搜索式表达（卡被收进筛选牌组后 due 已经变成位置值），
    # 所以按卡片自己的到期时间逐张判一遍，不再用 is:due。这条只在「只收到期」
    # 真的生效时才拦卡（和插件里的 due_filter_enabled 一个口径）。
    due_restricted = logic.due_restriction_applies(states, include_not_due)
    not_due_ids = {
        int(record["id"])
        for record in module._card_records(col, wanted_ids)
        if int(record.get("queue") or 0) >= 0
        and not logic.card_is_due(
            record, today=int(col.sched.today), now=int(time.time())
        )
    }
    due_ids = (not_due_ids - banned_ids - other_ids) if due_restricted else set()
    # 插件统计出来的「收不进来」卡号（预览里的 K 就是它的长度）
    blocked_ids = {int(x) for x in (counts.get("blocked_ids") or [])}
    expected_blocked = wanted_ids - collectible_ids
    segment_union = due_ids | banned_ids | other_ids
    # 「看收不进来的卡」按钮真正交给浏览器的搜索式：直接列卡号
    blocked_query_text, blocked_dropped = logic.blocked_query(
        counts.get("blocked_ids") or [], limit=logic.MAX_BROWSE_CARDS
    )
    blocked_by_search = (
        {int(x) for x in col.find_cards(blocked_query_text)}
        if blocked_query_text
        else set()
    )

    cards_by_name = {int(value): key for key, value in STATE["cards"].items()}
    record(
        "blocked_browse",
        {
            "counts": counts,
            "deck_name": name,
            "busy_deck_cards": cards_of(col, int(STATE["subtag_deck_id"])),
            "wanted": sorted(cards_by_name.get(x, x) for x in wanted_ids),
            "collectible": sorted(cards_by_name.get(x, x) for x in collectible_ids),
            "blocked": sorted(cards_by_name.get(x, x) for x in blocked_ids),
            "due_blocked": sorted(cards_by_name.get(x, x) for x in due_ids),
            "banned": sorted(cards_by_name.get(x, x) for x in banned_ids),
            "other_filtered": sorted(cards_by_name.get(x, x) for x in other_ids),
            "search_shown": blocked_query_text,
            "search_shown_count": len(blocked_by_search),
            "search_dropped": blocked_dropped,
        },
    )

    lines = logic.preview_lines(
        counts, tags, mode, states, int(rule.get("limit") or 9999)
    )
    check(
        "预览里的 K 与统计一致",
        any(f"另有 {len(blocked_ids)} 张这次收不进来" in line for line in lines),
        f"K={len(blocked_ids)} / {lines}",
    )
    check(
        "「看收不进来的卡」查出来的张数 = 预览里的 K",
        len(blocked_ids) == int(counts.get("blocked") or 0),
        f"{len(blocked_ids)} vs {counts.get('blocked')}",
    )
    check(
        "三种原因相加正好等于 K（互不重叠）",
        not (due_ids & banned_ids)
        and not (due_ids & other_ids)
        and not (banned_ids & other_ids)
        and len(segment_union) == len(due_ids) + len(banned_ids) + len(other_ids)
        and len(segment_union) == int(counts.get("blocked") or 0),
        f"{len(due_ids)}+{len(banned_ids)}+{len(other_ids)} vs {len(segment_union)} / K={counts.get('blocked')}",
    )
    check(
        "「看收不进来的卡」的卡号搜索式正好选中那 K 张",
        blocked_by_search == blocked_ids
        and blocked_dropped == 0
        and len(blocked_by_search) == int(counts.get("blocked") or 0),
        f"{len(blocked_by_search)} vs K={counts.get('blocked')}（丢 {blocked_dropped}）",
    )
    check(
        "那个搜索式是按卡号列的 cid: 形式",
        blocked_query_text.startswith("cid:")
        or blocked_query_text.startswith("(cid:"),
        blocked_query_text[:120],
    )
    check(
        "插件的逐张判定与搜索式对得上（三种原因恰好拼成 K）",
        blocked_ids == segment_union,
        f"{sorted(blocked_ids)} vs {sorted(segment_union)}",
    )
    check(
        "看收不进来的卡 = 命中里扣掉能收进来的那批",
        blocked_ids == expected_blocked,
        f"{sorted(blocked_ids)} vs {sorted(expected_blocked)}",
    )
    check(
        "能收进来 + 收不进来 = 命中",
        collectible_ids | blocked_ids == wanted_ids,
        f"{len(collectible_ids)} + {len(blocked_ids)} vs {len(wanted_ids)}",
    )
    check(
        "收不进来的这批和能收进来的那批完全不重叠",
        not (blocked_ids & collectible_ids),
        str(sorted(blocked_ids & collectible_ids)),
    )
    check(
        "旧 bug 已修（不再是能收进来的那批）",
        bool(blocked_ids) and blocked_ids != collectible_ids,
        f"{len(blocked_ids)} vs {len(collectible_ids)}",
    )
    check(
        "三种原因在样例里都有代表卡",
        cid("I-复习未到期") in due_ids
        and cid("F-暂停") in banned_ids
        and cid("G-搁置") in banned_ids
        and cid("B-唐诗子标签") in other_ids,
        f"due={sorted(due_ids)} banned={sorted(banned_ids)} other={sorted(other_ids)}",
    )
    check(
        "能收进来的正好是牌组里那 4 张",
        collectible_ids
        == ids_of_set("C-宋词", "H-学习中", "J-复习到期", "K-别的筛选"),
        str(sorted(collectible_ids)),
    )


def ids_of_set(*texts: str) -> set[int]:
    return {cid(text) for text in texts}


# --------------------------------------------------------------------------
# 集中刷：不影响排期 / 影响排期
# --------------------------------------------------------------------------


def revlog_by_type(col: Any) -> dict[str, int]:
    rows = col.db.all("select type, count() from revlog group by type")
    return {str(int(row[0])): int(row[1]) for row in rows}


def revlog_total(col: Any) -> int:
    return int(col.db.scalar("select count() from revlog") or 0)


# --- 1.4.0：集中刷用的是**独立的临时牌组** -------------------------------

# 突击牌组：标签只有「宋词」，并且打开「连还没到期的也收」
ASSAULT_TAGS: tuple[str, ...] = ("宋词",)
ASSAULT_EXPECTED: tuple[str, ...] = (
    "C-宋词",
    "D-两标签",
    "E-三标签",
    "H-学习中",
    "I-复习未到期",
    "J-复习到期",
    "K-别的筛选",
)
FILTERED_KEYS: tuple[str, ...] = (
    "union_deck_id",
    "subtag_deck_id",
    "intersection_deck_id",
    "other_deck_id",
    "assault_deck_id",
)


def assault_expected() -> list[int]:
    return ids_of(*ASSAULT_EXPECTED)


def cram_session(deck_id: Any) -> dict[str, Any] | None:
    return tfd().get_cram_session(int(deck_id), mw.col)


def cram_temp_id(deck_id: Any) -> int:
    return int(rl().cram_session_deck_id(cram_session(deck_id)) or 0)


def preview_secs_of(col: Any, deck_id: Any) -> list[int]:
    """集中刷「不影响排期」写在牌组设置里的三档延迟。"""
    cfg = col.sched.get_or_create_filtered_deck(DeckId(int(deck_id))).config
    return [
        int(cfg.preview_again_secs),
        int(cfg.preview_hard_secs),
        int(cfg.preview_good_secs),
    ]


def empty_known_filtered(col: Any, *skip: str) -> dict[str, list[int]]:
    """把探针建的筛选牌组都清空，卡片回源牌组（集中刷的前提）。"""
    out: dict[str, list[int]] = {}
    for key in FILTERED_KEYS:
        if key in skip:
            continue
        deck_id = STATE.get(key)
        if not deck_id:
            continue
        out[key] = cards_of(col, int(deck_id))
        try:
            col.sched.empty_filtered_deck(DeckId(int(deck_id)))
        except Exception as exc:
            out[key + "_error"] = [repr(exc)]
    return out


def reset_cram_sync(deck_id: Any, reason: str = "探针换模式") -> dict[str, Any]:
    """同步收尾一次集中刷（探针专用，免得两次收尾打架）。"""
    module = tfd()
    col = mw.col
    number = int(deck_id)
    module.close_cram_window(number, reason)
    temp_ids = module._cram_temp_deck_ids(col, number, cram_temp_id(number))
    module.drop_cram_session(number, col)
    for value in temp_ids:
        module._remove_filtered_decks(col, [value])
    return {"temp_ids": temp_ids}


def cram_window_info(module: Any, deck_id: Any) -> dict[str, Any]:
    """读出集中刷小窗上的关键文字（小窗关着就返回空 dict）。"""
    window = module.find_cram_window(int(deck_id))
    if window is None:
        return {}
    info: dict[str, Any] = {"writeback": bool(getattr(window, "writeback", False))}
    try:
        info["title"] = str(window.windowTitle())
    except Exception as exc:
        info["title"] = f"<{exc}>"
    for key, name in (
        ("mode", "mode_label"),
        ("counts", "count_label"),
        ("time", "time_label"),
        ("go", "go_button"),
        ("again", "again_button"),
        ("end", "end_button"),
    ):
        try:
            info[key] = str(getattr(window, name).text())
        except Exception as exc:
            info[key] = f"<{exc}>"
    try:
        info["current_cram_deck"] = int(window._current_cram_deck())
    except Exception as exc:
        info["current_cram_deck"] = f"<{exc}>"
    return info


def cram_cleanup_state(
    deck_id: Any, temp_id: Any, expected: list[int]
) -> tuple[bool, dict[str, Any]]:
    """结束集中刷之后：临时牌组没了、原牌组按规则收回来了吗。"""
    col = mw.col
    temp = int(temp_id or 0)
    temp_gone = bool(temp) and not bool(col.decks.get(DeckId(temp), default=False))
    cards = cards_of(col, int(deck_id))
    info = {"temp_gone": temp_gone, "cards": cards, "session": cram_session(deck_id)}
    return (temp_gone and cards == list(expected)), info


def requeue(
    name: str,
    fn: Callable[[], None],
    attempts_key: str,
    *,
    delay: int = 1500,
    limit: int = 120,
) -> bool:
    """异步的事还没做完：把这一步插回队列，过一会儿再检查一次。"""
    attempt = int(STATE.get(attempts_key, 0)) + 1
    STATE[attempts_key] = attempt
    if attempt < limit:
        STEP_QUEUE.insert(0, (name, fn, delay))
        return True
    return False


def step_cram_assault_setup() -> None:
    """1.5.0：集中刷对任何被管理的筛选牌组都可用；再建一个突击牌组备用。"""
    col = mw.col
    module = tfd()
    logic = rl()
    strict_id = STATE["union_deck_id"]

    # 老版本这里会拦下「只收到期」的规则并弹「请先打开『连还没到期的也收』」。
    # 1.5.0 去掉了这道门槛：管理窗口里这个按钮必须可用、文字就是「集中刷…」。
    strict_rule = module.get_rule(strict_id, col) or {}
    manager = module.ManagerDialog(mw)
    enabled = False
    label = ""
    try:
        manager._populate(
            [
                {
                    "deck_id": strict_id,
                    "name": module._deck_name(col, strict_id),
                    "rule": strict_rule,
                    "exists": True,
                    "filtered": True,
                    "managed": True,
                    "cram": None,
                    "cards": 0,
                }
            ]
        )
        manager.tree.setCurrentItem(manager.tree.topLevelItem(0))
        manager._sync_buttons()
        enabled = bool(manager.cram_button.isEnabled())
        label = manager.cram_button.text()
    finally:
        try:
            manager.reject()
        except Exception:
            pass
    record(
        "cram_gate_removed",
        {"enabled": enabled, "label": label, "strict_rule": strict_rule},
    )
    check(
        "只收到期的规则也能用集中刷（1.5.0 门槛已删除）",
        enabled and strict_rule.get("include_not_due") is False,
        str(strict_rule),
    )
    check("这种牌组的按钮文字是「集中刷…」", label == "集中刷…", label)
    check("这一步没有建任何临时牌组", cram_temp_id(strict_id) == 0, str(cram_temp_id(strict_id)))

    # 清掉其它托管牌组，造一个「宋词 + 含未到期」的突击牌组
    emptied = empty_known_filtered(col)
    rule = logic.make_rule(
        deck_id=0,
        tags=list(ASSAULT_TAGS),
        mode=logic.MODE_UNION,
        states=list(logic.DEFAULT_STATES),
        include_not_due=True,
        parent="标签筛选",
        leaf="宋词-突击",
        limit=9999,
        order=logic.DEFAULT_ORDER,
    )
    deck_id = build_deck(col, rule)
    note("assault_deck_id", deck_id)
    cards = cards_of(col, deck_id)
    actual = module.read_actual(col, deck_id) or {}
    record(
        "cram_assault_deck",
        {
            "emptied": emptied,
            "deck_id": deck_id,
            "deck_name": module._deck_name(col, deck_id),
            "cards": cards,
            "expected": assault_expected(),
            "search": rule["search"],
            "actual": actual,
        },
    )
    check("突击牌组已建", deck_id != 0 and module._is_filtered(col, deck_id))
    check("突击牌组的搜索式不含 is:due", "is:due" not in str(rule["search"]), rule["search"])
    check(
        "突击牌组收录含未到期的 7 张",
        cards == assault_expected(),
        f"{cards} != {assault_expected()}",
    )
    check("暂停的 F 还是收不进来", cid("F-暂停") not in cards, str(cards))
    check("搁置的 G 还是收不进来", cid("G-搁置") not in cards, str(cards))
    check("突击牌组仍开启「作答重排原卡」", actual.get("reschedule") is True, str(actual))

    manager = module.ManagerDialog(mw)
    scope = ""
    status = ""
    try:
        manager._populate(
            [
                {
                    "deck_id": deck_id,
                    "name": module._deck_name(col, deck_id),
                    "rule": module.get_rule(deck_id, col) or {},
                    "exists": True,
                    "filtered": True,
                    "managed": True,
                    "cram": None,
                    "cards": len(cards),
                }
            ]
        )
        row = manager.tree.topLevelItem(0)
        scope = row.text(2) if row else ""
        status = row.text(6) if row else ""
    finally:
        try:
            manager.reject()
        except Exception:
            pass
    record("cram_assault_manager", {"scope": scope, "status": status})
    check(
        "管理窗口范围列写明「含未到期」",
        scope == "未学习+学习中+复习中·含未到期",
        scope,
    )
    check("管理窗口标注突击牌组不自动重收", "不自动重收" in status, status)


def step_cram_step_source() -> None:
    """集中刷的三档延迟必须跟着「卡片自己的原牌组」走（1.5.0 核心修复）。

    1.4.0 读的是「当前选中的那个牌组」的牌组选项，于是卡片原牌组里设的学习步骤
    被忽略了。这里造一个原牌组「源牌组2」，把它的新卡学习步骤设成一眼能认出来的
    「2 分钟 / 40 分钟」，再往里塞足够多的命中卡把多数派翻过去，看插件挑出来的
    来源牌组与延迟是不是跟着换。测完删掉这批临时卡，后面的步骤不受影响。
    """
    col = mw.col
    module = tfd()
    logic = rl()
    deck_id = STATE["assault_deck_id"]
    rule = module.get_rule(deck_id, col) or {}
    scope = logic.cram_search_query(
        rule.get("tags"), rule.get("mode"), rule.get("states")
    )

    # 先让所有卡回自己的原牌组，来源牌组才好数
    empty_known_filtered(col)
    base_records = module._card_records(col, col.find_cards(scope))
    steps_base, name_base, info_base = module._cram_step_source(col, base_records)
    delays_base = list(logic.cram_preview_delays(steps_base))
    record(
        "cram_source_base",
        {
            "scope": scope,
            "steps": steps_base,
            "source": name_base,
            "info": info_base,
            "delays": delays_base,
            "records": len(base_records),
        },
    )
    check(
        "只有一套原牌组时，来源就是它、没有「另有 N 张」",
        name_base == "源牌组"
        and int(info_base.get("count") or 0) == len(base_records)
        and int(info_base.get("other_count") or 0) == 0,
        f"{name_base} {info_base}",
    )

    # 建第二个原牌组 + 一份专属牌组设置（2 分钟 / 40 分钟）
    other_id = int(col.decks.id("源牌组2"))
    conf = col.decks.add_config("TFD探针-快步骤")
    conf["new"]["delays"] = [2, 40]
    col.decks.update_config(conf)
    col.decks.set_config_id_for_deck_dict(
        col.decks.get(DeckId(other_id)), conf["id"]
    )
    steps_other = logic.new_card_steps_from_config(
        col.decks.config_dict_for_deck_id(DeckId(other_id))
    )
    check(
        "「源牌组2」的新卡步骤已设成 2 分钟 / 40 分钟",
        [float(x) for x in steps_other] == [120.0, 2400.0],
        str(steps_other),
    )

    extra: list[Any] = []
    notetype = make_notetype(col)
    for index in range(12):
        note = col.new_note(notetype)
        note["正面"] = f"多来源-{index}"
        note["背面"] = "x"
        note.tags = ["宋词"]
        col.add_note(note, DeckId(other_id))
        extra.append(note.id)

    flip_records = module._card_records(col, col.find_cards(scope))
    steps_flip, name_flip, info_flip = module._cram_step_source(col, flip_records)
    delays_flip = list(logic.cram_preview_delays(steps_flip))
    body_flip = module.cram_dialog_body(
        "宋词-突击",
        {"collectible": len(flip_records), "banned": 2, "other_filtered": 0},
        delays_flip,
        name_flip,
        info_flip,
    )
    record(
        "cram_source_flip",
        {
            "steps": steps_flip,
            "source": name_flip,
            "info": info_flip,
            "delays": delays_flip,
            "dialog": body_flip,
            "extra_notes": len(extra),
        },
    )
    check(
        "多数派换成「源牌组2」后，来源牌组跟着换",
        name_flip == "源牌组2"
        and int(info_flip.get("count") or 0) == len(extra)
        and int(info_flip.get("other_count") or 0) == len(base_records),
        f"{name_flip} {info_flip}",
    )
    check(
        "延迟按「源牌组2」的步骤换算（重来 2 分钟、困难 (2+40)/2=21 分钟）",
        delays_flip == [120, 1260, 0],
        f"{steps_flip} -> {delays_flip}",
    )
    check(
        "换来源后延迟确实变了（不再是源牌组那套）",
        delays_flip != delays_base,
        f"{delays_base} vs {delays_flip}",
    )
    check(
        "对话框写明延迟来源，并提示另有 N 张来自别的原牌组",
        "源牌组2" in body_flip
        and f"另有 {len(base_records)} 张来自别的原牌组" in body_flip,
        body_flip,
    )

    # 清理：临时卡与临时牌组都要删掉，来源立刻回到「源牌组」
    col.remove_notes(extra)
    col.decks.remove([DeckId(other_id)])
    after_records = module._card_records(col, col.find_cards(scope))
    steps_after, name_after, _info_after = module._cram_step_source(col, after_records)
    record(
        "cram_source_restored",
        {
            "steps": steps_after,
            "source": name_after,
            "records": len(after_records),
            "source_deck2_gone": col.decks.id_for_name("源牌组2") is None,
        },
    )
    check(
        "清掉临时卡后来源回到「源牌组」、卡片数与开始时一致",
        name_after == name_base
        and list(steps_after) == list(steps_base)
        and len(after_records) == len(base_records),
        f"{name_after} {steps_after} {len(after_records)} vs {name_base} {steps_base} {len(base_records)}",
    )
    check("临时建的「源牌组2」已经删掉", col.decks.id_for_name("源牌组2") is None)


def step_cram_preview() -> None:
    """点「集中刷…」：先算数量，再问排期影响，然后建**临时**集中刷牌组。"""
    col = mw.col
    module = tfd()
    logic = rl()
    deck_id = STATE["assault_deck_id"]

    # 先让卡片都回原牌组：临时牌组是从原牌组里收卡的，而且原卡快照要拿真实的
    # 到期时间（卡片在筛选牌组里时 due 已经变成位置值了）。
    emptied = empty_known_filtered(col)
    col.decks.select(DeckId(int(STATE["source_deck_id"])))

    rule = dict(module.get_rule(deck_id, col) or {})
    name = module._deck_name(col, deck_id)
    tags = rule.get("tags")
    mode = rule.get("mode")
    states = rule.get("states")

    counts = module.collect_cram_counts(col, tags, mode, states, name)
    # 1.5.0：延迟来源＝这批卡自己的原牌组（取张数最多的那个），不是当前选中的牌组
    cram_scope = logic.cram_search_query(tags, mode, states)
    steps, step_source, source_info = module._cram_step_source(
        col, module._card_records(col, col.find_cards(cram_scope))
    )
    delays = list(logic.cram_preview_delays(steps))
    body = module.cram_dialog_body(name, counts, delays, step_source, source_info)
    origin = card_snapshot(col, cid("I-复习未到期"))
    temp_name = logic.cram_temp_deck_name(
        name, module._deck_names(col, include_filtered=True)
    )
    note("cram_origin_deck_actual", module.read_actual(col, deck_id))
    note("cram_source_cards_before", cards_of(col, deck_id))

    plan = {
        "emptied": emptied,
        "counts": counts,
        "steps_secs": steps,
        "step_source": step_source,
        "source_info": source_info,
        "delays": delays,
        "dialog": body,
        "origin": origin,
        "temp_name": temp_name,
    }
    # 注意：record() 只写结果文件，后续步骤要读的东西必须再 note 一份进 STATE
    note("cram_preview_plan", plan)
    record("cram_preview_plan", plan)
    check(
        "集中刷预计能收 7 张（含没到期的 I）",
        int(counts.get("collectible") or 0) == 7,
        str(counts),
    )
    check(
        "集中刷临时拉进来的没到期卡只有 I 一张",
        int(counts.get("not_due") or 0) == 1,
        str(counts),
    )
    check("暂停的 F 仍然收不进来", int(counts.get("banned") or 0) == 1, str(counts))
    check(
        "没有卡被别的筛选牌组占着",
        int(counts.get("other_filtered") or 0) == 0,
        str(counts),
    )
    check(
        "对话框讲清两种模式与后果",
        all(
            word in body
            for word in ("不影响排期", "影响排期", "回原牌组", "临时牌组", "关掉", "再刷一轮")
        ),
        body,
    )
    check(
        "三档延迟由新卡步骤换算（重来=第 1 步、困难=前两步平均、良好=0）",
        delays[0] == int(round(steps[0]))
        and delays[1]
        == int(
            round((steps[0] + steps[1]) / 2.0 if len(steps) >= 2 else steps[0] * 1.5)
        )
        and delays[2] == 0,
        f"{steps} -> {delays}",
    )

    # 真的按插件的入口开一轮集中刷（不影响排期）
    module.start_cram(
        deck_id,
        False,
        parent=mw,
        study=False,
        info={"collected": int(counts.get("collectible") or 0), "not_due": 1},
    )


def step_cram_preview_check() -> None:
    """「不影响排期」这一轮：临时牌组收满、原牌组空、作答不动原卡。"""
    col = mw.col
    module = tfd()
    logic = rl()
    deck_id = STATE["assault_deck_id"]
    plan = STATE.get("cram_preview_plan") or {}
    session = cram_session(deck_id)
    temp_id = cram_temp_id(deck_id)
    temp_cards = cards_of(col, temp_id) if temp_id else []

    if not (session is not None and temp_id > 0 and len(temp_cards) == 7):
        if requeue("cram_preview_check", step_cram_preview_check, "cram_preview_attempt"):
            return
    STATE.pop("cram_preview_attempt", None)

    origin = plan.get("origin") or {}
    delays = list(plan.get("delays") or [])
    cards = cards_of(col, deck_id)
    actual = module.read_actual(col, deck_id) or {}
    temp_actual = module.read_actual(col, temp_id) or {}
    secs = preview_secs_of(col, temp_id)
    window = module.find_cram_window(deck_id)
    info = cram_window_info(module, deck_id)
    state_info = {
        "temp_id": temp_id,
        "temp_name": module._deck_name(col, temp_id),
        "temp_cards": temp_cards,
        "source_cards": cards,
        "actual": actual,
        "temp_actual": temp_actual,
        "preview_secs": secs,
        "session": session,
        "label": logic.cram_label(session),
        "window": info,
    }
    note("cram_preview_state", state_info)
    record("cram_preview_state", state_info)
    check(
        "集中刷会话记下了临时牌组",
        temp_id > 0 and int((session or {}).get("cram_deck_id") or 0) == temp_id,
        str(session),
    )
    check(
        "临时牌组名字带「·集中刷」",
        module._deck_name(col, temp_id) == plan.get("temp_name"),
        f"{module._deck_name(col, temp_id)} vs {plan.get('temp_name')}",
    )
    check("临时牌组收进 7 张", temp_cards == assault_expected(), str(temp_cards))
    check("暂停的 F 没被收进来", cid("F-暂停") not in temp_cards, str(temp_cards))
    check("搁置的 G 没被收进来", cid("G-搁置") not in temp_cards, str(temp_cards))
    check("原筛选牌组被临时清空（卡片回原牌组）", cards == [], str(cards))
    check(
        "原筛选牌组的设置一个字没动",
        actual == STATE.get("cram_origin_deck_actual"),
        f"{actual} vs {STATE.get('cram_origin_deck_actual')}",
    )
    check(
        "写进去的是「不影响排期」模式",
        temp_actual.get("reschedule") is False,
        str(temp_actual),
    )
    check(
        "预览三档延迟按新卡步骤写进临时牌组设置",
        secs == delays,
        f"{secs} vs {delays}",
    )
    # 独立核对一次：直接从「卡片原牌组」的牌组选项里读新卡步骤，换算出来的
    # 三档延迟必须与写进临时牌组的一模一样（这条不依赖前面算过的 plan）。
    home_steps = logic.new_card_steps_from_config(
        col.decks.config_dict_for_deck_id(DeckId(int(STATE["source_deck_id"])))
    )
    check(
        "预览延迟＝卡片原牌组（源牌组）自己的新卡步骤换算",
        bool(home_steps)
        and secs == list(logic.cram_preview_delays(home_steps))
        and plan.get("step_source") == "源牌组",
        f"{home_steps} -> {logic.cram_preview_delays(home_steps)} vs 写进牌组的 {secs}，来源 {plan.get('step_source')}",
    )
    check(
        "集中刷状态被记下来了",
        logic.cram_label(session) == "集中刷中（临时牌组·不影响排期）",
        str(session),
    )
    check(
        "小窗说明了临时牌组与轮次",
        bool(info)
        and "临时牌组" in str(info.get("time"))
        and "第 1 轮" in str(info.get("time")),
        str(info.get("time")),
    )
    check(
        "小窗指向临时牌组",
        int(info.get("current_cram_deck") or 0) == temp_id,
        str(info.get("current_cram_deck")),
    )
    check("小窗有「再刷一轮」按钮", info.get("again") == "再刷一轮", str(info.get("again")))
    check("小窗开着并登记在案", window is not None and module.find_cram_window(deck_id) is not None)

    # 答「重来」：卡留在临时集中刷牌组里，按第 1 步的延迟回来；原卡数据不动
    target = cid("I-复习未到期")
    revlog_before = revlog_by_type(col)
    now = int(time.time())
    answer_card(col, target, 1)
    after_again = card_snapshot(col, target)
    record(
        "cram_preview_again",
        {
            "before": origin,
            "after": after_again,
            "now": now,
            "expected_due": now + int(delays[0] or 0),
            "temp_id": temp_id,
        },
    )
    check(
        "答「重来」后卡留在临时集中刷牌组里",
        after_again["did"] == temp_id and after_again["odid"] != 0,
        str(after_again),
    )
    check(
        "答「重来」后按第 1 步的延迟（约 1 分钟）回到集中刷",
        now + 20 <= after_again["due"] <= now + max(120, int(delays[0] or 0) * 2),
        f"due={after_again['due']} now={now} 延迟={delays[0]}",
    )
    check(
        "答「重来」没有动原卡的次数/间隔/因子/类型",
        after_again["reps"] == origin["reps"]
        and after_again["ivl"] == origin["ivl"]
        and after_again["factor"] == origin["factor"]
        and after_again["type"] == origin["type"],
        f"{origin} -> {after_again}",
    )

    # 答「良好」：本次刷完，带着原来的到期时间回原牌组
    answer_card(col, target, 3)
    after_good = card_snapshot(col, target)
    record(
        "cram_preview_good",
        {
            "origin": origin,
            "after": after_good,
            "cards": cards_of(col, temp_id),
            "revlog_before": revlog_before,
            "revlog_after": revlog_by_type(col),
        },
    )
    check(
        "答「良好」后卡带着原到期时间回原牌组",
        after_good["did"] == STATE["source_deck_id"]
        and after_good["odid"] == 0
        and after_good["due"] == origin["due"],
        f"{origin} -> {after_good}",
    )
    check(
        "答「良好」后原卡的间隔/次数/因子/类型/队列全部复原",
        after_good["ivl"] == origin["ivl"]
        and after_good["reps"] == origin["reps"]
        and after_good["factor"] == origin["factor"]
        and after_good["type"] == origin["type"]
        and after_good["queue"] == origin["queue"],
        f"{origin} -> {after_good}",
    )
    check(
        "答「良好」后卡不再挂在临时牌组里",
        target not in cards_of(col, temp_id),
        str(cards_of(col, temp_id)),
    )
    check(
        "预览作答没有写正式复习日志（type 0/1/2 一条没多）",
        all(
            int(revlog_by_type(col).get(key, 0)) == int(revlog_before.get(key, 0))
            for key in ("0", "1", "2")
        ),
        f"{revlog_before} -> {revlog_by_type(col)}",
    )
    check(
        "预览作答会为本次集中刷留过滤日志（type 3 正好两条）",
        int(revlog_by_type(col).get("3", 0)) == int(revlog_before.get("3", 0)) + 2,
        f"{revlog_before} -> {revlog_by_type(col)}",
    )


def step_cram_restart() -> None:
    """小窗上的「再刷一轮」：同一个临时牌组重新收一遍，轮次 +1。"""
    module = tfd()
    deck_id = STATE["assault_deck_id"]
    note("cram_restart_before", cram_session(deck_id))
    via_button = False
    window = module.find_cram_window(deck_id)
    if window is not None:
        try:
            window.again_button.click()
            via_button = True
        except Exception as exc:
            note("cram_restart_click_error", repr(exc))
    if not via_button:
        module.restart_cram(deck_id, parent=mw, quiet=True)
    note("cram_restart_via_button", via_button)


def step_cram_restart_check() -> None:
    col = mw.col
    module = tfd()
    logic = rl()
    deck_id = STATE["assault_deck_id"]
    before = STATE.get("cram_restart_before") or {}
    preview = STATE.get("cram_preview_state") or {}
    session = cram_session(deck_id)
    temp_id = cram_temp_id(deck_id)
    temp_cards = cards_of(col, temp_id) if temp_id else []
    ready = (
        session is not None
        and logic.cram_round(session) >= 2
        and temp_id > 0
        and len(temp_cards) == 7
    )
    if not ready:
        if requeue("cram_restart_check", step_cram_restart_check, "cram_restart_attempt"):
            return
    STATE.pop("cram_restart_attempt", None)

    info = cram_window_info(module, deck_id)
    source_cards = cards_of(col, deck_id)
    record(
        "cram_restart",
        {
            "before": before,
            "session": session,
            "round": logic.cram_round(session),
            "temp_id": temp_id,
            "temp_cards": temp_cards,
            "source_cards": source_cards,
            "window": info,
            "via_button": STATE.get("cram_restart_via_button"),
        },
    )
    check(
        "「再刷一轮」是按小窗按钮触发的",
        STATE.get("cram_restart_via_button") is True,
        str(STATE.get("cram_restart_via_button")),
    )
    check(
        "轮次从第 1 轮变成第 2 轮",
        logic.cram_round(session) == 2 and logic.cram_round(before) == 1,
        f"{logic.cram_round(before)} -> {logic.cram_round(session)}",
    )
    check(
        "再刷一轮没有换模式（仍然不影响排期）",
        logic.cram_writes_back(session) is False,
        str(session),
    )
    check(
        "再刷一轮用的还是同一个临时牌组",
        temp_id == int(preview.get("temp_id") or 0) and temp_id > 0,
        f"{temp_id} vs {preview.get('temp_id')}",
    )
    check(
        "再刷一轮把 7 张重新收满（含刚刷完的 I）",
        temp_cards == assault_expected(),
        f"{temp_cards} != {assault_expected()}",
    )
    check(
        "再刷一轮期间原筛选牌组还是空的",
        source_cards == [],
        str(source_cards),
    )
    check("小窗上写着第 2 轮", "第 2 轮" in str(info.get("time")), str(info.get("time")))


def step_cram_writeback() -> None:
    """换成「影响排期」再集中刷一次：收录范围一样放宽，作答写回原卡。

    两种卡的预期不一样，所以两张都先记下真实原始状态：

    * 还没到期的 I——Anki 会把它当「提前复习」（源码 rslib/src/scheduler/states/
      review.rs：days_late() < 0 → passing_early_review_intervals）：间隔不缩短，
      新到期日按「今天 + 间隔」重算，因此只会维持或略微往后挪，绝不会提前；
      这次作答确实写回了原卡（次数 +1、一条 type 3「过滤/提前」复习日志、卡片回原牌组）；
    * 今天到期的 J——走的才是正常的复习算法，间隔与到期时间都会被改写。
    """
    col = mw.col
    module = tfd()
    deck_id = STATE["assault_deck_id"]
    note("cram_writeback_sync", reset_cram_sync(deck_id, "探针换成影响排期"))
    note(
        "cram_writeback_before",
        {
            "not_due": card_snapshot(col, cid("I-复习未到期")),
            "due": card_snapshot(col, cid("J-复习到期")),
        },
    )
    note("cram_writeback_revlog", revlog_by_type(col))
    module.start_cram(deck_id, True, parent=mw, study=False)


def step_cram_writeback_check() -> None:
    col = mw.col
    module = tfd()
    logic = rl()
    deck_id = STATE["assault_deck_id"]
    session = cram_session(deck_id)
    temp_id = cram_temp_id(deck_id)
    temp_cards = cards_of(col, temp_id) if temp_id else []
    ready = session is not None and temp_id > 0 and len(temp_cards) == 7
    if not ready:
        if requeue("cram_writeback_check", step_cram_writeback_check, "cram_writeback_attempt"):
            return
    STATE.pop("cram_writeback_attempt", None)

    actual = module.read_actual(col, temp_id) or {}
    secs = preview_secs_of(col, temp_id)
    source_cards = cards_of(col, deck_id)
    record(
        "cram_writeback_state",
        {
            "temp_id": temp_id,
            "temp_cards": temp_cards,
            "source_cards": source_cards,
            "actual": actual,
            "preview_secs": secs,
            "session": session,
            "label": logic.cram_label(session),
            "sync": STATE.get("cram_writeback_sync"),
        },
    )
    check(
        "影响排期模式下没到期的卡也进了临时牌组",
        cid("I-复习未到期") in temp_cards,
        str(temp_cards),
    )
    check("临时牌组收满 7 张", temp_cards == assault_expected(), str(temp_cards))
    check(
        "影响排期模式写的是「按作答重排原卡」",
        actual.get("reschedule") is True,
        str(actual),
    )
    check(
        "影响排期模式把预览延迟复位成 Anki 默认值",
        secs == [60, 600, 0],
        str(secs),
    )
    check(
        "集中刷状态记的是「会写回排期」",
        logic.cram_label(session) == "集中刷中（临时牌组·会写回排期）",
        str(session),
    )
    check("原筛选牌组仍然空着", source_cards == [], str(source_cards))

    before_all = STATE.get("cram_writeback_before") or {}
    before = before_all.get("not_due") or {}
    before_due = before_all.get("due") or {}
    revlog_before = STATE.get("cram_writeback_revlog") or {}

    # ① 还没到期的 I：答「良好」——次数 +1、写回原牌组，但间隔不缩短
    not_due_target = cid("I-复习未到期")
    answer_card(col, not_due_target, 3)
    after = card_snapshot(col, not_due_target)
    check(
        "影响排期的集中刷里，没到期的卡作答次数 +1",
        after["reps"] == int(before.get("reps") or 0) + 1,
        f"{before} -> {after}",
    )
    check(
        "没到期的卡「提前作答」不缩短间隔（Anki 原生行为：间隔保持原值）",
        after["ivl"] == before.get("ivl"),
        f"{before} -> {after}",
    )
    check(
        "没到期的卡「提前作答」不会把到期日提前（新到期日 = 今天 + 间隔）",
        after["due"] >= int(before.get("due") or 0),
        f"{before} -> {after}",
    )
    check(
        "没到期的卡答完也回原牌组",
        after["did"] == STATE["source_deck_id"] and after["odid"] == 0,
        f"{before} -> {after}",
    )

    # ② 今天到期的 J：答「良好」——这才是正常复习算法，间隔与到期时间都被改写
    due_target = cid("J-复习到期")
    answer_card(col, due_target, 3)
    after_due = card_snapshot(col, due_target)
    revlog_after = revlog_by_type(col)
    record(
        "cram_writeback_answer",
        {
            "not_due": {"before": before, "after": after},
            "due": {"before": before_due, "after": after_due},
            "revlog_before": revlog_before,
            "revlog_after": revlog_after,
            "cards": cards_of(col, temp_id),
        },
    )
    check(
        "影响排期时到期卡的作答次数 +1",
        after_due["reps"] == int(before_due.get("reps") or 0) + 1,
        f"{before_due} -> {after_due}",
    )
    check(
        "影响排期时到期卡的间隔/到期时间被算法改写",
        int(after_due["ivl"] or 0) > int(before_due.get("ivl") or 0)
        and after_due["due"] != before_due.get("due"),
        f"{before_due} -> {after_due}",
    )
    check(
        "到期卡作答后带着新排期回原牌组",
        after_due["did"] == STATE["source_deck_id"] and after_due["odid"] == 0,
        f"{before_due} -> {after_due}",
    )
    check(
        "影响排期时每次作答各记一条正式复习日志",
        sum(
            int(revlog_after.get(key, 0)) - int(revlog_before.get(key, 0))
            for key in ("0", "1", "2", "3")
        )
        == 2,
        f"{revlog_before} -> {revlog_after}",
    )


def step_cram_auto_skip() -> None:
    """集中刷进行中：进原牌组不该自动重收；切回原牌组才问「要结束吗」。"""
    col = mw.col
    module = tfd()
    deck_id = STATE["assault_deck_id"]
    temp_id = cram_temp_id(deck_id)
    note("cram_skip_session", cram_session(deck_id))
    note("cram_skip_temp_id", temp_id)
    note("cram_skip_origin_cards", cards_of(col, deck_id))
    note("cram_skip_temp_cards", cards_of(col, temp_id) if temp_id else [])

    calls: list[str] = []
    opened: list[int] = []
    original_ask = module.askUser
    original_open = module.open_cram_window
    # 假装用户选了「继续集中刷」（返回 False），并把询问文案记下来
    module.askUser = lambda text, **kw: (calls.append(str(text)) or False)
    module.open_cram_window = lambda did, **kw: opened.append(int(did))
    try:
        # ① 停在临时集中刷牌组自己的总览：正常刷题，不该问
        col.decks.select(DeckId(temp_id))
        module.on_state_did_change("overview", "deckBrowser")
        note("cram_skip_temp_asks", len(calls))
        # ② 切回原筛选牌组：算「离开集中刷」，该问
        col.decks.select(DeckId(deck_id))
        module.on_state_did_change("overview", "deckBrowser")
        note("cram_skip_origin_asks", len(calls))
    finally:
        module.askUser = original_ask
        module.open_cram_window = original_open
    note("cram_skip_calls", calls)
    note("cram_skip_opened", opened)


def step_cram_auto_skip_check() -> None:
    col = mw.col
    module = tfd()
    deck_id = STATE["assault_deck_id"]
    temp_id = int(STATE.get("cram_skip_temp_id") or 0)
    session = cram_session(deck_id)
    temp_cards = cards_of(col, temp_id) if temp_id else []
    status = ""
    manager = module.ManagerDialog(mw)
    try:
        manager._populate(
            [
                {
                    "deck_id": deck_id,
                    "name": module._deck_name(col, deck_id),
                    "rule": module.get_rule(deck_id, col) or {},
                    "exists": True,
                    "filtered": True,
                    "managed": True,
                    "cram": session,
                    "cards": len(cards_of(col, deck_id)),
                }
            ]
        )
        row = manager.tree.topLevelItem(0)
        status = row.text(6) if row else ""
    except Exception:
        status = f"出错：{traceback.format_exc()}"
    finally:
        try:
            manager.reject()
        except Exception:
            pass

    record(
        "cram_auto_skip",
        {
            "session": STATE.get("cram_skip_session"),
            "temp_id": temp_id,
            "origin_before": STATE.get("cram_skip_origin_cards"),
            "temp_before": STATE.get("cram_skip_temp_cards"),
            "origin_after": cards_of(col, deck_id),
            "temp_after": temp_cards,
            "temp_asks": STATE.get("cram_skip_temp_asks"),
            "origin_asks": STATE.get("cram_skip_origin_asks"),
            "calls": STATE.get("cram_skip_calls"),
            "opened": STATE.get("cram_skip_opened"),
            "manager_status": status,
        },
    )
    check("集中刷期间原筛选牌组保持空着（没被自动重收）", cards_of(col, deck_id) == [], str(cards_of(col, deck_id)))
    # 「影响排期」那一轮把 I（未到期复习）和 J（今天到期复习）各答了一次「良好」，
    # 两张都按 Anki 预览模式送回原牌组，所以临时集中刷牌组此时应是 7-2=5 张。
    # 这里要证明的是「没有被自动重收踢走」，所以按名字推算期望值，而不是写死个数。
    expected_temp = ids_of_set(*ASSAULT_EXPECTED) - ids_of_set("I-复习未到期", "J-复习到期")
    check(
        "临时集中刷牌组里的卡没被踢走（只剩还没答过的 5 张）",
        set(temp_cards) == expected_temp,
        f"{temp_cards} != {sorted(expected_temp)}",
    )
    check("集中刷记录还在", bool(session), str(session))
    check("停在临时集中刷牌组时不会问「要结束吗」", int(STATE.get("cram_skip_temp_asks") or 0) == 0, str(STATE.get("cram_skip_temp_asks")))
    check(
        "切回原牌组时问了「还在集中刷」",
        int(STATE.get("cram_skip_origin_asks") or 0) == 1,
        str(STATE.get("cram_skip_calls")),
    )
    check(
        "询问文案提到「还在集中刷」",
        any("还在集中刷" in str(text) for text in (STATE.get("cram_skip_calls") or [])),
        str(STATE.get("cram_skip_calls")),
    )
    check(
        "选「继续集中刷」会把小窗调出来",
        STATE.get("cram_skip_opened") == [int(deck_id)],
        str(STATE.get("cram_skip_opened")),
    )
    check(
        "管理窗口显示「集中刷中」并说明用的是临时牌组、自动重收已跳过",
        "集中刷中" in status and "临时牌组" in status and "自动重收已跳过" in status,
        status,
    )


def step_cram_window() -> None:
    """集中刷小窗：调出来、看得懂、菜单里给了「结束集中刷」。"""
    module = tfd()
    col = mw.col
    deck_id = STATE["assault_deck_id"]
    temp_id = cram_temp_id(deck_id)
    window = module.open_cram_window(deck_id, parent=mw)
    info = cram_window_info(module, deck_id)
    repeated = module.open_cram_window(deck_id, parent=mw)

    menu = QMenu()
    module.on_deck_browser_menu(menu, deck_id)
    labels = [action.text() for action in menu.actions() if action.text()]
    record(
        "cram_window",
        {
            "opened": window is not None,
            "info": info,
            "same_window": repeated is window,
            "registered": module.find_cram_window(deck_id) is not None,
            "temp_id": temp_id,
            "menu": labels,
            "session": cram_session(deck_id),
        },
    )
    check("集中刷时能打开小窗", window is not None)
    check(
        "小窗标题带「集中刷」和原牌组名",
        bool(info)
        and str(info.get("title", "")).startswith("集中刷 · ")
        and module._deck_name(col, deck_id) in str(info.get("title", "")),
        str(info.get("title")),
    )
    check(
        "小窗写明了这次的刷题模式（会写回排期）",
        "写回" in str(info.get("mode", "")),
        str(info.get("mode")),
    )
    check(
        "小窗写明用的是临时牌组和轮次",
        "临时牌组" in str(info.get("time", "")) and "第 1 轮" in str(info.get("time", "")),
        str(info.get("time")),
    )
    check(
        "小窗指向临时集中刷牌组",
        int(info.get("current_cram_deck") or 0) == temp_id and temp_id > 0,
        f"{info.get('current_cram_deck')} vs {temp_id}",
    )
    check(
        "小窗有「去刷题」「再刷一轮」「结束集中刷」三个按钮",
        info.get("go") == "去刷题"
        and info.get("again") == "再刷一轮"
        and "结束集中刷" in str(info.get("end", "")),
        str(info),
    )
    check("再点一次调出的是同一个窗口", repeated is window)
    check("小窗登记在案，随后能按牌组找到", module.find_cram_window(deck_id) is not None)
    check(
        "牌组右键只留「打开集中刷窗口」（1.5.0 精简掉重复的结束入口）",
        "打开集中刷窗口" in labels
        and not any("结束集中刷" in label for label in labels),
        str(labels),
    )


def step_cram_window_check() -> None:
    """**关掉小窗就等于结束集中刷**：临时牌组被删、原牌组按规则收回来。"""
    col = mw.col
    module = tfd()
    deck_id = STATE["assault_deck_id"]
    phase = STATE.get("cram_window_phase") or "start"

    if phase == "start":
        session_before = cram_session(deck_id)
        temp_id = cram_temp_id(deck_id)
        window = module.find_cram_window(deck_id)
        if window is None:
            record("cram_window_close", {"error": "集中刷小窗没找到"})
            check("集中刷小窗开着，能按关窗结束", False, "窗口没找到")
            return
        window.close()
        session_after = cram_session(deck_id)
        record(
            "cram_window_close",
            {
                "session_before": session_before,
                "session_after": session_after,
                "window_gone": module.find_cram_window(deck_id) is None,
                "temp_id": temp_id,
            },
        )
        check("关窗之前集中刷还在", bool(session_before), str(session_before))
        check("关窗立刻结束这次集中刷（记录已清）", session_after is None, str(session_after))
        check("小窗已从登记表里移除", module.find_cram_window(deck_id) is None)
        STATE["cram_window_phase"] = "waiting"
        STATE["cram_window_temp"] = temp_id
        requeue("cram_window_check", step_cram_window_check, "cram_window_attempt", delay=1500)
        return

    temp_id = int(STATE.get("cram_window_temp") or 0)
    done, info = cram_cleanup_state(deck_id, temp_id, assault_expected())
    if not done:
        if requeue("cram_window_check", step_cram_window_check, "cram_window_attempt", delay=1500):
            return
    STATE.pop("cram_window_attempt", None)

    actual = module.read_actual(col, deck_id) or {}
    rule = module.get_rule(deck_id, col) or {}
    menu = QMenu()
    module.on_deck_browser_menu(menu, deck_id)
    labels = [action.text() for action in menu.actions() if action.text()]
    record(
        "cram_window_cleanup",
        {
            "temp_id": temp_id,
            "temp_gone": info["temp_gone"],
            "cards": info["cards"],
            "expected": assault_expected(),
            "session": info["session"],
            "actual": actual,
            "rule_search": rule.get("search"),
            "menu": labels,
        },
    )
    check("关窗后临时集中刷牌组被删掉", bool(info["temp_gone"]), str(info))
    check(
        "关窗后原筛选牌组按规则收回来",
        info["cards"] == assault_expected(),
        f"{info['cards']} != {assault_expected()}",
    )
    check("关窗后集中刷记录清干净", info["session"] is None, str(info["session"]))
    check("原牌组仍是「作答重排原卡」的日常筛选牌组", actual.get("reschedule") is True, str(actual))
    check(
        "原牌组的搜索式回到规则本身",
        bool(actual)
        and set(col.find_cards(actual["search"]))
        == set(col.find_cards(str(rule.get("search") or ""))),
        f"{actual} vs {rule}",
    )
    check(
        "右键菜单回到「集中刷…」入口",
        any(str(label).startswith("集中刷…") for label in labels),
        str(labels),
    )


def step_cram_end() -> None:
    """再开一轮集中刷，准备走「结束集中刷」按钮那条路。"""
    module = tfd()
    deck_id = STATE["assault_deck_id"]
    note("cram_end_sync", reset_cram_sync(deck_id, "探针准备测结束集中刷"))
    module.start_cram(deck_id, False, parent=mw, study=False)


def step_cram_end_check() -> None:
    col = mw.col
    module = tfd()
    deck_id = STATE["assault_deck_id"]
    phase = STATE.get("cram_end_phase") or "start"

    if phase == "start":
        session = cram_session(deck_id)
        temp_id = cram_temp_id(deck_id)
        cards = cards_of(col, temp_id) if temp_id else []
        if not (session is not None and temp_id > 0 and len(cards) == 7):
            if requeue("cram_end_check", step_cram_end_check, "cram_end_attempt", delay=1500):
                return
        STATE.pop("cram_end_attempt", None)
        record(
            "cram_end_ready",
            {"temp_id": temp_id, "temp_cards": cards, "session": session,
             "origin_cards": cards_of(col, deck_id)},
        )
        check("结束前临时集中刷牌组收满 7 张", cards == assault_expected(), str(cards))
        check("结束前原筛选牌组是空的", cards_of(col, deck_id) == [], str(cards_of(col, deck_id)))
        module.end_cram(deck_id, parent=mw, quiet=True, close_window=False)
        after = cram_session(deck_id)
        record("cram_end_requested", {"session_after": after})
        check("点结束时集中刷记录立刻清掉", after is None, str(after))
        STATE["cram_end_phase"] = "waiting"
        STATE["cram_end_temp"] = temp_id
        requeue("cram_end_check", step_cram_end_check, "cram_end_wait", delay=1500)
        return

    temp_id = int(STATE.get("cram_end_temp") or 0)
    done, info = cram_cleanup_state(deck_id, temp_id, assault_expected())
    if not done:
        if requeue("cram_end_check", step_cram_end_check, "cram_end_wait", delay=1500):
            return
    STATE.pop("cram_end_wait", None)
    rule = module.get_rule(deck_id, col) or {}
    record(
        "cram_end",
        {
            "temp_id": temp_id,
            "temp_gone": info["temp_gone"],
            "cards": info["cards"],
            "expected": assault_expected(),
            "session": info["session"],
            "rule": rule,
        },
    )
    check("结束后临时集中刷牌组被删掉", bool(info["temp_gone"]), str(info))
    check(
        "结束后原筛选牌组按规则收回来",
        info["cards"] == assault_expected(),
        f"{info['cards']} != {assault_expected()}",
    )
    check("结束后集中刷记录清干净", info["session"] is None, str(info["session"]))
    check(
        "原规则还在、仍是「含未到期」的突击牌组",
        rule.get("include_not_due") is True,
        str(rule),
    )


def step_cram_leave_ask() -> None:
    """离开集中刷那个牌组时，会不会弹「要结束集中刷吗」。"""
    module = tfd()
    col = mw.col
    deck_id = STATE["assault_deck_id"]
    # 借一个现成的筛选牌组当「临时集中刷牌组」；这一步只测判定，不建也不删牌组
    fake_temp = int(STATE["union_deck_id"])
    module.put_cram_session(
        deck_id,
        rl().make_cram_session(False, int(time.time()), fake_temp, 1),
        col,
    )

    calls: list[str] = []
    opened: list[int] = []
    original_ask = module.askUser
    original_open = module.open_cram_window
    # 假装用户点了「继续集中刷」（返回 False），并把询问文案记下来
    module.askUser = lambda text, **kw: (calls.append(str(text)) or False)
    module.open_cram_window = lambda did, **kw: opened.append(int(did))
    try:
        # ① 停在临时集中刷牌组自己的总览：不该问
        col.decks.select(DeckId(fake_temp))
        module._maybe_ask_cram_end("overview", "deckBrowser")
        staying = len(calls)
        # ② 切到原筛选牌组：该问
        col.decks.select(DeckId(deck_id))
        module._maybe_ask_cram_end("overview", "review")
        leaving = len(calls)
        # ③ 回到牌组列表：也该问
        module._maybe_ask_cram_end("deckBrowser", "overview")
        browser = len(calls)
    finally:
        module.askUser = original_ask
        module.open_cram_window = original_open

    data = {
        "fake_temp": fake_temp,
        "calls": calls,
        "staying": staying,
        "leaving": leaving,
        "browser": browser,
        "opened": opened,
    }
    record("cram_leave_ask", data)
    STATE["cram_leave_ask"] = data


def step_cram_leave_ask_check() -> None:
    module = tfd()
    logic = rl()
    col = mw.col
    deck_id = STATE["assault_deck_id"]
    data = STATE.get("cram_leave_ask") or {}
    fake_temp = int(data.get("fake_temp") or 0)
    table = {
        "no_session": logic.should_ask_cram_end("deckBrowser", "overview", 1, 0),
        "staying": logic.should_ask_cram_end("overview", "deckBrowser", 5, 5),
        "leaving": logic.should_ask_cram_end("overview", "review", 6, 5),
        "browser": logic.should_ask_cram_end("deckBrowser", "overview", 0, 5),
        "unchanged": logic.should_ask_cram_end("overview", "overview", 6, 5),
        "irrelevant": logic.should_ask_cram_end("stats", "overview", 6, 5),
    }
    record("cram_leave_table", {"table": table, "calls": data.get("calls")})
    check("没有集中刷时不会问", table["no_session"] is False, str(table))
    check("停在临时集中刷牌组自己不问", table["staying"] is False, str(table))
    check("切到别的牌组要问", table["leaving"] is True, str(table))
    check("回到牌组列表要问", table["browser"] is True, str(table))
    check("状态没变不问", table["unchanged"] is False, str(table))
    check("与集中刷无关的界面状态不问", table["irrelevant"] is False, str(table))
    check(
        "离开牌组时真的问了（停在原地没问）",
        int(data.get("staying") or 0) == 0
        and int(data.get("leaving") or 0) == 1
        and int(data.get("browser") or 0) == 2,
        str(data),
    )
    check(
        "询问文案提到「还在集中刷」",
        any("还在集中刷" in str(text) for text in (data.get("calls") or [])),
        str(data.get("calls")),
    )
    check(
        "选「继续集中刷」会把小窗调出来",
        data.get("opened") == [int(deck_id), int(deck_id)],
        str(data.get("opened")),
    )

    # 收尾：把这一步借来的假会话清干净
    module.drop_cram_session(deck_id, col)
    col.decks.select(DeckId(int(STATE["source_deck_id"])))
    record(
        "cram_leave_cleanup",
        {"session": cram_session(deck_id), "fake_temp": fake_temp,
         "selected": int(col.decks.selected())},
    )
    check("假会话已清掉", cram_session(deck_id) is None)
    check("收尾后不再停在集中刷状态", cram_session(deck_id) is None)


def step_cram_log() -> None:
    """插件自己的日志文件与 crash.log 解析。"""
    module = tfd()
    logic = rl()
    marker = f"探针日志 {int(time.time())}"
    module._log(marker)
    path = module._log_path()
    text = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        text = ""

    crash_text = ""
    try:
        crash_text = module.crash_culprit_text()
    except Exception:
        crash_text = ""

    found = None
    crash_path = ""
    try:
        base = str(getattr(getattr(mw, "pm", None), "base", "") or "")
        crash_path = os.path.join(base, "crash.log") if base else ""
        if crash_path and os.path.isfile(crash_path):
            with open(crash_path, encoding="utf-8", errors="replace") as fh:
                found = logic.crash_log_culprit(fh.read())
    except Exception:
        found = None

    record(
        "log_probe",
        {
            "path": path,
            "has_marker": marker in text,
            "crash_path": crash_path,
            "crash_text": crash_text,
            "crash_found": found,
            "culprit_label": logic.crash_culprit_label(found, 1790401455),
        },
    )
    check("日志写进了 user_files/log.txt", marker in text, path)
    check(
        "日志路径在插件目录下的 user_files",
        path.endswith(os.path.join("user_files", "log.txt")),
        path,
    )
    check(
        "crash.log 有记录时能解析出插件名",
        found is None or bool(found.get("addon")),
        str(found),
    )


def step_cram_stale_cleanup() -> None:
    """崩溃/强关留下的临时集中刷牌组：下次打开用户配置自动收掉。"""
    col = mw.col
    module = tfd()
    logic = rl()
    deck_id = STATE["assault_deck_id"]
    name = module._deck_name(col, deck_id)
    # 先把别的筛选牌组清空：Anki 不会让两张筛选牌组抢同一张卡，卡还挂在
    # 原筛选牌组里的话，这个「残留的临时牌组」根本收不到卡。
    note("cram_stale_freed", empty_known_filtered(col))
    temp_name = logic.cram_temp_deck_name(
        name, module._deck_names(col, include_filtered=True)
    )
    deck = col.sched.get_or_create_filtered_deck(DeckId(0))
    deck.name = temp_name
    deck.allow_empty = True
    deck.config.reschedule = False
    del deck.config.search_terms[:]
    deck.config.search_terms.append(
        FilteredDeckConfig.SearchTerm(
            search=logic.cram_search_query(
                ["宋词"], logic.MODE_UNION, list(logic.DEFAULT_STATES)
            ),
            limit=9999,
            order=logic.DEFAULT_ORDER,
        )
    )
    temp_id = int(col.sched.add_or_update_filtered_deck(deck).id)
    col.sched.rebuild_filtered_deck(DeckId(temp_id))
    module.put_cram_session(
        deck_id, logic.make_cram_session(False, int(time.time()), temp_id, 3), col
    )
    note("cram_stale_temp_id", temp_id)
    note("cram_stale_temp_name", temp_name)
    note("cram_stale_temp_cards", cards_of(col, temp_id))
    note("cram_stale_session", cram_session(deck_id))
    note("cram_stale_ended", module.end_stale_cram_sessions(col))


def step_cram_stale_cleanup_check() -> None:
    col = mw.col
    module = tfd()
    deck_id = STATE["assault_deck_id"]
    temp_id = int(STATE.get("cram_stale_temp_id") or 0)
    done, info = cram_cleanup_state(deck_id, temp_id, assault_expected())
    if not done:
        if requeue("cram_stale_cleanup_check", step_cram_stale_cleanup_check, "cram_stale_attempt", delay=1500):
            return
    STATE.pop("cram_stale_attempt", None)
    rule = module.get_rule(deck_id, col) or {}
    record(
        "cram_stale_cleanup",
        {
            "temp_id": temp_id,
            "temp_name": STATE.get("cram_stale_temp_name"),
            "temp_cards_before": STATE.get("cram_stale_temp_cards"),
            "session_before": STATE.get("cram_stale_session"),
            "ended": STATE.get("cram_stale_ended"),
            "temp_gone": info["temp_gone"],
            "cards": info["cards"],
            "session": info["session"],
            "rule": rule,
        },
    )
    check("上一次留下的临时集中刷牌组确实收进了卡", len(STATE.get("cram_stale_temp_cards") or []) == 7, str(STATE.get("cram_stale_temp_cards")))
    check("上次的集中刷记录确实在", bool(STATE.get("cram_stale_session")))
    check("打开用户配置时报告结束了残留集中刷", int(STATE.get("cram_stale_ended") or 0) >= 1)
    check("残留的临时集中刷牌组被删掉", bool(info["temp_gone"]), str(info))
    check(
        "残留清理后原筛选牌组按规则收回来",
        info["cards"] == assault_expected(),
        f"{info['cards']} != {assault_expected()}",
    )
    check("残留的集中刷记录被清掉", info["session"] is None, str(info["session"]))
    check(
        "规则没被残留清理弄丢",
        bool(rule) and rule.get("include_not_due") is True,
        str(rule),
    )

    # 收尾：让卡回源牌组，后面的步骤都在「卡都在源牌组」的前提下跑
    col.sched.empty_filtered_deck(DeckId(deck_id))
    record("cram_stale_cleanup_tail", {"origin_after": cards_of(col, deck_id)})
    check("收尾后原筛选牌组已空", cards_of(col, deck_id) == [], str(cards_of(col, deck_id)))


def step_cram_no_auto_rebuild() -> None:
    """突击牌组（含未到期）不该自动重收：进牌组和启动那一遍都不收。"""
    col = mw.col
    module = tfd()
    deck_id = STATE["assault_deck_id"]
    note("cram_no_auto_emptied", empty_known_filtered(col, "assault_deck_id"))
    note("cram_no_auto_before", cards_of(col, deck_id))
    col.decks.select(DeckId(deck_id))
    module.on_state_did_change("overview", "deckBrowser")
    note("cram_no_auto_after_state", cards_of(col, deck_id))
    module.rebuild_all_managed(quiet=True)


def step_cram_no_auto_rebuild_check() -> None:
    col = mw.col
    module = tfd()
    deck_id = STATE["assault_deck_id"]
    cards = cards_of(col, deck_id)
    rule = module.get_rule(deck_id, col) or {}
    actual = module.read_actual(col, deck_id) or {}
    record(
        "cram_no_auto_rebuild",
        {
            "origin_before": STATE.get("cram_no_auto_before"),
            "origin_after_state": STATE.get("cram_no_auto_after_state"),
            "origin_after_all": cards,
            "session": cram_session(deck_id),
            "rule": rule,
            "actual": actual,
            "emptied": STATE.get("cram_no_auto_emptied"),
        },
    )
    check("突击牌组进牌组时不自动重收", STATE.get("cram_no_auto_after_state") == [], str(STATE.get("cram_no_auto_after_state")))
    check("启动那一遍也跳过突击牌组", cards == [], str(cards))
    check("突击牌组期间没有集中刷记录", cram_session(deck_id) is None, str(cram_session(deck_id)))
    check(
        "规则还在、仍是「含未到期」的突击牌组",
        rule.get("include_not_due") is True and rule.get("tags") == ["宋词"],
        str(rule),
    )
    check("突击牌组仍是筛选牌组、仍是写回排期", module._is_filtered(col, deck_id) and actual.get("reschedule") is True, str(actual))


def step_auto_rebuild_switch() -> None:
    """「自动重收」关掉后，进牌组不再重收。"""
    col = mw.col
    module = tfd()
    deck_id = STATE["union_deck_id"]
    settings = module._addon_config()
    settings["auto_rebuild"] = False
    note("auto_rebuild_save_off", module.save_addon_config(settings))
    col.sched.empty_filtered_deck(DeckId(deck_id))
    note("auto_off_before", cards_of(col, deck_id))
    col.decks.select(DeckId(deck_id))
    module.on_state_did_change("overview", "deckBrowser")


def step_auto_rebuild_switch_check() -> None:
    col = mw.col
    module = tfd()
    deck_id = STATE["union_deck_id"]
    cards = cards_of(col, deck_id)
    enabled = module.auto_rebuild_enabled()
    record(
        "auto_rebuild_off",
        {
            "saved": STATE.get("auto_rebuild_save_off"),
            "enabled_after_off": enabled,
            "before": STATE.get("auto_off_before"),
            "after": cards,
        },
    )
    check("关掉自动重收后进牌组不重收", cards == [], str(cards))
    check("开关确实关上了", enabled is False, str(enabled))

    # 打开开关，用「Anki 启动时那一遍」把牌组收回来
    settings = module._addon_config()
    settings["auto_rebuild"] = True
    module.save_addon_config(settings)
    check("开关恢复成打开", module.auto_rebuild_enabled() is True)
    col.sched.empty_filtered_deck(DeckId(deck_id))
    note("startup_before", cards_of(col, deck_id))
    module.rebuild_all_managed(quiet=True)


def step_startup_rebuild_check() -> None:
    col = mw.col
    module = tfd()
    deck_id = STATE["union_deck_id"]
    cards = cards_of(col, deck_id)
    # J-复习到期 在「影响排期」的集中刷那一轮被答了一次「良好」：间隔 3→6、到期日
    # 从今天推到 6 天后，于是它不再是「已到期」的卡，「只收到期」的并集牌组自然收不到
    # 它——这是正确行为，不是漏收。所以启动重收的期望名单里没有 J。
    expected = ids_of(
        "B-唐诗子标签",
        "C-宋词",
        "D-两标签",
        "E-三标签",
        "H-学习中",
        "K-别的筛选",
    )
    attempt = int(STATE.get("startup_attempt", 0)) + 1
    STATE["startup_attempt"] = attempt
    if cards != expected and attempt < 120:
        record(
            "startup_rebuild",
            {"attempt": attempt, "before": STATE.get("startup_before"), "after": cards},
        )
        STEP_QUEUE.insert(
            0, ("startup_rebuild_check", step_startup_rebuild_check, 1500)
        )
        return
    record(
        "startup_rebuild",
        {
            "attempt": attempt,
            "before": STATE.get("startup_before"),
            "after": cards,
            "expected": expected,
        },
    )
    check("启动时那一遍把牌组按规则收回来", cards == expected, f"{cards} != {expected}")
    check("进牌组的自动重收也是打开的", module.auto_rebuild_enabled() is True)

    # 把别的托管牌组清空，免得它们占着卡影响后面的删除步骤（那几步本来就跑在
    # 「卡都在源牌组」的前提下）
    for key in ("subtag_deck_id", "intersection_deck_id"):
        other = STATE.get(key)
        if other:
            col.sched.empty_filtered_deck(DeckId(int(other)))
    others = {
        key: cards_of(col, int(STATE[key]))
        for key in ("subtag_deck_id", "intersection_deck_id")
    }
    record("other_decks_emptied", others)
    check(
        "其它托管牌组已清空",
        all(not value for value in others.values()),
        str(others),
    )


# --------------------------------------------------------------------------
# 更新层（纯逻辑，不联网）
# --------------------------------------------------------------------------


def step_update_layer() -> None:
    import io
    import zipfile

    module = tfd()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", '{"package": "tag_filtered_deck"}')
        archive.writestr("__init__.py", "# probe\n")
        archive.writestr("README.md", "x" * 900)
    package = buffer.getvalue()

    urls = module._download_urls(module.UPDATE_ASSET)
    state_before = module._update_state()
    module._save_update_state({"checked_at": 123, "latest": "9.9.9", "probe": True})
    state_after = module._update_state()
    module._save_update_state(state_before)

    record(
        "update_layer",
        {
            "newer": module.is_newer("1.4.1", "1.4.0"),
            "same": module.is_newer("1.4.0", "1.4.0"),
            "older": module.is_newer("1.3.9", "1.4.0"),
            "v_prefix": module.is_newer("v1.5", "1.4.0"),
            "junk": module.is_newer("abc", "1.4.0"),
            "running": module.__version__,
            "on_disk": module.local_disk_version(),
            "repo": module.UPDATE_REPO,
            "asset": module.UPDATE_ASSET,
            "repo_ready": module._update_repo_ready(),
            "urls": urls,
            "good_package": module._looks_like_package(package),
            "truncated_package": module._looks_like_package(
                b"PK\x03\x04" + b"0" * 900
            ),
            "empty_package": module._looks_like_package(b""),
            "state_before": state_before,
            "state_after": state_after,
            "prompt": module.update_prompt_text("1.4.0"),
            "pending": module.update_pending_restart_text("1.4.0"),
            "anki_addon_updates": module.anki_addon_updates_enabled(),
        },
    )
    check(
        "版本比较认得新版/同版/旧版",
        module.is_newer("1.4.1", "1.4.0")
        and not module.is_newer("1.4.0", "1.4.0")
        and not module.is_newer("1.3.9", "1.4.0"),
    )
    check("版本号带 v 前缀也能比", module.is_newer("v1.5", "1.4.0"))
    check("认不出的版本号当成 0，不会误报新版", not module.is_newer("abc", "1.4.0"))
    # 版本号跟着 version.txt 走，避免每次发版都要回来改探针
    expected_version_file = os.path.join(
        os.path.dirname(os.path.abspath(str(module.__file__))), "version.txt"
    )
    try:
        with open(expected_version_file, encoding="utf-8") as fh:
            expected_version = fh.read().strip()
    except Exception:
        expected_version = ""
    check(
        "运行中的版本和 version.txt 是同一个",
        bool(expected_version) and module.__version__ == expected_version,
        f"{module.__version__} vs {expected_version}",
    )
    check(
        "磁盘上的版本读得出来且一致",
        bool(expected_version) and module.local_disk_version() == expected_version,
        f"{module.local_disk_version()} vs {expected_version}",
    )
    check(
        "更新仓库已配置",
        module._update_repo_ready()
        and module.UPDATE_REPO == "creeperboo/anki-tag-filtered-deck",
        module.UPDATE_REPO,
    )
    check(
        "raw 优先、Release 附件兜底",
        len(urls) == 2
        and urls[0].startswith(
            "https://raw.githubusercontent.com/creeperboo/anki-tag-filtered-deck/"
        )
        and "/releases/latest/download/" in urls[1],
        str(urls),
    )
    check(
        "完整包能识别、半截包能挡下",
        module._looks_like_package(package)
        and not module._looks_like_package(b"PK\x03\x04" + b"0" * 900)
        and not module._looks_like_package(b""),
    )
    check(
        "更新状态能写能读",
        state_after.get("latest") == "9.9.9" and state_after.get("probe") is True,
        str(state_after),
    )
    check("已装好没重启时只提醒重启", "重启" in module.update_pending_restart_text("1.4.0"))
    check(
        "Anki 自己的更新开关读得出来",
        isinstance(module.anki_addon_updates_enabled(), bool),
    )


# --------------------------------------------------------------------------
# 1.5.1：集中刷的界面落点
#   ① 点「集中刷…」→ 直接进答题界面，小窗退到主窗口后面（不抢焦点）
#   ② 关掉小窗 / 点「结束集中刷」→ 除了数据收尾，界面也要跟着恢复原样
# --------------------------------------------------------------------------


def ui_state() -> str:
    return rl().state_key(getattr(mw, "state", ""))


def ui_selected() -> int:
    try:
        return int(mw.col.decks.selected())
    except Exception:
        return 0


def install_redraw_spy() -> list[dict[str, Any]]:
    """记下插件「结束集中刷后的界面收尾」到底被调用了几次、画的是哪一页。

    1.5.1 修的就是这一步：数据早就对了，但界面没人重画。探针直接把
    _redraw_after_cram 包一层，看它在收尾时有没有真的按当前页面画一次。
    """
    module = tfd()
    log = getattr(module, "_probe_redraw_log", None)
    if log is not None:
        return log
    log: list[dict[str, Any]] = []
    original = module._redraw_after_cram

    def spy(deck_id: Any, temp_ids: Any = ()) -> Any:
        entry: dict[str, Any] = {
            "deck_id": int(deck_id),
            "state_before": ui_state(),
            "selected_before": ui_selected(),
            "temp_ids": [int(x) for x in (temp_ids or ())],
            "at": round(time.time() - float(STATE.get("cram_ui_t0") or 0), 3),
        }
        out = original(deck_id, temp_ids)
        entry["state_after"] = ui_state()
        entry["selected_after"] = ui_selected()
        entry["cards"] = cards_of(mw.col, int(deck_id))
        log.append(entry)
        return out

    module._redraw_after_cram = spy
    module._probe_redraw_log = log
    return log


def step_cram_ui_enter() -> None:
    """点「集中刷…」之后应当**直接进答题界面**，小窗退到后面。"""
    module = tfd()
    col = mw.col
    deck_id = STATE["assault_deck_id"]
    reset_cram_sync(deck_id, "探针准备测「集中刷直接进答题」")
    for key in ("cram_ui_phase", "cram_ui_attempt", "cram_ui_close_attempt",
                "cram_ui_button_attempt"):
        STATE.pop(key, None)
    install_redraw_spy()
    STATE["cram_ui_t0"] = time.time()
    note("cram_ui_window_before", module.find_cram_window(deck_id))
    # 从牌组列表出发，和用户真实点菜单的路径一致
    col.decks.select(DeckId(int(STATE["source_deck_id"])))
    mw.moveToState("deckBrowser")

    asked: list[dict[str, Any]] = []
    STATE["cram_ui_asked"] = asked
    STATE["cram_ui_ask_original"] = module.ask_cram_mode

    def fake_ask(parent, name, counts, delays, step_source="", source_info=None):
        asked.append(
            {
                "name": str(name),
                "delays": [int(x) for x in (delays or [])],
                "source": str(step_source),
                "collectible": int((counts or {}).get("collectible") or 0),
            }
        )
        return False  # 「不影响排期」

    module.ask_cram_mode = fake_ask
    module.begin_cram(deck_id, parent=mw)


def step_cram_ui_enter_check() -> None:
    module = tfd()
    col = mw.col
    deck_id = int(STATE["assault_deck_id"])
    temp_id = cram_temp_id(deck_id)
    temp_cards = cards_of(col, temp_id) if temp_id else []
    ready = (
        temp_id > 0
        and bool(temp_cards)
        and ui_state() == "review"
        and ui_selected() == temp_id
    )
    if not ready:
        if requeue(
            "cram_ui_enter_check",
            step_cram_ui_enter_check,
            "cram_ui_attempt",
            delay=1000,
            limit=60,
        ):
            return
    STATE.pop("cram_ui_attempt", None)
    STATE["cram_ui_temp"] = temp_id

    # 收工：把假的三选一对话框换回真的
    original_ask = STATE.pop("cram_ui_ask_original", None)
    if original_ask is not None:
        module.ask_cram_mode = original_ask

    window = module.find_cram_window(deck_id)
    info = cram_window_info(module, deck_id)
    parent_widget: Any = None
    activate_flag: Any = None
    if window is not None:
        try:
            parent_widget = window.parent()
        except Exception as exc:
            parent_widget = f"<{exc}>"
        try:
            activate_flag = bool(window.testAttribute(module._no_activate_flag()))
        except Exception as exc:
            activate_flag = f"<{exc}>"

    record(
        "cram_ui_enter",
        {
            "begin_before_window": STATE.get("cram_ui_window_before"),
            "asked": STATE.get("cram_ui_asked"),
            "temp_id": temp_id,
            "temp_cards": temp_cards,
            "state": ui_state(),
            "selected": ui_selected(),
            "window": window is not None,
            "window_parent_is_none": parent_widget is None,
            "show_without_activating": activate_flag,
            "writeback": info.get("writeback"),
        },
    )
    check(
        "点集中刷之前没有残留的小窗",
        STATE.get("cram_ui_window_before") is None,
        str(STATE.get("cram_ui_window_before")),
    )
    check(
        "问了「要不要影响排期」并且这一次选了不影响排期",
        bool(STATE.get("cram_ui_asked"))
        and STATE["cram_ui_asked"][0].get("collectible", 0) > 0,
        str(STATE.get("cram_ui_asked")),
    )
    check("临时集中刷牌组建好了、里面收进了卡", temp_id > 0 and bool(temp_cards), str(temp_cards))
    check(
        "点集中刷后直接进了答题界面（不用先回牌组再点一次）",
        ui_state() == "review",
        f"state={ui_state()}",
    )
    check(
        "答题界面用的就是临时集中刷牌组",
        ui_selected() == temp_id,
        f"{ui_selected()} != {temp_id}",
    )
    check("集中刷小窗还开着", window is not None)
    check(
        "小窗是独立窗口、不抢焦点（主窗口能盖在它前面）",
        parent_widget is None,
        str(parent_widget),
    )
    check(
        "小窗设了「只显示不激活」标志",
        activate_flag is True,
        str(activate_flag),
    )
    check("小窗写着这次不影响排期", info.get("writeback") is False, str(info.get("writeback")))


def _cram_ui_start_end(mode: str) -> None:
    """结束路径的公共开头：记下重画次数与收尾前状态，然后触发结束。"""
    module = tfd()
    col = mw.col
    deck_id = int(STATE["assault_deck_id"])
    log = install_redraw_spy()
    STATE["cram_ui_t0"] = time.time()
    STATE["cram_ui_redraw_before"] = len(log)
    STATE["cram_ui_temp"] = cram_temp_id(deck_id)
    STATE["cram_ui_state_before"] = ui_state()
    STATE["cram_ui_selected_before"] = ui_selected()
    window = module.find_cram_window(deck_id)
    record(
        f"cram_ui_{mode}_request",
        {
            "mode": mode,
            "window": window is not None,
            "state": STATE["cram_ui_state_before"],
            "selected": STATE["cram_ui_selected_before"],
            "temp_id": STATE["cram_ui_temp"],
            "origin_deck": deck_id,
        },
    )


def _cram_ui_finish_check(mode: str, attempt_key: str) -> bool:
    """结束路径的公共收尾：数据 + 界面都要恢复原样。返回 True 表示已经收敛。"""
    module = tfd()
    col = mw.col
    deck_id = int(STATE["assault_deck_id"])
    temp_id = int(STATE.get("cram_ui_temp") or 0)
    done, info = cram_cleanup_state(deck_id, temp_id, assault_expected())
    settled = done and ui_state() == "overview" and ui_selected() == deck_id
    if not settled:
        if requeue(
            f"cram_ui_{mode}_check",
            check_fn_for(mode),
            attempt_key,
            delay=1200,
        ):
            return False
    STATE.pop(attempt_key, None)

    log = install_redraw_spy()
    before = int(STATE.get("cram_ui_redraw_before") or 0)
    drawn = list(log[before:])
    record(
        f"cram_ui_{mode}",
        {
            "mode": mode,
            "temp_id": temp_id,
            "temp_gone": info["temp_gone"],
            "cards": info["cards"],
            "expected": assault_expected(),
            "session": info["session"],
            "state_before": STATE.get("cram_ui_state_before"),
            "selected_before": STATE.get("cram_ui_selected_before"),
            "state": ui_state(),
            "selected": ui_selected(),
            "origin_deck": deck_id,
            "redraws": drawn,
        },
    )
    prefix = "关窗" if mode == "close" else "点「结束集中刷」"
    check(
        f"{prefix}后临时集中刷牌组被删掉",
        bool(info["temp_gone"]),
        str(info),
    )
    check(
        f"{prefix}后原筛选牌组按规则收回来",
        info["cards"] == assault_expected(),
        f"{info['cards']} != {assault_expected()}",
    )
    check(f"{prefix}后集中刷记录清干净", info["session"] is None, str(info["session"]))
    check(
        f"{prefix}后选中的牌组回到原筛选牌组",
        ui_selected() == deck_id,
        f"{ui_selected()} != {deck_id}",
    )
    check(
        f"{prefix}后界面回到原牌组的总览页",
        ui_state() == "overview",
        f"state={ui_state()}",
    )
    check(
        f"{prefix}后界面被显式重画了一次（1.5.0 漏掉的就是这一步）",
        len(drawn) >= 1,
        str(drawn),
    )
    check(
        f"{prefix}重画的时候原牌组的张数已经是规则口径",
        bool(drawn) and drawn[-1].get("cards") == assault_expected(),
        str(drawn[-1] if drawn else None),
    )
    check(
        f"{prefix}重画的时候界面已经回到原牌组总览页",
        bool(drawn)
        and drawn[-1].get("state_after") == "overview"
        and drawn[-1].get("selected_after") == deck_id,
        str(drawn[-1] if drawn else None),
    )
    return True


def check_fn_for(mode: str) -> Callable[[], None]:
    return step_cram_ui_close_check if mode == "close" else step_cram_ui_button_check


def log_tail_after(marker: str) -> str:
    """读插件自己的日志里、marker 之后的那一段（用来核对界面收尾那几行）。"""
    try:
        path = tfd()._log_path()
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        return ""
    if not marker:
        return text[-4000:]
    index = text.rfind(marker)
    if index < 0:
        return ""
    return text[index:]


def step_cram_ui_browser_end() -> None:
    """1.5.2：在**牌组列表**上结束集中刷（用户这次的反馈）。

    走的是一条完整真实的路径：先开一轮集中刷、把主界面切到牌组列表，
    再真的触发一次界面切换，让 Anki 发出 state_did_change；插件据此问
    「要结束集中刷吗」，这里替用户回答「结束」。

    1.5.1 的问题就在这条路上：重画判定把状态名写成 ``"deckBrowser"``，而
    插件内部统一转成小写，两边永不相等 → 牌组列表这一页从来不重画，
    所以列表里还留着已经删掉的临时牌组、原牌组还显示旧张数。
    """
    module = tfd()
    col = mw.col
    deck_id = int(STATE["assault_deck_id"])
    reset_cram_sync(deck_id, "探针准备测「在牌组列表结束集中刷」")
    STATE.pop("cram_ui_browser_attempt", None)
    STATE.pop("cram_ui_browser_asked", None)
    STATE["cram_ui_browser_ready"] = False
    log = install_redraw_spy()
    STATE["cram_ui_t0"] = time.time()
    STATE["cram_ui_browser_redraw_before"] = len(log)
    # 从牌组列表出发，和用户真实点菜单的路径一致
    col.decks.select(DeckId(deck_id))
    mw.moveToState("deckBrowser")
    module.start_cram(deck_id, False, parent=mw, study=False)


def step_cram_ui_browser_end_check() -> None:
    module = tfd()
    col = mw.col
    deck_id = int(STATE["assault_deck_id"])

    if not STATE.get("cram_ui_browser_ready"):
        temp_id = cram_temp_id(deck_id)
        window = module.find_cram_window(deck_id)
        if not (temp_id > 0 and cards_of(col, temp_id) and window is not None):
            if requeue(
                "cram_ui_browser_end_check",
                step_cram_ui_browser_end_check,
                "cram_ui_browser_attempt",
                delay=1200,
            ):
                return
            check(
                "集中刷开起来了（在牌组列表结束这条路的起始条件）",
                False,
                f"temp={temp_id} cards={len(cards_of(col, temp_id)) if temp_id else 0}",
            )
            return

        STATE["cram_ui_browser_ready"] = True
        STATE["cram_ui_browser_temp"] = temp_id
        # 先把界面放回「临时牌组的总览页」，这样下面切到牌组列表一定是一次真实
        # 的状态变化——已经在牌组列表时再 moveToState("deckBrowser") 是空操作，
        # 钩子不会发出来，也就永远问不到，探针会假阴性。
        col.decks.select(DeckId(temp_id))
        mw.moveToState("overview")
        STATE["cram_ui_browser_state_before"] = ui_state()
        marker = f"探针牌组列表结束集中刷 {int(time.time() * 1000)}"
        module._log(marker)
        STATE["cram_ui_browser_marker"] = marker

        asked: list[dict[str, Any]] = []
        STATE["cram_ui_browser_asked"] = asked
        original_ask = module.askUser

        def responder(text: str, **_kw: Any) -> bool:
            asked.append(str(text))
            # 用户从临时牌组切回牌组列表，插件问「要结束集中刷吗」，这里选「结束」。
            return True

        module.askUser = responder
        try:
            # 真的切一次界面，让 Anki 自己发 state_did_change 钩子
            mw.moveToState("deckBrowser")
        finally:
            module.askUser = original_ask
        record(
            "cram_ui_browser_request",
            {
                "temp_id": temp_id,
                "state_before": STATE["cram_ui_browser_state_before"],
                "asked": list(asked),
                "origin_deck": deck_id,
            },
        )
        requeue(
            "cram_ui_browser_end_check",
            step_cram_ui_browser_end_check,
            "cram_ui_browser_attempt",
            delay=1200,
        )
        return

    temp_id = int(STATE.get("cram_ui_browser_temp") or 0)
    done, info = cram_cleanup_state(deck_id, temp_id, assault_expected())
    settled = done and ui_state() == "deckbrowser" and ui_selected() == deck_id
    if not settled:
        if requeue(
            "cram_ui_browser_end_check",
            step_cram_ui_browser_end_check,
            "cram_ui_browser_attempt",
            delay=1200,
        ):
            return
    STATE.pop("cram_ui_browser_attempt", None)

    log = install_redraw_spy()
    before = int(STATE.get("cram_ui_browser_redraw_before") or 0)
    drawn = list(log[before:])
    tail = log_tail_after(str(STATE.get("cram_ui_browser_marker") or ""))
    record(
        "cram_ui_browser_end",
        {
            "temp_id": temp_id,
            "temp_gone": info["temp_gone"],
            "cards": info["cards"],
            "expected": assault_expected(),
            "session": info["session"],
            "state": ui_state(),
            "selected": ui_selected(),
            "origin_deck": deck_id,
            "redraws": drawn,
            "log_tail": tail[-1500:],
        },
    )
    asked = list(STATE.get("cram_ui_browser_asked") or [])
    check(
        "在牌组列表上结束集中刷时，插件问了「要结束集中刷吗」",
        any("还在集中刷" in text for text in asked),
        str(asked),
    )
    check(
        "在牌组列表上结束集中刷后，临时集中刷牌组被删掉",
        bool(info["temp_gone"]),
        str(info),
    )
    check(
        "在牌组列表上结束集中刷后，原筛选牌组按规则收回来",
        info["cards"] == assault_expected(),
        f"{info['cards']} != {assault_expected()}",
    )
    check(
        "在牌组列表上结束集中刷后，集中刷记录清干净",
        info["session"] is None,
        str(info["session"]),
    )
    check(
        "界面还停在牌组列表、选中的是原筛选牌组",
        ui_state() == "deckbrowser" and ui_selected() == deck_id,
        f"state={ui_state()} selected={ui_selected()}",
    )
    check(
        "在牌组列表上结束集中刷后，界面被显式重画过一次",
        len(drawn) >= 1,
        str(drawn),
    )
    check(
        "重画的这一页确实是牌组列表（1.5.1 会画成「无」）",
        "画了 牌组列表" in tail,
        tail[-600:],
    )
    check(
        "重画的时候原牌组的张数已经是规则口径",
        bool(drawn) and drawn[-1].get("cards") == assault_expected(),
        str(drawn[-1] if drawn else None),
    )


def step_op_return_values() -> None:
    """1.5.2：所有后台操作交给 Anki 的返回值都必须是原生结果。"""
    module = tfd()
    col = mw.col
    spy = install_op_result_spy()
    violations = list(spy.get("violations") or [])
    kinds = dict(spy.get("kinds") or {})
    # 兜底路径：一组原生结果都拿不到时会走 _op_changes() 的空 OpChanges()
    fallback = module._op_changes()
    # 「要删的牌组已经不在了」（临时牌组被手动删掉后再结束集中刷）这条路，
    # 1.5.1 会返回 None，Anki 读到 .changes 就抛
    # 「'NoneType' object has no attribute 'changes'」并弹「遇到了问题」。
    _removed, gone_case = module._remove_filtered_decks(col, [0, 999_999_999])
    record(
        "op_return_values",
        {
            "checked": spy.get("checked"),
            "kinds": kinds,
            "violations": violations,
            "fallback": type(fallback).__name__,
            "gone_case": repr(gone_case),
        },
    )
    check(
        "兜底返回值也是合规的 OpChanges（一条原生结果都拿不到时用）",
        module.op_changes_like(fallback),
        type(fallback).__name__,
    )
    check(
        "要删的牌组已经不存在时，也交出合规返回值（不再返回 None）",
        module.op_changes_like(gone_case),
        repr(gone_case),
    )
    check(
        "后台操作返回值全部合规（不会再有 list/tuple/int 引起弹窗）",
        not violations,
        str(violations),
    )
    check(
        "这一轮确实跑够了后台操作（数量对得上）",
        int(spy.get("checked") or 0) >= 10,
        str(spy.get("checked")),
    )
    check(
        "Anki 原生结果（带 .changes 的 OpChanges 家族）确实出现过",
        any(str(name).startswith("OpChanges") for name in kinds),
        str(kinds),
    )


def step_cram_ui_close() -> None:
    """关掉集中刷小窗（× / Esc 那条路）：数据 + 界面都要恢复原样。"""
    module = tfd()
    deck_id = int(STATE["assault_deck_id"])
    _cram_ui_start_end("close")
    check(
        "关窗之前正停在集中刷的答题界面",
        STATE.get("cram_ui_state_before") == "review",
        str(STATE.get("cram_ui_state_before")),
    )
    check(
        "关窗之前选中的就是临时集中刷牌组",
        STATE.get("cram_ui_selected_before") == STATE.get("cram_ui_temp"),
        f"{STATE.get('cram_ui_selected_before')} vs {STATE.get('cram_ui_temp')}",
    )
    window = module.find_cram_window(deck_id)
    if window is None:
        check("集中刷小窗开着，能按关窗结束", False, "没找到小窗")
        return
    window.close()
    STATE["cram_ui_phase"] = "waiting"
    requeue(
        "cram_ui_close_check",
        step_cram_ui_close_check,
        "cram_ui_close_attempt",
        delay=1200,
    )


def step_cram_ui_close_check() -> None:
    if _cram_ui_finish_check("close", "cram_ui_close_attempt"):
        STATE.pop("cram_ui_phase", None)


def step_cram_ui_button() -> None:
    """再开一轮，走小窗里的「结束集中刷」按钮那条路。"""
    module = tfd()
    deck_id = int(STATE["assault_deck_id"])
    reset_cram_sync(deck_id, "探针准备测「结束集中刷」按钮")
    STATE.pop("cram_ui_button_attempt", None)
    STATE["cram_ui_button_ready"] = False
    module.start_cram(deck_id, False, parent=mw, study=False)


def step_cram_ui_button_check() -> None:
    module = tfd()
    col = mw.col
    deck_id = int(STATE["assault_deck_id"])
    if not STATE.get("cram_ui_button_ready"):
        temp_id = cram_temp_id(deck_id)
        window = module.find_cram_window(deck_id)
        if not (temp_id > 0 and cards_of(col, temp_id) and window is not None):
            if requeue(
                "cram_ui_button_check",
                step_cram_ui_button_check,
                "cram_ui_button_attempt",
                delay=1200,
            ):
                return
        # 停在临时牌组的总览页，模拟用户「刷完回到牌组」再点结束
        col.decks.select(DeckId(int(temp_id)))
        mw.moveToState("overview")
        STATE["cram_ui_button_ready"] = True
        STATE["cram_ui_button_request"] = {
            "temp_id": temp_id,
            "state": ui_state(),
            "selected": ui_selected(),
        }
        _cram_ui_start_end("button")
        record("cram_ui_button_ready", STATE["cram_ui_button_request"])
        try:
            window.end_button.click()
        except Exception as exc:
            check("「结束集中刷」按钮点得动", False, repr(exc))
            return
        requeue(
            "cram_ui_button_check",
            step_cram_ui_button_check,
            "cram_ui_button_attempt",
            delay=1200,
        )
        return
    _cram_ui_finish_check("button", "cram_ui_button_attempt")


STEPS: list[tuple[str, Callable[[], None], int]] = [
    ("import_and_menus", step_import, 600),
    ("sample_data", step_sample_data, 1200),
    ("include_not_due_switch", step_include_not_due_switch, 1500),
    ("union_deck", step_union_deck, 1400),
    ("other_deck_cleanup", step_other_deck_cleanup, 1200),
    ("dialogs", step_dialogs, 1600),
    ("subtag_deck", step_subtag_deck, 1400),
    ("intersection_deck", step_intersection_deck, 1400),
    ("answer", step_answer, 1400),
    ("auto_rebuild", step_auto_rebuild, 2600),
    ("auto_rebuild_check", step_auto_rebuild_check, 1400),
    ("no_rebuild_from_review", step_no_rebuild_from_review, 2600),
    ("no_rebuild_from_review_check", step_no_rebuild_from_review_check, 1400),
    ("external_change", step_external_change, 2600),
    ("external_change_check", step_external_change_check, 1400),
    ("move", step_move, 2600),
    ("move_check", step_move_check, 2400),
    ("blocked_browse", step_blocked_browse, 1600),
    ("cram_assault_setup", step_cram_assault_setup, 1600),
    ("cram_step_source", step_cram_step_source, 1800),
    ("cram_preview", step_cram_preview, 2000),
    ("cram_preview_check", step_cram_preview_check, 2000),
    ("cram_restart", step_cram_restart, 1800),
    ("cram_restart_check", step_cram_restart_check, 1800),
    ("cram_writeback", step_cram_writeback, 2000),
    ("cram_writeback_check", step_cram_writeback_check, 2000),
    ("cram_auto_skip", step_cram_auto_skip, 1800),
    ("cram_auto_skip_check", step_cram_auto_skip_check, 1800),
    ("cram_window", step_cram_window, 1400),
    ("cram_window_check", step_cram_window_check, 1500),
    ("cram_end", step_cram_end, 1800),
    ("cram_end_check", step_cram_end_check, 1500),
    ("cram_leave_ask", step_cram_leave_ask, 1200),
    ("cram_leave_ask_check", step_cram_leave_ask_check, 800),
    ("cram_log", step_cram_log, 600),
    ("cram_stale_cleanup", step_cram_stale_cleanup, 1800),
    ("cram_stale_cleanup_check", step_cram_stale_cleanup_check, 1500),
    ("cram_no_auto_rebuild", step_cram_no_auto_rebuild, 1800),
    ("cram_no_auto_rebuild_check", step_cram_no_auto_rebuild_check, 1800),
    ("auto_rebuild_switch", step_auto_rebuild_switch, 1800),
    ("auto_rebuild_switch_check", step_auto_rebuild_switch_check, 2600),
    ("startup_rebuild_check", step_startup_rebuild_check, 2600),
    ("update_layer", step_update_layer, 1200),
    ("delete", step_delete, 2600),
    ("delete_check", step_delete_check, 2500),
    ("search_normalization", step_search_normalization, 800),
    ("editor_live_counts", step_editor_live_counts, 2400),
    ("editor_live_counts_check", step_editor_live_counts_check, 600),
    # 1.5.1：集中刷的界面落点（进答题 / 关窗与按钮两条结束路径）
    ("cram_ui_enter", step_cram_ui_enter, 1500),
    ("cram_ui_enter_check", step_cram_ui_enter_check, 2600),
    ("cram_ui_close", step_cram_ui_close, 1500),
    ("cram_ui_close_check", step_cram_ui_close_check, 2600),
    ("cram_ui_button", step_cram_ui_button, 1800),
    ("cram_ui_button_check", step_cram_ui_button_check, 2600),
    # 1.5.2：在牌组列表上结束集中刷（用户这次的反馈）+ 后台返回值体检
    ("cram_ui_browser_end", step_cram_ui_browser_end, 1500),
    ("cram_ui_browser_end_check", step_cram_ui_browser_end_check, 2600),
    ("op_return_values", step_op_return_values, 1200),
    ("no_state_mutation", step_no_state_mutation, 800),
]


def start() -> None:
    STEP_QUEUE[:] = list(STEPS)
    write_result()
    name, fn, _delay = STEP_QUEUE.pop(0)
    run_step(name, fn)


gui_hooks.profile_did_open.append(lambda: QTimer.singleShot(1200, start))
