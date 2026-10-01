"""Other hostnames for the site redirect to birdsconferring.com, keeping the path and query."""
import pytest


@pytest.mark.parametrize('host', ['birdsconferring.fly.dev', 'www.birdsconferring.com'])
def test_alias_hosts_redirect(app_module, host):
    resp = app_module.app.test_client().get('/radio?seed=42', base_url=f'https://{host}')
    assert resp.status_code == 301
    assert resp.headers['Location'] == 'https://birdsconferring.com/radio?seed=42'


def test_forwarded_proto_is_kept(app_module):
    # fly's proxy terminates TLS and says so in X-Forwarded-Proto
    resp = app_module.app.test_client().get('/shows', base_url='http://www.birdsconferring.com',
                                            headers={'X-Forwarded-Proto': 'https'})
    assert resp.headers['Location'] == 'https://birdsconferring.com/shows'


@pytest.mark.parametrize('host', ['birdsconferring.com', 'localhost:5000', '127.0.0.1:5000'])
def test_other_hosts_are_served(app_module, host):
    resp = app_module.app.test_client().get('/shows', base_url=f'http://{host}')
    assert resp.status_code == 200
