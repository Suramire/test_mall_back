import io
from .conftest import TENANT_A, assert_code


def test_goods_batch_status_validation_and_tenant_isolation(client, headers_a, seed_tenants):
    body = assert_code(client.post('/api/mc/goods/batch-status', headers=headers_a,
                                   json={'ids':[seed_tenants['goods_a'], seed_tenants['goods_b']], 'status':'OFF_SALE'}), 0)['data']
    assert len(body['success']) == 1 and len(body['failed']) == 1
    assert assert_code(client.post('/api/mc/goods/batch-status', headers=headers_a,
                                   json={'ids':[101], 'status':'BAD'}), 40001)['code'] == 40001


def test_goods_export_and_import_csv(client, headers_a, seed_tenants, db_session):
    exported = client.get('/api/mc/goods/export', headers=headers_a)
    assert exported.status_code == 200 and exported.content.startswith(b'\xef\xbb\xbf')
    assert b'name' in exported.content and b'channel' in exported.content
    imported = client.post('/api/mc/goods/import', headers=headers_a,
                           files={'file': ('goods.csv', io.BytesIO(b'name,type,channel\nCSVQA,PHYSICAL,NORMAL\n'), 'text/csv')})
    data = assert_code(imported, 0)['data']
    assert data['total'] == 1 and data['success'] == 1 and data['fail'] == 0
    bad = client.post('/api/mc/goods/import', headers=headers_a,
                      files={'file': ('goods.csv', io.BytesIO(b'name,type,channel\n=CMD,PHYSICAL,NORMAL\n'), 'text/csv')})
    assert assert_code(bad, 0)['data']['fail'] == 1
