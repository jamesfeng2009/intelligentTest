"""通用工具函数（base 版本）。"""


def normalize_username(name):
    """规范化用户名：去空格、转小写。"""
    return name.strip().lower()


def is_valid_price(price):
    """校验价格合法性。"""
    return isinstance(price, (int, float)) and price > 0
