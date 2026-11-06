"""部门与资料可见性。

权限模型（参与者只有两类账号）：
  - 部门主管（数据库内部仍用 role=staff 兼容旧数据）
  - 新人（role=newcomer）

资料访问按两个维度判断：
  visibility：all=全员公开，department=本部门，staff=仅主管，sensitive=仅人事
  department：资料归属部门；通用资料不限制部门

新人只能看「全员公开」和「自己部门」；主管额外能看「仅主管」；
人事主管可以管理所有部门的资料；敏感资料只给人事主管，且不进问答检索。
"""

from database import ROLE_STAFF

HR_DEPARTMENT = "人力资源部"
GENERIC_DEPARTMENT = "通用"

VIS_ALL = "all"
VIS_DEPARTMENT = "department"
VIS_STAFF = "staff"
VIS_SENSITIVE = "sensitive"
VISIBILITIES = (VIS_ALL, VIS_DEPARTMENT, VIS_STAFF, VIS_SENSITIVE)


def normalize_visibility(value: str | None) -> str:
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in VISIBILITIES else VIS_ALL


def is_supervisor(role: str) -> bool:
    return role == ROLE_STAFF


def is_hr(role: str, department: str) -> bool:
    """人事主管：只有人力资源部的主管能跨部门管理和看敏感资料。"""
    return is_supervisor(role) and (department or "").strip() == HR_DEPARTMENT


def can_view(visibility: str, owner_department: str, role: str,
             department: str, include_sensitive: bool = False) -> bool:
    """某份资料当前账号能不能看。"""
    visibility = normalize_visibility(visibility)
    owner = (owner_department or GENERIC_DEPARTMENT).strip()
    mine = (department or "").strip()
    same_department = owner in ("", GENERIC_DEPARTMENT) or (mine and owner == mine)
    hr = is_hr(role, mine)
    unscoped_supervisor = is_supervisor(role) and not mine

    if visibility == VIS_ALL:
        return True
    if visibility == VIS_SENSITIVE:
        return include_sensitive and hr
    if visibility == VIS_DEPARTMENT:
        return bool(hr or same_department or unscoped_supervisor)
    if visibility == VIS_STAFF:
        return bool(hr or (is_supervisor(role) and (same_department or unscoped_supervisor)))
    return False


def visibility_clauses(role: str, department: str,
                       include_sensitive: bool = False) -> list[dict]:
    """给 langchain-postgres 使用的 $or 子句（配合 rag_chunks 的真实列过滤）。"""
    mine = (department or "").strip()
    clauses: list[dict] = [{"visibility": VIS_ALL}]

    if is_hr(role, mine):
        # 人事可以看所有部门的普通资料和主管资料
        clauses.append({"visibility": VIS_DEPARTMENT})
        clauses.append({"visibility": VIS_STAFF})
        if include_sensitive:
            clauses.append({"visibility": VIS_SENSITIVE})
        return clauses

    if mine:
        clauses.append({"$and": [{"visibility": VIS_DEPARTMENT},
                                 {"department": mine}]})
    if is_supervisor(role):
        if mine:
            clauses.append({"$and": [{"visibility": VIS_STAFF},
                                     {"department": mine}]})
        else:
            # 没有配置部门的旧账号/评测账号保持兼容：能看所有部门资料
            clauses.append({"visibility": VIS_DEPARTMENT})
            clauses.append({"visibility": VIS_STAFF})
    return clauses


def department_selectable(role: str, department: str, target: str) -> bool:
    """上传/导入时能不能把资料放到目标部门。人事可以任选，其他人只能选自己部门。"""
    target = (target or "").strip()
    if not target:
        return False
    if is_hr(role, department):
        return True
    return target == (department or "").strip()


def allowed_upload_visibilities(role: str, department: str) -> list[str]:
    """当前账号上传时能选择的可见性。"""
    if is_hr(role, department):
        return [VIS_ALL, VIS_DEPARTMENT, VIS_STAFF, VIS_SENSITIVE]
    if is_supervisor(role):
        return [VIS_ALL, VIS_DEPARTMENT, VIS_STAFF]
    return [VIS_ALL, VIS_DEPARTMENT]
