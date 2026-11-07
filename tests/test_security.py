import io

import app as nm


def _first_id(client):
    nm.save_notification('SecTest', 'row', str(id(client)))
    return client.get('/notifications').get_json()[0]['id']


def test_index_embeds_token(client, token):
    r = client.get('/')
    assert r.status_code == 200
    assert token.encode() in r.data


def test_foreign_host_rejected(client):
    # DNS-rebinding: the browser sends the attacker's hostname in Host
    r = client.get('/notifications', base_url='http://evil.example:5000')
    assert r.status_code == 403


def test_loopback_hosts_allowed(client):
    for base in ('http://localhost:5000', 'http://127.0.0.1:5000', 'http://[::1]:5000'):
        assert client.get('/notifications', base_url=base).status_code == 200, base


def test_state_change_requires_token(client, token):
    nid = _first_id(client)
    url = f'/notifications/{nid}/check'
    assert client.patch(url, json={'checked': True}).status_code == 403
    assert client.patch(url, json={'checked': True}, headers={'X-NM-Token': 'wrong'}).status_code == 403
    assert client.patch(url, json={'checked': True}, headers={'X-NM-Token': token}).status_code == 200


def test_cross_origin_rejected_even_with_token(client, token):
    nid = _first_id(client)
    r = client.patch(f'/notifications/{nid}/comment', json={'comment': 'x'},
                     headers={'X-NM-Token': token, 'Origin': 'https://evil.example'})
    assert r.status_code == 403


def test_server_control_requires_token(client):
    # Without the token these must be refused (and must not exit the test process)
    assert client.post('/server/stop').status_code == 403
    assert client.post('/server/restart').status_code == 403


def test_wallpaper_rules(client, token):
    h = {'X-NM-Token': token}
    r = client.post('/wallpaper', headers=h, data={'file': (io.BytesIO(b'<svg/>'), 'x.svg')})
    assert r.status_code == 400
    big = io.BytesIO(b'0' * (nm.MAX_UPLOAD_BYTES + 1))
    r = client.post('/wallpaper', headers=h, data={'file': (big, 'x.png')})
    assert r.status_code == 413
    r = client.post('/wallpaper', headers=h, data={'file': (io.BytesIO(b'\x89PNG'), 'x.png')})
    assert r.status_code == 200
    assert client.get('/wallpaper').status_code == 200
    assert client.delete('/wallpaper', headers=h).status_code == 200
    assert client.get('/wallpaper').status_code == 404


def test_security_headers(client):
    r = client.get('/')
    assert r.headers['X-Frame-Options'] == 'DENY'
    assert r.headers['X-Content-Type-Options'] == 'nosniff'
    assert "frame-ancestors 'none'" in r.headers['Content-Security-Policy']
    assert 'Access-Control-Allow-Origin' not in r.headers


def test_token_persisted_with_private_permissions(token):
    import os
    import stat
    assert open(nm.TOKEN_PATH).read().strip() == token
    assert stat.S_IMODE(os.stat(nm.TOKEN_PATH).st_mode) == 0o600
