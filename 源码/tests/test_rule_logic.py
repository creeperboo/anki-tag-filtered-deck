"""rule_logic 的纯逻辑测试（不需要 Anki，普通 Python 就能跑）。

跑法：
    python -m unittest discover -s 源码\tests -p "test_*.py"
或直接双击项目根目录的「测试.cmd」。

这里只测不依赖 Anki 的部分：搜索式生成、名称生成、规则迁移、统计与预览文案。
真正落到 Anki 上的行为（收录结果、暂停卡不被改动、删除后卡片回原牌组）由
`工具\anki_probe.py` 在隔离用户配置里验证。

取卡范围是三个状态：未学习（is:new）、学习中（is:learn）、复习中
（is:review -is:learn）。三项互不重复，相加正好等于命中总数。
"""

from __future__ import annotations

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import rule_logic as rl  # noqa: E402


# --------------------------------------------------------------------------
# 标签 → 搜索式
# --------------------------------------------------------------------------


class TestTagQuery(unittest.TestCase):
    def test_single_tag(self):
        self.assertEqual(rl.tag_query(["唐诗"]), 'tag:"唐诗"')

    def test_union(self):
        self.assertEqual(
            rl.tag_query(["唐诗", "宋词"], rl.MODE_UNION),
            '(tag:"唐诗" OR tag:"宋词")',
        )

    def test_intersection(self):
        self.assertEqual(
            rl.tag_query(["唐诗", "宋词"], rl.MODE_INTERSECTION),
            '(tag:"唐诗" AND tag:"宋词")',
        )

    def test_unknown_mode_falls_back_to_union(self):
        self.assertEqual(
            rl.tag_query(["a", "b"], "随便写的"),
            '(tag:"a" OR tag:"b")',
        )

    def test_empty(self):
        self.assertEqual(rl.tag_query([]), "")
        self.assertEqual(rl.tag_query(None), "")

    def test_whitespace_and_duplicates_removed(self):
        self.assertEqual(rl.tag_query(["  唐诗  ", "唐诗", ""]), 'tag:"唐诗"')

    def test_space_in_tag_is_quoted(self):
        self.assertEqual(rl.tag_query(["重点 标记"]), 'tag:"重点 标记"')

    def test_quote_is_escaped(self):
        self.assertEqual(rl.tag_query(['a"b']), 'tag:"a\\"b"')

    def test_backslash_is_escaped(self):
        # 单反斜杠要写成两个，否则 Anki 的搜索解析器会吃掉它
        self.assertEqual(rl.tag_query(["a\\b"]), 'tag:"a\\\\b"')

    def test_hierarchical_tag_kept_as_one_term(self):
        # 子标签由 Anki 的 tag: 语义自动包含，插件只发一个词
        self.assertEqual(rl.tag_query(["唐诗::格律"]), 'tag:"唐诗::格律"')

    def test_role_colon_tag_is_escaped_by_quoting(self):
        self.assertEqual(rl.tag_query(["重点::标记 一"]), 'tag:"重点::标记 一"')

    def test_and_query_keeps_correct_grouping(self):
        # `(a) AND (b)` 不是「被一层括号完整包住」，再组合时要补括号
        self.assertEqual(
            rl.and_query("(a) AND (b)", "c"), "((a) AND (b)) AND (c)"
        )
        self.assertEqual(rl.and_query("(a)", "(b)"), "(a) AND (b)")
        self.assertEqual(rl.or_query("a", "b"), "(a) OR (b)")
        self.assertEqual(rl.and_query("", None, "x"), "(x)")

    def test_negate_term(self):
        self.assertEqual(rl.negate_term("deck:filtered"), "-deck:filtered")
        self.assertEqual(rl.negate_term("-deck:filtered"), "deck:filtered")
        self.assertEqual(rl.negate_term(""), "")


# --------------------------------------------------------------------------
# 牌组名
# --------------------------------------------------------------------------


class TestLeafNames(unittest.TestCase):
    def test_single_tag(self):
        self.assertEqual(rl.default_leaf_name(["唐诗"]), "唐诗")

    def test_union_joiner(self):
        self.assertEqual(
            rl.default_leaf_name(["唐诗", "宋词"], rl.MODE_UNION), "唐诗 + 宋词"
        )

    def test_intersection_joiner(self):
        self.assertEqual(
            rl.default_leaf_name(["唐诗", "宋词"], rl.MODE_INTERSECTION),
            "唐诗 ∩ 宋词",
        )

    def test_path_separator_replaced(self):
        self.assertEqual(rl.default_leaf_name(["唐诗::格律"]), "唐诗·格律")

    def test_empty_name(self):
        self.assertEqual(rl.default_leaf_name([]), "新筛选")

    def test_long_name_truncated(self):
        name = rl.default_leaf_name(["x" * 70])
        self.assertTrue(name.endswith("..."))
        self.assertEqual(len(name), 60)


class TestDeckPaths(unittest.TestCase):
    def test_normalize_parent(self):
        self.assertEqual(rl.normalize_parent("  a :: b :: "), "a::b")
        self.assertEqual(rl.normalize_parent(None), "")

    def test_join_plain(self):
        self.assertEqual(rl.join_deck_name("a::b", "c"), "a::b::c")
        self.assertEqual(rl.join_deck_name("", "c"), "c")
        self.assertEqual(rl.join_deck_name("a", ""), "a")

    def test_join_does_not_repeat_parent(self):
        self.assertEqual(rl.join_deck_name("a::b", "a::b::c"), "a::b::c")
        self.assertEqual(rl.join_deck_name("a::b", "a::b"), "a::b")

    def test_join_strips_colons(self):
        self.assertEqual(rl.join_deck_name(" 标签筛选 ", ":唐诗 + 宋词:"),
                         "标签筛选::唐诗 + 宋词")

    def test_split(self):
        self.assertEqual(rl.split_deck_name("a::b::c"), ("a::b", "c"))
        self.assertEqual(rl.split_deck_name("c"), ("", "c"))
        self.assertEqual(rl.split_deck_name(""), ("", ""))

    def test_unique_name(self):
        self.assertEqual(rl.unique_deck_name("p::n", []), "p::n")
        self.assertEqual(rl.unique_deck_name("p::n", ["p::n"]), "p::n (2)")
        self.assertEqual(
            rl.unique_deck_name("p::n", ["p::n", "p::n (2)"]), "p::n (3)"
        )

    def test_unique_name_exclude_self(self):
        # 编辑自己时，不能把自己的名字当成重名
        self.assertEqual(
            rl.unique_deck_name("p::n", ["p::n", "别的"], exclude="p::n"), "p::n"
        )

    def test_move_path(self):
        # 移动牌组＝换一个父路径重命名，父/叶子拆解要稳定
        parent, leaf = rl.split_deck_name("标签筛选::唐诗 + 宋词")
        self.assertEqual((parent, leaf), ("标签筛选", "唐诗 + 宋词"))
        moved = rl.join_deck_name("复习专用", leaf)
        self.assertEqual(moved, "复习专用::唐诗 + 宋词")
        self.assertEqual(rl.join_deck_name("", leaf), "唐诗 + 宋词")


class TestOrders(unittest.TestCase):
    def test_fallback_labels(self):
        self.assertEqual(rl.order_label(6), "到期顺序")
        self.assertEqual(rl.order_label(3), "间隔从长到短")

    def test_unknown_value_falls_back(self):
        self.assertEqual(rl.order_label(99), rl.ORDER_CHOICES[0][1])

    def test_anki_labels_preferred(self):
        labels = ["a", "b", "c"]
        self.assertEqual(rl.order_label(1, labels), "b")
        # 越界时回落到内置中文
        self.assertEqual(rl.order_label(9, labels), "提取难度从易到难（需 FSRS）")

    def test_available_orders_without_fsrs(self):
        self.assertEqual(
            rl.available_orders(11, False), [0, 1, 2, 3, 4, 5, 6, 7, 10]
        )

    def test_available_orders_with_fsrs(self):
        self.assertEqual(rl.available_orders(11, True), list(range(11)))

    def test_available_orders_short_list(self):
        self.assertEqual(rl.available_orders(8, False), list(range(8)))
        self.assertEqual(rl.available_orders(0, False), [])

    def test_clamp_order(self):
        self.assertEqual(rl.clamp_order(8), 8)
        self.assertEqual(rl.clamp_order(99), rl.DEFAULT_ORDER)
        self.assertEqual(rl.clamp_order("x"), rl.DEFAULT_ORDER)

    def test_clamp_limit(self):
        self.assertEqual(rl.clamp_limit(0), rl.MIN_LIMIT)
        self.assertEqual(rl.clamp_limit(10**9), rl.MAX_LIMIT)
        self.assertEqual(rl.clamp_limit("abc"), rl.DEFAULT_LIMIT)


class TestCleanTags(unittest.TestCase):
    def test_strip_and_dedupe(self):
        self.assertEqual(rl.clean_tags([" a ", "a", "", None, "b"]), ["a", "b"])

    def test_order_kept(self):
        self.assertEqual(rl.clean_tags(["b", "a"]), ["b", "a"])

    def test_non_string_values(self):
        self.assertEqual(rl.clean_tags([1, 2]), ["1", "2"])

    def test_none(self):
        self.assertEqual(rl.clean_tags(None), [])


# --------------------------------------------------------------------------
# 三个取卡状态
# --------------------------------------------------------------------------


class TestStates(unittest.TestCase):
    def test_only_three_states(self):
        self.assertEqual(rl.STATE_VALUES, ("new", "learn", "review"))
        self.assertEqual(
            [label for _value, label in rl.STATE_CHOICES],
            ["未学习", "学习中", "复习中"],
        )

    def test_state_search_words(self):
        # 三项互不重复：学习中用 is:learn，复习中要减掉正在重学的卡
        self.assertEqual(rl.STATE_SEARCH["new"], "is:new")
        self.assertEqual(rl.STATE_SEARCH["learn"], "is:learn")
        self.assertEqual(rl.STATE_SEARCH["review"], "is:review -is:learn")

    def test_state_order_is_fixed(self):
        self.assertEqual(rl.normalize_states(["review", "new"]), ["new", "review"])

    def test_normalize_states_various_shapes(self):
        self.assertEqual(rl.normalize_states("new, review"), ["new", "review"])
        self.assertEqual(rl.normalize_states("new，review"), ["new", "review"])
        self.assertEqual(
            rl.normalize_states({"new": True, "review": False}), ["new"]
        )
        self.assertEqual(rl.normalize_states(None), [])

    def test_legacy_banned_states_are_dropped(self):
        # 老版本出现过 suspended / buried，新版一律丢掉
        self.assertEqual(rl.normalize_states(["new", "suspended", "buried"]), ["new"])
        self.assertEqual(rl.normalize_states(["suspended"]), [])
        self.assertEqual(rl.normalize_states(["buried"]), [])

    def test_invalid_states_are_dropped(self):
        self.assertEqual(rl.normalize_states(["new", "不存在"]), ["new"])
        self.assertEqual(rl.normalize_states(["没有", "合法的"]), [])

    def test_union_query_single(self):
        self.assertEqual(rl.states_union_query(["new"]), "(is:new)")
        self.assertEqual(rl.states_union_query(["review"]), "(is:review -is:learn)")

    def test_union_query_multiple(self):
        self.assertEqual(
            rl.states_union_query(["new", "review"]),
            "(is:new OR is:review -is:learn)",
        )

    def test_union_query_empty(self):
        self.assertEqual(rl.states_union_query([]), "")

    def test_default_triple_matches_old_scope(self):
        # 全勾 + 不勾未到期 → 老版的 due_new，逐字一致
        self.assertEqual(rl.states_query(rl.DEFAULT_STATES, False), rl.DUE_FILTER)
        self.assertEqual(rl.states_query(rl.DEFAULT_STATES, False), "(is:due OR is:new)")

    def test_default_triple_without_due_limit(self):
        # 全勾 + 勾了未到期 → 老版的 all，不写范围片段
        self.assertEqual(rl.states_query(rl.DEFAULT_STATES, True), "")

    def test_new_only_ignores_due_switch(self):
        # 新卡本来就算「在范围内」，两个开关下写法一样
        self.assertEqual(rl.states_query(["new"], True), "(is:new)")
        self.assertEqual(rl.states_query(["new"], False), "(is:new)")

    def test_learn_only(self):
        self.assertEqual(
            rl.states_query(["learn"], False),
            "(is:learn) AND (is:due OR is:new)",
        )
        self.assertEqual(rl.states_query(["learn"], True), "(is:learn)")

    def test_review_only(self):
        self.assertEqual(
            rl.states_query(["review"], False),
            "(is:review -is:learn) AND (is:due OR is:new)",
        )
        self.assertEqual(rl.states_query(["review"], True), "(is:review -is:learn)")

    def test_new_and_learn(self):
        self.assertEqual(
            rl.states_query(["new", "learn"], False),
            "(is:new OR is:learn) AND (is:due OR is:new)",
        )
        self.assertEqual(
            rl.states_query(["new", "learn"], True), "(is:new OR is:learn)"
        )

    def test_learn_and_review(self):
        self.assertEqual(
            rl.states_query(["learn", "review"], False),
            "(is:learn OR is:review -is:learn) AND (is:due OR is:new)",
        )
        self.assertEqual(
            rl.states_query(["learn", "review"], True),
            "(is:learn OR is:review -is:learn)",
        )

    def test_empty_states(self):
        self.assertEqual(rl.states_query([]), "")
        self.assertEqual(rl.states_query([], True), "")
        self.assertEqual(rl.states_query(None), "")

    def test_all_invalid_states(self):
        self.assertEqual(rl.states_query(["乱写"]), "")
        self.assertEqual(rl.states_query(["suspended", "buried"]), "")

    def test_state_labels(self):
        self.assertEqual(rl.state_label("new"), "未学习")
        self.assertEqual(rl.state_label("learn"), "学习中")
        self.assertEqual(rl.state_label("review"), "复习中")
        self.assertEqual(rl.states_label(rl.DEFAULT_STATES, False), "未学习+学习中+复习中")
        self.assertEqual(
            rl.states_label(rl.DEFAULT_STATES, True), "未学习+学习中+复习中·含未到期"
        )
        self.assertEqual(rl.states_label(["new"], True), "未学习·含未到期")
        self.assertEqual(rl.states_label(["new"], False), "未学习")
        self.assertEqual(rl.states_label([], True), "（没有勾选状态）")

    def test_state_count_query(self):
        self.assertEqual(
            rl.state_count_query(["唐诗"], rl.MODE_UNION, "new"),
            '(tag:"唐诗") AND (is:new)',
        )
        self.assertEqual(
            rl.state_count_query(["唐诗"], rl.MODE_UNION, "learn"),
            '(tag:"唐诗") AND (is:learn)',
        )
        self.assertEqual(
            rl.state_count_query(["唐诗"], rl.MODE_UNION, "review"),
            '(tag:"唐诗") AND (is:review -is:learn)',
        )

    def test_state_count_query_empty_cases(self):
        self.assertEqual(rl.state_count_query([], rl.MODE_UNION, "new"), "")
        self.assertEqual(rl.state_count_query(["唐诗"], rl.MODE_UNION, "乱写"), "")
        self.assertEqual(rl.state_count_query(["唐诗"], rl.MODE_UNION, ""), "")

    def test_wanted_query(self):
        # 「勾选状态」覆盖到的卡，不管是否到期
        self.assertEqual(
            rl.wanted_query(["唐诗"], rl.MODE_UNION, rl.DEFAULT_STATES),
            '(tag:"唐诗") AND (is:new OR is:learn OR is:review -is:learn)',
        )
        self.assertEqual(
            rl.wanted_query(["唐诗"], rl.MODE_UNION, ["review"]),
            '(tag:"唐诗") AND (is:review -is:learn)',
        )
        self.assertEqual(rl.wanted_query([], rl.MODE_UNION, rl.DEFAULT_STATES), "")
        self.assertEqual(rl.wanted_query(["唐诗"], rl.MODE_UNION, []), "")


class TestBuildSearchQuery(unittest.TestCase):
    def test_union_default(self):
        self.assertEqual(
            rl.build_search_query(["唐诗", "宋词"], rl.MODE_UNION),
            '(tag:"唐诗" OR tag:"宋词") AND (is:due OR is:new)',
        )

    def test_single_tag_default(self):
        self.assertEqual(
            rl.build_search_query(["唐诗"]),
            '(tag:"唐诗") AND (is:due OR is:new)',
        )

    def test_no_tags(self):
        self.assertEqual(rl.build_search_query([], rl.MODE_UNION), "")

    def test_new_only(self):
        self.assertEqual(
            rl.build_search_query(["唐诗"], rl.MODE_UNION, ["new"], True),
            '(tag:"唐诗") AND (is:new)',
        )

    def test_due_switch_changes_only_for_learn_review(self):
        due = rl.build_search_query(["唐诗"], rl.MODE_UNION, ["review"], False)
        all_cards = rl.build_search_query(["唐诗"], rl.MODE_UNION, ["review"], True)
        self.assertIn(rl.DUE_FILTER, due)
        self.assertNotIn(rl.DUE_FILTER, all_cards)

    def test_in_scope_query_is_same(self):
        args = (["唐诗"], rl.MODE_INTERSECTION, ["new", "review"], False)
        self.assertEqual(rl.in_scope_query(*args), rl.build_search_query(*args))


class TestOldVersionCompatibility(unittest.TestCase):
    """老规则升级后生成的搜索式必须逐字一致，否则会被误判成「被 Anki 改过」。"""

    TAGS = ["唐诗", "宋词"]

    def test_due_new_unchanged(self):
        old = rl.and_query(
            rl.tag_query(self.TAGS, rl.MODE_UNION), rl.scope_query("due_new")
        )
        new = rl.build_search_query(
            self.TAGS, rl.MODE_UNION, rl.DEFAULT_STATES, False
        )
        self.assertEqual(old, new)
        self.assertEqual(new, '(tag:"唐诗" OR tag:"宋词") AND (is:due OR is:new)')

    def test_all_unchanged(self):
        old = rl.and_query(
            rl.tag_query(self.TAGS, rl.MODE_UNION), rl.scope_query("all")
        )
        new = rl.build_search_query(
            self.TAGS, rl.MODE_UNION, rl.DEFAULT_STATES, True
        )
        self.assertEqual(old, new)
        self.assertEqual(new, '(tag:"唐诗" OR tag:"宋词")')

    def test_scope_query_compat(self):
        self.assertEqual(rl.scope_query("due_new"), "(is:due OR is:new)")
        self.assertEqual(rl.scope_query("all"), "")
        self.assertEqual(rl.scope_query("乱写"), "(is:due OR is:new)")

    def test_migrated_rule_search_is_unchanged(self):
        raw = {"deck_id": 1, "tags": self.TAGS, "mode": "union", "scope": "due_new"}
        rule = rl.normalize_rule(raw)
        self.assertEqual(
            rule["search"], '(tag:"唐诗" OR tag:"宋词") AND (is:due OR is:new)'
        )

    def test_due_only_true_migrates_verbatim(self):
        # 1.4.0 存的 due_only=True（只收到期）升级后搜索式必须逐字不变
        raw = {
            "deck_id": 1,
            "tags": self.TAGS,
            "mode": "union",
            "states": list(rl.DEFAULT_STATES),
            "due_only": True,
        }
        rule = rl.normalize_rule(raw)
        self.assertFalse(rule["include_not_due"])
        self.assertEqual(rule["search"], '(tag:"唐诗" OR tag:"宋词") AND (is:due OR is:new)')
        self.assertTrue(
            rl.rule_matches_actual(
                rule,
                rule["search"],
                rl.DEFAULT_LIMIT,
                rl.DEFAULT_ORDER,
                True,
            )
        )

    def test_due_only_false_migrates_verbatim(self):
        # 1.4.0 存的 due_only=False（含未到期）升级后同样逐字不变，
        # 也不能被误判成「被 Anki 内置设置改过」（用户那条 RWBY 就属于这种）
        raw = {
            "deck_id": 1790348666290,
            "tags": ["RWBY"],
            "mode": "union",
            "states": list(rl.DEFAULT_STATES),
            "due_only": False,
            "limit": rl.DEFAULT_LIMIT,
            "order": rl.DEFAULT_ORDER,
            "search": 'tag:"RWBY"',
        }
        rule = rl.normalize_rule(raw)
        self.assertTrue(rule["include_not_due"])
        self.assertEqual(rule["search"], 'tag:"RWBY"')
        self.assertTrue(
            rl.rule_matches_actual(
                rule, 'tag:"RWBY"', rl.DEFAULT_LIMIT, rl.DEFAULT_ORDER, True
            )
        )


class TestTagChoices(unittest.TestCase):
    TAGS = ["唐诗", "唐诗::格律", "Song", "song of joy", "  宋词 "]

    def test_empty_keyword_returns_all(self):
        self.assertEqual(
            rl.filter_tag_choices(self.TAGS, ""),
            ["唐诗", "唐诗::格律", "Song", "song of joy", "宋词"],
        )

    def test_case_insensitive_contains(self):
        self.assertEqual(rl.filter_tag_choices(self.TAGS, "SONG"), ["Song", "song of joy"])

    def test_contains_not_prefix(self):
        self.assertEqual(rl.filter_tag_choices(self.TAGS, "词"), ["宋词"])

    def test_hierarchical_subtag_matches_parent_keyword(self):
        self.assertEqual(
            rl.filter_tag_choices(self.TAGS, "唐诗"), ["唐诗", "唐诗::格律"]
        )

    def test_no_match(self):
        self.assertEqual(rl.filter_tag_choices(self.TAGS, "没有"), [])

    def test_whitespace_keyword_is_ignored(self):
        self.assertEqual(len(rl.filter_tag_choices(self.TAGS, "   ")), 5)

    def test_input_order_kept(self):
        self.assertEqual(rl.filter_tag_choices(["b", "a", "B"], ""), ["b", "a", "B"])

    def test_duplicates_removed(self):
        self.assertEqual(rl.filter_tag_choices(["a", "a", " b "], ""), ["a", "b"])

    def test_none(self):
        self.assertEqual(rl.filter_tag_choices(None, "x"), [])


class TestNoUnbanHelpersLeft(unittest.TestCase):
    """按用户决定：插件绝不去改动卡片的暂停/搁置状态，旧解禁机制要彻底删干净。"""

    def test_unban_helpers_are_gone(self):
        for name in (
            "normalize_unbanned",
            "plan_unban",
            "restorable_ids",
            "unban_candidate_query",
            "unbanned_ids",
            "unbanned_count",
            "unbanned_summary",
            "cap_ids",
            "UNBANNABLE_STATES",
        ):
            self.assertFalse(hasattr(rl, name), f"rule_logic 里不该再有 {name}")

    def test_no_state_mentions_paused_or_buried(self):
        self.assertNotIn("suspended", rl.STATE_VALUES)
        self.assertNotIn("buried", rl.STATE_VALUES)

    def test_banned_query_still_known(self):
        # 只用于「显示有多少张收不进来」，不用于改动状态
        self.assertEqual(rl.ALWAYS_EXCLUDED_QUERY, "(is:suspended OR is:buried)")


# --------------------------------------------------------------------------
# 规则结构
# --------------------------------------------------------------------------


class TestRules(unittest.TestCase):
    def test_make_rule_defaults(self):
        rule = rl.make_rule(tags=["唐诗"])
        self.assertEqual(rule["states"], list(rl.DEFAULT_STATES))
        self.assertFalse(rule["include_not_due"])
        self.assertEqual(rule["name"], "标签筛选::唐诗")
        self.assertEqual(rule["search"], '(tag:"唐诗") AND (is:due OR is:new)')
        self.assertEqual(rule["limit"], rl.DEFAULT_LIMIT)
        self.assertEqual(rule["order"], rl.DEFAULT_ORDER)
        self.assertNotIn("unbanned", rule)
        self.assertNotIn("scope", rule)
        self.assertNotIn("due_only", rule)

    def test_make_rule_explicit_states(self):
        rule = rl.make_rule(tags=["唐诗"], states=["review"], include_not_due=True)
        self.assertEqual(rule["states"], ["review"])
        self.assertTrue(rule["include_not_due"])
        self.assertEqual(rule["search"], '(tag:"唐诗") AND (is:review -is:learn)')

    def test_make_rule_include_not_due_defaults_false(self):
        rule = rl.make_rule(tags=["唐诗"], states=["review"])
        self.assertFalse(rule["include_not_due"])

    def test_make_rule_invalid_states_fall_back(self):
        rule = rl.make_rule(tags=["唐诗"], states=["乱写"])
        self.assertEqual(rule["states"], list(rl.DEFAULT_STATES))

    def test_make_rule_legacy_banned_states_fall_back(self):
        rule = rl.make_rule(tags=["唐诗"], states=["suspended", "buried"])
        self.assertEqual(rule["states"], list(rl.DEFAULT_STATES))

    def test_make_rule_from_old_scope_due_new(self):
        rule = rl.make_rule(tags=["唐诗"], scope="due_new")
        self.assertEqual(rule["states"], list(rl.DEFAULT_STATES))
        self.assertFalse(rule["include_not_due"])
        self.assertEqual(rule["search"], '(tag:"唐诗") AND (is:due OR is:new)')

    def test_make_rule_from_old_scope_all(self):
        rule = rl.make_rule(tags=["唐诗"], scope="all")
        self.assertEqual(rule["states"], list(rl.DEFAULT_STATES))
        self.assertTrue(rule["include_not_due"])
        self.assertEqual(rule["search"], 'tag:"唐诗"')

    def test_normalize_rule_new_shape(self):
        raw = {
            "deck_id": 7,
            "tags": ["唐诗"],
            "mode": "intersection",
            "states": ["review", "new"],
            "due_only": False,
        }
        rule = rl.normalize_rule(raw)
        self.assertEqual(rule["states"], ["new", "review"])
        self.assertTrue(rule["include_not_due"])
        self.assertEqual(rule["deck_id"], 7)

    def test_normalize_rule_drops_legacy_banned_states(self):
        rule = rl.normalize_rule(
            {"deck_id": 1, "tags": ["a"], "states": ["new", "suspended", "buried"]}
        )
        self.assertEqual(rule["states"], ["new"])

    def test_normalize_rule_all_banned_states_fall_back(self):
        rule = rl.normalize_rule(
            {"deck_id": 1, "tags": ["a"], "states": ["suspended", "buried"]}
        )
        self.assertEqual(rule["states"], list(rl.DEFAULT_STATES))
        self.assertFalse(rule["include_not_due"])

    def test_normalize_rule_migrates_due_new(self):
        rule = rl.normalize_rule(
            {"deck_id": 1, "tags": ["a"], "mode": "union", "scope": "due_new"}
        )
        self.assertEqual(rule["states"], list(rl.DEFAULT_STATES))
        self.assertFalse(rule["include_not_due"])
        self.assertEqual(rule["search"], '(tag:"a") AND (is:due OR is:new)')

    def test_normalize_rule_migrates_all(self):
        rule = rl.normalize_rule(
            {"deck_id": 1, "tags": ["a"], "mode": "union", "scope": "all"}
        )
        self.assertEqual(rule["states"], list(rl.DEFAULT_STATES))
        self.assertTrue(rule["include_not_due"])
        self.assertEqual(rule["search"], 'tag:"a"')

    def test_normalize_rule_migrates_due_only_true(self):
        # 1.4.0 的 due_only=True ＝ 只收到期 → 新版 include_not_due=False
        rule = rl.normalize_rule(
            {"deck_id": 1, "tags": ["a"], "mode": "union", "due_only": True}
        )
        self.assertFalse(rule["include_not_due"])
        self.assertEqual(rule["search"], '(tag:"a") AND (is:due OR is:new)')

    def test_normalize_rule_migrates_due_only_false(self):
        # 1.4.0 的 due_only=False ＝ 连没到期的也收 → 新版 include_not_due=True
        rule = rl.normalize_rule(
            {"deck_id": 1, "tags": ["a"], "mode": "union", "due_only": False}
        )
        self.assertTrue(rule["include_not_due"])
        self.assertEqual(rule["search"], 'tag:"a"')

    def test_include_not_due_wins_over_due_only(self):
        raw = {"deck_id": 1, "tags": ["a"], "include_not_due": False, "due_only": False}
        self.assertFalse(rl.normalize_rule(raw)["include_not_due"])

    def test_normalize_rule_without_states_or_scope(self):
        rule = rl.normalize_rule({"deck_id": 1, "tags": ["a"]})
        self.assertEqual(rule["states"], list(rl.DEFAULT_STATES))
        self.assertFalse(rule["include_not_due"])

    def test_normalize_rule_rejects_junk(self):
        self.assertIsNone(rl.normalize_rule(None))
        self.assertIsNone(rl.normalize_rule({"tags": []}))
        self.assertIsNone(rl.normalize_rule({"tags": ["  "]}))

    def test_normalize_rule_keeps_extra_fields(self):
        raw = {
            "deck_id": 3,
            "tags": ["a"],
            "states": ["new"],
            "actual_search": "x",
            "actual_limit": 5,
            "actual_order": 1,
            "actual_reschedule": True,
            "externally_modified": True,
        }
        rule = rl.normalize_rule(raw)
        self.assertEqual(rule["actual_search"], "x")
        self.assertEqual(rule["actual_limit"], 5)
        self.assertTrue(rule["externally_modified"])

    def test_rule_summary(self):
        rule = rl.make_rule(tags=["唐诗", "宋词"], mode=rl.MODE_INTERSECTION)
        summary = rl.rule_summary(rule)
        self.assertIn("唐诗", summary)
        self.assertIn("交集", summary)
        self.assertIn("未学习+学习中+复习中", summary)
        self.assertEqual(rl.rule_summary(None), "无效规则")

    def test_rule_search(self):
        rule = rl.make_rule(tags=["唐诗"])
        self.assertEqual(rl.rule_search(rule), '(tag:"唐诗") AND (is:due OR is:new)')
        self.assertEqual(rl.rule_search(None), "")


class TestRuleMatchesActual(unittest.TestCase):
    def setUp(self):
        self.rule = rl.make_rule(tags=["唐诗"], limit=100, order=4)
        self.rule["deck_id"] = 12

    def test_matches_when_rule_has_no_actual_fields(self):
        self.assertTrue(
            rl.rule_matches_actual(
                self.rule, self.rule["search"], 100, 4, True
            )
        )

    def test_mismatch_on_each_field(self):
        self.assertFalse(rl.rule_matches_actual(self.rule, "别的", 100, 4, True))
        self.assertFalse(
            rl.rule_matches_actual(self.rule, self.rule["search"], 5, 4, True)
        )
        self.assertFalse(
            rl.rule_matches_actual(self.rule, self.rule["search"], 100, 1, True)
        )
        self.assertFalse(
            rl.rule_matches_actual(self.rule, self.rule["search"], 100, 4, False)
        )

    def test_actual_fields_win(self):
        rule = dict(self.rule)
        rule["actual_search"] = "写入后的写法"
        rule["actual_limit"] = 7
        rule["actual_order"] = 2
        rule["actual_reschedule"] = False
        self.assertTrue(rl.rule_matches_actual(rule, "写入后的写法", 7, 2, False))
        self.assertFalse(rl.rule_matches_actual(rule, "写入后的写法", 7, 2, True))

    def test_invalid_rule_never_matches(self):
        self.assertFalse(rl.rule_matches_actual(None, "", 0, 0, False))

    def test_deck_state_never_used(self):
        # 换个标记，规则本身没变，仍然算一致
        rule = dict(self.rule)
        rule["externally_modified"] = True
        self.assertTrue(
            rl.rule_matches_actual(rule, self.rule["search"], 100, 4, True)
        )


# --------------------------------------------------------------------------
# 统计与预览
# --------------------------------------------------------------------------


class TestCardDue(unittest.TestCase):
    """到期口径：按卡片自己的到期时间算（1.4.0 的核心修正）。

    复习卡的 due 是「第几天」，学习 / 重学卡的 due 是 Unix 时间戳；卡片被收进
    筛选牌组后 due 变成位置值，真正的到期时间在 odue 里。
    """

    def due(self, **fields):
        base = {"type": 2, "queue": 2, "due": 100, "odid": 0, "odue": 0}
        today = fields.pop("today", 159)
        now = fields.pop("now", 1_000_000)
        base.update(fields)
        return rl.card_is_due(base, today=today, now=now)

    def test_review_card_due_today_or_earlier(self):
        self.assertTrue(self.due(due=159))
        self.assertTrue(self.due(due=100))

    def test_review_card_not_due_yet(self):
        self.assertFalse(self.due(due=160))
        self.assertFalse(self.due(due=300))

    def test_learning_card_uses_timestamp(self):
        self.assertTrue(self.due(type=1, queue=1, due=999_999))
        self.assertFalse(self.due(type=1, queue=1, due=1_000_001))

    def test_relearn_card_uses_timestamp(self):
        self.assertTrue(self.due(type=3, queue=3, due=1_000_000))
        self.assertFalse(self.due(type=3, queue=3, due=1_000_001))

    def test_buried_learning_card_keeps_type(self):
        # 搁置不改变卡片类型，仍然按学习卡的时间戳判
        self.assertFalse(self.due(type=1, queue=-2, due=1_000_001))

    def test_new_card_is_never_blocked_by_due(self):
        self.assertTrue(self.due(type=0, queue=0, due=0))
        self.assertTrue(self.due(type=0, queue=0, due=99999))

    def test_card_in_filtered_deck_uses_odue(self):
        # 在筛选牌组里：due 是位置值（负数），要看 odue
        self.assertFalse(self.due(odid=1234, odue=280, due=-100123))
        self.assertTrue(self.due(odid=1234, odue=100, due=-100123))

    def test_effective_due_prefers_original(self):
        self.assertEqual(
            rl.card_effective_due({"odid": 5, "odue": 42, "due": -100}), 42
        )
        self.assertEqual(rl.card_effective_due({"odid": 0, "odue": 42, "due": 7}), 7)
        self.assertEqual(rl.card_effective_due({"odid": 5, "odue": 0, "due": -100}), -100)

    def test_card_fields_from_object(self):
        class Fake:
            type = 2
            queue = 2
            due = 5
            odid = 0
            odue = 0

        fields = rl.card_due_fields(Fake())
        self.assertEqual(fields["type"], 2)
        self.assertTrue(rl.card_is_due(fields, today=5))
        self.assertFalse(rl.card_is_due(fields, today=4))

    def test_missing_card_counts_as_due(self):
        self.assertTrue(rl.card_is_due(None, today=1))

    def test_due_restriction_switch(self):
        # 第二个参数是 include_not_due（1.5.0 起是正向语义）
        self.assertFalse(rl.due_restriction_applies(["new"], False))
        self.assertTrue(rl.due_restriction_applies(["learn"], False))
        self.assertTrue(rl.due_restriction_applies(rl.DEFAULT_STATES, False))
        self.assertFalse(rl.due_restriction_applies(rl.DEFAULT_STATES, True))
        self.assertFalse(rl.due_restriction_applies([], False))

    def test_day_zero_collection_still_filters(self):
        # 新集合的第一天，「今天」就是第 0 天；这一天不能当成「读不到今天是几号」
        self.assertTrue(self.due(due=0))
        self.assertFalse(self.due(due=3, today=0))

    def test_due_filter_enabled_needs_a_real_today(self):
        # 第一个参数是 include_not_due：不勾「连还没到期的也收」时才按到期过滤
        self.assertTrue(rl.due_filter_enabled(False, 0))
        self.assertTrue(rl.due_filter_enabled(False, 159))
        self.assertFalse(rl.due_filter_enabled(False, None))
        self.assertFalse(rl.due_filter_enabled(True, 159))


class TestClassifyCards(unittest.TestCase):
    """把一批卡分成「能收进来 / 收不进来」，四种原因互不重叠。"""

    def records(self):
        return [
            {"id": 1, "type": 2, "queue": 2, "due": 100},
            {"id": 2, "type": 2, "queue": 2, "due": 300},
            {"id": 3, "type": 2, "queue": -1, "due": 100},
            {"id": 4, "type": 2, "queue": 2, "due": 100},
            {"id": 5, "type": 0, "queue": 0, "due": 0},
        ]

    def test_split_is_disjoint_and_complete(self):
        out = rl.classify_cards(
            self.records(),
            states=rl.DEFAULT_STATES,
            include_not_due=False,
            today=159,
            now=1,
            other_filtered_ids=[4],
        )
        self.assertEqual(out["collectible"], [1, 5])
        self.assertEqual(out["due"], [2])
        self.assertEqual(out["banned"], [3])
        self.assertEqual(out["other"], [4])
        total = sum(len(value) for value in out.values())
        self.assertEqual(total, len(self.records()))

    def test_no_due_segment_when_switch_off(self):
        out = rl.classify_cards(
            self.records()[:2], states=rl.DEFAULT_STATES, include_not_due=True, today=159
        )
        self.assertEqual(out["collectible"], [1, 2])
        self.assertEqual(out["due"], [])

    def test_new_only_ignores_due_switch(self):
        out = rl.classify_cards(
            [{"id": 2, "type": 2, "queue": 2, "due": 300}],
            states=["new"],
            include_not_due=False,
            today=159,
        )
        self.assertEqual(out["collectible"], [2])
        self.assertEqual(out["due"], [])

    def test_cards_without_id_are_skipped(self):
        out = rl.classify_cards([{"type": 2, "queue": 2, "due": 1}])
        self.assertEqual(sum(len(value) for value in out.values()), 0)


class TestCounts(unittest.TestCase):
    def test_empty_counts_shape(self):
        counts = rl.empty_counts()
        self.assertEqual(counts["matched"], 0)
        self.assertEqual(counts["wanted"], 0)
        self.assertEqual(counts["collectible"], 0)
        self.assertEqual(counts["blocked"], 0)
        self.assertEqual(counts["due_blocked"], 0)
        self.assertEqual(counts["banned"], 0)
        self.assertEqual(counts["other_filtered"], 0)
        self.assertEqual(sorted(counts["states"]), sorted(rl.STATE_VALUES))
        self.assertEqual(counts["per_tag"], {})
        self.assertEqual(counts["blocked_ids"], [])
        self.assertEqual(counts["cram_collectible"], 0)
        self.assertNotIn("unban", counts)
        self.assertNotIn("in_scope", counts)

    def test_estimate_collectible(self):
        counts = rl.empty_counts()
        counts["collectible"] = 10
        self.assertEqual(rl.estimate_collectible(counts, 9999), 10)
        self.assertEqual(rl.estimate_collectible(counts, 4), 4)

    def test_estimate_collectible_never_negative(self):
        counts = rl.empty_counts()
        counts["collectible"] = -3
        self.assertEqual(rl.estimate_collectible(counts, 9999), 0)

    def test_estimate_collectible_junk(self):
        self.assertEqual(rl.estimate_collectible({}, 9999), 0)
        self.assertEqual(rl.estimate_collectible({"collectible": "x"}, 9999), 0)

    def test_blocked_query_lists_card_ids(self):
        # 1.4.0：「看收不进来的卡」改成直接列卡号，保证和预览里的 K 一致
        query, dropped = rl.blocked_query([11, 22, 33])
        self.assertEqual(query, "cid:11,22,33")
        self.assertEqual(dropped, 0)

    def test_blocked_query_without_cards(self):
        self.assertEqual(rl.blocked_query([]), ("", 0))
        self.assertEqual(rl.blocked_query(None), ("", 0))
        self.assertEqual(rl.blocked_query(["x", 0, None]), ("", 0))

    def test_blocked_query_truncates_and_reports(self):
        query, dropped = rl.blocked_query(range(1, 11), limit=4)
        self.assertEqual(dropped, 6)
        self.assertIn("cid:1,2,3,4", query)
        self.assertNotIn("cid:5", query)

    def test_blocked_query_default_limit(self):
        self.assertEqual(rl.blocked_query([1], limit=0)[0], "cid:1")

    def test_cid_query_single_chunk(self):
        self.assertEqual(rl.cid_query([7, 8]), "cid:7,8")
        self.assertEqual(rl.cid_query([]), "")

    def test_cid_query_chunks_long_lists(self):
        query = rl.cid_query(range(1, 1000))
        self.assertTrue(query.startswith("("))
        self.assertIn(" OR ", query)
        self.assertEqual(query.count("cid:"), 3)


def _counts(**kwargs):
    counts = rl.empty_counts()
    counts.update(kwargs)
    return counts


def _state_counts(new=0, learn=0, review=0):
    return {"new": new, "learn": learn, "review": review}


class TestPreviewLines(unittest.TestCase):
    def test_no_tags(self):
        lines = rl.preview_lines(rl.empty_counts(), [], rl.MODE_UNION)
        self.assertEqual(lines, ["请先选择至少 1 个标签。"])

    def test_basic_collectible_line(self):
        counts = _counts(
            matched=7,
            wanted=7,
            collectible=5,
            blocked=2,
            due_blocked=1,
            banned=1,
            states=_state_counts(new=5, learn=1, review=1),
        )
        lines = rl.preview_lines(
            counts, ["唐诗", "宋词"], rl.MODE_UNION, rl.DEFAULT_STATES, 9999
        )
        text = "\n".join(lines)
        self.assertIn("预计收录：5 张（上限 9999）", text)
        self.assertIn("命中这些标签的卡共 7 张", text)
        for label in ("未学习 5", "学习中 1", "复习中 1"):
            self.assertIn(label, text)
        self.assertIn("另有 2 张这次收不进来", text)
        self.assertIn("还没到期的 1 张", text)
        self.assertIn("暂停或搁置的 1 张", text)
        self.assertIn("插件也不会改动它们的暂停状态", text)

    def test_banned_notice_only_when_blocked(self):
        counts = _counts(
            matched=2, wanted=2, collectible=2, states=_state_counts(new=2)
        )
        text = "\n".join(
            rl.preview_lines(
                counts, ["唐诗"], rl.MODE_UNION, rl.DEFAULT_STATES, 9999
            )
        )
        self.assertNotIn("暂停/搁置的卡", text)
        self.assertNotIn("这次收不进来", text)

    def test_other_filtered_listed(self):
        counts = _counts(
            matched=3,
            wanted=3,
            collectible=1,
            blocked=2,
            other_filtered=2,
            states=_state_counts(new=3),
        )
        text = "\n".join(
            rl.preview_lines(
                counts, ["唐诗"], rl.MODE_UNION, rl.DEFAULT_STATES, 9999
            )
        )
        self.assertIn("已在别的筛选牌组里的 2 张", text)

    def test_limit_shown(self):
        counts = _counts(
            matched=50, wanted=50, collectible=50, states=_state_counts(new=50)
        )
        lines = rl.preview_lines(
            counts, ["a"], rl.MODE_UNION, rl.DEFAULT_STATES, 10
        )
        self.assertIn("预计收录：10 张（上限 10）", lines[0])

    def test_no_states_warns(self):
        counts = _counts(
            matched=1, wanted=1, collectible=1, states=_state_counts(new=1)
        )
        lines = rl.preview_lines(counts, ["a"], rl.MODE_UNION, [], 9999)
        self.assertIn("至少要勾一个取卡范围状态", "\n".join(lines))

    def test_empty_union(self):
        lines = rl.preview_lines(
            rl.empty_counts(), ["唐诗"], rl.MODE_UNION, rl.DEFAULT_STATES, 9999
        )
        self.assertIn("暂时没有卡片命中这些标签", "\n".join(lines))

    def test_empty_intersection_mentions_each_tag(self):
        counts = rl.empty_counts()
        counts["per_tag"] = {"唐诗": 4, "不存在的标签": 0}
        lines = rl.preview_lines(
            counts,
            ["唐诗", "不存在的标签"],
            rl.MODE_INTERSECTION,
            rl.DEFAULT_STATES,
            9999,
        )
        text = "\n".join(lines)
        self.assertIn("交集为空", text)
        self.assertIn("只有「唐诗」的卡：4 张", text)

    def test_state_counts_sum_to_matched(self):
        # 三个状态的数字相加正好是命中数（互不重复）
        counts = _counts(
            matched=9,
            wanted=9,
            collectible=9,
            states=_state_counts(new=1, learn=2, review=6),
        )
        self.assertEqual(sum(counts["states"].values()), counts["matched"])

    def test_state_counts_use_no_banned_terms(self):
        # 「收不进来」的张数不参与 a+b+c=N 这条恒等式
        counts = _counts(
            matched=5,
            wanted=7,
            collectible=5,
            blocked=2,
            due_blocked=1,
            banned=1,
            states=_state_counts(new=3, learn=1, review=1),
        )
        self.assertEqual(sum(counts["states"].values()), counts["matched"])
        self.assertEqual(counts["wanted"], counts["collectible"] + counts["blocked"])


# --------------------------------------------------------------------------
# 集中刷：延迟换算、收录搜索式、会话记录
# --------------------------------------------------------------------------


class TestPickHomeDeck(unittest.TestCase):
    """1.5.0：集中刷「不影响排期」的延迟要按卡片自己的原牌组算，而不是当前选中的牌组。"""

    def test_single_home_deck(self):
        records = [{"did": 5}, {"did": 5}, {"did": 5}]
        info = rl.pick_home_deck(records)
        self.assertEqual(info["deck_id"], 5)
        self.assertEqual(info["count"], 3)
        self.assertEqual(info["other_count"], 0)
        self.assertEqual(info["other_decks"], 0)

    def test_filtered_card_uses_original_deck(self):
        # 卡在筛选牌组里时 did 是筛选牌组，odid 才是原牌组
        records = [{"did": 99, "odid": 5}, {"did": 99, "odid": 5}]
        self.assertEqual(rl.home_deck_id(records[0]), 5)
        self.assertEqual(rl.pick_home_deck(records)["deck_id"], 5)

    def test_most_cards_wins(self):
        records = [{"did": 5}, {"did": 7}, {"did": 7}, {"did": 7}]
        info = rl.pick_home_deck(records)
        self.assertEqual(info["deck_id"], 7)
        self.assertEqual(info["count"], 3)
        self.assertEqual(info["other_count"], 1)
        self.assertEqual(info["other_decks"], 1)

    def test_tie_picks_smaller_id(self):
        records = [{"did": 7}, {"did": 7}, {"did": 5}, {"did": 5}]
        self.assertEqual(rl.pick_home_deck(records)["deck_id"], 5)

    def test_filtered_decks_are_skipped(self):
        records = [{"did": 9}, {"did": 9}, {"did": 9}, {"did": 4}]
        info = rl.pick_home_deck(records, filtered_ids={9})
        self.assertEqual(info["deck_id"], 4)
        self.assertEqual(info["count"], 1)

    def test_all_filtered_falls_back_to_zero(self):
        records = [{"did": 9, "odid": 9}, {"did": 9, "odid": 9}]
        info = rl.pick_home_deck(records, filtered_ids=[9])
        self.assertEqual(info["deck_id"], 0)
        self.assertEqual(info["count"], 0)

    def test_empty_records(self):
        info = rl.pick_home_deck([])
        self.assertEqual(info["deck_id"], 0)
        self.assertEqual(info["other_count"], 0)
        self.assertEqual(rl.pick_home_deck(None)["deck_id"], 0)

    def test_cards_without_deck_are_ignored(self):
        info = rl.pick_home_deck([{"did": 0}, {"did": 0}, {"did": 6}])
        self.assertEqual(info["deck_id"], 6)
        self.assertEqual(info["count"], 1)

    def test_card_fields_carry_deck_ids(self):
        """回归：真实卡片的字段来自 card_due_fields，它必须带 did / odid。

        1.5.0 第一次跑隔离探针时，card_due_fields 只给了 type/queue/due/odid/odue，
        于是 pick_home_deck 永远数不到牌组、每次都退回默认牌组，集中刷的延迟还是
        跟着「当前选中的牌组」走。这条用例把 did 钉住。
        """

        class InDeck:
            type = 0
            queue = 0
            due = 0
            did = 12
            odid = 0
            odue = 0

        fields = rl.card_due_fields(InDeck())
        self.assertEqual(fields["did"], 12)
        self.assertEqual(rl.pick_home_deck([fields])["deck_id"], 12)

        class InFilteredDeck:
            type = 2
            queue = 2
            due = -9999
            did = 99
            odid = 12
            odue = 30

        inside = rl.card_due_fields(InFilteredDeck())
        self.assertEqual(rl.home_deck_id(inside), 12)
        self.assertEqual(rl.pick_home_deck([inside])["deck_id"], 12)


class TestCramDelays(unittest.TestCase):
    """集中刷「不影响排期」的三档延迟：重来 / 困难 / 良好（＝0 送回原牌组）。"""
    def test_two_steps_default(self):
        # Anki 默认的新卡步骤 1 分钟 / 10 分钟 → 重来 1 分钟、困难 (1+10)/2=5.5 分钟
        self.assertEqual(rl.cram_preview_delays([60.0, 600.0]), (60, 330, 0))

    def test_good_is_always_zero(self):
        # 0 就是 Anki 预览模式的「送回原牌组」，良好/简单固定这样
        for steps in ([60.0], [60.0, 600.0], [30.0, 90.0, 300.0]):
            self.assertEqual(rl.cram_preview_delays(steps)[2], 0)

    def test_one_step_uses_1_5x(self):
        again, hard, good = rl.cram_preview_delays([60.0])
        self.assertEqual((again, hard, good), (60, 90, 0))

    def test_one_step_long_is_rounded_to_days(self):
        # 20 小时 × 1.5 = 30 小时，超过一天 → 取整天
        self.assertEqual(rl.cram_preview_delays([20 * 3600])[1], rl.DAY_SECS)

    def test_one_step_capped_at_plus_one_day(self):
        # 单步时最多只再加一天
        self.assertEqual(rl.cram_preview_delays([10 * rl.DAY_SECS])[1], 11 * rl.DAY_SECS)

    def test_multi_steps_average_first_two(self):
        self.assertEqual(rl.cram_preview_delays([60.0, 90.0, 300.0])[1], 75)

    def test_empty_falls_back_to_anki_defaults(self):
        self.assertEqual(
            rl.cram_preview_delays([]), rl.cram_preview_delays(rl.DEFAULT_NEW_STEP_SECS)
        )
        self.assertEqual(rl.cram_preview_delays(None), (60, 330, 0))

    def test_junk_values_ignored(self):
        self.assertEqual(rl.cram_preview_delays(["x", 0, -5]), (60, 330, 0))

    def test_again_never_zero(self):
        again = rl.cram_preview_delays([0.4])[0]
        self.assertGreaterEqual(again, 1)

    def test_maybe_round_in_days(self):
        self.assertEqual(rl.maybe_round_in_days(3600), 3600)
        self.assertEqual(rl.maybe_round_in_days(rl.DAY_SECS), rl.DAY_SECS)
        self.assertEqual(rl.maybe_round_in_days(rl.DAY_SECS + 60), rl.DAY_SECS)
        self.assertEqual(
            rl.maybe_round_in_days(int(rl.DAY_SECS * 2.6)), 3 * rl.DAY_SECS
        )


class TestCramStepsFromConfig(unittest.TestCase):
    def test_dict_config(self):
        config = {"new": {"delays": [1, 10]}}
        self.assertEqual(rl.new_card_steps_from_config(config), [60.0, 600.0])

    def test_object_config(self):
        class _New:
            delays = [1, 10]

        class _Conf:
            new = _New()

        self.assertEqual(rl.new_card_steps_from_config(_Conf()), [60.0, 600.0])

    def test_missing_shapes(self):
        for config in (None, {}, {"new": {}}, {"new": {"delays": []}}):
            self.assertEqual(rl.new_card_steps_from_config(config), [])

    def test_junk_and_non_positive_dropped(self):
        config = {"new": {"delays": [1, "x", 0, -3, 10]}}
        self.assertEqual(rl.new_card_steps_from_config(config), [60.0, 600.0])

    def test_filtered_deck_config_has_no_new_steps(self):
        # 筛选牌组自己的配置里没有 "new"，所以要回落到别的牌组
        self.assertEqual(rl.new_card_steps_from_config({"dyn": 1}), [])


class TestCramQueries(unittest.TestCase):
    def test_search_query_drops_due_limit(self):
        # 集中刷＝「勾选状态」的并集，但不再加 (is:due OR is:new) 这一层限制
        query = rl.cram_search_query(["唐诗"], rl.MODE_UNION, rl.DEFAULT_STATES)
        self.assertNotIn(rl.DUE_FILTER, query)
        self.assertEqual(
            query, rl.wanted_query(["唐诗"], rl.MODE_UNION, rl.DEFAULT_STATES)
        )
        # 和日常规则（只收到期 + 新卡）不是一回事
        self.assertNotEqual(
            query,
            rl.build_search_query(["唐诗"], rl.MODE_UNION, rl.DEFAULT_STATES, True),
        )

    def test_search_query_keeps_state_union(self):
        self.assertEqual(
            rl.cram_search_query(["唐诗"], rl.MODE_UNION, rl.DEFAULT_STATES),
            rl.and_query(
                rl.tag_query(["唐诗"]), rl.states_union_query(rl.DEFAULT_STATES)
            ),
        )

    def test_new_only_query_is_the_same_with_or_without_due_limit(self):
        # 只勾「未学习」时本来就不受到期限制，集中刷前后的搜索式逐字一致
        self.assertEqual(
            rl.cram_search_query(["唐诗"], rl.MODE_UNION, ["new"]),
            rl.build_search_query(["唐诗"], rl.MODE_UNION, ["new"], True),
        )

    def test_search_query_partial_states(self):
        self.assertEqual(
            rl.cram_search_query(["唐诗"], rl.MODE_UNION, ["review"]),
            rl.and_query('tag:"唐诗"', rl.STATE_SEARCH["review"]),
        )

    def test_search_query_needs_tags_and_states(self):
        self.assertEqual(rl.cram_search_query([], rl.MODE_UNION, rl.DEFAULT_STATES), "")
        self.assertEqual(rl.cram_search_query(["唐诗"], rl.MODE_UNION, []), "")

    def test_blocked_query_has_no_due_segment(self):
        query = rl.cram_blocked_query(
            ["唐诗"], rl.MODE_UNION, rl.DEFAULT_STATES, "标签筛选::唐诗"
        )
        self.assertNotIn(rl.DUE_FILTER, query)
        self.assertIn(rl.ALWAYS_EXCLUDED_QUERY, query)
        self.assertIn("deck:filtered", query)
        self.assertIn('-deck:"标签筛选::唐诗"', query)

    def test_blocked_query_empty_without_tags_or_states(self):
        self.assertEqual(rl.cram_blocked_query([], rl.MODE_UNION, rl.DEFAULT_STATES), "")
        self.assertEqual(rl.cram_blocked_query(["唐诗"], rl.MODE_UNION, []), "")


class TestCramSessions(unittest.TestCase):
    def test_writeback_session(self):
        session = rl.make_cram_session(True, 123)
        self.assertEqual(
            session,
            {
                "mode": "writeback",
                "started_at": 123,
                "cram_deck_id": 0,
                "round": 1,
            },
        )
        self.assertTrue(rl.cram_writes_back(session))
        self.assertIn("写回排期", rl.cram_label(session))

    def test_preview_session(self):
        session = rl.make_cram_session(False, 0)
        self.assertEqual(
            session,
            {
                "mode": "preview",
                "started_at": 0,
                "cram_deck_id": 0,
                "round": 1,
            },
        )
        self.assertFalse(rl.cram_writes_back(session))
        self.assertIn("不影响排期", rl.cram_label(session))
        self.assertIn("临时牌组", rl.cram_label(session))

    def test_session_keeps_temp_deck_and_round(self):
        session = rl.make_cram_session(False, 5, 4242, 3)
        self.assertEqual(rl.cram_session_deck_id(session), 4242)
        self.assertEqual(rl.cram_round(session), 3)
        self.assertEqual(rl.cram_session_deck_id(None), 0)
        self.assertEqual(rl.cram_round(None), 1)

    def test_round_never_below_one(self):
        self.assertEqual(rl.cram_round({"mode": "preview", "round": 0}), 1)
        self.assertEqual(rl.cram_round({"mode": "preview", "round": "x"}), 1)
        self.assertEqual(rl.make_cram_session(False, 0, 0, 0)["round"], 1)

    def test_junk_started_at_becomes_zero(self):
        self.assertEqual(rl.make_cram_session(False, "x")["started_at"], 0)

    def test_normalize_rejects_junk(self):
        self.assertIsNone(rl.normalize_cram_session(None))
        self.assertIsNone(rl.normalize_cram_session("preview"))
        self.assertIsNone(rl.normalize_cram_session({"mode": "随便"}))
        self.assertIsNone(rl.normalize_cram_session({}))

    def test_normalize_fills_defaults(self):
        self.assertEqual(
            rl.normalize_cram_session({"mode": "preview"}),
            {
                "mode": "preview",
                "started_at": 0,
                "cram_deck_id": 0,
                "round": 1,
            },
        )
        self.assertEqual(
            rl.normalize_cram_session({"mode": "writeback", "started_at": "9"})[
                "started_at"
            ],
            9,
        )

    def test_old_record_without_temp_deck_is_compatible(self):
        # 1.3.0 的记录：没有 cram_deck_id / round，补成 0 / 1（走兼容收尾）
        session = rl.normalize_cram_session({"mode": "preview", "started_at": 7})
        self.assertEqual(session["started_at"], 7)
        self.assertEqual(rl.cram_session_deck_id(session), 0)
        self.assertEqual(rl.cram_round(session), 1)

    def test_temp_deck_names(self):
        self.assertEqual(rl.cram_temp_name("标签筛选::RWBY"), "标签筛选::RWBY·集中刷")
        self.assertEqual(rl.cram_temp_name(""), "集中刷")
        self.assertEqual(
            rl.cram_temp_deck_name("RWBY", ["RWBY·集中刷"]), "RWBY·集中刷 (2)"
        )
        self.assertEqual(rl.cram_temp_deck_name("RWBY", ["别的"]), "RWBY·集中刷")
        self.assertTrue(
            rl.is_cram_temp_name("标签筛选::RWBY·集中刷", "标签筛选::RWBY")
        )
        self.assertFalse(rl.is_cram_temp_name("标签筛选::RWBY", "标签筛选::RWBY"))

    def test_label_of_missing_session(self):
        self.assertEqual(rl.cram_label(None), "")
        self.assertEqual(rl.cram_label({}), "")
        self.assertFalse(rl.cram_writes_back(None))


# --------------------------------------------------------------------------
# 离开集中刷牌组时的询问判定
# --------------------------------------------------------------------------


class TestCramLeaveDecision(unittest.TestCase):
    def test_no_session_never_asks(self):
        # 没有集中刷会话（cram_deck_id 为空）→ 任何切换都不问
        self.assertFalse(
            rl.should_ask_cram_end("deckBrowser", "overview", 1790348666290, 0)
        )
        self.assertFalse(rl.should_ask_cram_end("deckBrowser", "overview", 1, None))

    def test_back_to_deck_list_asks(self):
        # 回到牌组列表 → 问
        self.assertTrue(
            rl.should_ask_cram_end("deckBrowser", "overview", 0, 1790348666290)
        )

    def test_staying_in_same_deck_does_not_ask(self):
        # 停在集中刷牌组自己的总览／复习 → 正常刷题，不问
        cram = 1790348666290
        self.assertFalse(rl.should_ask_cram_end("overview", "review", cram, cram))
        self.assertFalse(rl.should_ask_cram_end("review", "overview", cram, cram))

    def test_switching_to_other_deck_asks(self):
        # 切到别的牌组的总览／复习 → 问
        cram = 1790348666290
        other = 1790348666291
        self.assertTrue(rl.should_ask_cram_end("overview", "deckBrowser", other, cram))
        self.assertTrue(rl.should_ask_cram_end("review", "overview", other, cram))

    def test_unchanged_state_does_not_ask(self):
        # 状态没变 → 不问（避免同状态重复触发）
        cram = 1790348666290
        self.assertFalse(rl.should_ask_cram_end("overview", "overview", 123, cram))

    def test_irrelevant_state_does_not_ask(self):
        # 既不是总览／复习／牌组列表（比如统计、添加卡片）→ 不问
        cram = 1790348666290
        self.assertFalse(rl.should_ask_cram_end("stats", "overview", 123, cram))
        self.assertFalse(rl.should_ask_cram_end("add", "overview", 123, cram))
        self.assertFalse(rl.should_ask_cram_end("", "overview", 123, cram))

    def test_junk_ids_do_not_crash(self):
        # 选中牌组 id 是垃圾值 → 保守起见不问，也不能抛异常
        self.assertFalse(rl.should_ask_cram_end("overview", "deckBrowser", "x", 5))
        self.assertFalse(
            rl.should_ask_cram_end("overview", "deckBrowser", 5, "not-a-number")
        )
        # selected 缺失（0 / None）也不问
        self.assertFalse(rl.should_ask_cram_end("overview", "deckBrowser", 0, 9))
        self.assertFalse(rl.should_ask_cram_end("overview", "deckBrowser", None, 9))

    def test_state_key_normalizes(self):
        class FakeEnum:
            value = "DeckBrowser"

        self.assertEqual(rl.state_key("OverView"), "overview")
        self.assertEqual(rl.state_key(FakeEnum()), "deckbrowser")
        self.assertEqual(rl.state_key(None), "")

    def test_string_ids_still_compare(self):
        # 传入字符串形式的 id（某些接口返回字符串）也要能正确比较
        cram = 1790348666290
        self.assertFalse(rl.should_ask_cram_end("overview", "review", str(cram), cram))
        self.assertTrue(
            rl.should_ask_cram_end("overview", "review", str(cram + 1), cram)
        )


# --------------------------------------------------------------------------
# 1.5.2：结束集中刷后该重画哪一页
# --------------------------------------------------------------------------


class TestRedrawTarget(unittest.TestCase):
    def test_deck_browser_camel_case(self):
        # 这就是 1.5.0 那个 bug：Anki 传的是 "deckBrowser"（首字母大写），
        # 插件内部转成小写后再比，两边必须判成同一页。
        self.assertEqual(rl.redraw_target("deckBrowser"), rl.REDRAW_DECK_BROWSER)

    def test_deck_browser_lower_case(self):
        self.assertEqual(rl.redraw_target("deckbrowser"), rl.REDRAW_DECK_BROWSER)
        self.assertEqual(rl.redraw_target("DeckBrowser"), rl.REDRAW_DECK_BROWSER)
        self.assertEqual(rl.redraw_target("  deckBrowser  "), rl.REDRAW_DECK_BROWSER)

    def test_overview(self):
        self.assertEqual(rl.redraw_target("overview"), rl.REDRAW_OVERVIEW)
        self.assertEqual(rl.redraw_target("OverView"), rl.REDRAW_OVERVIEW)

    def test_other_states_draw_nothing(self):
        # 复习界面、统计、添加卡片：都不能乱画（会跟 WebView 抢同一页）
        for state in ("review", "stats", "add", "profileManager", ""):
            self.assertEqual(rl.redraw_target(state), rl.REDRAW_NONE, state)
        self.assertEqual(rl.redraw_target(None), rl.REDRAW_NONE)

    def test_state_wrapper(self):
        class FakeEnum:
            value = "DeckBrowser"

        self.assertEqual(rl.redraw_target(FakeEnum()), rl.REDRAW_DECK_BROWSER)

    def test_constants_are_lowercase(self):
        # 常量本身必须是小写：调用方拿它跟 state_key() 的结果比
        self.assertEqual(rl.REDRAW_DECK_BROWSER, rl.REDRAW_DECK_BROWSER.lower())
        self.assertEqual(rl.REDRAW_OVERVIEW, rl.REDRAW_OVERVIEW.lower())
        self.assertEqual(rl.REDRAW_NONE, "")


# --------------------------------------------------------------------------
# crash.log：最后一次致命异常指向哪个插件
# --------------------------------------------------------------------------


class TestOpChangesLike(unittest.TestCase):
    class FakeChanges:
        """假装是 protobuf 的 OpChanges 本体（没有 .changes 字段）。"""

        def SerializeToString(self) -> bytes:
            return b""

    class FakeWithChanges:
        """假装是 OpChangesWithCount / OpChangesWithId（带 .changes）。"""

        changes = object()

    def test_plain_results_are_invalid(self):
        # 这就是 1.5.1 那次「遇到了问题」弹窗的来源：后台操作返回了 list
        for value in ([], [1, 2], (), (1, "x"), 0, 7, "x", b"x", {}, set(), True, 1.5):
            self.assertFalse(rl.op_changes_like(value), repr(value))

    def test_none_is_invalid(self):
        self.assertFalse(rl.op_changes_like(None))

    def test_with_changes_field(self):
        self.assertTrue(rl.op_changes_like(self.FakeWithChanges()))

    def test_protobuf_like(self):
        self.assertTrue(rl.op_changes_like(self.FakeChanges()))

    def test_first_op_changes(self):
        good = self.FakeWithChanges()
        self.assertIs(rl.first_op_changes(None, 3, [], good), good)
        self.assertIs(rl.first_op_changes(good, self.FakeChanges()), good)
        self.assertIsNone(rl.first_op_changes([], (), 5, None))
        self.assertIsNone(rl.first_op_changes())

    def test_changes_property_none_is_invalid(self):
        class Empty:
            changes = None

        self.assertFalse(rl.op_changes_like(Empty()))


class TestCrashLogCulprit(unittest.TestCase):
    # 取自本机 crash.log 里最后一次致命异常的一段（互动答题卡）
    SAMPLE = (
        "Windows fatal exception: code 0x8001010d\n"
        "\n"
        "Thread 0x00004a98 (most recent call first):\n"
        '  File "C:\\Users\\creep\\AppData\\Roaming\\Anki2\\addons21\\interactive_quiz\\'
        '__init__.py", line 1880 in _run_quiz_dialog\n'
        '  File "C:\\Users\\creep\\AppData\\Roaming\\Anki2\\addons21\\interactive_quiz\\'
        '__init__.py", line 1495 in open_quiz_dialog\n'
    )

    def test_parses_addon_and_line(self):
        found = rl.crash_log_culprit(self.SAMPLE)
        self.assertIsNotNone(found)
        self.assertEqual(found["addon"], "interactive_quiz")
        self.assertEqual(found["file"], "__init__.py")
        self.assertEqual(found["line"], 1880)

    def test_forward_slashes(self):
        text = (
            "Fatal Python error: Segmentation fault\n"
            "  File \"C:/Users/x/Anki2/addons21/tag_filtered_deck/rule_logic.py\", "
            "line 42 in boom\n"
        )
        found = rl.crash_log_culprit(text)
        self.assertIsNotNone(found)
        self.assertEqual(found["addon"], "tag_filtered_deck")
        self.assertEqual(found["file"], "rule_logic.py")
        self.assertEqual(found["line"], 42)

    def test_takes_last_block_only(self):
        # 前面一段是别的插件，最后一段才是真正的现场 → 只认最后一段
        text = (
            "Windows fatal exception: code 0x1\n"
            '  File "C:\\Anki2\\addons21\\some_other_addon\\__init__.py", line 7 in a\n'
            "Windows fatal exception: code 0x2\n"
            '  File "C:\\Anki2\\addons21\\tag_filtered_deck\\__init__.py", line 99 in b\n'
        )
        found = rl.crash_log_culprit(text)
        self.assertEqual(found["addon"], "tag_filtered_deck")
        self.assertEqual(found["line"], 99)

    def test_last_block_without_addon_returns_none(self):
        # 最后一次异常里完全没有插件文件 → 不是插件引起的
        text = (
            "Windows fatal exception: code 0x1\n"
            '  File "C:\\Anki2\\addons21\\tag_filtered_deck\\__init__.py", line 7 in a\n'
            "Windows fatal exception: code 0x2\n"
            '  File "C:\\Program Files\\Anki\\aqt\\main.py", line 12 in run\n'
        )
        self.assertIsNone(rl.crash_log_culprit(text))

    def test_junk_input(self):
        self.assertIsNone(rl.crash_log_culprit(""))
        self.assertIsNone(rl.crash_log_culprit(None))
        self.assertIsNone(rl.crash_log_culprit("没有崩溃的普通日志\n"))

    def test_label_contains_addon_and_time(self):
        found = rl.crash_log_culprit(self.SAMPLE)
        stamp = 1790401455  # 与项目里记账用的一致，固定值方便断言
        label = rl.crash_culprit_label(found, stamp)
        self.assertIn("interactive_quiz", label)
        self.assertIn(time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)), label)
        # 没有时间戳时只显示插件名
        self.assertEqual(rl.crash_culprit_label(found, 0), "最近一次致命异常指向 interactive_quiz")
        # 解析不到就返回空字符串
        self.assertEqual(rl.crash_culprit_label(None, stamp), "")
        self.assertEqual(rl.crash_culprit_label({}, stamp), "")

    def test_real_crash_log_when_present(self):
        # 本机真有 crash.log 时，解析结果应当稳定指向某个插件（没有就跳过）
        path = os.path.join(
            os.environ.get("APPDATA", ""), "Anki2", "crash.log"
        )
        if not os.path.exists(path):
            self.skipTest("本机没有 crash.log")
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        found = rl.crash_log_culprit(text)
        if found is None:
            self.skipTest("最后一次致命异常没有插件栈帧")
        self.assertTrue(found["addon"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
