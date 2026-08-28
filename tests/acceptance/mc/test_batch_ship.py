from .conftest import assert_code


def test_batch_ship_mixed_empty_express_and_state_readback(client, headers_a, seed_tenants):
    data = assert_code(client.post('/api/mc/order/batch-ship', headers=headers_a,
                                   json={'ids':[1003, 999999], 'expressCompany':'', 'expressNo':''}), 0)['data']
    assert any(x['id'] == 1003 for x in data['success'])
    assert any(x['id'] == 999999 for x in data['failed'])
    detail = assert_code(client.get('/api/mc/order/1003', headers=headers_a), 0)['data']
    assert detail['status'] == 'SHIPPED'


def test_batch_ship_cross_tenant_and_invalid_payload(client, headers_a, seed_tenants):
    assert assert_code(client.post('/api/mc/order/batch-ship', headers=headers_a,
                                   json={'ids':[2001], 'status':'PAID'}), 0)['data']['failed']
    assert assert_code(client.post('/api/mc/order/batch-ship', headers=headers_a,
                                   json={'ids':[]}), 40001)['code'] == 40001
