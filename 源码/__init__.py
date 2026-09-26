"""标签筛选牌组 —— Anki 插件

按标签（并集或交集）把卡片挑出来，建成 Anki 原生筛选牌组：

* 筛选牌组里的卡和源卡是**同一张**，复习记录、排期全部共享；
* 在筛选牌组里作答会按你的选择重排原卡（固定开启）；
* 删掉筛选牌组只是把卡送回原牌组，不会删卡。

入口：工具菜单「标签筛选牌组…」、卡片浏览器的标签右键、
牌组列表里的筛选牌组右键。

接口全部走 Anki 26.09 的现代 API：
col.sched.get_or_create_filtered_deck / add_or_update_filtered_deck /
rebuild_filtered_deck，删除走 col.decks.remove（官方对筛选牌组的处理就是
把卡送回原牌组、不删卡，见 rslib/src/decks/remove.rs）。

「集中刷」是一次性的动作（不是规则里的开关，也不改原筛选牌组的设置）：点一下，
先清空原筛选牌组（卡片回原牌组、恢复原始到期时间），再另建一个临时筛选牌组
「<原牌组名>·集中刷」把还没到期的卡也收进来突击刷。可以选「不影响排期」（走 Anki
的预览模式）或「影响排期」（照常按算法写回原卡）。每次点击＝一轮，刷完可以点
「再刷一轮」；**关掉集中刷小窗就等于结束这一轮**（删掉临时牌组、按原规则重建原
筛选牌组）；关掉 Anki 重开也会自动收尾。

点「集中刷…」之后主窗口会**直接进答题界面**（集中刷小窗退到后面，需要时从
管理窗口或牌组右键调回前面）；关掉小窗后主窗口回到**原筛选牌组的总览页**，
牌组列表与总览页都会当场重画，不会再留着已经删掉的临时牌组。

插件自己往 addons21/<插件>/user_files/log.txt 写运行日志，菜单与钩子都做过防护：
插件内部出错只记日志，不再冒泡到 Anki 的「遇到了问题」弹窗。
"""

from __future__ import annotations

import functools
import json
import os
import traceback
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from anki.collection import OpChanges
from anki.decks import DeckId, FilteredDeckConfig
from anki.scheduler import FilteredDeckForUpdate
from aqt import gui_hooks, mw
from aqt.operations import CollectionOp, QueryOp
from aqt.qt import (
    QAction,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDesktopServices,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTimer,
    QTreeWidget,
    QTreeWidgetItem,
    QUrl,
    QVBoxLayout,
    QWidget,
    Qt,
    qconnect,
)
from aqt.utils import askUser, showInfo, showWarning, tooltip

try:  # 作为插件包加载
    from .rule_logic import (
        ALWAYS_EXCLUDED_QUERY,
        CRAM_MODE_PREVIEW,
        CRAM_MODE_WRITEBACK,
        DEFAULT_INCLUDE_NOT_DUE,
        DEFAULT_LIMIT,
        DEFAULT_ORDER,
        DEFAULT_PARENT,
        DEFAULT_STATES,
        DEFAULT_NEW_STEP_SECS,
        MAX_LIMIT,
        MIN_LIMIT,
        MODE_INTERSECTION,
        MODE_UNION,
        STATE_CHOICES,
        and_query,
        available_orders,
        blocked_query,
        build_search_query,
        clamp_limit,
        clean_tags,
        cram_label,
        cram_preview_delays,
        cram_search_query,
        cram_writes_back,
        default_leaf_name,
        deck_search_term,
        include_not_due_for_scope,
        empty_counts,
        filter_tag_choices,
        in_scope_query,
        join_deck_name,
        make_cram_session,
        make_rule,
        negate_term,
        new_card_steps_from_config,
        normalize_cram_session,
        normalize_states,
        normalize_rule,
        order_label,
        preview_lines,
        rule_matches_actual,
        rule_summary,
        split_deck_name,
        state_label,
        state_count_query,
        states_for_scope,
        states_label,
        tag_query,
        tag_search_term,
        unique_deck_name,
        wanted_query,
    )
    from .rule_logic import (  # 1.3.0 新增：集中刷窗口判定 + crash.log 解析
        crash_culprit_label,
        crash_log_culprit,
        should_ask_cram_end,
        state_key,
    )
    from .rule_logic import (  # 1.4.0 新增：按卡片自己的到期时间判定
        MAX_BROWSE_CARDS,
        card_due_fields,
        card_is_due,
        classify_cards,
        cram_round,
        cram_session_deck_id,
        cram_temp_deck_name,
        cram_temp_name,
        due_filter_enabled,
        due_restriction_applies,
    )
    from .rule_logic import (  # 1.5.0 新增：正向 include_not_due + 来源牌组选择
        include_not_due_from_due_only,
        normalize_include_not_due,
        pick_home_deck,
    )
    from .rule_logic import (  # 1.5.2 新增：界面重画目标 + 后台返回值判定
        REDRAW_DECK_BROWSER,
        REDRAW_NONE,
        REDRAW_OVERVIEW,
        first_op_changes,
        op_changes_like,
        redraw_target,
    )
except ImportError:  # 被当成顶层脚本加载（测试时）
    from rule_logic import (  # type: ignore
        ALWAYS_EXCLUDED_QUERY,
        CRAM_MODE_PREVIEW,
        CRAM_MODE_WRITEBACK,
        DEFAULT_INCLUDE_NOT_DUE,
        DEFAULT_LIMIT,
        DEFAULT_ORDER,
        DEFAULT_PARENT,
        DEFAULT_STATES,
        DEFAULT_NEW_STEP_SECS,
        MAX_LIMIT,
        MIN_LIMIT,
        MODE_INTERSECTION,
        MODE_UNION,
        STATE_CHOICES,
        and_query,
        available_orders,
        blocked_query,
        build_search_query,
        clamp_limit,
        clean_tags,
        cram_label,
        cram_preview_delays,
        cram_search_query,
        cram_writes_back,
        default_leaf_name,
        deck_search_term,
        include_not_due_for_scope,
        empty_counts,
        filter_tag_choices,
        in_scope_query,
        join_deck_name,
        make_cram_session,
        make_rule,
        negate_term,
        new_card_steps_from_config,
        normalize_cram_session,
        normalize_states,
        normalize_rule,
        order_label,
        preview_lines,
        rule_matches_actual,
        rule_summary,
        split_deck_name,
        state_label,
        state_count_query,
        states_for_scope,
        states_label,
        tag_query,
        tag_search_term,
        unique_deck_name,
        wanted_query,
    )
    from rule_logic import (  # type: ignore  # 1.3.0 新增
        crash_culprit_label,
        crash_log_culprit,
        should_ask_cram_end,
        state_key,
    )
    from rule_logic import (  # type: ignore  # 1.4.0 新增
        MAX_BROWSE_CARDS,
        card_due_fields,
        card_is_due,
        classify_cards,
        cram_round,
        cram_session_deck_id,
        cram_temp_deck_name,
        cram_temp_name,
        due_filter_enabled,
        due_restriction_applies,
    )
    from rule_logic import (  # type: ignore  # 1.5.0 新增
        include_not_due_from_due_only,
        normalize_include_not_due,
        pick_home_deck,
    )
    from rule_logic import (  # type: ignore  # 1.5.2 新增
        REDRAW_DECK_BROWSER,
        REDRAW_NONE,
        REDRAW_OVERVIEW,
        first_op_changes,
        op_changes_like,
        redraw_target,
    )

__version__ = "1.5.2"

ADDON_TITLE = "标签筛选牌组"
CONFIG_KEY = "tag_filtered_deck_rules"
CRAM_KEY = "tag_filtered_deck_cram"
LOG_PREFIX = "[标签筛选牌组]"

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
ADDON_FOLDER = os.path.basename(ADDON_DIR)
# 插件自己的日志：写进 addons21\<插件>\user_files\，Anki 升级插件时不会碰它
LOG_FILE_NAME = "log.txt"
LOG_MAX_BYTES = 512 * 1024

# 从 GitHub 检查更新。仓库是公开的，也在 AnkiWeb 之外放一份 .ankiaddon 供下载。
UPDATE_REPO = "creeperboo/anki-tag-filtered-deck"
UPDATE_BRANCH = "main"
UPDATE_ASSET = "tag_filtered_deck.ankiaddon"
UPDATE_STATE_PATH = os.path.join(ADDON_DIR, "update.json")
UPDATE_INTERVAL_SECONDS = 86400  # 启动时最多一天查一次

# 一次会话里只为同一个牌组提示一次「被内置设置改过」
_EXTERNAL_WARNED: set[int] = set()
# 打开的对话框要留引用，否则非模态窗口会被回收
_OPEN_DIALOGS: set[Any] = set()
# 每个牌组最多一个集中刷小窗；关窗就等于结束集中刷
_CRAM_WINDOWS: dict[int, Any] = {}
_MANAGER: Any = None


def _log_path() -> str:
    return os.path.join(ADDON_DIR, "user_files", LOG_FILE_NAME)


def _log(message: str) -> None:
    """写一行日志：终端打一份，插件目录里的 log.txt 存一份。

    日志只用来排查问题，任何写盘失败都必须静默——绝不能因为它把功能带崩。
    """
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(f"{LOG_PREFIX} {message}")
    try:
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            # 太长就砍掉前一半，保留最近的记录，避免无限长大
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                kept = fh.read()[-LOG_MAX_BYTES // 2 :]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("（日志过长，已截断，下面是最近的部分）\n" + kept)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def open_log_file(parent: Any = None) -> None:
    """点「打开日志文件」时调用：日志不存在就先写一行说明再打开。"""
    path = _log_path()
    try:
        if not os.path.exists(path):
            _log("（打开了日志文件，此前还没有别的记录）")
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        return
    except Exception as exc:
        _log(f"打开日志文件失败：{exc}")
    try:
        showInfo(f"日志文件在：\n{path}", parent=parent)
    except Exception:
        pass


def _read_crash_culprit() -> dict[str, Any] | None:
    """读 Anki 的 crash.log，解析最后一次致命异常涉及的插件。"""
    path = ""
    try:
        base = str(getattr(getattr(mw, "pm", None), "base", "") or "")
        if base:
            path = os.path.join(base, "crash.log")
    except Exception:
        path = ""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        mtime = os.path.getmtime(path)
    except Exception as exc:
        _log(f"读取 crash.log 失败：{exc}")
        return None
    found = crash_log_culprit(text)
    if found is None:
        return None
    found["mtime"] = mtime
    return found


def crash_culprit_text() -> str:
    """管理窗口底部那行只读提示；解析不到就返回空字符串。"""
    found = _read_crash_culprit()
    if not found:
        return ""
    label = crash_culprit_label(found, found.get("mtime") or 0)
    addon = str(found.get("addon") or "")
    if label and addon == ADDON_FOLDER:
        label = label.replace(addon, f"{ADDON_TITLE}（本插件）", 1)
    return label


def _guarded(label: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """把菜单/钩子回调包一层：出错只写日志，不再冒泡给 Anki 弹「遇到了问题」。"""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            _log(f"{label} 出错：{exc}")
            _log(traceback.format_exc())
            return None

    return wrapper


def _col() -> Any:
    return getattr(mw, "col", None)


def _addon_config() -> dict[str, Any]:
    """插件设置里的默认值，可在「插件 → 配置」里改。"""
    defaults: dict[str, Any] = {
        "default_parent": DEFAULT_PARENT,
        "default_states": list(DEFAULT_STATES),
        "default_include_not_due": bool(DEFAULT_INCLUDE_NOT_DUE),
        "default_order": DEFAULT_ORDER,
        "default_limit": DEFAULT_LIMIT,
        "auto_rebuild": True,
        "update_check": True,
    }
    try:
        raw = mw.addonManager.getConfig(__name__) or {}
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        # 老配置只有 default_scope，先迁移成新字段
        if "default_scope" in raw and "default_states" not in raw:
            defaults["default_states"] = states_for_scope(raw.get("default_scope"))
            defaults["default_include_not_due"] = include_not_due_for_scope(
                raw.get("default_scope")
            )
        # 1.4.0 的 default_due_only 是反向的（True ＝ 只收到期），取反后迁移
        elif "default_due_only" in raw and "default_include_not_due" not in raw:
            defaults["default_include_not_due"] = include_not_due_from_due_only(
                raw.get("default_due_only")
            )
        for key in list(defaults):
            if key in raw and raw[key] is not None:
                defaults[key] = raw[key]
    defaults["default_states"] = normalize_states(defaults["default_states"]) or list(
        DEFAULT_STATES
    )
    defaults["default_include_not_due"] = bool(
        defaults.get("default_include_not_due", DEFAULT_INCLUDE_NOT_DUE)
    )
    defaults["auto_rebuild"] = bool(defaults["auto_rebuild"])
    defaults["update_check"] = bool(defaults["update_check"])
    return defaults


def save_addon_config(config: dict[str, Any]) -> bool:
    """把插件设置写回 addon 的配置（管理窗口里的几个勾选框用）。"""
    try:
        mw.addonManager.writeConfig(__name__, config)
        return True
    except Exception as exc:
        _log(f"保存插件设置失败：{exc}")
        showWarning(f"保存插件设置失败：{exc}")
        return False


def auto_rebuild_enabled() -> bool:
    return bool(_addon_config().get("auto_rebuild", True))


# --------------------------------------------------------------------------
# 规则的读写（存在当前集合配置里，按牌组 ID 索引）
# --------------------------------------------------------------------------


def load_rules(col: Any = None) -> dict[str, dict]:
    collection = col if col is not None else _col()
    if collection is None:
        return {}
    try:
        raw = collection.get_config(CONFIG_KEY, {}) or {}
    except Exception as exc:
        _log(f"读取规则失败：{exc}")
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for key, value in raw.items():
        rule = normalize_rule(value)
        if not rule:
            continue
        try:
            rule["deck_id"] = int(key)
        except Exception:
            continue
        out[str(rule["deck_id"])] = rule
    return out


def save_rules(rules: dict[str, dict], col: Any = None) -> None:
    collection = col if col is not None else _col()
    if collection is None:
        return
    try:
        collection.set_config(CONFIG_KEY, rules)
    except Exception as exc:
        _log(f"保存规则失败：{exc}")


def put_rule(rule: dict, col: Any = None) -> None:
    collection = col if col is not None else _col()
    if collection is None:
        return
    rules = load_rules(collection)
    rules[str(int(rule["deck_id"]))] = rule
    save_rules(rules, collection)


def drop_rule(deck_id: int, col: Any = None) -> None:
    collection = col if col is not None else _col()
    if collection is None:
        return
    rules = load_rules(collection)
    if rules.pop(str(int(deck_id)), None) is not None:
        save_rules(rules, collection)


def get_rule(deck_id: int, col: Any = None) -> dict | None:
    return load_rules(col).get(str(int(deck_id)))


def cleanup_stale_rules(col: Any = None) -> list[str]:
    """手动删掉筛选牌组后，把失效规则清掉；顺便跟随 Anki 里的重命名。"""
    collection = col if col is not None else _col()
    if collection is None:
        return []
    rules = load_rules(collection)
    kept: dict[str, dict] = {}
    removed: list[str] = []
    for key, rule in rules.items():
        deck_id = int(rule.get("deck_id") or key)
        name = _deck_name(collection, deck_id)
        if not name or not _is_filtered(collection, deck_id):
            removed.append(str(rule.get("name") or f"牌组 {deck_id}"))
            continue
        rule["deck_id"] = deck_id
        if name != rule.get("name") or key != str(deck_id):
            rule["name"] = name
            parent, leaf = split_deck_name(name)
            rule["parent"] = parent
            rule["leaf"] = leaf
        kept[str(deck_id)] = rule
    if kept != rules:
        save_rules(kept, collection)
    return removed


# --------------------------------------------------------------------------
# 集中刷会话（存在当前集合配置里，按牌组 ID 索引）
# --------------------------------------------------------------------------


def load_cram_sessions(col: Any = None) -> dict[str, dict]:
    """读出所有「正在集中刷」的记录。"""
    collection = col if col is not None else _col()
    if collection is None:
        return {}
    try:
        raw = collection.get_config(CRAM_KEY, {}) or {}
    except Exception as exc:
        _log(f"读取集中刷记录失败：{exc}")
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for key, value in raw.items():
        session = normalize_cram_session(value)
        if session is None:
            continue
        try:
            deck_id = int(key)
        except Exception:
            continue
        out[str(deck_id)] = session
    return out


def save_cram_sessions(sessions: dict[str, dict], col: Any = None) -> None:
    collection = col if col is not None else _col()
    if collection is None:
        return
    try:
        collection.set_config(CRAM_KEY, sessions)
    except Exception as exc:
        _log(f"保存集中刷记录失败：{exc}")


def get_cram_session(deck_id: int, col: Any = None) -> dict | None:
    return load_cram_sessions(col).get(str(int(deck_id)))


def put_cram_session(deck_id: int, session: dict, col: Any = None) -> None:
    collection = col if col is not None else _col()
    if collection is None:
        return
    sessions = load_cram_sessions(collection)
    sessions[str(int(deck_id))] = session
    save_cram_sessions(sessions, collection)


def drop_cram_session(deck_id: int, col: Any = None) -> None:
    collection = col if col is not None else _col()
    if collection is None:
        return
    sessions = load_cram_sessions(collection)
    if sessions.pop(str(int(deck_id)), None) is not None:
        save_cram_sessions(sessions, collection)


# --------------------------------------------------------------------------
# 牌组小工具
# --------------------------------------------------------------------------


def _deck_dict(col: Any, deck_id: int) -> dict | None:
    try:
        return col.decks.get(DeckId(int(deck_id)), default=False)
    except Exception:
        return None


def _deck_exists(col: Any, deck_id: int) -> bool:
    return bool(_deck_dict(col, deck_id))


def _is_filtered(col: Any, deck_id: int) -> bool:
    deck = _deck_dict(col, deck_id)
    return bool(deck and deck.get("dyn"))


def _deck_name(col: Any, deck_id: int) -> str:
    deck = _deck_dict(col, deck_id)
    if not deck:
        return ""
    return str(deck.get("name") or "")


def _cards_in_deck(col: Any, deck_id: int) -> int:
    try:
        return len(col.decks.cids(DeckId(int(deck_id))))
    except Exception:
        return 0


def _child_decks(col: Any, deck_id: int) -> list[tuple[str, int]]:
    try:
        kids = col.decks.children(DeckId(int(deck_id)))
    except Exception:
        return []
    return [(str(name), int(did)) for name, did in kids]


def _deck_names(col: Any, include_filtered: bool = True) -> list[str]:
    try:
        items = col.decks.all_names_and_ids(include_filtered=include_filtered)
    except Exception:
        return []
    return sorted({str(item.name) for item in items})


def _parent_choices(col: Any) -> list[str]:
    normal = _deck_names(col, include_filtered=False)
    others = sorted(name for name in normal if name != DEFAULT_PARENT)
    return ["（顶层）", DEFAULT_PARENT] + others


def _deck_ids_by_name(col: Any, name: str, include_filtered: bool = True) -> list[int]:
    """按名字找牌组 ID（Anki 的牌组名是唯一的，这里只用来兜底清理残留）。"""
    wanted = str(name or "").strip()
    if not wanted:
        return []
    try:
        items = col.decks.all_names_and_ids(include_filtered=include_filtered)
    except Exception:
        return []
    return [int(item.id) for item in items if str(item.name).strip() == wanted]


def _cram_temp_deck_ids(col: Any, deck_id: int, temp_id: Any = None) -> list[int]:
    """这条规则的临时集中刷牌组 id（含名字对得上、但记录里没记下的残留）。"""
    out: list[int] = []
    try:
        temp = int(temp_id or 0)
    except Exception:
        temp = 0
    if temp:
        out.append(temp)
    original = _deck_name(col, deck_id)
    if not original:
        return out
    base = cram_temp_name(original)
    prefix = base + " ("
    for name in _deck_names(col, include_filtered=True):
        if name == base or name.startswith(prefix):
            out.extend(_deck_ids_by_name(col, name))
    seen: set[int] = set()
    unique: list[int] = []
    for value in out:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _remove_filtered_decks(col: Any, deck_ids: Iterable[Any]) -> tuple[int, Any]:
    """删掉一批**筛选牌组**（卡片回原牌组）；不是筛选牌组的一律不碰。

    返回 ``(删掉几个, Anki 原生结果)``：个数是给日志和断言的，原生结果留给
    后台操作当返回值（见 ``_op_changes``）。
    """
    targets: list[DeckId] = []
    found: list[int] = []
    skipped: list[int] = []
    for value in deck_ids:
        try:
            deck_id = int(value)
        except Exception:
            continue
        if not deck_id:
            continue
        if not _is_filtered(col, deck_id):
            if deck_id not in skipped:
                skipped.append(deck_id)
            continue
        if deck_id in found:
            continue
        found.append(deck_id)
        targets.append(DeckId(deck_id))
    if not targets:
        if skipped:
            _log(f"没有需要删除的筛选牌组（{skipped} 已经不是筛选牌组或不在了）")
        # 没有任何目标也必须交出合规的返回值：后台操作**成功**返回 None 时，
        # Anki 会在 on_op_finished() 里读 result.changes 并抛
        # 「'NoneType' object has no attribute 'changes'」弹窗（临时牌组已经被
        # 手动删掉、再结束集中刷就是这条路径）。
        return 0, _op_changes()
    out: Any = None
    try:
        out = col.decks.remove(targets)
    except Exception as exc:
        _log(f"删除筛选牌组失败：{exc}")
        return 0, _op_changes()
    _log(
        f"已删除筛选牌组 {len(targets)} 个：{found}"
        + (f"（跳过不是筛选牌组的 {skipped}）" if skipped else "")
    )
    return len(targets), _op_changes(out)


def _all_tags(col: Any) -> list[str]:
    found: set[str] = set()
    try:
        found.update(str(tag) for tag in col.tags.all())
    except Exception:
        pass
    try:

        def walk(node: Any, head: str = "") -> None:
            for child in node.children:
                name = head + str(child.name)
                found.add(name)
                walk(child, name + "::")

        walk(col.tags.tree())
    except Exception:
        pass
    return sorted(found, key=lambda text: text.lower())


def read_actual(col: Any, deck_id: int) -> dict | None:
    """读筛选牌组当前真实的设置；不是筛选牌组就返回 None。"""
    try:
        deck = col.sched.get_or_create_filtered_deck(DeckId(int(deck_id)))
    except Exception:
        return None
    terms = list(deck.config.search_terms)
    first = terms[0] if terms else None
    return {
        "name": str(deck.name or ""),
        "search": str(first.search) if first else "",
        "limit": int(first.limit) if first else 0,
        "order": int(first.order) if first else DEFAULT_ORDER,
        "reschedule": bool(deck.config.reschedule),
        "terms": len(terms),
    }


# --------------------------------------------------------------------------
# 命中数量
# --------------------------------------------------------------------------


def _count(col: Any, query: str) -> int:
    if not query:
        return 0
    try:
        return len(col.find_cards(query))
    except Exception as exc:
        _log(f"搜索式无法执行：{query}（{exc}）")
        return 0


def _find_cards(col: Any, query: str) -> list[int]:
    """跑一条搜索式，拿回卡号列表（拿不到就当空）。"""
    if not query:
        return []
    try:
        return [int(value) for value in col.find_cards(query)]
    except Exception as exc:
        _log(f"搜索式无法执行：{query}（{exc}）")
        return []


def _today(col: Any) -> int | None:
    """Anki 今天的「第几天」（复习卡的到期日就是这个口径）。

    读不到时返回 None——不能返回 0：新集合的第一天这个值本来就是 0，
    用 0 当「读不到」的哨兵会让「只收到期」在第一天完全不生效。
    """
    try:
        return int(col.sched.today)
    except Exception as exc:
        _log(f"读不到今天的日期，这次不按到期过滤：{exc}")
        return None


def _card_records(col: Any, card_ids: Iterable[Any]) -> list[dict[str, Any]]:
    """逐张取出「到期判定」要用的字段；卡已经不在了就跳过。

    Anki 的卡片对象带 type / queue / due / odid / odue，正好够判断
    「这张卡自己到期了没有」（见 rule_logic.card_is_due）。
    """
    out: list[dict[str, Any]] = []
    for value in card_ids:
        try:
            card_id = int(value)
        except Exception:
            continue
        try:
            card = col.get_card(card_id)
        except Exception:
            card = None
        if card is None:
            continue
        record = card_due_fields(card)
        record["id"] = card_id
        out.append(record)
    return out


def _exclude_names(value: Any) -> list[str]:
    """把「要排除的牌组名」参数统一成字符串列表（支持单个名字或一串名字）。"""
    if not value:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _own_deck_names(col: Any, value: Any) -> list[str]:
    """哪些牌组名算「这条规则自己的牌组」（要排除，不算「已在别的筛选牌组里」）。

    除了筛选牌组本身，还包括它的临时集中刷牌组（集中刷期间卡都停在那里）。
    """
    names: list[str] = []
    for name in _exclude_names(value):
        names.append(name)
        temp = cram_temp_name(name)
        names.append(temp)
        prefix = temp + " ("
        for other in _deck_names(col, include_filtered=True):
            if other.startswith(prefix):
                names.append(other)
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _other_filtered_ids(col: Any, scope_query: str, exclude_deck_name: Any) -> set[int]:
    """这一批卡里，已经被**别的**筛选牌组占着的卡号。"""
    query = and_query(scope_query, "deck:filtered")
    for name in _own_deck_names(col, exclude_deck_name):
        query = and_query(query, negate_term(deck_search_term(name)))
    return set(_find_cards(col, query))


def collect_counts(
    col: Any,
    tags: Iterable[Any],
    mode: str,
    states: Any,
    include_not_due: bool = bool(DEFAULT_INCLUDE_NOT_DUE),
    exclude_deck_name: Any = None,
) -> dict[str, Any]:
    """统计命中、真正能收进来的、以及收不进来的张数。

    「states」里的三项互不重复，相加正好等于命中总数；见 rule_logic 里
    state_count_query 的说明。

    「收不进来」包含三种：按**卡片自己的到期时间**还没到期的、暂停/搁置的
    （Anki 重建筛选牌组时一定会排除，插件也不去改动它们）、以及已经在别的
    筛选牌组里的。三种原因的卡号会存进 blocked_ids，「看收不进来的卡」直接用
    它打开浏览器，保证和这里的张数一模一样。

    include_not_due=True 表示「连还没到期的也收」，这时前一种原因不会拦卡。
    """
    counts = empty_counts()
    cleaned = clean_tags(tags)
    if not cleaned:
        return counts

    chosen = normalize_states(states)
    counts["matched"] = _count(col, tag_query(cleaned, mode))
    counts["per_tag"] = {tag: _count(col, tag_search_term(tag)) for tag in cleaned}
    counts["states"] = {
        value: _count(col, state_count_query(cleaned, mode, value))
        for value, _label in STATE_CHOICES
    }

    wanted = wanted_query(cleaned, mode, chosen)
    wanted_ids = _find_cards(col, wanted)
    counts["wanted"] = len(wanted_ids)
    if not wanted_ids:
        return counts

    today = _today(col)
    # 读不到今天就不按到期过滤（宁可多收，也不要凭空把卡挡住）；
    # today=0 是「新集合的第一天」，仍然要正常按到期过滤。
    # 注意语义方向：due_filter_enabled 第一个参数是 include_not_due，
    # 返回「这次到底要不要按到期拦卡」。
    effective_due_filter = due_filter_enabled(include_not_due, today)
    split = classify_cards(
        _card_records(col, wanted_ids),
        include_not_due=not effective_due_filter,
        states=chosen,
        today=today if today is not None else 0,
        other_filtered_ids=_other_filtered_ids(col, wanted, exclude_deck_name),
    )
    counts["collectible"] = len(split["collectible"])
    counts["due_blocked"] = len(split["due"])
    counts["banned"] = len(split["banned"])
    counts["other_filtered"] = len(split["other"])
    blocked_ids = split["due"] + split["banned"] + split["other"]
    counts["blocked"] = len(blocked_ids)
    counts["blocked_total"] = len(blocked_ids)
    counts["blocked_ids"] = blocked_ids[:MAX_BROWSE_CARDS]
    return counts


# --------------------------------------------------------------------------
# 建牌组 / 重建 / 删除
# --------------------------------------------------------------------------


def _prepare_deck(
    deck: FilteredDeckForUpdate,
    rule: dict,
    *,
    reschedule: bool = True,
    preview_delays: Sequence[int] | None = None,
) -> None:
    """把规则写进 Anki 的筛选牌组结构。

    日常模式：reschedule=True，作答按算法写回原卡（固定开启）。
    集中刷「不影响排期」：reschedule=False，走的是一套独立的预览延迟
    （preview_again_secs / preview_hard_secs / preview_good_secs），
    0 表示「这张卡结束集中刷、带着原来的到期时间回原牌组」。
    """
    deck.name = str(rule["name"])
    deck.allow_empty = True  # 空交集也允许保存，否则 Anki 会直接报错回滚
    config = deck.config
    config.reschedule = bool(reschedule)
    if preview_delays is None:
        # 回到日常模式时把预览延迟复位成 Anki 默认值，免得之后用 Anki 内置窗口
        # 看到一堆上一次集中刷留下的数字。
        config.preview_again_secs = 60
        config.preview_hard_secs = 600
        config.preview_good_secs = 0
    else:
        again, hard, good = (list(preview_delays) + [0, 0, 0])[:3]
        config.preview_again_secs = max(0, int(again))
        config.preview_hard_secs = max(0, int(hard))
        config.preview_good_secs = max(0, int(good))
    del config.search_terms[:]
    config.search_terms.append(
        FilteredDeckConfig.SearchTerm(
            search=str(rule["search"]),
            limit=int(rule["limit"]),
            order=int(rule["order"]),
        )
    )


def _error_handler(
    parent: Any, on_error: Callable[[Exception], None] | None
) -> Callable[[Exception], None]:
    def handle(exc: Exception) -> None:
        if on_error is not None:
            on_error(exc)
            return
        _log(f"操作失败：{exc}")
        showWarning(f"操作失败：{exc}", parent=parent)

    return handle


def _op_changes(*values: Any) -> Any:
    """后台操作（CollectionOp）必须交出一个能当 OpChanges 用的返回值。

    Anki 26.09 的 ``on_op_finished()`` 会直接读 ``result.changes``：操作成功了但
    返回的是 ``list`` / ``tuple`` / ``int`` 时，它会抛
    ``AttributeError: 'list' object has no attribute 'changes'``，然后在界面上弹
    「遇到了问题…可能是由某个插件引起」，而且 Anki 也收不到「内容变了」的通知。

    这里统一挑一个原生结果出来（都挑不到时返回全 False 的 ``OpChanges()``）；
    真正的业务数据由调用方自己放进闭包容器里拿。
    """
    picked = first_op_changes(*values)
    if picked is not None:
        return picked
    return OpChanges()


def _write_filtered_deck(
    col: Any, deck: FilteredDeckForUpdate
) -> tuple[int, Any]:
    """写入筛选牌组，并立刻做一次重建。

    Anki 在重建时会把搜索式重新序列化成它自己的规范写法（例如去掉多余引号、
    把 `AND` 写成空格）。这里主动重建一次，让「规则有没有被外部改过」的比较
    拿到的是稳定字符串，不会因为一次重建就被误判成外部修改。

    返回 ``(牌组 id, Anki 原生结果)``：牌组 id 是要用的数据，原生结果留给
    后台操作当返回值（见 ``_op_changes``）。
    """
    out = col.sched.add_or_update_filtered_deck(deck)
    deck_id = int(out.id)
    rebuilt: Any = None
    try:
        rebuilt = col.sched.rebuild_filtered_deck(DeckId(deck_id))
    except Exception as exc:  # 建组本身已经成功，这里失败不影响结果
        _log(f"写入后重建失败（下次进入牌组会再试）：{exc}")
    _log(f"已写入筛选牌组：{getattr(deck, 'name', '')}（牌组 {deck_id}）")
    return deck_id, _op_changes(out, rebuilt)


def _store_actual(
    deck_id: int,
    rule: dict,
    parent: Any,
    on_done: Callable[[int], None] | None,
) -> None:
    """建好/更新完之后，把 Anki 实际写入的设置记进规则里。"""

    def done(actual: dict | None) -> None:
        stored = dict(rule)
        stored["deck_id"] = deck_id
        stored.pop("externally_modified", None)
        stored["updated_at"] = int(time.time())
        if actual:
            stored["name"] = actual["name"]
            stored["actual_search"] = actual["search"]
            stored["actual_limit"] = actual["limit"]
            stored["actual_order"] = actual["order"]
            stored["actual_reschedule"] = actual["reschedule"]
            parent_name, leaf = split_deck_name(actual["name"])
            stored["parent"] = parent_name
            stored["leaf"] = leaf
        put_rule(stored)
        if on_done is not None:
            on_done(deck_id)

    QueryOp(
        parent=parent, op=lambda col: read_actual(col, deck_id), success=done
    ).run_in_background()


def apply_rule(
    rule: dict,
    *,
    parent: Any,
    on_done: Callable[[int], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """按规则新建（deck_id=0）或更新筛选牌组，并记录 Anki 实际写入的设置。"""
    if _col() is None:
        return
    deck_id = int(rule.get("deck_id") or 0)
    failed = _error_handler(parent, on_error)
    working = dict(rule)

    def fetch(col: Any) -> FilteredDeckForUpdate:
        deck = col.sched.get_or_create_filtered_deck(DeckId(deck_id))
        _prepare_deck(deck, working)
        return deck

    def fetched(deck: FilteredDeckForUpdate) -> None:
        box: dict[str, Any] = {}

        def write(collection: Any) -> Any:
            written_id, changes = _write_filtered_deck(collection, deck)
            box["deck_id"] = written_id
            return changes

        def written(_changes: Any) -> None:
            _store_actual(int(box.get("deck_id") or 0), working, parent, on_done)

        CollectionOp(parent, write).success(written).failure(failed).run_in_background()

    QueryOp(parent=parent, op=fetch, success=fetched).failure(failed).run_in_background()


def rebuild_deck(
    deck_id: int,
    *,
    parent: Any,
    on_done: Callable[[], None] | None = None,
    quiet: bool = False,
) -> None:
    def op(collection: Any) -> Any:
        return collection.sched.rebuild_filtered_deck(DeckId(int(deck_id)))

    def success(out: Any) -> None:
        if not quiet:
            tooltip(f"已重新收卡：{getattr(out, 'count', 0)} 张", parent=parent)
        if on_done is not None:
            on_done()

    CollectionOp(parent, op).success(success).failure(
        _error_handler(parent, None)
    ).run_in_background()


def rebuild_with_check(
    deck_id: int,
    *,
    parent: Any,
    on_done: Callable[[], None] | None = None,
) -> None:
    col = _col()
    if col is None:
        return
    if not _deck_exists(col, deck_id):
        drop_rule(deck_id)
        showInfo("这个牌组已经不存在了，规则已被清理。", parent=parent)
        if on_done is not None:
            on_done()
        return
    if not _is_filtered(col, deck_id):
        showInfo(
            "这个牌组现在不是筛选牌组，没法按筛选规则重新收卡。",
            parent=parent,
        )
        return
    rebuild_deck(deck_id, parent=parent, on_done=on_done)


def delete_filtered_deck(
    deck_id: int,
    *,
    parent: Any,
    on_done: Callable[[], None] | None = None,
    extra_decks: Iterable[Any] | None = None,
) -> None:
    """只删除筛选：Anki 会把卡送回原牌组，卡片与复习记录都不动。

    extra_decks 是这条规则的临时集中刷牌组：一并删掉，免得留下一个孤儿牌组。
    """

    def op(col: Any) -> Any:
        targets = [DeckId(int(deck_id))]
        for value in extra_decks or ():
            try:
                other = int(value)
            except Exception:
                continue
            if other and other != int(deck_id) and _is_filtered(col, other):
                targets.append(DeckId(other))
        return col.decks.remove(targets)

    def success(_out: Any) -> None:
        drop_cram_session(deck_id)
        drop_rule(deck_id)
        if on_done is not None:
            on_done()

    CollectionOp(parent, op).success(success).failure(
        _error_handler(parent, None)
    ).run_in_background()


def confirm_and_delete(
    deck_id: int,
    *,
    parent: Any,
    on_done: Callable[[], None] | None = None,
) -> None:
    col = _col()
    if col is None:
        return
    if not _deck_exists(col, deck_id):
        drop_rule(deck_id)
        if on_done is not None:
            on_done()
        showInfo("这个牌组已经不存在了，规则已被清理。", parent=parent)
        return
    if not _is_filtered(col, deck_id):
        showWarning(
            "这不是筛选牌组。为安全起见，插件不会删除普通牌组"
            "（删普通牌组会连同卡片一起删掉）。",
            parent=parent,
        )
        return
    children = _child_decks(col, deck_id)
    if children:
        names = "、".join(name for name, _did in children[:5])
        showWarning(
            f"这个筛选牌组下面还挂着子牌组（{names}）。\n"
            "删除它会连子牌组一起删掉，子牌组里的卡片会被真正删除。\n"
            "请先把子牌组移走，或者改用 Anki 自己的删除按钮逐个确认。",
            parent=parent,
        )
        return

    name = _deck_name(col, deck_id) or f"牌组 {deck_id}"
    count = _cards_in_deck(col, deck_id)
    # 顺带把这条规则的临时集中刷牌组一起收掉（集中刷会让原牌组暂时空着）
    temp_ids = _cram_temp_deck_ids(
        col, deck_id, cram_session_deck_id(get_cram_session(deck_id, col))
    )
    question = (
        f"要删除筛选牌组「{name}」吗？\n\n"
        f"只删除这次筛选：里面的 {count} 张卡会立刻回到各自原来的牌组，\n"
        "卡片本身、复习记录和排期都不会丢。"
    )
    if temp_ids:
        question += "\n\n（这条规则正在集中刷：那个临时集中刷牌组也会一起删掉。）"
    if not askUser(question, parent=parent):
        return
    close_cram_window(deck_id, "删除筛选时一并收掉集中刷")
    delete_filtered_deck(
        deck_id, parent=parent, on_done=on_done, extra_decks=temp_ids
    )


# --------------------------------------------------------------------------
# 集中刷：临时把还没到期的卡也收进来，规则本身不动
# --------------------------------------------------------------------------


def collect_cram_counts(
    col: Any,
    tags: Iterable[Any],
    mode: str,
    states: Any,
    exclude_deck_name: Any = None,
) -> dict[str, Any]:
    """集中刷之前预估：能收进来多少、仍然收不进来多少。

    集中刷只是把「是否到期」这一条临时去掉，Anki 硬拼的
    `-is:suspended -is:buried -deck:filtered` 依然生效，所以暂停/搁置的卡和
    已经在别的筛选牌组里的卡还是进不来（这里如实算给用户看）。

    「其中多少张是本来没到期、这次被临时拉进来的」也按卡片自己的到期时间逐张
    判定（不能用 is:due，筛选牌组里的 due 已经变成位置值了）。
    """
    counts = empty_counts()
    cleaned = clean_tags(tags)
    if not cleaned:
        return counts
    chosen = normalize_states(states)
    cram_scope = cram_search_query(cleaned, mode, chosen)
    if not cram_scope:
        return counts

    counts["matched"] = _count(col, tag_query(cleaned, mode))
    scope_ids = _find_cards(col, cram_scope)
    counts["wanted"] = len(scope_ids)
    if not scope_ids:
        return counts

    records = _card_records(col, scope_ids)
    today = _today(col)
    today_value = today if today is not None else 0
    other_ids = _other_filtered_ids(col, cram_scope, exclude_deck_name)
    split = classify_cards(
        records,
        include_not_due=True,
        states=chosen,
        today=today_value,
        other_filtered_ids=other_ids,
    )
    # 同一批卡再按「只收到期」分一次：差出来的就是这次被临时拉进来的没到期卡
    strict = classify_cards(
        records,
        include_not_due=False,
        states=chosen,
        today=today_value,
        other_filtered_ids=other_ids,
    )
    counts["collectible"] = len(split["collectible"])
    counts["cram_collectible"] = counts["collectible"]
    counts["banned"] = len(split["banned"])
    counts["other_filtered"] = len(split["other"])
    counts["blocked"] = counts["banned"] + counts["other_filtered"]
    counts["not_due"] = len(strict["due"])
    counts["cram_not_due"] = counts["not_due"]
    counts["due_blocked"] = counts["not_due"]
    counts["blocked_ids"] = (split["banned"] + split["other"])[:MAX_BROWSE_CARDS]
    counts["blocked_total"] = counts["blocked"]
    return counts


def _filtered_deck_ids(col: Any) -> set[int]:
    """当前集合里所有筛选牌组的 ID（挑来源牌组时要跳过它们）。"""
    out: set[int] = set()
    try:
        items = col.decks.all_names_and_ids(include_filtered=True)
    except Exception:
        return out
    for item in items:
        try:
            deck_id = int(item.id)
        except Exception:
            continue
        if _is_filtered(col, deck_id):
            out.add(deck_id)
    return out


def _cram_step_source(
    col: Any, records: Any = None
) -> tuple[list[float], str, dict[str, Any]]:
    """集中刷「不影响排期」要用的新卡学习步骤（秒）与它来自哪个牌组。

    筛选牌组自己的配置里没有「新卡学习步骤」这一项，所以取：

    1. **卡片自己的原牌组**（1.5.0 起）——从这批卡里挑张数最多的那个普通牌组，
       这样「筛选牌组的设置跟随原牌组」这一条在集中刷里也成立；
    2. 拿不到就用默认牌组（Anki 的 1 号牌组，出厂是 1 分钟 / 10 分钟）。
    """
    picked = pick_home_deck(records, filtered_ids=_filtered_deck_ids(col))
    chosen = int(picked.get("deck_id") or 0)
    for did in (chosen, 1):
        if not did:
            continue
        try:
            config = col.decks.config_dict_for_deck_id(DeckId(int(did)))
        except Exception:
            continue
        steps = new_card_steps_from_config(config)
        if steps:
            picked["deck_id"] = int(did)
            return steps, _deck_name(col, did), picked
    return list(DEFAULT_NEW_STEP_SECS), "", picked


def _human_secs(secs: Any) -> str:
    """把秒数说成人话，用在集中刷对话框里。"""
    try:
        total = int(secs)
    except Exception:
        total = 0
    if total <= 0:
        return "0（刷完送回原牌组）"
    if total % 60 == 0:
        return f"{total // 60} 分钟"
    if total < 60:
        return f"{total} 秒"
    return f"{total / 60:.1f} 分钟"


def cram_dialog_body(
    name: str,
    counts: dict[str, Any],
    delays: Sequence[int],
    step_source: str = "",
    source_info: Mapping[str, Any] | None = None,
) -> str:
    """集中刷三选一对话框的正文（抽成纯函数，方便核对文案）。"""
    again, hard, _good = (list(delays) + [0, 0, 0])[:3]
    collectible = int(counts.get("collectible") or 0)
    banned = int(counts.get("banned") or 0)
    other = int(counts.get("other_filtered") or 0)
    blocked = banned + other
    info = source_info or {}
    other_decks = int(info.get("other_decks") or 0)
    other_count = int(info.get("other_count") or 0)

    lines = [
        f"要用「{name}」建一个临时集中刷牌组，突击刷一轮吗？"
        f"预计能收进来 {collectible} 张（临时忽略「是否到期」，标签、并集/交集、"
        "状态打勾都照旧，规则和原筛选牌组的设置都不动）。"
    ]
    if blocked:
        parts: list[str] = []
        if banned:
            parts.append(f"暂停或搁置的 {banned} 张")
        if other:
            parts.append(f"已在别的筛选牌组里的 {other} 张")
        lines.append(
            f"另有 {blocked} 张仍然收不进来（{'、'.join(parts)}）——Anki 本身的限制。"
        )
    extra = ""
    if step_source and other_decks and other_count:
        extra = f"，另有 {other_count} 张来自别的原牌组"
    delay_source = f"　延迟来源：{step_source}{extra} → " if step_source else "　"
    lines += [
        "【不影响排期（推荐）】按 Anki 预览模式刷，原卡的间隔、因子、次数、到期时间"
        "都不变（复习历史里多一条预览记录）；三档延迟按卡片原牌组的新卡学习步骤换算。",
        f"{delay_source}重来 {_human_secs(again)}、困难 {_human_secs(hard)} 后回来接着刷，"
        "良好/简单＝刷完带着原到期时间回原牌组。",
        "【影响排期】收录范围一样放宽，作答按算法写回原卡排期。",
        "关掉小窗＝结束这一轮：临时牌组删掉、卡片回原牌组、原筛选牌组按规则重收；"
        "想再突击一遍点小窗里的「再刷一轮」，关掉 Anki 重开也会自动收尾。",
    ]
    return "\n".join(lines)


def ask_cram_mode(
    parent: Any,
    name: str,
    counts: dict[str, Any],
    delays: Sequence[int],
    step_source: str = "",
    source_info: Mapping[str, Any] | None = None,
) -> bool | None:
    """弹三选一：不影响排期 / 影响排期 / 取消。

    返回 True＝影响排期，False＝不影响排期，None＝取消。
    """
    box = QMessageBox(parent or mw)
    box.setWindowTitle(ADDON_TITLE)
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(cram_dialog_body(name, counts, delays, step_source, source_info))
    preview_button = box.addButton(
        "不影响排期（推荐）", QMessageBox.ButtonRole.AcceptRole
    )
    writeback_button = box.addButton(
        "影响排期（写回原卡）", QMessageBox.ButtonRole.DestructiveRole
    )
    cancel_button = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(preview_button)
    box.setEscapeButton(cancel_button)
    box.exec()
    clicked = box.clickedButton()
    if clicked is preview_button:
        return False
    if clicked is writeback_button:
        return True
    return None


CRAM_WINDOW_TITLE = "集中刷"


class CramWindow(QDialog):
    """集中刷小窗：**关掉这个窗口就等于结束这一轮集中刷**，牌组恢复原样。

    集中刷用的是独立的临时牌组，原筛选牌组的设置一个字都不动。这里不去搬 Anki
    的复习界面（那样风险太大），只在窗口里说明状态：「去刷题」把主窗口切到临时
    牌组直接开始复习，「再刷一轮」重建临时牌组再刷一遍。
    """

    def __init__(
        self,
        qt_parent: Any,
        deck_id: int,
        *,
        deck_name: str,
        session: dict,
        cram_deck_id: Any = 0,
        cram_name: str = "",
        collected: Any = None,
        not_due: Any = None,
    ) -> None:
        # qt_parent=None 表示「独立窗口」：这样主窗口才能盖在它前面（集中刷时
        # 小窗退到后面，不挡答题）。窗口引用一直存在 _CRAM_WINDOWS 里，不会被回收。
        super().__init__(qt_parent)
        self.deck_id = int(deck_id)
        self.deck_name = str(deck_name or "")
        self._finishing = False
        self.writeback = cram_writes_back(session)
        self.cram_deck_id = int(cram_deck_id or 0)
        self.cram_name = str(cram_name or "")
        self.setWindowTitle(f"{CRAM_WINDOW_TITLE} · {deck_name}")
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        mode_text = (
            "模式：影响排期 —— 作答会按算法写回原卡排期"
            if self.writeback
            else "模式：不影响排期 —— 原卡的到期时间、间隔、因子、次数都不会变"
        )
        self.mode_label = QLabel(mode_text)
        self.mode_label.setWordWrap(True)
        layout.addWidget(self.mode_label)

        self.count_label = QLabel("")
        self.count_label.setWordWrap(True)
        layout.addWidget(self.count_label)

        started = int((session or {}).get("started_at") or 0)
        when = ""
        if started:
            try:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(started))
            except Exception:
                when = ""
        self.time_label = QLabel(
            f"原筛选牌组：{deck_name}\n"
            f"本次临时牌组：{self.cram_name or '（正在建立）'}\n"
            f"轮次：第 {cram_round(session)} 轮　开始时间：{when or '刚刚'}"
        )
        self.time_label.setWordWrap(True)
        layout.addWidget(self.time_label)

        hint = QLabel(
            "集中刷用的是单独的临时牌组，原筛选牌组的设置不会被改动。\n"
            "关掉这个窗口（右上角的 × 或 Esc）＝结束这次集中刷：临时牌组会被删掉、"
            "卡片回原牌组，原筛选牌组按规则重新收卡。\n"
            "想再刷一遍没到期的卡，点「再刷一轮」（等于重新点一次集中刷）。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        self.go_button = QPushButton("去刷题")
        self.again_button = QPushButton("再刷一轮")
        self.end_button = QPushButton("结束集中刷（恢复正常筛选牌组）")
        buttons.addWidget(self.go_button)
        buttons.addWidget(self.again_button)
        buttons.addStretch(1)
        buttons.addWidget(self.end_button)
        layout.addLayout(buttons)

        qconnect(self.go_button.clicked, self._on_go)
        qconnect(self.again_button.clicked, self._on_again)
        qconnect(self.end_button.clicked, lambda: end_cram(self.deck_id, parent=self))

        self.set_counts(collected, not_due)

    def _current_cram_deck(self) -> int:
        """现在这个牌组对应的临时集中刷牌组 id（会话里记着）。"""
        session = get_cram_session(self.deck_id)
        return cram_session_deck_id(session) or self.cram_deck_id

    def _on_go(self) -> None:
        target = self._current_cram_deck() or self.deck_id
        _enter_deck(target, parent=self, study=True)

    def _on_again(self) -> None:
        restart_cram(self.deck_id, parent=self, on_done=self._after_again)

    def _after_again(self) -> None:
        session = get_cram_session(self.deck_id)
        self.cram_deck_id = cram_session_deck_id(session)
        mode_text = (
            "模式：影响排期 —— 作答会按算法写回原卡排期"
            if self.writeback
            else "模式：不影响排期 —— 原卡的到期时间、间隔、因子、次数都不会变"
        )
        try:
            self.mode_label.setText(mode_text)
            self.set_counts(None, None)
            self._refresh_time_label(session)
        except Exception as exc:
            _log(f"刷新集中刷窗口失败：{exc}")

    def _refresh_time_label(self, session: dict | None) -> None:
        started = int((session or {}).get("started_at") or 0)
        when = ""
        if started:
            try:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(started))
            except Exception:
                when = ""
        self.time_label.setText(
            f"原筛选牌组：{self.deck_name}\n"
            f"本次临时牌组：{self.cram_name or '（正在建立）'}\n"
            f"轮次：第 {cram_round(session)} 轮　开始时间：{when or '刚刚'}"
        )

    def set_counts(self, collected: Any, not_due: Any = None) -> None:
        try:
            count = int(collected or 0)
        except Exception:
            count = 0
        text = f"本次收进 {count} 张（已经把没到期的卡一起收进来了）"
        try:
            extra = None if not_due is None else int(not_due)
        except Exception:
            extra = None
        if extra:
            text += f"，其中按卡片自己的到期日算「本来还没到期」的 {extra} 张"
        text += "。"
        try:
            self.count_label.setText(text)
        except Exception:
            pass

    def finish(self, reason: str = "") -> None:
        """只负责把窗口收掉；结束集中刷本身由调用方做，免得重复执行。

        （点右上角 × 或 Esc 走的是 closeEvent，那条路会自己触发结束。）
        """
        self._finishing = True
        if reason:
            _log(f"关闭集中刷窗口：{self.deck_name}（{reason}）")
        _CRAM_WINDOWS.pop(self.deck_id, None)
        try:
            self.close()
        except Exception as exc:
            _log(f"关闭集中刷窗口失败：{exc}")

    def closeEvent(self, event: Any) -> None:  # noqa: N802（Qt 的命名）
        """点 × / 按 Esc 也当作「结束集中刷」。"""
        if self._finishing:
            event.accept()
            return
        self._finishing = True
        event.accept()
        _CRAM_WINDOWS.pop(self.deck_id, None)
        _log(f"关闭集中刷窗口：{self.deck_name}（关窗即结束）")
        try:
            end_cram(self.deck_id, parent=mw, close_window=False)
        except Exception as exc:  # 关窗不能反过来把 Anki 弄崩
            _log(f"关窗结束集中刷失败：{exc}")
            _log(traceback.format_exc())


def find_cram_window(deck_id: int) -> Any:
    return _CRAM_WINDOWS.get(int(deck_id))


def open_cram_window(
    deck_id: int,
    *,
    parent: Any = None,
    info: dict[str, Any] | None = None,
    behind: bool = False,
) -> Any:
    """打开（或调出）集中刷小窗；这个牌组没在集中刷就返回 None。

    behind=True 表示「刚点完集中刷、马上要进答题界面」：小窗做成独立窗口，
    只显示不抢焦点，主窗口盖在它前面；用户随时可以从管理窗口或牌组右键
    把它调回前面（那时走 behind=False 这条路，会主动置顶）。
    """
    col = _col()
    if col is None:
        return None
    deck_id = int(deck_id)
    session = get_cram_session(deck_id, col)
    if session is None:
        return None

    existing = _CRAM_WINDOWS.get(deck_id)
    if existing is not None:
        if behind:
            # 已经开着了：保持现状（别把刚进答题的主窗口又盖回去）
            return existing
        try:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return existing
        except Exception:
            _CRAM_WINDOWS.pop(deck_id, None)

    name = _deck_name(col, deck_id) or f"牌组 {deck_id}"
    cram_deck_id = cram_session_deck_id(session)
    cram_name = _deck_name(col, cram_deck_id) if cram_deck_id else ""
    if not cram_name:
        cram_name = cram_temp_name(name)
    window = CramWindow(
        None if behind else (parent or mw),
        deck_id,
        deck_name=name,
        session=session,
        cram_deck_id=cram_deck_id,
        cram_name=cram_name,
        collected=(info or {}).get("collected"),
        not_due=(info or {}).get("not_due"),
    )
    _CRAM_WINDOWS[deck_id] = window
    _show_dialog(window, activate=not behind)
    _refresh_cram_counts(window, deck_id, info)
    return window


def _refresh_cram_counts(
    window: Any, deck_id: int, info: dict[str, Any] | None = None
) -> None:
    """补上「本次收进多少张、其中多少是没到期的」；后台算，不卡界面。"""
    col = _col()
    if col is None:
        return
    if info and info.get("collected") is not None:
        window.set_counts(info.get("collected"), info.get("not_due"))
        return
    rule = get_rule(deck_id, col)
    if not rule or not rule.get("tags"):
        return
    deck_name = _deck_name(col, deck_id)
    session = get_cram_session(deck_id, col)
    temp_id = cram_session_deck_id(session)
    try:
        collected = _cards_in_deck(col, temp_id) if temp_id else 0
    except Exception:
        collected = 0

    def op(collection: Any) -> dict[str, Any]:
        return collect_cram_counts(
            collection,
            rule.get("tags"),
            rule.get("mode"),
            rule.get("states"),
            deck_name,
        )

    def success(counts: dict[str, Any]) -> None:
        try:
            window.set_counts(
                collected, int((counts or {}).get("cram_not_due") or 0)
            )
        except Exception:
            pass

    QueryOp(parent=window, op=op, success=success).failure(
        _error_handler(window, None)
    ).run_in_background()


def close_cram_window(deck_id: int, reason: str = "") -> None:
    """只关窗，不动牌组（结束流程里调用）。"""
    window = _CRAM_WINDOWS.pop(int(deck_id), None)
    if window is None:
        return
    try:
        window.finish(reason)
    except Exception as exc:
        _log(f"关闭集中刷窗口失败：{exc}")


def cram_active(deck_id: int, col: Any = None) -> bool:
    return get_cram_session(deck_id, col) is not None


def start_cram(
    deck_id: int,
    writeback: bool,
    *,
    parent: Any,
    on_done: Callable[[], None] | None = None,
    info: dict[str, Any] | None = None,
    study: bool = True,
    round_no: int = 1,
) -> None:
    """开始一轮集中刷。

    1.4.0 起集中刷用**独立的临时牌组**：

    1. 先清空原筛选牌组 —— 卡片回原牌组、恢复原始到期时间（原牌组的设置一个字
       都不动，只是暂时空着）；
    2. 另建一个临时筛选牌组「<原牌组名>·集中刷」，收录条件只把「是否到期」那条
       临时去掉；
    3. 打开集中刷小窗，并把主界面切到临时牌组直接开刷。

    关掉小窗就等于结束这一轮：删掉临时牌组、按原规则重建原筛选牌组。
    """
    col = _col()
    if col is None:
        return
    rule = get_rule(deck_id, col)
    if rule is None:
        showWarning(
            "这个牌组不在标签规则管理下，先用「编辑筛选规则…」建好规则再集中刷。",
            parent=parent,
        )
        return
    if not _deck_exists(col, deck_id) or not _is_filtered(col, deck_id):
        showWarning("这个牌组已经不在了，或者已经不是筛选牌组。", parent=parent)
        return
    name = _deck_name(col, deck_id) or str(rule.get("name") or "筛选牌组")
    working = dict(rule)
    working["search"] = cram_search_query(
        rule.get("tags"), rule.get("mode"), rule.get("states")
    )
    if not working["search"]:
        showWarning("这条规则还没有可用的搜索条件。", parent=parent)
        return

    # 三档延迟按卡片自己的原牌组换算（1.5.0）
    cram_ids = _find_cards(col, working["search"])
    steps, _source, _source_info = _cram_step_source(
        col, _card_records(col, cram_ids)
    )
    delays = cram_preview_delays(steps)
    failed = _error_handler(parent, None)
    temp_name = cram_temp_deck_name(name, _deck_names(col, include_filtered=True))
    box: dict[str, Any] = {}

    def op(collection: Any) -> Any:
        # 1. 清空原筛选牌组：卡片回原牌组、恢复原始到期时间
        try:
            collection.sched.empty_filtered_deck(DeckId(int(deck_id)))
        except Exception as exc:
            _log(f"清空原筛选牌组失败（继续建临时牌组）：{exc}")
        # 2. 建临时集中刷牌组
        working["name"] = temp_name
        deck = collection.sched.get_or_create_filtered_deck(DeckId(0))
        _prepare_deck(
            deck,
            working,
            reschedule=bool(writeback),
            preview_delays=None if writeback else delays,
        )
        out = collection.sched.add_or_update_filtered_deck(deck)
        temp_id = int(out.id)
        rebuilt: Any = None
        try:
            rebuilt = collection.sched.rebuild_filtered_deck(DeckId(temp_id))
        except Exception as exc:  # 牌组已经建好，这里失败不影响后续
            _log(f"写入后重建临时集中刷牌组失败：{exc}")
        box["temp_id"] = temp_id
        return _op_changes(out, rebuilt)

    def started(_changes: Any) -> None:
        temp_id = int(box.get("temp_id") or 0)
        actual_name = temp_name
        put_cram_session(
            deck_id,
            make_cram_session(writeback, int(time.time()), temp_id, round_no),
            col,
        )
        mode_text = "影响排期" if writeback else "不影响排期"
        _log(f"开始集中刷：{name}（{mode_text}，临时牌组「{actual_name}」）")
        open_cram_window(
            deck_id,
            parent=parent,
            info=info or {"collected": _cards_in_deck(col, temp_id)},
            behind=study,
        )
        if on_done is not None:
            on_done()
        if study:
            _enter_deck(temp_id, parent=parent or mw, study=True)

    CollectionOp(parent, op).success(started).failure(failed).run_in_background()


def restart_cram(
    deck_id: int,
    *,
    parent: Any,
    on_done: Callable[[], None] | None = None,
    quiet: bool = False,
) -> None:
    """「再刷一轮」：按同一条件把临时集中刷牌组重新收一遍，轮次 +1。

    不会碰原筛选牌组，也不会重新询问是否影响排期——还是这一次集中刷的模式。
    """
    col = _col()
    if col is None:
        return
    deck_id = int(deck_id)
    session = get_cram_session(deck_id, col)
    if session is None:
        if on_done is not None:
            on_done()
        return
    temp_id = cram_session_deck_id(session)
    if not temp_id or not _deck_exists(col, temp_id):
        # 临时牌组没了（被手动删掉之类）：直接按原流程收尾，别卡在中间状态
        end_cram(deck_id, parent=parent, on_done=on_done, quiet=quiet)
        return

    box: dict[str, int] = {}

    def op(collection: Any) -> Any:
        out = collection.sched.rebuild_filtered_deck(DeckId(temp_id))
        box["count"] = int(getattr(out, "count", 0))
        return _op_changes(out)

    def success(_changes: Any) -> None:
        count = int(box.get("count") or 0)
        updated = dict(session)
        updated["round"] = cram_round(session) + 1
        put_cram_session(deck_id, updated, col)
        message = (
            f"已开始第 {updated['round']} 轮集中刷：这次收了 {int(count)} 张"
        )
        _log(f"{message}（{_deck_name(col, deck_id)}）")
        if not quiet:
            tooltip(message)
        if on_done is not None:
            on_done()

    CollectionOp(parent, op).success(success).failure(
        _error_handler(parent, None)
    ).run_in_background()


def begin_cram(
    deck_id: int,
    *,
    parent: Any,
    on_done: Callable[[], None] | None = None,
    study: bool = True,
) -> None:
    """点「集中刷…」的入口：先算数量，再问要不要影响排期，然后开刷。"""
    col = _col()
    if col is None:
        return
    rule = get_rule(deck_id, col)
    if rule is None:
        showWarning(
            "这个牌组不在标签规则管理下，先用「编辑筛选规则…」建好规则再集中刷。",
            parent=parent,
        )
        return
    if not _deck_exists(col, deck_id) or not _is_filtered(col, deck_id):
        showWarning("这个牌组已经不在了，或者已经不是筛选牌组。", parent=parent)
        return
    if cram_active(deck_id, col):
        # 已经在集中刷：把那个小窗调出来，不重复建
        if open_cram_window(deck_id, parent=parent) is None:
            showInfo("这个牌组正在集中刷，但集中刷小窗没能打开。", parent=parent)
        return
    name = _deck_name(col, deck_id) or str(rule.get("name") or "筛选牌组")
    tags = rule.get("tags")
    mode = rule.get("mode")
    states = rule.get("states")

    def op(collection: Any) -> dict[str, Any]:
        counts = collect_cram_counts(collection, tags, mode, states, name)
        # 三档延迟按卡片自己的原牌组换算（1.5.0）
        scope = cram_search_query(tags, mode, states)
        steps, source, source_info = _cram_step_source(
            collection, _card_records(collection, _find_cards(collection, scope))
        )
        return {
            "counts": counts,
            "delays": list(cram_preview_delays(steps)),
            "source": source,
            "source_info": source_info,
        }

    def success(data: dict[str, Any]) -> None:
        counts = data.get("counts") or {}
        if int(counts.get("collectible") or 0) <= 0:
            banned = int(counts.get("banned") or 0)
            other = int(counts.get("other_filtered") or 0)
            detail = ""
            if banned or other:
                detail = (
                    f"\n\n其中暂停或搁置的 {banned} 张、已在别的筛选牌组里的 {other} 张，"
                    "Anki 本身就不让收进筛选牌组。"
                )
            showInfo(f"这条规则现在没有能收进来的卡。{detail}", parent=parent)
            return
        choice = ask_cram_mode(
            parent,
            name,
            counts,
            data.get("delays") or [],
            data.get("source") or "",
            data.get("source_info") or None,
        )
        if choice is None:
            return
        start_cram(
            deck_id,
            bool(choice),
            parent=parent,
            on_done=on_done,
            info={
                "collected": int(counts.get("collectible") or 0),
                "not_due": int(counts.get("not_due") or 0),
            },
            study=study,
        )

    QueryOp(parent=parent, op=op, success=success).failure(
        _error_handler(parent, None)
    ).run_in_background()


def end_cram(
    deck_id: int,
    *,
    parent: Any,
    on_done: Callable[[], None] | None = None,
    quiet: bool = False,
    close_window: bool = True,
) -> None:
    """结束这一轮集中刷。

    顺序固定成下面这样，一步都不能挪（1.5.1 修的就是这里的界面收尾）：

    1. 关掉集中刷小窗；
    2. 清掉集中刷会话（这样切换界面状态时不会再弹「要结束集中刷吗」）；
    3. 若主窗口正停在临时牌组的复习界面，先退回总览，并把**选中的牌组切回原
       筛选牌组**——否则后面删临时牌组时，界面还停在一个马上要被删掉的牌组上；
    4. 删掉临时集中刷牌组（卡片回原牌组、恢复原始到期时间）；
    5. 按原规则重建原筛选牌组；
    6. **最后显式重画界面**：牌组列表 / 总览页都当场对上真实状态。

    第 6 步不能省：Anki 只在窗口重新获得焦点时才按内部标记自己重画，后台删牌组
    时窗口一直是有焦点的，那个标记就挂着不消费，于是牌组列表里还留着已经删掉的
    临时牌组、原牌组还显示旧的张数——这正是「关掉集中刷后界面没恢复原样」。

    原筛选牌组的设置从头到尾没被改过。
    """
    deck_id = int(deck_id)
    if close_window:
        close_cram_window(deck_id, "结束集中刷")
    col = _col()
    if col is None:
        return
    session = get_cram_session(deck_id, col)
    temp_ids = _cram_temp_deck_ids(col, deck_id, cram_session_deck_id(session))
    # 先把会话清掉，这样切换界面状态时不会再弹「要结束集中刷吗」
    drop_cram_session(deck_id, col)
    # 还在等「总览页画完再进临时牌组答题」的话，一并取消
    for value in list(temp_ids) + [deck_id]:
        _cancel_study_wait(int(value), "集中刷已结束")
    # 主窗口可能正停在临时集中刷牌组里答题：先退回总览，选中切回原筛选牌组
    _return_to_origin_deck(col, deck_id, temp_ids)
    rule = get_rule(deck_id, col)
    name = _deck_name(col, deck_id) or f"牌组 {deck_id}"

    box: dict[str, int] = {}

    def op(collection: Any) -> Any:
        removed, changes = _remove_filtered_decks(collection, temp_ids)
        box["removed"] = removed
        return changes

    def cleaned(_changes: Any) -> None:
        removed = int(box.get("removed") or 0)
        _log(
            f"结束集中刷：处理了 {removed} 个临时牌组"
            f"（挂了 {len(temp_ids)} 个，{name}）"
        )
        if (
            rule is None
            or not _deck_exists(col, deck_id)
            or not _is_filtered(col, deck_id)
        ):
            _redraw_after_cram(deck_id, temp_ids)
            if on_done is not None:
                on_done()
            return

        def done(_deck_id: int) -> None:
            _log(f"已结束集中刷并按原规则重收：{name}")
            _redraw_after_cram(deck_id, temp_ids)
            if not quiet:
                tooltip(f"已结束集中刷：{name} 已按原规则重新收卡")
            if on_done is not None:
                on_done()

        apply_rule(rule, parent=parent, on_done=done)

    CollectionOp(parent, op).success(cleaned).failure(
        _error_handler(parent, None)
    ).run_in_background()


def _return_to_origin_deck(col: Any, deck_id: int, temp_ids: Any = ()) -> None:
    """结束集中刷前把界面收干净。

    只会在「当前选中的就是这条规则的牌组」时动手：若正停在临时集中刷牌组的
    复习界面，先退回总览（免得答到一半被重新收卡）；然后把选中的牌组切回原
    筛选牌组，这样后面删掉临时牌组时界面绝不会停在一个已经消失的牌组上。
    用户在别的牌组里的话就不打扰他，只把牌组删掉、原牌组重收。
    """
    try:
        selected = int(col.decks.selected())
    except Exception:
        selected = 0
    watched = {int(deck_id)}
    for value in temp_ids or ():
        try:
            number = int(value)
        except Exception:
            continue
        if number:
            watched.add(number)
    if selected not in watched:
        return
    if _state_name(getattr(mw, "state", "")) == "review":
        try:
            mw.moveToState("overview")
        except Exception as exc:
            _log(f"从集中刷的复习界面退回总览失败：{exc}")
    try:
        col.decks.select(DeckId(int(deck_id)))
    except Exception as exc:
        _log(f"把选中的牌组切回原筛选牌组失败：{exc}")


def _redraw_after_cram(deck_id: int, temp_ids: Any = ()) -> None:
    """结束集中刷后显式重画**当前那一页**，并写一行日志便于对时间线。

    只重画主窗口现在正显示的那一页：

    * 牌组列表 → deckBrowser.refresh()
    * 总览页   → overview.refresh()

    两个视图共用同一个 webview，所以**只能画当前这一个**，画错了会把页面冲掉。

    1.5.2 修的是这里的判定：以前写成 ``state == "deckBrowser"``，而状态名早就统一
    转成小写了（``"deckbrowser"``），两边永不相等 → 在牌组列表上结束集中刷时
    从来不重画，列表里还留着已经删掉的临时牌组。现在统一走纯函数
    ``redraw_target()``，大小写两种写法都认。
    """
    col = _col()
    state = _state_name(getattr(mw, "state", ""))
    name = (_deck_name(col, deck_id) if col is not None else "") or f"牌组 {deck_id}"
    count = _cards_in_deck(col, deck_id) if col is not None else 0
    target = redraw_target(getattr(mw, "state", ""))
    drawn = "无"
    try:
        if target == REDRAW_DECK_BROWSER:
            mw.deckBrowser.refresh()
            drawn = "牌组列表"
        elif target == REDRAW_OVERVIEW:
            mw.overview.refresh()
            drawn = "总览页"
    except Exception as exc:
        _log(f"结束集中刷后重画界面失败：{exc}")
    if drawn == "无" and target == REDRAW_NONE:
        _log(
            f"结束集中刷后界面不收尾：当前页面「{state or '未知'}」不需要重画"
            f"（牌组 {deck_id}）"
        )
    _log(
        f"结束集中刷后重画界面：{name}（现在 {count} 张，"
        f"当前页面 {state or '未知'}，画了 {drawn}）"
    )


def _start_study() -> bool:
    """让主窗口在当前牌组直接开始复习；拿不到接口就退化成只停在总览页。"""
    on_study = getattr(mw, "onStudyKey", None)
    if callable(on_study):
        try:
            on_study()
            return True
        except Exception as exc:
            _log(f"onStudyKey 打不开复习界面：{exc}")
    try:
        mw.moveToState("review")
        return True
    except Exception as exc:
        _log(f"moveToState('review') 打不开复习界面：{exc}")
    return False


def _main_window_front() -> None:
    """把主窗口提到最前：集中刷的小窗退到它后面，不挡答题。"""
    for name in ("show", "raise_", "activateWindow"):
        fn = getattr(mw, name, None)
        if not callable(fn):
            continue
        try:
            fn()
        except Exception as exc:
            _log(f"主窗口 {name}() 失败：{exc}")


def _already_studying(deck_id: int, col: Any = None) -> bool:
    """主窗口现在是不是正停在这个牌组的答题界面里。"""
    if _state_name(getattr(mw, "state", "")) != "review":
        return False
    collection = col if col is not None else _col()
    if collection is None:
        return False
    try:
        return int(collection.decks.selected()) == int(deck_id)
    except Exception:
        return False


# 正在等「总览页画完再进答题」的牌组：deck_id -> {"token":…, "tries":…, "timer":…}
_STUDY_PENDING: dict[int, dict[str, Any]] = {}
STUDY_WAIT_TIMEOUT_MS = 1500
STUDY_MAX_TRIES = 3


def _cancel_study_wait(deck_id: Any, reason: str = "") -> None:
    """不再等这个牌组的总览页了（比如集中刷已经结束）。"""
    try:
        key = int(deck_id)
    except Exception:
        return
    entry = _STUDY_PENDING.pop(key, None)
    if entry is None:
        return
    timer = entry.get("timer")
    if timer is not None:
        try:
            timer.stop()
        except Exception:
            pass
    if reason:
        _log(f"取消「等总览页再进答题」：牌组 {key}（{reason}）")


def _enter_study_when_ready(deck_id: int, *, tries: int = 0) -> None:
    """切到总览页之后，**等它真的画完**再进答题界面。

    不能「切总览 → 立刻 onStudyKey」：Anki 的 Overview.refresh() 是异步的，
    画完那一刻会往**同一个 webview** 重写一遍总览页，正好盖掉刚打开的答题
    界面 —— 表现就是「第一次点集中刷没反应，回一趟牌组列表再点才行」。
    这里等一次 overview_did_refresh 钩子（另有超时重试兜底）再进答题；
    进不去就退化成停在总览页，并写一行日志，不会「什么都没发生」。
    """
    deck_id = int(deck_id)
    col = _col()
    if col is None:
        return
    try:
        selected = int(col.decks.selected())
    except Exception:
        selected = 0
    if selected != deck_id:
        _cancel_study_wait(deck_id, f"选中的牌组已经不是它（现在是 {selected}）")
        return
    state = _state_name(getattr(mw, "state", ""))
    if state == "review":
        _cancel_study_wait(deck_id)
        _main_window_front()
        return
    if state != "overview":
        _cancel_study_wait(deck_id, f"界面停在 {state or '未知'}")
        return
    token = object()
    entry: dict[str, Any] = {"token": token, "tries": int(tries), "timer": None}
    _STUDY_PENDING[deck_id] = entry
    timer = QTimer(mw)
    timer.setSingleShot(True)
    timer.setInterval(STUDY_WAIT_TIMEOUT_MS)
    qconnect(timer.timeout, lambda: _fire_study(deck_id, token, "等总览页超时"))
    entry["timer"] = timer
    timer.start()


def _fire_study(deck_id: int, token: Any, reason: str) -> None:
    """等到了（或者等超时了）：按「开始学习」进答题界面。"""
    deck_id = int(deck_id)
    entry = _STUDY_PENDING.get(deck_id)
    if entry is None or entry.get("token") is not token:
        return
    _STUDY_PENDING.pop(deck_id, None)
    timer = entry.get("timer")
    if timer is not None:
        try:
            timer.stop()
        except Exception:
            pass
    col = _col()
    if col is None:
        return
    try:
        selected = int(col.decks.selected())
    except Exception:
        selected = 0
    if selected != deck_id:
        _log(f"总览页已经切走，不再自动进答题：牌组 {deck_id}（{reason}）")
        return
    state = _state_name(getattr(mw, "state", ""))
    if state == "review":
        _main_window_front()
        return
    if state != "overview":
        _log(f"自动进答题前界面已经变了（{state or '未知'}），跳过：牌组 {deck_id}")
        return
    if _start_study():
        _log(f"已进入答题界面并把主窗口置前：牌组 {deck_id}（{reason}）")
        _main_window_front()
        return
    tries = int(entry.get("tries") or 0) + 1
    if tries < STUDY_MAX_TRIES:
        # 总览页还没画完的时候不能抢着进答题（会被它盖掉）：重发一次总览页渲染，
        # 再等一轮。
        _log(f"总览页还没就绪，重试第 {tries} 次：牌组 {deck_id}")
        try:
            mw.moveToState("overview")
        except Exception as exc:
            _log(f"重发总览页失败：{exc}")
        _enter_study_when_ready(deck_id, tries=tries)
    else:
        _log(f"答题界面打不开，已退化为停在该牌组的总览页：牌组 {deck_id}")


def _on_overview_did_refresh(_overview: Any = None) -> None:
    """总览页画完了：把正在等它的「自动进答题」放行（下一帧再动手，顺序更稳）。"""
    for deck_id, entry in list(_STUDY_PENDING.items()):
        token = (entry or {}).get("token")
        if token is None:
            continue
        QTimer.singleShot(0, lambda d=deck_id, t=token: _fire_study(d, t, "总览页已画完"))


def _enter_deck(deck_id: int, *, parent: Any, study: bool = False) -> None:
    """把界面切到这个牌组的总览页（和「开始学习」按钮一样的落点）。

    study=True 时再往前一步直接开始复习——集中刷就是从这一步开刷的。
    已经在这个牌组的答题界面里的话，只把主窗口提到最前，不做多余跳转。
    """
    col = _col()
    if col is None:
        return
    deck_id = int(deck_id)
    if study and _already_studying(deck_id, col):
        _main_window_front()
        return
    try:
        col.decks.select(DeckId(deck_id))
    except Exception as exc:
        showWarning(f"打不开这个牌组：{exc}", parent=parent)
        return
    try:
        mw.moveToState("overview")
    except Exception:
        pass
    if study:
        _main_window_front()
        _enter_study_when_ready(deck_id)


# --------------------------------------------------------------------------
# 自动重建：只在「从牌组列表进入筛选牌组总览」时触发
# --------------------------------------------------------------------------


def _auto_plan(col: Any, deck_id: int, rule: dict) -> dict[str, Any]:
    if not _deck_exists(col, deck_id) or not _is_filtered(col, deck_id):
        return {"action": "cleanup"}
    actual = read_actual(col, deck_id)
    if actual is None:
        return {"action": "cleanup"}
    if not rule_matches_actual(
        rule, actual["search"], actual["limit"], actual["order"], actual["reschedule"]
    ):
        return {"action": "external", "actual": actual}
    return {"action": "rebuild"}


def _on_auto_plan(deck_id: int, result: dict[str, Any]) -> None:
    action = result.get("action")
    col = _col()
    if col is None:
        return

    if action == "cleanup":
        drop_rule(deck_id, col)
        return

    if action == "external":
        rule = get_rule(deck_id, col)
        if rule is not None and not rule.get("externally_modified"):
            rule["externally_modified"] = True
            put_rule(rule, col)
        if deck_id not in _EXTERNAL_WARNED:
            _EXTERNAL_WARNED.add(deck_id)
            tooltip(
                "这个筛选牌组的条件被 Anki 内置设置改过，已跳过自动重收；"
                "打开「标签筛选牌组…」可以看到详情。"
            )
        return

    # 条件已经和规则一致了（用户自己改回来，或者重新保存过），就把旧标记清掉
    rule = get_rule(deck_id, col)
    if rule is not None and rule.get("externally_modified"):
        rule.pop("externally_modified", None)
        put_rule(rule, col)

    def op(collection: Any) -> Any:
        return collection.sched.rebuild_filtered_deck(DeckId(int(deck_id)))

    def done(_out: Any) -> None:
        overview = getattr(mw, "overview", None)
        if overview is not None:
            try:
                overview.refresh_if_needed()
            except Exception:
                pass

    CollectionOp(mw, op).success(done).failure(
        _error_handler(mw, None)
    ).run_in_background()


def _state_name(state: Any) -> str:
    """把主窗口状态名统一成小写字符串。

    Anki 26.09 的 MainWindowState 是字符串字面量（"deckBrowser" / "overview"…），
    这里额外兼容一下枚举或其它包装类型，避免以后上游换成枚举时静默失效。
    """
    return state_key(state)


def _maybe_ask_cram_end(new_state: Any, old_state: Any) -> None:
    """离开集中刷那个牌组时问一句：要结束集中刷、把牌组恢复原样吗？"""
    col = _col()
    if col is None:
        return
    sessions = load_cram_sessions(col)
    if not sessions:
        return
    try:
        selected = int(col.decks.selected())
    except Exception:
        selected = 0
    for key in list(sessions):
        try:
            deck_id = int(key)
        except Exception:
            continue
        # 集中刷时卡片都停在临时牌组里，所以要比对的是那个临时牌组
        session = sessions[key]
        cram_deck_id = cram_session_deck_id(session) or deck_id
        if not should_ask_cram_end(new_state, old_state, selected, cram_deck_id):
            continue
        name = _deck_name(col, deck_id) or f"牌组 {deck_id}"
        question = (
            f"「{name}」还在集中刷。\n\n"
            "现在离开这个牌组，要结束集中刷、按原规则恢复这个筛选牌组吗？\n"
            "（选「继续集中刷」就保持现状，集中刷小窗也留着。）"
        )
        if askUser(question, parent=mw, title=ADDON_TITLE):
            end_cram(deck_id, parent=mw)
        else:
            open_cram_window(deck_id, parent=mw)
        return


def on_state_did_change(new_state: Any, old_state: Any) -> None:
    _maybe_ask_cram_end(new_state, old_state)
    # 只在「从牌组列表进入总览」时自动重收；从学习中返回总览不会重算。
    if _state_name(new_state) != "overview" or _state_name(old_state) != "deckbrowser":
        return
    if not auto_rebuild_enabled():
        return
    col = _col()
    if col is None:
        return
    try:
        deck_id = int(col.decks.selected())
    except Exception:
        return
    rule = get_rule(deck_id, col)
    if rule is None:
        return
    if bool(rule.get("include_not_due", DEFAULT_INCLUDE_NOT_DUE)):
        # 「连还没到期的也收」的突击牌组不自动重收：一轮才刷得完，
        # 想重新装满就点「立即重新收卡」或「集中刷…」。
        return
    if cram_active(deck_id, col):
        # 集中刷进行中不去碰它，免得刷到一半被重新收卡踢走
        return
    QueryOp(
        parent=mw,
        op=lambda collection: _auto_plan(collection, deck_id, rule),
        success=lambda result: _on_auto_plan(deck_id, result),
    ).failure(_error_handler(mw, None)).run_in_background()


def _rebuild_raw(collection: Any, deck_id: int) -> Any:
    """重建一个筛选牌组，返回 Anki 的原生结果；失败返回 None。"""
    try:
        return collection.sched.rebuild_filtered_deck(DeckId(int(deck_id)))
    except Exception as exc:
        _log(f"重新收卡失败（牌组 {deck_id}）：{exc}")
        return None


def _do_rebuild(collection: Any, deck_id: int) -> int | None:
    """重建一个筛选牌组，返回收进来的张数；失败返回 None。"""
    out = _rebuild_raw(collection, deck_id)
    if out is None:
        return None
    return int(getattr(out, "count", 0))


def _note_external(deck_id: int, col: Any, *, quiet: bool = False) -> None:
    """记下「被 Anki 内置设置改过」，必要时提示一次。"""
    rule = get_rule(deck_id, col)
    if rule is not None and not rule.get("externally_modified"):
        rule["externally_modified"] = True
        put_rule(rule, col)
    if quiet or deck_id in _EXTERNAL_WARNED:
        return
    _EXTERNAL_WARNED.add(deck_id)
    tooltip(
        "这个筛选牌组的条件被 Anki 内置设置改过，已跳过自动重收；"
        "打开「标签筛选牌组…」可以看到详情。"
    )


def _clear_external(deck_id: int, col: Any) -> None:
    """条件又和规则一致了（用户改回来或者重新保存过），把旧标记清掉。"""
    rule = get_rule(deck_id, col)
    if rule is not None and rule.get("externally_modified"):
        rule.pop("externally_modified", None)
        put_rule(rule, col)


def rebuild_all_managed(*, parent: Any = None, quiet: bool = False) -> None:
    """把插件管理的每个筛选牌组按规则重收一次（Anki 启动时用）。

    只在「规则和牌组实际设置一致、且确实还是筛选牌组」时才重建；集中刷中的牌组
    跳过；被外部改过的照旧跳过并记下来。
    """
    col = _col()
    if col is None:
        return
    rules = load_rules(col)
    if not rules:
        return

    box: dict[str, Any] = {}

    def op(collection: Any) -> Any:
        actions: list[tuple[int, str]] = []
        raw: list[Any] = []
        for key, rule in rules.items():
            try:
                deck_id = int(rule.get("deck_id") or key)
            except Exception:
                continue
            if get_cram_session(deck_id, collection) is not None:
                continue
            if bool(rule.get("include_not_due", DEFAULT_INCLUDE_NOT_DUE)):
                # 突击牌组（连还没到期的也收）不自动重收，保持现状
                continue
            plan = _auto_plan(collection, deck_id, rule)
            action = str(plan.get("action") or "")
            if action == "rebuild":
                out = _rebuild_raw(collection, deck_id)
                if out is not None:
                    action = "rebuilt"
                    raw.append(out)
            actions.append((deck_id, action))
        box["actions"] = actions
        # 把重建的原生结果交回给 Anki：这样它自己也会通知界面刷新，
        # 不会再出现「操作成功了却抛 AttributeError、界面也不刷新」。
        return _op_changes(*raw)

    def success(_changes: Any) -> None:
        actions = list(box.get("actions") or [])
        rebuilt = 0
        for deck_id, action in actions:
            if action == "cleanup":
                drop_rule(deck_id, col)
            elif action == "external":
                _note_external(deck_id, col, quiet=quiet)
            elif action == "rebuilt":
                _clear_external(deck_id, col)
                rebuilt += 1
        if rebuilt:
            _log(f"启动时已重新收卡 {rebuilt} 个筛选牌组")
        overview = getattr(mw, "overview", None)
        if overview is not None:
            try:
                overview.refresh_if_needed()
            except Exception:
                pass

    CollectionOp(parent or mw, op).success(success).failure(
        _error_handler(parent or mw, None)
    ).run_in_background()


def end_stale_cram_sessions(col: Any, *, quiet: bool = True) -> int:
    """打开用户配置时结束上一次留下的集中刷：删临时牌组 + 按原规则重收。

    集中刷状态不写进规则本身，所以哪怕「自动重收」是关着的，这里也必须结束，
    否则会一直留着一个临时集中刷牌组。Anki 崩溃/强关后靠这一步收尾。
    """
    sessions = load_cram_sessions(col)
    if not sessions:
        return 0
    ended = 0
    for key in list(sessions):
        try:
            deck_id = int(key)
        except Exception:
            continue
        session = sessions.get(key) or {}
        if _deck_exists(col, deck_id) and not _is_filtered(col, deck_id):
            # 原牌组已经不是筛选牌组了：只把临时牌组清掉、丢掉这条会话
            _remove_filtered_decks(
                col, _cram_temp_deck_ids(col, deck_id, cram_session_deck_id(session))
            )
            drop_cram_session(deck_id, col)
            continue
        mode_text = "影响排期" if cram_writes_back(session) else "不影响排期"
        _log(f"结束上次的集中刷（牌组 {deck_id}，{mode_text}）")
        end_cram(deck_id, parent=mw, quiet=quiet)
        ended += 1
    return ended


# --------------------------------------------------------------------------
# 界面：规则编辑器
# --------------------------------------------------------------------------


def _show_dialog(dialog: QDialog, *, activate: bool = True) -> None:
    """显示一个非模态窗口。

    activate=False 时只显示、不抢焦点（用于「集中刷」刚启动时让小窗退到
    主窗口后面），窗口引用仍然留在这里，不会被 Python 回收。
    """
    _OPEN_DIALOGS.add(dialog)
    qconnect(dialog.finished, lambda _result: _OPEN_DIALOGS.discard(dialog))
    if not activate:
        try:
            dialog.setAttribute(_no_activate_flag(), True)
        except Exception as exc:
            _log(f"设置「显示但不抢焦点」失败：{exc}")
    dialog.show()
    if activate:
        dialog.raise_()
        dialog.activateWindow()


def _no_activate_flag() -> Any:
    """「显示但不抢焦点」那个 Qt 标志。

    PyQt6 里既可能是 Qt.WidgetAttribute.WA_ShowWithoutActivating，也可能是
    旧写法的 Qt.WA_ShowWithoutActivating（PyQt5 只有后者）。两种都试，
    免得换版本时静默失效、小窗又跑出来抢焦点。
    """
    holder = getattr(Qt, "WidgetAttribute", None)
    flag = getattr(holder, "WA_ShowWithoutActivating", None) if holder else None
    if flag is None:
        flag = getattr(Qt, "WA_ShowWithoutActivating", None)
    return flag


class RuleEditor(QDialog):
    """新建/编辑一条标签筛选规则。"""

    def __init__(
        self,
        parent: Any = None,
        rule: dict | None = None,
        preset_tags: Sequence[str] | None = None,
        focus_parent: bool = False,
    ) -> None:
        super().__init__(parent or mw)
        self.col = _col()
        self.settings = _addon_config()
        self.original = dict(rule) if rule else None
        self.deck_id = int(rule["deck_id"]) if rule else 0
        self.original_name = str(rule["name"]) if rule else ""
        self._name_touched = bool(rule)
        self._query_token = 0
        self._counts: dict[str, Any] | None = None
        # 这份统计是对哪一套条件算的（换标签/状态后要重新算，避免张数对不上）
        self._counts_signature: tuple | None = None
        self._count_error = ""
        self._order_values: list[int] = []

        self.setWindowTitle("编辑标签筛选规则" if rule else "新建标签筛选牌组")
        self.setMinimumWidth(760)
        self._build_ui()

        if self.col is None:
            showWarning("还没有打开牌组（集合），请先打开一个用户配置。", parent=self)
            return

        self._fill_parent_choices()
        self._fill_order_choices()
        self._fill_tag_completer()

        if rule:
            self._load_rule(rule)
            if rule.get("_unmanaged"):
                self.hint_label.setText(
                    "这个筛选牌组还不是由标签规则管理的。保存后，Anki 内置设置里的"
                    "筛选条件会被这里的标签规则覆盖（卡和复习记录不受影响）。"
                )
        else:
            for tag in clean_tags(preset_tags or []):
                self._add_tag(tag)
            self._sync_name_from_tags()

        self._count_timer = QTimer(self)
        self._count_timer.setSingleShot(True)
        self._count_timer.setInterval(250)
        qconnect(self._count_timer.timeout, self._start_count_query)

        if focus_parent:
            self.hint_label.setText(
                "改「牌组位置」保存后，牌组会连同里面的卡一起移到新位置。"
            )
            self.parent_combo.setFocus()
        self._refresh_preview()

    # ---- 界面搭建 ------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        tag_box = QGroupBox("标签（至少 1 个）")
        tag_outer = QHBoxLayout(tag_box)

        # 左栏：手打 + 自动补全 + 已选标签块
        tag_layout = QVBoxLayout()
        tag_layout.addWidget(QLabel("手打添加（输入时有补全提示）"))
        tag_row = QHBoxLayout()
        self.tag_edit = QLineEdit()
        self.tag_edit.setPlaceholderText("输入标签，回车或点「添加」；输入时会提示已有标签")
        self.add_tag_button = QPushButton("添加")
        tag_row.addWidget(self.tag_edit, 1)
        tag_row.addWidget(self.add_tag_button)
        tag_layout.addLayout(tag_row)
        self.tag_list = QListWidget()
        self.tag_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.tag_list.setMinimumHeight(170)
        tag_layout.addWidget(self.tag_list)
        tag_buttons = QHBoxLayout()
        tag_buttons.addStretch(1)
        tag_buttons.addWidget(QLabel("双击或按 Delete 键移除已选标签"))
        tag_layout.addLayout(tag_buttons)
        tag_outer.addLayout(tag_layout, 1)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        tag_outer.addWidget(divider)

        # 右栏：从已有标签里挑
        pick_layout = QVBoxLayout()
        pick_layout.addWidget(QLabel("已有标签（点选后「加入所选」，或双击直接加入）"))
        self.avail_search = QLineEdit()
        self.avail_search.setPlaceholderText("搜索标签（大小写不敏感、包含匹配）")
        self.avail_search.setClearButtonEnabled(True)
        pick_layout.addWidget(self.avail_search)
        self.avail_list = QListWidget()
        self.avail_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.avail_list.setMinimumHeight(170)
        pick_layout.addWidget(self.avail_list)
        pick_row = QHBoxLayout()
        self.avail_add_button = QPushButton("加入所选")
        self.avail_hint_label = QLabel("")
        pick_row.addWidget(self.avail_add_button)
        pick_row.addStretch(1)
        pick_row.addWidget(self.avail_hint_label)
        pick_layout.addLayout(pick_row)
        tag_outer.addLayout(pick_layout, 1)

        tag_count_row = QHBoxLayout()
        tag_count_row.addStretch(1)
        self.tag_count_label = QLabel("")
        tag_count_row.addWidget(self.tag_count_label)
        tag_outer.addLayout(tag_count_row)
        layout.addWidget(tag_box)

        rule_box = QGroupBox("筛选规则")
        form = QFormLayout(rule_box)

        mode_row = QHBoxLayout()
        self.union_radio = QRadioButton("并集：命中任意一个标签就收")
        self.intersection_radio = QRadioButton("交集：要同时带全部标签")
        self.union_radio.setChecked(True)
        mode_row.addWidget(self.union_radio)
        mode_row.addWidget(self.intersection_radio)
        mode_row.addStretch(1)
        form.addRow("标签关系", _wrap_layout(mode_row))

        state_column = QVBoxLayout()
        state_row = QHBoxLayout()
        self.state_boxes: dict[str, QCheckBox] = {}
        for value, label in STATE_CHOICES:
            box = QCheckBox(label)
            box.setChecked(value in DEFAULT_STATES)
            self.state_boxes[value] = box
            state_row.addWidget(box)
            qconnect(box.toggled, self._on_any_change)
        state_row.addStretch(1)
        state_column.addLayout(state_row)
        self.include_not_due_check = QCheckBox("连还没到期的也收（不勾＝只收到期）")
        self.include_not_due_check.setChecked(
            bool(self.settings.get("default_include_not_due", DEFAULT_INCLUDE_NOT_DUE))
        )
        state_column.addWidget(self.include_not_due_check)
        scope_hint = QLabel(
            "可多选，至少勾一个。三项互不重复、相加正好等于命中总数；答错后重学的卡算"
            "「学习中」。\n"
            "「是否到期」按卡片自己的到期日算（不是筛选牌组里的位置值）；未学习的新卡"
            "不受它影响。\n"
            "已暂停/已搁置的卡 Anki 不会收进筛选牌组，插件也不改动它们的暂停状态，"
            "预览里会告诉你这类卡有多少张收不进来。"
        )
        scope_hint.setWordWrap(True)
        state_column.addWidget(scope_hint)
        cram_hint = QLabel(
            "勾了上面这个开关＝突击牌组：进牌组和启动 Anki 都不会自动重收，"
            "这样一轮才刷得完；想重新装满就点「立即重新收卡」。\n"
            "想反复突击刷还没到期的卡，用「集中刷…」：它另建一个临时牌组突击一轮，"
            "关掉小窗就等于结束这一轮、按原规则恢复。"
        )
        cram_hint.setWordWrap(True)
        state_column.addWidget(cram_hint)
        form.addRow("取卡范围", _wrap_layout(state_column))

        self.parent_combo = QComboBox()
        self.parent_combo.setEditable(True)
        self.parent_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        form.addRow("牌组位置", self.parent_combo)

        name_row = QHBoxLayout()
        self.leaf_edit = QLineEdit()
        self.leaf_edit.setPlaceholderText("牌组名（默认按标签生成）")
        self.auto_name_button = QPushButton("按标签命名")
        name_row.addWidget(self.leaf_edit, 1)
        name_row.addWidget(self.auto_name_button)
        form.addRow("牌组名", _wrap_layout(name_row))

        self.order_combo = QComboBox()
        form.addRow("收录顺序", self.order_combo)

        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(MIN_LIMIT, MAX_LIMIT)
        self.limit_spin.setValue(int(self.settings["default_limit"]))
        self.limit_spin.setAccelerated(True)
        form.addRow("单次收录上限", self.limit_spin)

        layout.addWidget(rule_box)

        preview_box = QGroupBox("预览")
        preview_layout = QVBoxLayout(preview_box)
        self.name_hint_label = QLabel("")
        self.name_hint_label.setWordWrap(True)
        preview_layout.addWidget(self.name_hint_label)
        self.preview_label = QLabel("")
        self.preview_label.setWordWrap(True)
        preview_layout.addWidget(self.preview_label)
        self.search_label = QLabel("")
        self.search_label.setWordWrap(True)
        self.search_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        preview_layout.addWidget(self.search_label)
        preview_buttons = QHBoxLayout()
        self.browse_button = QPushButton("在浏览器里看匹配的卡")
        self.browse_blocked_button = QPushButton("看收不进来的卡")
        preview_buttons.addWidget(self.browse_button)
        preview_buttons.addWidget(self.browse_blocked_button)
        preview_buttons.addStretch(1)
        preview_layout.addLayout(preview_buttons)
        layout.addWidget(preview_box)

        self.hint_label = QLabel(
            "筛选牌组里的卡和源卡是同一张：复习记录、排期共享，作答会按你的选择重排原卡；"
            "删除筛选只是把卡送回原牌组。"
        )
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setText("保存并收卡" if self.original else "创建并收卡")
        layout.addWidget(self.button_box)

        qconnect(self.add_tag_button.clicked, self._on_add_tag_clicked)
        qconnect(self.tag_edit.returnPressed, self._on_add_tag_clicked)
        qconnect(self.tag_list.itemDoubleClicked, self._on_tag_double_clicked)
        qconnect(self.avail_add_button.clicked, self._on_avail_add_clicked)
        qconnect(self.avail_list.itemDoubleClicked, self._on_avail_double_clicked)
        qconnect(self.avail_search.textChanged, self._refresh_avail_tags)
        qconnect(self.auto_name_button.clicked, self._on_auto_name)
        qconnect(self.leaf_edit.textEdited, self._on_leaf_edited)
        qconnect(self.parent_combo.currentTextChanged, self._on_any_change)
        qconnect(self.include_not_due_check.toggled, self._on_any_change)
        qconnect(self.union_radio.toggled, self._on_any_change)
        qconnect(self.order_combo.currentIndexChanged, self._on_any_change)
        qconnect(self.limit_spin.valueChanged, self._on_any_change)
        qconnect(self.browse_button.clicked, self._browse_matching)
        qconnect(self.browse_blocked_button.clicked, self._browse_blocked)
        qconnect(self.button_box.accepted, self.accept)
        qconnect(self.button_box.rejected, self.reject)

    def _fill_parent_choices(self) -> None:
        self.parent_combo.clear()
        for name in _parent_choices(self.col):
            self.parent_combo.addItem(name)
        if self.original:
            parent = str(self.original.get("parent") or "")
            self.parent_combo.setCurrentText(parent or "（顶层）")
        else:
            self.parent_combo.setCurrentText(str(self.settings["default_parent"]))

    def _fill_order_choices(self) -> None:
        labels: list[str] = []
        try:
            labels = [str(text) for text in self.col.sched.filtered_deck_order_labels()]
        except Exception:
            labels = []
        try:
            fsrs = bool(self.col.get_config("fsrs"))
        except Exception:
            fsrs = False
        count = len(labels) if labels else 11
        self._order_values = available_orders(count, fsrs)
        if not self._order_values:
            self._order_values = [DEFAULT_ORDER]
        self.order_combo.clear()
        for value in self._order_values:
            self.order_combo.addItem(order_label(value, labels), value)
        row = (
            self._order_values.index(int(self.settings["default_order"]))
            if int(self.settings["default_order"]) in self._order_values
            else 0
        )
        self.order_combo.setCurrentIndex(row)

    def _fill_tag_completer(self) -> None:
        self._all_tag_options = _all_tags(self.col)
        self._refresh_avail_tags()
        if not self._all_tag_options:
            return
        completer = QCompleter(list(self._all_tag_options), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setMaxVisibleItems(12)
        self.tag_edit.setCompleter(completer)

    # ---- 已有标签面板 --------------------------------------------------

    def _refresh_avail_tags(self, *_args: Any) -> None:
        options = getattr(self, "_all_tag_options", None) or []
        keyword = self.avail_search.text() if hasattr(self, "avail_search") else ""
        shown = filter_tag_choices(options, keyword)
        self.avail_list.clear()
        for tag in shown:
            self.avail_list.addItem(QListWidgetItem(tag))
        if hasattr(self, "avail_hint_label"):
            if not options:
                self.avail_hint_label.setText("这个集合里还没有标签")
            elif keyword and not shown:
                self.avail_hint_label.setText("没有匹配的标签")
            else:
                self.avail_hint_label.setText(f"共 {len(shown)} 个")

    def _on_avail_double_clicked(self, item: QListWidgetItem) -> None:
        self._add_tag(item.text())
        self._on_rules_changed()

    def _on_avail_add_clicked(self) -> None:
        picked = [item.text() for item in self.avail_list.selectedItems()]
        if not picked:
            current = self.avail_list.currentItem()
            if current is not None:
                picked = [current.text()]
        for tag in picked:
            self._add_tag(tag)
        self._on_rules_changed()

    # ---- 数据 ↔ 控件 ---------------------------------------------------

    def _load_rule(self, rule: dict) -> None:
        for tag in clean_tags(rule.get("tags")):
            self._add_tag(tag)
        if rule.get("mode") == MODE_INTERSECTION:
            self.intersection_radio.setChecked(True)
        else:
            self.union_radio.setChecked(True)
        chosen = normalize_states(rule.get("states"))
        if not chosen:
            chosen = list(DEFAULT_STATES)
        for value, box in self.state_boxes.items():
            box.setChecked(value in chosen)
        self.include_not_due_check.setChecked(normalize_include_not_due(rule))
        self.leaf_edit.setText(str(rule.get("leaf") or ""))
        self.limit_spin.setValue(int(rule.get("limit") or DEFAULT_LIMIT))
        wanted = int(rule.get("order") or DEFAULT_ORDER)
        row = self._order_values.index(wanted) if wanted in self._order_values else 0
        self.order_combo.setCurrentIndex(row)

    def _tags(self) -> list[str]:
        return [self.tag_list.item(row).text() for row in range(self.tag_list.count())]

    def _mode(self) -> str:
        return MODE_INTERSECTION if self.intersection_radio.isChecked() else MODE_UNION

    def _states(self) -> list[str]:
        return [
            value
            for value, _label in STATE_CHOICES
            if self.state_boxes[value].isChecked()
        ]

    def _include_not_due(self) -> bool:
        """勾选框：True ＝ 连还没到期的也收（默认 False ＝ 只收到期）。"""
        return bool(self.include_not_due_check.isChecked())

    def _order(self) -> int:
        data = self.order_combo.currentData()
        try:
            return int(data)
        except Exception:
            return DEFAULT_ORDER

    def _parent(self) -> str:
        text = self.parent_combo.currentText().strip()
        return "" if text in ("", "（顶层）") else text

    def _leaf(self) -> str:
        return self.leaf_edit.text().strip()

    def _add_tag(self, tag: str) -> None:
        text = str(tag).strip()
        if not text or text in self._tags():
            return
        self.tag_list.addItem(QListWidgetItem(text))

    def _on_add_tag_clicked(self) -> None:
        self._add_tag(self.tag_edit.text())
        self.tag_edit.setText("")
        self._on_rules_changed()

    def _on_tag_double_clicked(self, item: QListWidgetItem) -> None:
        self.tag_list.takeItem(self.tag_list.row(item))
        self._on_rules_changed()

    def _remove_selected_tags(self) -> None:
        for item in self.tag_list.selectedItems():
            self.tag_list.takeItem(self.tag_list.row(item))
        self._on_rules_changed()

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802（Qt 的命名）
        """已选标签列表里按 Delete / Backspace 就移除选中的标签。

        1.5.0 起去掉了「移除所选标签」按钮，只留这个快捷键和双击。
        """
        if self.tag_list.hasFocus() and event.key() in (
            Qt.Key.Key_Delete,
            Qt.Key.Key_Backspace,
        ):
            self._remove_selected_tags()
            event.accept()
            return
        super().keyPressEvent(event)

    def _on_auto_name(self) -> None:
        self.leaf_edit.setText(default_leaf_name(self._tags(), self._mode()))
        self._name_touched = True
        self._refresh_preview()

    def _on_leaf_edited(self, _text: str) -> None:
        self._name_touched = True
        self._refresh_preview()

    def _on_any_change(self, *_args: Any) -> None:
        self._refresh_preview()

    def _on_rules_changed(self) -> None:
        if not self._name_touched:
            self.leaf_edit.setText(default_leaf_name(self._tags(), self._mode()))
        self._refresh_preview()

    def _sync_name_from_tags(self) -> None:
        if not self._name_touched:
            self.leaf_edit.setText(default_leaf_name(self._tags(), self._mode()))

    # ---- 预览 ----------------------------------------------------------

    def _target_name(self) -> str:
        full = join_deck_name(self._parent(), self._leaf())
        return unique_deck_name(
            full, _deck_names(self.col, include_filtered=True), exclude=self.original_name
        )

    def _build_rule(self) -> dict:
        tags = self._tags()
        rule = make_rule(
            deck_id=self.deck_id,
            tags=tags,
            mode=self._mode(),
            states=self._states(),
            include_not_due=self._include_not_due(),
            parent=self._parent(),
            leaf=self._leaf(),
            limit=self.limit_spin.value(),
            order=self._order(),
        )
        rule["name"] = self._target_name()
        rule["search"] = build_search_query(
            tags, self._mode(), self._states(), self._include_not_due()
        )
        return rule

    def _refresh_preview(self) -> None:
        tags = self._tags()
        self.tag_count_label.setText(
            f"已选 {len(tags)} 个标签" if tags else "还没有选标签"
        )

        typed_full = join_deck_name(self._parent(), self._leaf())
        target = self._target_name()
        if not tags:
            self.name_hint_label.setText("牌组：先选标签，名称会自动生成。")
        elif target != typed_full:
            self.name_hint_label.setText(
                f"牌组：「{typed_full}」已被占用，将自动改名为「{target}」。"
            )
        else:
            self.name_hint_label.setText(f"牌组：{target}")

        query = build_search_query(
            tags, self._mode(), self._states(), self._include_not_due()
        )
        self.search_label.setText(
            f"搜索式：{query}" if query else "搜索式：（还没有选标签）"
        )

        if not tags:
            self.preview_label.setText("请先选择至少 1 个标签。")
        elif not self._states():
            self.preview_label.setText("至少要勾一个取卡范围状态。")
        else:
            self._refresh_preview_text_only()
        if hasattr(self, "_count_timer"):
            self._count_timer.start()

    def _refresh_preview_text_only(self) -> None:
        tags = self._tags()
        if not tags:
            return
        if self._counts is None:
            self.preview_label.setText("正在统计命中数量…")
            return
        lines = preview_lines(
            self._counts,
            tags,
            self._mode(),
            self._states(),
            self.limit_spin.value(),
        )
        if self._count_error:
            lines.append(f"（统计出错：{self._count_error}）")
        self.preview_label.setText("\n".join(lines))

    def _start_count_query(self) -> None:
        if self.col is None:
            return
        tags = self._tags()
        self._query_token += 1
        token = self._query_token
        if not tags:
            self._counts = None
            self.preview_label.setText("请先选择至少 1 个标签。")
            return
        mode = self._mode()
        states = self._states()
        include_not_due = self._include_not_due()
        exclude = self.original_name
        self._count_error = ""
        self._counts_signature = (
            tuple(tags),
            str(mode),
            tuple(states),
            bool(include_not_due),
        )

        def op(collection: Any) -> dict[str, Any]:
            return collect_counts(
                collection, tags, mode, states, include_not_due, exclude
            )

        def success(counts: dict[str, Any]) -> None:
            if token != self._query_token:
                return
            self._counts = counts
            self._refresh_preview_text_only()

        def failure(exc: Exception) -> None:
            if token != self._query_token:
                return
            self._counts = None
            self._count_error = str(exc)
            self._refresh_preview_text_only()

        QueryOp(parent=self, op=op, success=success).failure(failure).run_in_background()

    def _open_browser(self, query: str) -> None:
        text = str(query or "").strip()
        if not text:
            showInfo("还没有可搜索的条件。", parent=self)
            return
        try:
            import aqt

            aqt.dialogs.open("Browser", mw, search=(text,))
        except Exception as exc:
            showWarning(f"打不开浏览器：{exc}", parent=self)

    def _browse_matching(self) -> None:
        self._open_browser(
            in_scope_query(
                self._tags(), self._mode(), self._states(), self._include_not_due()
            )
        )

    def _browse_blocked(self) -> None:
        """看「这次收不进来的卡」。

        「收不进来」里有一条是「卡片自己还没到期」，这一条没法用搜索式表达
        （卡片被收进筛选牌组后 due 已经变成位置值），所以这里直接用刚才统计出来的
        卡号列表拼 `cid:` 搜索式，保证和预览里那句「另有 K 张收不进来」一模一样。
        """
        signature = (
            tuple(self._tags()),
            str(self._mode()),
            tuple(self._states()),
            bool(self._include_not_due()),
        )
        if self._counts is None or signature != self._counts_signature:
            showInfo("还在统计，请稍候一秒钟再点。", parent=self)
            return
        ids = list(self._counts.get("blocked_ids") or [])
        query, dropped = blocked_query(ids, limit=MAX_BROWSE_CARDS)
        if not query:
            showInfo("现在没有收不进来的卡。", parent=self)
            return
        if dropped:
            showInfo(
                f"收不进来的卡太多，浏览器里只列出前 {MAX_BROWSE_CARDS} 张"
                f"（另有 {dropped} 张没列出来）。",
                parent=self,
            )
        self._open_browser(query)

    # ---- 保存 ----------------------------------------------------------

    def accept(self) -> None:
        if self.col is None:
            super().accept()
            return
        if not self._tags():
            showWarning("至少选 1 个标签。", parent=self)
            return
        states = self._states()
        if not states:
            showWarning(
                "至少要勾一个取卡范围状态（未学习／学习中／复习中）。",
                parent=self,
            )
            return
        if self._counts is not None and int(self._counts.get("matched") or 0) == 0:
            if not askUser(
                "现在没有卡片命中这些标签，牌组建出来会是空的。\n仍然要保存吗？",
                parent=self,
            ):
                return
        rule = self._build_rule()

        editor = self

        def done(_deck_id: int) -> None:
            tooltip(f"已{'更新' if editor.original else '创建'}筛选牌组：{rule['name']}")
            if _MANAGER is not None:
                try:
                    _MANAGER.refresh()
                except Exception:
                    pass
            QDialog.accept(editor)

        def go() -> None:
            apply_rule(rule, parent=self, on_done=done)

        if self.deck_id and cram_active(self.deck_id):
            # 这个牌组正在集中刷：先结束（删临时牌组、卡片回原牌组），再写入新规则
            _log(f"编辑规则前先结束集中刷：{self.original_name}")
            end_cram(self.deck_id, parent=self, quiet=True, on_done=go)
        else:
            go()


def _wrap_layout(layout: Any) -> QWidget:
    holder = QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    holder.setLayout(layout)
    return holder


# --------------------------------------------------------------------------
# 界面：管理窗口
# --------------------------------------------------------------------------

HELP_TEXT = """标签筛选牌组 · 使用说明

【它是怎么工作的】
用 Anki 自己的「筛选牌组」按标签把原卡临时挑出来，不做卡片副本。所以：
· 筛选牌组里的卡和源卡是同一张，复习记录、排期、间隔全部共享；
· 在里面作答会重排原卡（和 Anki 原生行为一致），牌组设置跟随卡片原牌组；
· 删掉筛选牌组只是把卡送回各自原来的牌组，卡片和复习记录都不会丢。

【怎么建一个】
1. 菜单「工具 → 标签筛选牌组…」→「新建」；也可以直接在浏览器左侧标签上点右键，
   选「用它创建标签筛选牌组…」。
2. 选至少 1 个标签：左边手打（有补全），或右边「已有标签」面板搜索后点选、
   点「加入所选」（双击也能加）。
3. 选「并集」（命中任一标签）或「交集」（同时带全部标签）；层级标签自动含子标签。
4. 取卡范围勾「未学习 / 学习中 / 复习中」，三项互不重复，答错重学的卡算「学习中」。
   「到期」按卡片自己的到期日算，跟你原牌组每天的新卡/复习上限无关；想连还没到期的
   卡一起收，再勾上「连还没到期的也收」。
5. 选牌组位置（默认「标签筛选」下）、名字、收录顺序和上限，点「创建并收卡」。

【日常怎么用】
· 从牌组列表点进筛选牌组会按规则自动重收一次（只在进入总览页时做，学习中返回不会）；
· 想手动重收就点「立即重新收卡」；牌组右键还有「编辑筛选规则…／立即重建／
  集中刷…／删除筛选（保留卡片）」；
· 勾了「连还没到期的也收」的是「突击牌组」，它不会自动重收，这样一轮才刷得完；
  想重新装满就点「立即重新收卡」，或干脆再点一次「集中刷…」；
· 管理窗口底部能关掉全部自动重收，也能开关插件更新检查、打开插件日志。

【集中刷（点一次＝刷一轮）】
想反复突击刷还没到期的卡就用「集中刷…」：点一下先问你这次要不要影响原卡排期，
然后另建一个临时的「原牌组名·集中刷」牌组，把小窗和复习界面切过去。
· 「不影响排期（推荐）」：按 Anki 预览模式刷，原卡的到期时间、间隔、因子、次数都不变
  （Anki 会在复习历史里多记一条预览记录），三档延迟按卡片原牌组的新卡学习步骤换算；
· 「影响排期」：收录范围一样放宽，但作答按算法写回原卡；
· 关掉小窗＝结束这一轮：临时牌组删掉、卡片回原牌组，原筛选牌组按规则重收。
  想再刷一轮就点小窗里的「再刷一轮」；关掉 Anki 重开也会自动收尾。

【三个常见问题】
1. 为什么没有「已暂停／已搁置」？
   Anki 重建筛选牌组时一定会排除暂停和搁置的卡，插件也不去改动它们的暂停状态。
   预览里会告诉你有多少张这样收不进来；想学它们，先在浏览器里取消暂停／搁置。
2. 「看收不进来的卡」看的是什么？
   和预览里那句「另有 K 张这次收不进来」是同一批，三段相加正好等于 K：还没到期的、
   暂停或搁置的、已在别的筛选牌组里的。
3. 用 Anki 内置设置改过筛选条件会怎样？
   插件会认出「被改过」，跳过自动重收并在管理窗口标出来。点「编辑」重新保存一次就
   按标签规则重新接管；卡片和复习记录始终不受影响。
"""


def show_help(parent: Any = None) -> None:
    showInfo(HELP_TEXT, parent=parent, title=ADDON_TITLE)


class ManagerDialog(QDialog):
    """列出所有由标签规则管理的筛选牌组。"""

    COLUMNS = (
        "牌组",
        "标签规则",
        "取卡范围",
        "收录顺序",
        "上限",
        "当前张数",
        "状态",
    )

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent or mw)
        self.setWindowTitle(f"{ADDON_TITLE} · 管理")
        self.setMinimumSize(920, 460)
        self._rows: dict[int, dict[str, Any]] = {}
        self._order_labels: list[str] = []
        self._build_ui()
        self.refresh()

    # ---- 界面搭建 ------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.intro_label = QLabel(
            "这里列出由标签规则自动收卡的筛选牌组。双击一行可以编辑规则。"
        )
        self.intro_label.setWordWrap(True)
        top.addWidget(self.intro_label, 1)
        layout.addLayout(top)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self.COLUMNS))
        self.tree.setHeaderLabels(list(self.COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.setSelectionBehavior(QTreeWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.tree, 1)

        self.notice_label = QLabel("")
        self.notice_label.setWordWrap(True)
        layout.addWidget(self.notice_label)

        row = QHBoxLayout()
        self.new_button = QPushButton("新建")
        self.edit_button = QPushButton("编辑")
        self.rebuild_button = QPushButton("立即重新收卡")
        self.cram_button = QPushButton("集中刷…")
        self.study_button = QPushButton("开始学习")
        self.delete_button = QPushButton("删除筛选（保留卡片）")
        row.addWidget(self.new_button)
        row.addWidget(self.edit_button)
        row.addWidget(self.rebuild_button)
        row.addWidget(self.cram_button)
        row.addWidget(self.study_button)
        row.addStretch(1)
        row.addWidget(self.delete_button)
        layout.addLayout(row)

        options = QHBoxLayout()
        self.auto_rebuild_check = QCheckBox(
            "进入牌组时自动重新收卡（并在 Anki 启动时各收一次）"
        )
        self.update_check_check = QCheckBox("启动时自动检查更新（每天最多一次）")
        self.anki_update_check = QCheckBox("让 Anki 也自动检查插件更新")
        options.addWidget(self.auto_rebuild_check)
        options.addWidget(self.update_check_check)
        options.addWidget(self.anki_update_check)
        options.addStretch(1)
        layout.addLayout(options)

        self.crash_label = QLabel("")
        self.crash_label.setWordWrap(True)
        self.crash_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.crash_label)

        bottom = QHBoxLayout()
        self.refresh_button = QPushButton("刷新")
        self.update_button = QPushButton("检查更新（GitHub）")
        self.log_button = QPushButton("打开日志文件")
        self.help_button = QPushButton("使用说明")
        self.close_button = QPushButton("关闭")
        bottom.addWidget(self.refresh_button)
        bottom.addWidget(self.update_button)
        bottom.addWidget(self.log_button)
        bottom.addWidget(self.help_button)
        bottom.addStretch(1)
        bottom.addWidget(self.close_button)
        layout.addLayout(bottom)

        qconnect(self.new_button.clicked, self._on_new)
        qconnect(self.edit_button.clicked, self._on_edit)
        qconnect(self.rebuild_button.clicked, self._on_rebuild)
        qconnect(self.cram_button.clicked, self._on_cram)
        qconnect(self.study_button.clicked, self._on_study)
        qconnect(self.delete_button.clicked, self._on_delete)
        qconnect(self.refresh_button.clicked, self.refresh)
        qconnect(self.update_button.clicked, self._on_check_update)
        qconnect(self.log_button.clicked, lambda: open_log_file(self))
        qconnect(self.help_button.clicked, lambda: show_help(self))
        qconnect(self.close_button.clicked, self.reject)
        qconnect(
            self.auto_rebuild_check.toggled, self._on_auto_rebuild_toggled
        )
        qconnect(self.update_check_check.toggled, self._on_update_check_toggled)
        qconnect(self.anki_update_check.toggled, self._on_anki_update_toggled)
        qconnect(self.tree.itemDoubleClicked, lambda *_a: self._on_edit())
        qconnect(self.tree.itemSelectionChanged, self._sync_buttons)
        self._load_settings()

    # ---- 设置（几个勾选框） --------------------------------------------

    def _load_settings(self) -> None:
        settings = _addon_config()
        for box, key, value in (
            (self.auto_rebuild_check, "auto_rebuild", bool(settings["auto_rebuild"])),
            (self.update_check_check, "update_check", bool(settings["update_check"])),
            (self.anki_update_check, "anki_updates", anki_addon_updates_enabled()),
        ):
            box.blockSignals(True)
            box.setChecked(bool(value))
            box.blockSignals(False)
            box.setToolTip(
                {
                    "auto_rebuild": "关掉后不再自动重收，但「立即重新收卡」按钮照样能用。",
                    "update_check": f"每天最多查一次 {UPDATE_REPO} 上的 version.txt。",
                    "anki_updates": "这是 Anki 自己的设置，插件只帮你改这一项。",
                }[key]
            )

    def _save_setting(self, key: str, value: Any) -> None:
        settings = _addon_config()
        settings[key] = value
        save_addon_config(settings)

    def _on_auto_rebuild_toggled(self, checked: bool) -> None:
        self._save_setting("auto_rebuild", bool(checked))

    def _on_update_check_toggled(self, checked: bool) -> None:
        self._save_setting("update_check", bool(checked))

    def _on_anki_update_toggled(self, checked: bool) -> None:
        if not set_anki_addon_updates(bool(checked)):
            self.anki_update_check.blockSignals(True)
            self.anki_update_check.setChecked(anki_addon_updates_enabled())
            self.anki_update_check.blockSignals(False)

    def _on_check_update(self) -> None:
        check_for_update(silent=False, parent=self)

    # ---- 读取与显示 ----------------------------------------------------

    def refresh(self) -> None:
        col = _col()
        if col is None:
            self.tree.clear()
            self._rows = {}
            self.notice_label.setText("还没有打开牌组（集合）。")
            self._sync_buttons()
            return
        rules = load_rules(col)
        try:
            self._order_labels = [
                str(text) for text in col.sched.filtered_deck_order_labels()
            ]
        except Exception:
            self._order_labels = []
        self.notice_label.setText("正在读取…")

        def op(collection: Any) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for key, rule in rules.items():
                try:
                    deck_id = int(rule.get("deck_id") or key)
                except Exception:
                    continue
                name = _deck_name(collection, deck_id)
                exists = bool(name)
                filtered = _is_filtered(collection, deck_id)
                actual = read_actual(collection, deck_id) if filtered else None
                managed = bool(actual) and rule_matches_actual(
                    rule,
                    actual["search"],
                    actual["limit"],
                    actual["order"],
                    actual["reschedule"],
                )
                out.append(
                    {
                        "deck_id": deck_id,
                        "name": name or str(rule.get("name") or f"牌组 {deck_id}"),
                        "rule": rule,
                        "exists": exists,
                        "filtered": filtered,
                        "managed": managed,
                        "cram": get_cram_session(deck_id, collection),
                        "cards": _cards_in_deck(collection, deck_id) if exists else 0,
                    }
                )
            out.sort(key=lambda item: str(item["name"]).lower())
            return out

        def success(rows: list[dict[str, Any]]) -> None:
            self._populate(rows)

        def failure(exc: Exception) -> None:
            self.notice_label.setText(f"读取失败：{exc}")
            self._sync_buttons()

        QueryOp(parent=self, op=op, success=success).failure(failure).run_in_background()

    def _status_of(self, row: dict[str, Any]) -> str:
        if not row["exists"]:
            return "牌组已不在（规则会在下次打开用户配置时清理）"
        if not row["filtered"]:
            return "已不是筛选牌组，插件不再管理它"
        if row.get("cram"):
            return cram_label(row["cram"]) + " · 自动重收已跳过"
        if not row["managed"]:
            return "被 Anki 内置设置改过，自动重收已暂停"
        if bool((row.get("rule") or {}).get("include_not_due", DEFAULT_INCLUDE_NOT_DUE)):
            return "正常 · 突击牌组（含未到期，不自动重收）"
        return "正常"

    def _populate(self, rows: list[dict[str, Any]]) -> None:
        self.tree.clear()
        self._rows = {}
        for row in rows:
            rule = row["rule"]
            tags = "、".join(rule.get("tags") or [])
            mode = "并集" if rule.get("mode") == MODE_UNION else "交集"
            scope = states_label(
                rule.get("states"), normalize_include_not_due(rule)
            )
            limit = str(int(rule.get("limit") or DEFAULT_LIMIT))
            order = order_label(rule.get("order"), self._order_labels)
            status = self._status_of(row)
            item = QTreeWidgetItem(
                [
                    str(row["name"]),
                    f"{tags}（{mode}）",
                    scope,
                    order,
                    limit,
                    str(row["cards"]),
                    status,
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, int(row["deck_id"]))
            item.setToolTip(0, f"牌组 ID：{row['deck_id']}")
            for column in range(1, len(self.COLUMNS)):
                item.setToolTip(column, item.text(column))
            self.tree.addTopLevelItem(item)
            self._rows[int(row["deck_id"])] = row

        for column in range(len(self.COLUMNS)):
            self.tree.resizeColumnToContents(column)

        if not rows:
            self.notice_label.setText(
                "还没有标签筛选牌组。点「新建」，或者在卡片浏览器的标签上点右键。"
            )
        else:
            broken = [row for row in rows if not row["exists"] or not row["filtered"]]
            external = [
                row
                for row in rows
                if row["exists"] and row["filtered"] and not row["managed"]
            ]
            parts: list[str] = []
            if external:
                parts.append(
                    "有 "
                    f"{len(external)} 个牌组被 Anki 内置设置改过，已跳过自动重收"
                    "（点「编辑」重新保存即可交回插件管理）："
                    + "、".join(str(row["name"]) for row in external[:3])
                )
            if broken:
                parts.append(
                    f"有 {len(broken)} 条规则的牌组已不在或已不是筛选牌组，"
                    "可以选中后删除规则。"
                )
            if any(
                row["exists"]
                and bool(
                    (row.get("rule") or {}).get(
                        "include_not_due", DEFAULT_INCLUDE_NOT_DUE
                    )
                )
                for row in rows
            ):
                parts.append(
                    "范围里带「含未到期」的是突击牌组：不会自动重收，"
                    "想重新装满请点「立即重新收卡」或「集中刷…」。"
                )
            self.notice_label.setText("\n".join(parts))
        try:
            self.crash_label.setText(crash_culprit_text())
        except Exception as exc:
            _log(f"读取 crash.log 提示失败：{exc}")
            self.crash_label.setText("")
        self._sync_buttons()

    # ---- 选择状态 ------------------------------------------------------

    def _current_deck_id(self) -> int | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        try:
            return int(data)
        except Exception:
            return None

    def _sync_buttons(self) -> None:
        deck_id = self._current_deck_id()
        row = self._rows.get(deck_id) if deck_id is not None else None
        usable = bool(row and row["exists"])
        filtered = bool(row and row["filtered"])
        self.edit_button.setEnabled(usable)
        self.rebuild_button.setEnabled(filtered)
        self.study_button.setEnabled(filtered)
        self.delete_button.setEnabled(filtered)
        on_cram = bool(row and row.get("cram"))
        # 集中刷对任何被管理的筛选牌组都可用（1.5.0 去掉了「只收到期」的门槛）
        self.cram_button.setEnabled(filtered)
        self.cram_button.setText("打开集中刷窗口" if on_cram else "集中刷…")
        self.cram_button.setToolTip(
            "点一下把集中刷小窗调出来（关掉小窗就等于结束集中刷、恢复原样）"
            if on_cram
            else "另建一个临时牌组把还没到期的卡也收进来突击刷一轮，规则本身不动"
        )

    # ---- 按钮动作 ------------------------------------------------------

    def _on_new(self) -> None:
        _show_dialog(RuleEditor(self))

    def _on_edit(self) -> None:
        deck_id = self._current_deck_id()
        if deck_id is None:
            return
        open_editor_for_deck(deck_id)

    def _on_rebuild(self) -> None:
        deck_id = self._current_deck_id()
        if deck_id is None:
            return
        rebuild_with_check(deck_id, parent=self, on_done=self.refresh)

    def _on_cram(self) -> None:
        deck_id = self._current_deck_id()
        if deck_id is None:
            return
        row = self._rows.get(deck_id) or {}
        if row.get("cram"):
            # 已经在集中刷：把那个小窗调出来，不重复建
            if open_cram_window(deck_id, parent=self) is None:
                self.refresh()
            return
        # 集中刷开起来之后要直接跳到牌组，所以先把管理窗口关掉
        begin_cram(deck_id, parent=self, on_done=self.accept)

    def _on_delete(self) -> None:
        deck_id = self._current_deck_id()
        if deck_id is None:
            return
        confirm_and_delete(deck_id, parent=self, on_done=self.refresh)

    def _on_study(self) -> None:
        deck_id = self._current_deck_id()
        col = _col()
        if deck_id is None or col is None:
            return
        try:
            col.decks.select(DeckId(int(deck_id)))
        except Exception as exc:
            showWarning(f"打不开这个牌组：{exc}", parent=self)
            return
        self.accept()
        try:
            mw.moveToState("overview")
        except Exception:
            pass


def open_manager() -> None:
    global _MANAGER
    if _col() is None:
        showWarning("还没有打开牌组（集合），请先打开一个用户配置。")
        return
    if isinstance(_MANAGER, QDialog) and _MANAGER.isVisible():
        _MANAGER.refresh()
        _MANAGER.raise_()
        _MANAGER.activateWindow()
        return
    _MANAGER = ManagerDialog(mw)
    _show_dialog(_MANAGER)


def open_editor_for_deck(deck_id: int, focus_parent: bool = False) -> None:
    col = _col()
    if col is None or not _deck_exists(col, deck_id):
        showWarning("这个牌组已经不在了。")
        return
    name = _deck_name(col, deck_id)
    parent, leaf = split_deck_name(name)
    rule = get_rule(deck_id, col)
    if rule is None:
        actual = read_actual(col, deck_id) if _is_filtered(col, deck_id) else None
        rule = {
            "version": 1,
            "deck_id": int(deck_id),
            "tags": [],
            "mode": MODE_UNION,
            "states": list(DEFAULT_STATES),
            "include_not_due": bool(DEFAULT_INCLUDE_NOT_DUE),
            "include_subtags": True,
            "parent": parent,
            "leaf": leaf,
            "name": name,
            "order": DEFAULT_ORDER,
            "limit": DEFAULT_LIMIT,
            "search": "",
            "updated_at": 0,
            "_unmanaged": True,
        }
        if actual:
            rule["order"] = int(actual["order"] or DEFAULT_ORDER)
            rule["limit"] = int(actual["limit"] or DEFAULT_LIMIT)
    _show_dialog(RuleEditor(mw, rule=rule, focus_parent=focus_parent))


def open_editor_with_tag(tag: str) -> None:
    _show_dialog(RuleEditor(mw, preset_tags=[str(tag)]))


# --------------------------------------------------------------------------
# 从 GitHub 检查 / 安装更新（公开仓库，只做这一件事，不联网干别的）
# --------------------------------------------------------------------------


def _update_repo_ready() -> bool:
    return bool(UPDATE_REPO) and "TODO" not in UPDATE_REPO


def _version_tuple(text: str) -> tuple:
    """把 "1.2.3" 这种版本号变成可比较的元组；认不出就当 0。"""
    import re

    numbers = re.findall(r"\d+", str(text or ""))
    if not numbers:
        return (0,)
    return tuple(int(number) for number in numbers[:4])


def is_newer(remote: str, local: str) -> bool:
    """远端版本是不是比本地新。"""
    return _version_tuple(remote) > _version_tuple(local)


def _raw_url(name: str) -> str:
    return f"https://raw.githubusercontent.com/{UPDATE_REPO}/{UPDATE_BRANCH}/{name}"


def _release_asset_url(name: str) -> str:
    """最新 Release 的附件地址（raw 传一半断掉时的兜底通道）。"""
    return f"https://github.com/{UPDATE_REPO}/releases/latest/download/{name}"


def _download_urls(name: str) -> list[str]:
    if not _update_repo_ready():
        return []
    return [_raw_url(name), _release_asset_url(name)]


def _fetch(url: str, timeout: int = 10, retries: int = 2) -> bytes:
    """下载一个小文件；分块读 + 失败重试（raw.githubusercontent 偶尔会卡住）。"""
    import urllib.request

    last_error: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": f"anki-tag-filtered-deck/{__version__}"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                chunks: list[bytes] = []
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
        except Exception as exc:
            last_error = exc
            _log(f"下载失败（第 {attempt + 1} 次）：{url}（{exc}）")
            time.sleep(1.0 + attempt)
    if last_error is not None:
        raise last_error
    return b""


def fetch_latest_version() -> str:
    if not _update_repo_ready():
        return ""
    for url in _download_urls("version.txt"):
        try:
            text = _fetch(url, timeout=8).decode("utf-8", "replace").strip()
        except Exception as exc:
            _log(f"取版本号失败（{url}）：{exc}")
            continue
        if text:
            return text
    return ""


def _looks_like_package(data: bytes) -> bool:
    """下到的东西是不是一个像样的 .ankiaddon（防半截包）。"""
    if not data or len(data) < 512 or not data.startswith(b"PK"):
        return False
    try:
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except Exception:
        return False
    return "manifest.json" in names and "__init__.py" in names


def fetch_package() -> bytes:
    """下载最新的 .ankiaddon；raw 挂了就换 Release 附件。"""
    last_error: BaseException | None = None
    for url in _download_urls(UPDATE_ASSET):
        try:
            data = _fetch(url, timeout=25, retries=1)
        except Exception as exc:
            last_error = exc
            _log(f"下载分发包失败（{url}）：{exc}")
            continue
        if _looks_like_package(data):
            _log(f"分发包下载成功（{url}，{len(data)} 字节）")
            return data
        last_error = RuntimeError(f"下载到的文件不完整（{len(data)} 字节）")
        _log(f"分发包不完整（{url}，{len(data)} 字节），换个地址再试")
    if last_error is not None:
        raise last_error
    raise RuntimeError("没有可用的下载地址")


def local_disk_version() -> str:
    """磁盘上这份插件现在是哪一版（直接读文件里的 __version__，不导入）。

    运行中的代码是启动那一刻载入内存的：用户「装好了但还没重启」时，磁盘上的版本会
    比运行中的新，检查更新要靠它分辨这种情况，别再催着下载。
    """
    import re

    try:
        with open(
            os.path.join(ADDON_DIR, "__init__.py"), "r", encoding="utf-8"
        ) as fh:
            text = fh.read()
    except Exception:
        return ""
    match = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", text, re.M)
    return match.group(1).strip() if match else ""


def update_prompt_text(latest: str) -> str:
    url = f"https://github.com/{UPDATE_REPO}/releases"
    return (
        f"{ADDON_TITLE}有新版本：{latest}（当前 {__version__}）\n\n"
        f"这次改了什么：{url}\n\n"
        "现在下载并安装吗？装完要重启 Anki 才生效。"
    )


def update_pending_restart_text(version: str) -> str:
    return (
        f"{ADDON_TITLE}：新版 {version} 已经装好了，重启 Anki 后生效"
        f"（当前运行中的还是 {__version__}）"
    )


def _update_state() -> dict[str, Any]:
    try:
        with open(UPDATE_STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_update_state(state: dict[str, Any]) -> None:
    try:
        os.makedirs(ADDON_DIR, exist_ok=True)
        with open(UPDATE_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        _log(f"记录更新状态失败：{exc}")


def download_and_install_update() -> bool:
    """下载最新的 .ankiaddon 并交给 Anki 安装。

    Anki 26.9 的接口是 AddonManager.install(file, manifest=None, force_enable=False)，
    返回 InstallOk 或 InstallError，靠类型名区分成功与否。
    """
    import tempfile

    data = fetch_package()
    target = os.path.join(tempfile.gettempdir(), "tag_filtered_deck_update.ankiaddon")
    with open(target, "wb") as fh:
        fh.write(data)

    manager = getattr(mw, "addonManager", None)
    install = getattr(manager, "install", None)
    result: Any = None
    if callable(install):
        try:
            result = install(target)
        except Exception as exc:
            _log(f"调用 addonManager.install 失败：{exc}")
            result = exc
    if (
        result is not None
        and "Error" not in type(result).__name__
        and not isinstance(result, Exception)
    ):
        return True

    detail = ""
    if result is not None:
        for attr in ("error", "message", "text", "reason", "errmsg"):
            value = getattr(result, attr, None)
            if value:
                detail = f"\n\n{value}"
                break
        if not detail and not isinstance(result, Exception):
            detail = f"\n\n{result!r}"
    showInfo(
        f"{ADDON_TITLE}：新版已经下载好了，但自动安装没成功。\n\n"
        f"文件在：{target}\n"
        "请用「工具 → 插件 → 从文件安装」选它，然后重启 Anki。" + detail
    )
    return False


def check_for_update(silent: bool = False, *, parent: Any = None) -> None:
    """检查 GitHub 上的最新版本；silent=True 时只在有新版本时提示。"""
    if not _update_repo_ready():
        if not silent:
            showInfo(f"{ADDON_TITLE}：还没配置更新地址。", parent=parent)
        return

    def work() -> str:
        return fetch_latest_version()

    def done(future: Any) -> None:
        try:
            latest = future.result()
        except Exception as exc:
            _log(f"检查更新失败：{exc}")
            if not silent:
                showInfo(f"{ADDON_TITLE}：检查更新失败\n{exc}", parent=parent)
            return
        state = _update_state()
        on_disk = local_disk_version()
        state["checked_at"] = int(time.time())
        state["latest"] = latest or ""
        state["running"] = __version__
        state["on_disk"] = on_disk
        state["repo"] = UPDATE_REPO
        _save_update_state(state)
        _log(f"检查更新：线上 {latest or '未知'}，本地 {__version__}，磁盘 {on_disk or '未知'}")

        if not latest or not is_newer(latest, __version__):
            if not silent:
                tooltip(f"{ADDON_TITLE}：已经是最新版本（{__version__}）")
            return
        # 「装好了但还没重启」：磁盘上那份已经不比线上旧（或上次就是装的这一版）
        disk_is_current = bool(on_disk) and not is_newer(latest, on_disk)
        if disk_is_current or state.get("installed") == latest:
            tooltip(update_pending_restart_text(on_disk or latest))
            return
        if not askUser(update_prompt_text(latest), parent=parent):
            return
        try:
            if download_and_install_update():
                state = _update_state()
                state["installed"] = latest
                _save_update_state(state)
                tooltip(f"{ADDON_TITLE}：已更新到 {latest}，请重启 Anki")
        except Exception as exc:
            showInfo(f"{ADDON_TITLE}：下载更新失败\n{exc}", parent=parent)

    taskman = getattr(mw, "taskman", None)
    runner = getattr(taskman, "run_in_background", None)
    if callable(runner):
        runner(work, done)
        return

    class _Ready:
        def __init__(self, value: Any) -> None:
            self._value = value

        def result(self) -> Any:
            return self._value

    done(_Ready(work()))


def maybe_check_update_on_start() -> None:
    """启动时悄悄查一次（默认一天最多一次）。"""
    if not bool(_addon_config().get("update_check", True)):
        return
    if not _update_repo_ready():
        return
    state = _update_state()
    try:
        last = float(state.get("checked_at") or 0)
    except Exception:
        last = 0.0
    if time.time() - last < UPDATE_INTERVAL_SECONDS:
        return
    check_for_update(silent=True)


def anki_addon_updates_enabled() -> bool:
    """Anki 自己那个「自动检查插件更新」开关现在是什么状态。"""
    pm = getattr(mw, "pm", None)
    getter = getattr(pm, "check_for_addon_updates", None)
    if callable(getter):
        try:
            return bool(getter())
        except Exception as exc:
            _log(f"读取 Anki 插件更新开关失败：{exc}")
    return True


def set_anki_addon_updates(on: bool) -> bool:
    pm = getattr(mw, "pm", None)
    setter = getattr(pm, "set_check_for_addon_updates", None)
    if callable(setter):
        try:
            setter(bool(on))
            return True
        except Exception as exc:
            _log(f"写入 Anki 插件更新开关失败：{exc}")
            showWarning(f"写入 Anki 的插件更新开关失败：{exc}")
    else:
        showWarning("这个 Anki 版本没有提供修改「自动检查插件更新」的接口。")
    return False


# --------------------------------------------------------------------------
# 菜单入口
# --------------------------------------------------------------------------


def _parent_from_name(name: str) -> str:
    return split_deck_name(name)[0]


def _menu_action(menu: Any, label: str, callback: Callable[[], None]) -> Any:
    action = QAction(label, menu)
    guarded = _guarded(f"菜单「{label}」", callback)
    qconnect(action.triggered, lambda _checked=False: guarded())
    menu.addAction(action)
    return action


def on_deck_browser_menu(menu: Any, deck_id: int) -> None:
    col = _col()
    if col is None:
        return
    try:
        deck_number = int(deck_id)
    except Exception:
        return
    if not _is_filtered(col, deck_number):
        return
    rule = get_rule(deck_number, col)
    menu.addSeparator()
    if rule is not None:
        _menu_action(
            menu,
            "编辑筛选规则…",
            lambda: open_editor_for_deck(deck_number),
        )
        _menu_action(
            menu,
            "立即重建",
            lambda: rebuild_with_check(deck_number, parent=mw),
        )
        if cram_active(deck_number, col):
            _menu_action(
                menu,
                "打开集中刷窗口",
                lambda: open_cram_window(deck_number, parent=mw),
            )
        else:
            _menu_action(
                menu,
                "集中刷…（临时把没到期的也收进来，突击一轮）",
                lambda: begin_cram(deck_number, parent=mw),
            )
        _menu_action(
            menu,
            "删除筛选（保留卡片）",
            lambda: confirm_and_delete(deck_number, parent=mw),
        )
    else:
        _menu_action(
            menu,
            "用标签规则接管这个筛选牌组…",
            lambda: open_editor_for_deck(deck_number),
        )


def on_sidebar_menu(sidebar: Any, menu: Any, item: Any, index: Any) -> None:
    try:
        from aqt.browser.sidebar.item import SidebarItemType
    except Exception:
        return
    item_type = getattr(item, "item_type", None)
    if item_type != SidebarItemType.TAG:
        return
    tag = str(getattr(item, "full_name", "") or getattr(item, "name", "")).strip()
    if not tag:
        return
    menu.addSeparator()
    _menu_action(
        menu,
        "用它创建标签筛选牌组…",
        lambda: open_editor_with_tag(tag),
    )


def install_menu(*_args: Any, **_kwargs: Any) -> None:
    form = getattr(mw, "form", None)
    if form is None or not hasattr(form, "menuTools"):
        return
    if getattr(mw, "_tag_filtered_deck_menu", None):
        return
    try:
        action = QAction(f"{ADDON_TITLE}…", mw)
        qconnect(action.triggered, lambda _checked=False: open_manager())
        form.menuTools.addAction(action)
        mw._tag_filtered_deck_menu = action  # type: ignore[attr-defined]
    except Exception as exc:
        _log(f"工具菜单创建失败：{exc}")


def on_profile_did_open(*_args: Any, **_kwargs: Any) -> None:
    install_menu()
    _EXTERNAL_WARNED.clear()
    col = _col()
    if col is None:
        return
    try:
        _log(f"打开用户配置：管理中的筛选牌组 {len(load_rules(col))} 个")
    except Exception:
        pass
    # 集中刷不写进规则，所以每次打开用户配置都先把它收掉（恢复正常筛选牌组）
    try:
        end_stale_cram_sessions(col)
    except Exception as exc:
        _log(f"结束上次的集中刷失败：{exc}")
    try:
        removed = cleanup_stale_rules(col)
    except Exception as exc:
        _log(f"清理失效规则失败：{exc}")
    else:
        if removed:
            _log(f"已清理失效规则：{'、'.join(removed)}")
    # 「自动重收」开着时，Anki 启动后把每个筛选牌组各收一次
    if auto_rebuild_enabled():
        try:
            rebuild_all_managed(quiet=True)
        except Exception as exc:
            _log(f"启动时自动重收失败：{exc}")
    try:
        maybe_check_update_on_start()
    except Exception as exc:
        _log(f"启动时检查更新失败：{exc}")


def _add_hook(name: str, fn: Any) -> None:
    hook = getattr(gui_hooks, name, None)
    if hook is None:
        return
    try:
        hook.append(fn)
    except Exception as exc:
        _log(f"注册钩子 {name} 失败：{exc}")


# 钩子统一套一层防护：插件自己出错只写日志，绝不冒泡到 Anki 的「遇到了问题」弹窗
_add_hook("main_window_did_init", _guarded("启动时建菜单", install_menu))
_add_hook("profile_did_open", _guarded("打开用户配置", on_profile_did_open))
_add_hook("state_did_change", _guarded("切换界面状态", on_state_did_change))
_add_hook("overview_did_refresh", _guarded("总览页画完", _on_overview_did_refresh))
_add_hook(
    "deck_browser_will_show_options_menu",
    _guarded("牌组右键菜单", on_deck_browser_menu),
)
_add_hook(
    "browser_sidebar_will_show_context_menu",
    _guarded("标签右键菜单", on_sidebar_menu),
)

_log(f"插件已加载：{ADDON_TITLE} {__version__}（{ADDON_DIR}）")
