"""从 Anki 编译好的 .pyc 里抓字符串，用来确认内部接口名称。

Anki 26.9.3 只随包提供 .pyc（Python 3.13），本机可用的解释器是 3.10，
没法直接 marshal 反序列化，所以这里用朴素的办法：把字节流里能读出来的
可打印 ASCII 片段全部找出来，再按关键字过滤。

用法：python 工具/dump_pyc_strings.py <pyc 路径> <关键字> [关键字...]
"""

import pathlib
import re
import sys

PRINTABLE = re.compile(rb"[\x20-\x7e]{4,}")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    path = pathlib.Path(sys.argv[1])
    needles = sys.argv[2:]
    data = path.read_bytes()
    hits = [m.group().decode("ascii") for m in PRINTABLE.finditer(data)]
    hits = [t for t in hits if any(n in t for n in needles)]
    for text in hits:
        print(text)
    print(f"--- {path.name}: {len(hits)} 条命中 ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
