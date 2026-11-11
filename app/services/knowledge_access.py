"""个人手册复用现有tenant过滤，使用服务器派生的独立命名空间。"""
import hashlib


def personal_namespace(user_id: str) -> str:
    return "personal-" + hashlib.sha256(user_id.encode()).hexdigest()[:32]


def readable_namespaces(user):
    return (user.tenant_id, personal_namespace(user.user_id))
