import pytest
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import reset, set_tenant
from app.core.exceptions import ParamError
from app.models.gd_goods import GdGoods
from app.models.sys_common import SysFile
from app.services.goods import _apply_goods_fields


def test_goods_internal_upload_reference_is_tenant_scoped(engine):
    Session = sessionmaker(bind=engine)
    set_tenant(1001)
    try:
        with Session() as session:
            session.add(SysFile(id=9001, tenant_id=1001, biz_type="goods", name="ok.png", url="/api/common/upload/file/ok.png", size=3, mime="image/png", uploader_id=10))
            session.add(SysFile(id=9002, tenant_id=2002, biz_type="goods", name="other.png", url="/api/common/upload/file/other.png", size=3, mime="image/png", uploader_id=20))
            session.commit()
            good = GdGoods(tenant_id=1001, name="测试", type="PHYSICAL", channel="NORMAL")
            _apply_goods_fields(session, good, {"mainImage": "/api/common/upload/file/ok.png"})
            assert good.main_image.endswith("ok.png")
            for url in ("/api/common/upload/file/other.png", "/api/common/upload/file/missing.png"):
                with pytest.raises(ParamError):
                    _apply_goods_fields(session, GdGoods(tenant_id=1001, name="测试", type="PHYSICAL", channel="NORMAL"), {"mainImage": url})
            # 外部图片按现有策略允许。
            _apply_goods_fields(session, GdGoods(tenant_id=1001, name="测试", type="PHYSICAL", channel="NORMAL"), {"mainImage": "https://cdn.example/a.png"})
    finally:
        reset()
