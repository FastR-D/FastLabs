import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from fastcas import FastCASError

from server import Store, FastLabHandler
from web_accounts import WebAccounts, AccountError


class Provider:
    config = SimpleNamespace(issuer='https://cas.test', client_id='labs')
    def __init__(self):
        self.identity = dict(issuer=self.config.issuer, subject='admin-subject', email='cas@example.test', email_verified=True, name='CAS Admin', role='admin')
        self.transaction = dict(purpose='login')
        self.link = None
    def finish_login(self, *args, **kwargs):
        return dict(identity=self.identity, transaction=self.transaction)
    def resolve_link(self, subject):
        if not self.link:
            raise FastCASError('authentication_required', 'No link', 401)
        return self.link
    def begin_registration(self, binding, *, new_local_account_ref):
        self.transaction = dict(purpose='register', local_account_ref=new_local_account_ref)
        return 'https://cas.test/authorize'
    def prepare_link(self, result):
        self.link = dict(id='link', client_id='labs', subject=result['identity']['subject'], local_account_ref=result['transaction']['local_account_ref'], state='prepared', version=1)
        return self.link
    def activate_link(self, link_id):
        self.link = {**self.link, 'state': 'active', 'version': 2}
        return self.link


def test_cas_registration_promotion_and_account_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('FASTLAB_PUBLIC_URL', 'https://labs.test')
    provider = Provider(); accounts = WebAccounts(Store(tmp_path/'labs.db'), provider)
    assert accounts.finish('callback', 'binding', None) == 'register'
    data = dict(email='cas@example.test', name='Admin', password='account-password-long', role='admin')
    with pytest.raises(AccountError):
        accounts.begin_registration('wrong-browser', data)
    binding, _ = accounts.begin_registration('binding', data)
    raw, _ = accounts.finish('callback', binding, None)
    user = accounts.session(raw)
    assert user['role'] == 'admin'
    assert accounts.login(data, 'peer')
    with accounts.write() as db:
        db.execute("UPDATE web_users SET role='user'")
    provider.transaction = dict(purpose='login')
    raw, _ = accounts.finish('callback', 'new-login', None)
    assert accounts.session(raw)['role'] == 'admin'
    provider.identity['role'] = 'member'
    accounts.finish('callback', 'new-login', None)
    assert accounts.session(raw)['role'] == 'admin'
    with accounts.write() as db:
        db.execute("UPDATE web_users SET status='disabled'")
    with pytest.raises(AccountError):
        accounts.finish('callback', 'new-login', None)


def test_registration_rejects_identity_switch(tmp_path, monkeypatch):
    monkeypatch.setenv('FASTLAB_PUBLIC_URL', 'https://labs.test')
    provider = Provider(); accounts = WebAccounts(Store(tmp_path/'labs.db'), provider)
    accounts.finish('callback', 'binding', None)
    binding, _ = accounts.begin_registration('binding', dict(email='new@example.test', password='account-password-long'))
    provider.identity['subject'] = 'other'
    with pytest.raises(AccountError):
        accounts.finish('callback', binding, None)
    assert not accounts.all('SELECT id FROM web_users')


def test_hosted_http_admin_and_task_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv('FASTLAB_PUBLIC_URL', 'http://localhost')
    store = Store(tmp_path/'labs.db'); accounts = WebAccounts(store)
    admin = accounts.prepare(dict(email='admin@example.test', name='Admin', password='admin-password-long', role='admin'))
    with accounts.write() as db:
        accounts.insert(db, admin)
        for task_id in ['a'*32, 'b'*32]:
            db.execute("INSERT INTO tasks(id,title,goal,workspace,status,created_at,updated_at) VALUES(?,?,?,'/tmp','completed','','')", (task_id, task_id, 'Goal'))
    app = SimpleNamespace(web_accounts=accounts, list_payload=lambda:[{'id':'a'*32},{'id':'b'*32}], task_payload=lambda value:{'id':value}, fastcas_pairing=SimpleNamespace(csrf='local-proof'))
    handler = type('Handler', (FastLabHandler,), {'app':app, 'log_message':lambda *args:None})
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    accounts.origin = 'http://127.0.0.1:'+str(server.server_port)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    try:
        with httpx.Client(base_url=accounts.origin, trust_env=False) as client:
            assert client.get('/api/health').status_code == 200
            assert client.get('/api/tasks').status_code == 401
            csrf = client.get('/api/auth/session-init').json()['csrf']
            client.headers.update({'Origin':accounts.origin,'X-CSRF-Token':csrf})
            assert client.post('/api/auth/register', json={}).status_code == 403
            result = client.post('/api/auth/login', json={'email':admin['email'],'password':'admin-password-long'})
            assert result.status_code == 200
            client.headers['X-CSRF-Token'] = result.json()['csrf']
            data = dict(email='member@example.test', name='Member', password='member-password-long', reason='Onboarding')
            response = client.post('/api/admin/users', json=data)
            assert response.status_code == 201 and 'set-cookie' not in response.headers and 'password' not in response.json()
            member = response.json()
            with accounts.write() as db:
                db.execute('INSERT INTO web_task_owners VALUES(?,?)', ('a'*32,member['id']))
            with httpx.Client(base_url=accounts.origin, trust_env=False) as other:
                pre = other.get('/api/auth/session-init').json()['csrf']
                other.headers.update({'Origin':accounts.origin,'X-CSRF-Token':pre})
                logged = other.post('/api/auth/login', json=data)
                other.headers['X-CSRF-Token'] = logged.json()['csrf']
                assert other.get('/api/admin/users').status_code == 403
                assert other.get('/api/tasks').json() == {'tasks':[{'id':'a'*32}]}
                assert other.get('/api/tasks/'+'b'*32).status_code == 403
                assert other.post('/api/repositories', json={}).status_code == 403
                assert client.put('/api/admin/users/'+member['id'], json={**data,'status':'disabled'}).status_code == 200
                assert other.get('/api/tasks').status_code == 401
            assert client.request('DELETE','/api/admin/users/'+admin['id'],json={'reason':'delete'}).status_code == 409
            assert client.request('DELETE','/api/admin/users/'+member['id'],json={'reason':'delete'}).status_code == 200
    finally:
        server.shutdown(); server.server_close(); worker.join()
