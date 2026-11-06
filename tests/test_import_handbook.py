"""导入脚本的纯函数测试。"""

import csv

from scripts.import_handbook import split_multi


def test_split_multi_handles_pipe_separated_values():
    assert split_multi("报销|发票|差旅") == ["报销", "发票", "差旅"]
    assert split_multi(" 单值 ") == ["单值"]
    assert split_multi("") == []
    assert split_multi(None) == []
    assert split_multi("a||b") == ["a", "b"]      # 空项被丢掉
