import io
from .conftest import assert_code


def test_verify_log_paging_filter_export(client, headers_a, seed_tenants):
    data = assert_code(client.get('/api/mc/verify/log?page=1&size=1', headers=headers_a), 0)['data']
    assert 'list' in data and data['size'] == 1
    out = client.get('/api/mc/verify/log/export?code=HX-USED-A', headers=headers_a)
    assert out.status_code == 200 and out.content.startswith(b'\xef\xbb\xbf')


def test_points_import_requires_idempotency_and_tags_persist(client, headers_a, seed_tenants):
    csv = b'memberId,points,remark,idempotencyKey\n201,5,QA,tag-batch-1\n'
    no_key = client.post('/api/mc/points/import', headers=headers_a, files={'file': ('p.csv', io.BytesIO(csv), 'text/csv')})
    assert assert_code(no_key, 40001)['code'] == 40001
    first = client.post('/api/mc/points/import', headers={**headers_a, 'Idempotency-Key':'batch-test-1'}, files={'file': ('p.csv', io.BytesIO(csv), 'text/csv')})
    body = assert_code(first, 0)['data']
    again = client.post('/api/mc/points/import', headers={**headers_a, 'Idempotency-Key':'batch-test-1'}, files={'file': ('p.csv', io.BytesIO(csv), 'text/csv')})
    assert assert_code(again, 0)['data']['batchId'] == body['batchId']
    tags = assert_code(client.put('/api/mc/member/201/tags', headers=headers_a, json={'tags':['A','B']*15}), 0)['data']
    assert len(tags['tags']) == 2
