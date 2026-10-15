# 使 tests 成为常规包:site-packages 中存在同名 ultralytics `tests` 包,
# 缺 __init__.py 时命名空间包会被其遮蔽,导致 `from tests.xxx import` 失败。
