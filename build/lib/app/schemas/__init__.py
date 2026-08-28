"""平台端 Pydantic 模型（请求/响应）。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------- 认证 ----------
class PlatformLoginReq(BaseModel):
    account: str
    password: str


class TokenResp(BaseModel):
    accessToken: str
    refreshToken: str
    expiresIn: int
    user: dict


class ChangePasswordReq(BaseModel):
    oldPassword: str
    newPassword: str
    confirmPassword: str | None = None


# ---------- 商家（租户）管理 ----------
class OpenAccountReq(BaseModel):
    name: str
    contactName: str = ""
    contactPhone: str = ""
    qualification: str = ""
    status: str = "TRIAL"
    expireAt: date | None = None
    goodsLimit: int = 0
    memberLimit: int = 0
    storeLimit: int = 0
    staffLimit: int = 0
    wxAppid: str = ""
    wxSecret: str = ""
    features: list[str] = Field(default_factory=list)
    adminAccount: str = "admin"
    adminName: str = "超级管理员"
    adminPhone: str = ""
    remark: str = ""


class UpdateTenantReq(BaseModel):
    name: str | None = None
    contactName: str | None = None
    contactPhone: str | None = None
    qualification: str | None = None
    status: str | None = None
    expireAt: date | None = None
    goodsLimit: int | None = None
    memberLimit: int | None = None
    storeLimit: int | None = None
    staffLimit: int | None = None
    wxAppid: str | None = None
    wxSecret: str | None = None
    remark: str | None = None


class TenantListItem(BaseModel):
    id: int
    tenantNo: str
    name: str
    status: str
    expireAt: date | None = None
    goodsLimit: int = 0
    memberLimit: int = 0
    staffLimit: int = 0
    storeLimit: int = 0
    goodsUsed: int = 0
    memberUsed: int = 0


class TenantDetail(BaseModel):
    id: int
    tenantNo: str
    name: str
    contactName: str = ""
    contactPhone: str = ""
    qualification: str = ""
    status: str
    expireAt: date | None = None
    goodsLimit: int = 0
    memberLimit: int = 0
    storeLimit: int = 0
    staffLimit: int = 0
    wxAppid: str = ""
    wxAuthStatus: int = 0
    permVer: int = 1
    remark: str | None = None
    openedAt: datetime | None = None
    createdAt: datetime | None = None
    featureCount: int = 0
    staffCount: int = 0


class OpenAccountResp(BaseModel):
    id: int
    tenantNo: str
    adminInitPassword: str


class RenewReq(BaseModel):
    expireAt: date


class ImpersonateResp(BaseModel):
    redirectUrl: str
    ticket: str


# ---------- 功能点树 ----------
class FeatureNode(BaseModel):
    code: str
    name: str
    defaultOn: int = 1


class FeatureGroup(BaseModel):
    l2: str
    items: list[FeatureNode]


class FeatureTreeResp(BaseModel):
    end: str
    l1: str
    groups: list[FeatureGroup]


# ---------- 角色 ----------
class RoleReq(BaseModel):
    name: str
    remark: str = ""
    perms: list[str] = Field(default_factory=list)


class RoleItem(BaseModel):
    id: int
    name: str
    remark: str = ""
    perms: list[str] = Field(default_factory=list)
    isSystem: int = 0


# ---------- 员工 ----------
class StaffReq(BaseModel):
    account: str
    name: str
    password: str
    phone: str = ""
    roleId: int


class StaffItem(BaseModel):
    id: int
    account: str
    name: str
    phone: str = ""
    roleId: int
    roleName: str = ""
    status: str = "ENABLED"
    lastLoginAt: datetime | None = None


# ---------- 审计 ----------
class AuditItem(BaseModel):
    id: int
    operatorId: int
    operatorName: str
    scope: str
    tenantId: int | None = None
    action: str
    targetType: str = ""
    targetId: str = ""
    detail: Any | None = None
    ip: str = ""
    createdAt: datetime | None = None
