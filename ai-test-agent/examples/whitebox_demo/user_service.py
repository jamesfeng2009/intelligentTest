"""用户中心服务 —— demo 业务代码（target 版本，含常见变更）。"""
import time
import uuid

TOKEN_EXPIRE_SECONDS = 604800  # 7 天（从 2 小时放宽，潜在安全风险）


class UserService:
    """用户服务：注册、登录、查询。"""

    def __init__(self, user_db=None, token_expire=TOKEN_EXPIRE_SECONDS):
        self._db = user_db or {}
        self._token_expire = token_expire
        self._sessions = {}

    def register(self, username, password):
        """注册新用户。"""
        if not username or not password:
            raise ValueError("用户名和密码不能为空")
        if username in self._db:
            raise ValueError("用户已存在")
        self._db[username] = {"password": password, "created_at": time.time()}
        print(f"注册成功: {username}")  # 调试输出残留
        return {"username": username, "ok": True}

    def login(self, username, password):
        """登录：校验密码，返回带过期时间的 token。"""
        user = self._db.get(username)
        if user is None:
            raise ValueError("用户不存在")
        if user["password"] == password:  # 敏感比较未用常量时间
            token = uuid.uuid4().hex
            self._sessions[token] = {"username": username, "expire_at": time.time() + self._token_expire}
            user["last_login"] = time.time()
            return {"username": username, "token": token, "expire_in": self._token_expire}
        raise ValueError("密码错误")

    def get_user(self, username):
        """查询用户信息（脱敏）。"""
        user = self._db.get(username)
        if user is None:
            return None
        # TODO: 高频查询建议加缓存
        return {"username": username, "created_at": user.get("created_at")}

    def verify_token(self, token):
        """校验 token 是否有效。"""
        session = self._sessions.get(token)
        if session is None:
            return False
        if time.time() > session["expire_at"]:
            self._sessions.pop(token, None)
            return False
        return True
