from .conftest import assert_code


def test_dashboard_trend_days_and_rank_real_data(client, headers_a, seed_tenants):
    for days in (7, 30, 90):
        data = assert_code(client.get(f'/api/mc/dashboard/trend?days={days}', headers=headers_a), 0)['data']
        assert len(data) == days
    assert assert_code(client.get('/api/mc/dashboard/trend?days=8', headers=headers_a), 40001)['code'] == 40001
    goods = assert_code(client.get('/api/mc/dashboard/goods-rank?limit=1', headers=headers_a), 0)['data']
    assert len(goods) <= 1
    members = assert_code(client.get('/api/mc/dashboard/member-rank?limit=1', headers=headers_a), 0)['data']
    assert len(members) <= 1
    # 租户 B 的商品不能出现在租户 A 排行。
    assert all(x.get('goodsId') != seed_tenants['goods_b'] for x in goods)
