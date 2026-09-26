"""把「标签筛选牌组」插件安装到 Anki，并重新打包 .ankiaddon。

用法（在本文件夹里执行）：
    python 安装到Anki.py                     # 自动找 Anki 插件目录并安装
    python 安装到Anki.py --addons "路径"      # 手动指定 addons21 目录
    python 安装到Anki.py --package-only      # 只重新打包，不安装

装完记得重启 Anki。插件只在 Anki 启动时加载。
"""

import argparse
import os
import shutil
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "源码")
PACKAGE = os.path.join(HERE, "tag_filtered_deck.ankiaddon")
SOURCE_ZIP = os.path.join(HERE, "标签筛选牌组-源码.zip")
FOLDER_NAME = "tag_filtered_deck"

# 放进 .ankiaddon 的文件（路径用正斜杠，zip 条目也必须是正斜杠）
ADDON_FILES = [
    "manifest.json",
    "config.json",
    "config.md",
    "README.md",
    "__init__.py",
    "rule_logic.py",
    "version.txt",
    "操作指南.txt",
]

# 源码压缩包里额外包含的东西
SOURCE_EXTRA = [
    "安装到Anki.py",
    "安装到Anki.cmd",
    "项目说明.md",
    "测试.cmd",
    "源码/tests/test_rule_logic.py",
    "_维护记录/当前状态.md",
    "_维护记录/探针结果-2026-09-25.md",
    "_维护记录/探针结果-2026-09-26.md",
    "_维护记录/探针结果-2026-09-26-1.5.0.md",
    "_维护记录/探针结果-2026-09-26-1.5.1.md",
    "_维护记录/探针结果-2026-09-26-1.5.2.md",
]

# 面向使用者的文本文件（双击打开的那种），统一成 Windows 记事本友好的写法
TEXT_WITH_BOM = ["操作指南.txt"]
TEXT_WITHOUT_BOM = ["安装到Anki.cmd", "测试.cmd"]


def normalize_text(path, bom=True):
    """统一成 UTF-8（可选 BOM）+ CRLF，避免记事本里乱码或整段挤成一行。"""
    with open(path, "rb") as fh:
        data = fh.read()
    text = data.decode("utf-8-sig")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    with open(path, "wb") as fh:
        fh.write(("\ufeff" if bom else "").encode("utf-8") + text.encode("utf-8"))


def tidy_text_files():
    """整理文本编码，并在根目录放一份操作指南副本，方便直接双击打开。"""
    for name in TEXT_WITH_BOM:
        path = os.path.join(SRC, *name.split("/"))
        if os.path.isfile(path):
            normalize_text(path, bom=True)
    guide = os.path.join(SRC, "操作指南.txt")
    if os.path.isfile(guide):
        shutil.copyfile(guide, os.path.join(HERE, "操作指南.txt"))
    for name in TEXT_WITHOUT_BOM:
        path = os.path.join(HERE, *name.split("/"))
        if os.path.isfile(path):
            normalize_text(path, bom=False)


def default_addons_dir():
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return os.path.join(appdata, "Anki2", "addons21")
    for candidate in (
        os.path.expanduser("~/Library/Application Support/Anki2/addons21"),
        os.path.expanduser("~/.local/share/Anki2/addons21"),
    ):
        if os.path.isdir(candidate):
            return candidate
    return None


def write_zip(path, entries):
    """entries: [(zip 内路径, 磁盘上的绝对路径)]

    压缩包里的时间戳固定成一个常量，这样同样的输入每次打出来的包字节完全一致；
    以后重建只要哈希不变，就能确定内容没动过。
    """
    if os.path.exists(path):
        os.remove(path)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, disk_path in entries:
            info = zipfile.ZipInfo(arcname, date_time=(2026, 9, 25, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(disk_path, "rb") as fh:
                z.writestr(info, fh.read())
    print("已打包：%s（%d 字节）" % (path, os.path.getsize(path)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--addons", help="Anki 的 addons21 目录")
    parser.add_argument("--package-only", action="store_true", help="只重新打包，不安装")
    parser.add_argument(
        "--no-source-zip", action="store_true", help="不生成源码压缩包"
    )
    args = parser.parse_args()

    if not os.path.isdir(SRC):
        raise SystemExit("找不到「源码」目录：" + SRC)

    missing = [
        f for f in ADDON_FILES if not os.path.isfile(os.path.join(SRC, *f.split("/")))
    ]
    if missing:
        raise SystemExit("源码目录缺少文件：" + "、".join(missing))

    # 0) 整理文本编码，并生成根目录的操作指南副本
    tidy_text_files()

    # 1) 打包 .ankiaddon
    write_zip(
        PACKAGE,
        [
            (rel, os.path.join(SRC, *rel.split("/")))
            for rel in ADDON_FILES
        ],
    )

    # 2) 顺便打包一份源码（含安装脚本和测试脚本）
    if not args.no_source_zip:
        entries = [
            (rel, os.path.join(SRC, *rel.split("/"))) for rel in ADDON_FILES
        ]
        for rel in SOURCE_EXTRA:
            disk = os.path.join(HERE, *rel.split("/"))
            if os.path.isfile(disk):
                entries.append((rel, disk))
        write_zip(SOURCE_ZIP, entries)

    if args.package_only:
        return

    # 3) 安装到 addons21
    addons = args.addons or default_addons_dir()
    if not addons or not os.path.isdir(addons):
        raise SystemExit("找不到 Anki 的 addons21 目录，请用 --addons 指定")
    target = os.path.join(addons, FOLDER_NAME)
    os.makedirs(target, exist_ok=True)
    for rel in ADDON_FILES:
        dst = os.path.join(target, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(SRC, *rel.split("/")), dst)
    print("已安装到：%s" % target)
    print("重启 Anki 后生效。")


if __name__ == "__main__":
    main()
