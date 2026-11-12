"""Web accounts for hosted FastLabs; loopback installations remain local."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse


class AccountError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 256:
        raise AccountError(400, '密码需要 12–256 个字符')
    salt = secrets.token_hex(16)
    return salt + '$' + hashlib.scrypt(value.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def password_matches(value, encoded):
    try:
        if not isinstance(value, str) or len(value) > 256:
            return False
        salt, expected = encoded.split('$')
        actual = hashlib.scrypt(value.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class WebAccounts:
    def __init__(self, store, sdk=None):
        self.store = store
        self.origin = os.environ.get('FASTLAB_PUBLIC_URL', '').strip().rstrip('/')
        self.enabled = bool(self.origin)
        self.issuer = os.environ.get('FASTLAB_WEB_FASTCAS_ISSUER', '').strip().rstrip('/')
        self.client_id = os.environ.get('FASTLAB_WEB_FASTCAS_CLIENT_ID', '').strip()
        self.redirect_uri = self.origin + '/api/auth/fastcas/callback'
        self.sdk = sdk
        if not self.enabled:
            return
        parsed = urlparse(self.origin)
        if parsed.scheme not in {'https', 'http'} or not parsed.netloc or parsed.path or parsed.username:
            raise ValueError('FASTLAB_PUBLIC_URL must be an origin')
        if parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('Hosted FastLabs requires HTTPS')
        with self.write() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS web_users(id TEXT PRIMARY KEY,email TEXT NOT NULL UNIQUE,name TEXT NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'user',status TEXT NOT NULL DEFAULT 'active');
                CREATE TABLE IF NOT EXISTS web_sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES web_users(id),csrf TEXT NOT NULL,expires REAL NOT NULL,source TEXT NOT NULL,link_id TEXT,checked REAL NOT NULL DEFAULT 0,sid TEXT);
                CREATE TABLE IF NOT EXISTS web_cas_transactions(state TEXT PRIMARY KEY,payload TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS web_cas_signups(binding TEXT PRIMARY KEY,subject TEXT NOT NULL,email TEXT NOT NULL,name TEXT NOT NULL,expires REAL NOT NULL,prepared TEXT);
                CREATE TABLE IF NOT EXISTS web_cas_links(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES web_users(id),issuer TEXT NOT NULL,subject TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL,payload TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS web_cas_user_live ON web_cas_links(issuer,user_id) WHERE state!='revoked';
                CREATE UNIQUE INDEX IF NOT EXISTS web_cas_subject_live ON web_cas_links(issuer,subject) WHERE state!='revoked';
                CREATE TABLE IF NOT EXISTS web_cas_events(id TEXT PRIMARY KEY,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS web_account_audit(id TEXT PRIMARY KEY,actor TEXT,target TEXT,action TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS web_task_owners(task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,user_id TEXT NOT NULL REFERENCES web_users(id));
                CREATE TABLE IF NOT EXISTS web_login_limits(key TEXT PRIMARY KEY,attempts INTEGER NOT NULL,expires REAL NOT NULL);
            ''')
        if sdk is None and self.issuer:
            from fastcas import Configuration, FastCAS
            secret = os.environ.get('FASTLAB_WEB_FASTCAS_CLIENT_SECRET', '')
            if not self.client_id or not secret:
                raise ValueError('Hosted FastCAS requires a client ID and secret')
            self.sdk = FastCAS(Configuration(issuer=self.issuer, client_id=self.client_id, client_secret=secret, redirect_uri=self.redirect_uri,
                                            allow_loopback_http=os.environ.get('FASTLAB_FASTCAS_ALLOW_LOOPBACK_HTTP') == 'true'), self)
        if sdk is not None:
            self.issuer, self.client_id = sdk.config.issuer, sdk.config.client_id
        bootstrap_email = os.environ.get('FASTLAB_ADMIN_EMAIL', '')
        bootstrap_password = os.environ.get('FASTLAB_ADMIN_PASSWORD', '')
        if bootstrap_email and bootstrap_password and not self.one('SELECT id FROM web_users LIMIT 1'):
            user = self.prepare(dict(email=bootstrap_email, password=bootstrap_password, name='管理员', role='admin'))
            with self.write() as db:
                self.insert(db, user)

    @contextmanager
    def write(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            yield db

    def one(self, sql, values=()):
        with self.store.connect() as db:
            row = db.execute(sql, values).fetchone()
            return dict(row) if row else None

    def all(self, sql, values=()):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(sql, values)]

    def put(self, transaction):
        with self.write() as db:
            db.execute('DELETE FROM web_cas_transactions WHERE expires<=?', (time.time(),))
            db.execute('INSERT INTO web_cas_transactions VALUES(?,?,?)', (transaction['state'], json.dumps(transaction), transaction['expires_at']))

    def take(self, state):
        with self.write() as db:
            row = db.execute('SELECT payload FROM web_cas_transactions WHERE state=?', (state,)).fetchone()
            db.execute('DELETE FROM web_cas_transactions WHERE state=?', (state,))
            return json.loads(row[0]) if row else None

    def audit(self, db, actor, target, action, reason=''):
        db.execute('INSERT INTO web_account_audit VALUES(?,?,?,?,?,?)', (secrets.token_hex(16), actor, target, action, reason, time.time()))

    @staticmethod
    def public(user):
        return {key: user[key] for key in ('id', 'email', 'name', 'role', 'status')}

    @staticmethod
    def prepare(data):
        email = str(data.get('email', '')).strip().lower()
        name = str(data.get('name', '')).strip() or email
        if len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or len(name) > 120:
            raise AccountError(400, '请填写有效邮箱和姓名')
        role, status = data.get('role', 'user'), data.get('status', 'active')
        if role not in {'user', 'admin'} or status not in {'active', 'disabled'}:
            raise AccountError(400, '角色或状态无效')
        return dict(id=secrets.token_hex(16), email=email, name=name, password=password_hash(data['password']) if data.get('password') else '', role=role, status=status)

    @staticmethod
    def insert(db, user):
        if not user['password']:
            raise AccountError(400, '请设置本地密码')
        if db.execute('SELECT id FROM web_users WHERE email=?', (user['email'],)).fetchone():
            raise AccountError(409, '此邮箱已有账号，请登录后绑定 FastCAS')
        db.execute('INSERT INTO web_users VALUES(:id,:email,:name,:password,:role,:status)', user)

    def issue(self, db, user_id, source='local', link=None, identity=None):
        if not db.execute("SELECT id FROM web_users WHERE id=? AND status='active'", (user_id,)).fetchone():
            raise AccountError(403, '账号已停用')
        raw = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        db.execute('INSERT INTO web_sessions VALUES(?,?,?,?,?,?,?,?)', (digest(raw), user_id, csrf, time.time()+7*86400, source, link['id'] if link else None, time.time(), (identity or {}).get('sid')))
        return raw, csrf

    def session(self, raw):
        row = self.one("SELECT s.*,u.email,u.name,u.role,u.status FROM web_sessions s JOIN web_users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.status='active'", (digest(raw), time.time()))
        if row and row['source'] == 'fastcas':
            link = self.one("SELECT * FROM web_cas_links WHERE id=? AND issuer=? AND state='active'", (row['link_id'], self.issuer))
            if not link or not self.sdk:
                return None
            if row['checked']+300 < time.time():
                from fastcas import FastCASError
                try:
                    remote = self.sdk.resolve_link(link['subject'])
                except FastCASError:
                    return None
                if any(remote.get(key) != json.loads(link['payload']).get(key) for key in ('id', 'client_id', 'subject', 'local_account_ref', 'version', 'state')):
                    return None
                with self.write() as db:
                    db.execute('UPDATE web_sessions SET checked=? WHERE token=?', (time.time(), row['token']))
        return row

    def login(self, data, peer):
        email = str(data.get('email', '')).strip().lower()
        key = digest(email+'|'+peer)
        with self.write() as db:
            row = db.execute('SELECT * FROM web_login_limits WHERE key=?', (key,)).fetchone()
            attempts = row['attempts']+1 if row and row['expires'] > time.time() else 1
            db.execute('INSERT OR REPLACE INTO web_login_limits VALUES(?,?,?)', (key, attempts, row['expires'] if row and row['expires'] > time.time() else time.time()+900))
        if attempts > 12:
            raise AccountError(429, '登录尝试过多，请稍后重试')
        user = self.one('SELECT * FROM web_users WHERE email=?', (email,))
        if not user or user['status'] != 'active' or not password_matches(data.get('password', ''), user['password']):
            raise AccountError(401, '邮箱或密码错误')
        with self.write() as db:
            live = db.execute('SELECT password FROM web_users WHERE id=?', (user['id'],)).fetchone()
            if not live or live[0] != user['password']:
                raise AccountError(401, '账号状态已改变，请重新登录')
            result = self.issue(db, user['id'])
            db.execute('DELETE FROM web_login_limits WHERE key=?', (key,))
            return result

    def manage(self, actor, user_id, method, data):
        if actor['role'] != 'admin':
            raise AccountError(403, '需要管理员权限')
        reason = str(data.get('reason', '')).strip()
        if not reason:
            raise AccountError(400, '请填写操作原因')
        user = self.prepare(data) if method != 'DELETE' else None
        with self.write() as db:
            if method == 'POST':
                self.insert(db, user)
                user_id = user['id']
            else:
                old = db.execute("SELECT * FROM web_users WHERE id=? AND status!='deleted'", (user_id,)).fetchone()
                if not old:
                    raise AccountError(404, '用户不存在')
                removing = method == 'DELETE' or user['role'] != 'admin' or user['status'] != 'active'
                if removing and user_id == actor['user_id']:
                    raise AccountError(409, '不能删除、停用或降级当前管理员')
                if removing and old['role'] == 'admin' and old['status'] == 'active' and not db.execute("SELECT id FROM web_users WHERE role='admin' AND status='active' AND id!=?", (user_id,)).fetchone():
                    raise AccountError(409, '必须保留至少一名启用的管理员')
                if method == 'DELETE':
                    db.execute("UPDATE web_users SET status='deleted',password='',email=?,name='已删除用户',role='user' WHERE id=?", (user_id+'@deleted.invalid', user_id))
                else:
                    if db.execute('SELECT id FROM web_users WHERE email=? AND id!=?', (user['email'], user_id)).fetchone():
                        raise AccountError(409, '邮箱已使用')
                    db.execute('UPDATE web_users SET email=?,name=?,password=?,role=?,status=? WHERE id=?', (user['email'], user['name'], user['password'] or old['password'], user['role'], user['status'], user_id))
                if method == 'DELETE' or user['password'] or any(user[k] != old[k] for k in ('email', 'role', 'status')):
                    db.execute('DELETE FROM web_sessions WHERE user_id=?', (user_id,))
            self.audit(db, actor['user_id'], user_id, 'user.'+method.lower(), reason)
            return self.public(db.execute('SELECT * FROM web_users WHERE id=?', (user_id,)).fetchone())

    def pending(self, binding, prepared=False):
        row = self.one('SELECT * FROM web_cas_signups WHERE binding=? AND expires>?', (digest(binding), time.time()))
        if not row or not binding or bool(row['prepared']) != prepared:
            raise AccountError(401, 'FastCAS 验证已过期，请重新登录')
        return row

    def begin_registration(self, binding, data):
        pending = self.pending(binding)
        user = self.prepare({**data, 'role': 'user', 'status': 'active'})
        if not user['password']:
            raise AccountError(400, '请设置本地密码')
        if self.one('SELECT id FROM web_users WHERE email=?', (user['email'],)):
            raise AccountError(409, '此邮箱已有账号，请登录后绑定 FastCAS')
        next_binding = secrets.token_urlsafe(32)
        url = self.sdk.begin_registration(next_binding, new_local_account_ref=user['id'])
        with self.write() as db:
            changed = db.execute('UPDATE web_cas_signups SET binding=?,prepared=? WHERE binding=? AND prepared IS NULL AND expires>?', (digest(next_binding), json.dumps(user), pending['binding'], time.time()))
            if changed.rowcount != 1:
                raise AccountError(401, '请重新使用 FastCAS 登录')
        return next_binding, url

    def save_link(self, db, link):
        if link.get('client_id') != self.client_id or link.get('state') not in {'prepared', 'active', 'revoked'}:
            raise AccountError(403, '认证关系无效')
        previous = db.execute('SELECT * FROM web_cas_links WHERE id=?', (link['id'],)).fetchone()
        if previous:
            if previous['issuer'] != self.issuer or previous['user_id'] != link['local_account_ref'] or previous['subject'] != link['subject']:
                raise AccountError(409, '认证关系不匹配')
            if previous['state'] == 'revoked' or previous['version'] > link['version']:
                raise AccountError(409, '认证关系已改变')
        db.execute('INSERT INTO web_cas_links VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,version=excluded.version,payload=excluded.payload',
                   (link['id'], link['local_account_ref'], self.issuer, link['subject'], link['state'], link['version'], json.dumps(link)))
        if link['state'] == 'revoked':
            db.execute("DELETE FROM web_sessions WHERE source='fastcas' AND link_id=?", (link['id'],))

    def cas_login(self, link, identity):
        if identity['issuer'] != self.issuer or identity['subject'] != link['subject'] or link['state'] != 'active':
            raise AccountError(403, '认证关系无效')
        with self.write() as db:
            row = db.execute("SELECT * FROM web_cas_links WHERE id=? AND issuer=? AND state='active'", (link['id'], self.issuer)).fetchone()
            if not row or row['user_id'] != link['local_account_ref'] or row['subject'] != identity['subject'] or row['version'] != link['version']:
                raise AccountError(403, '本地认证关系无效')
            if identity.get('role') == 'admin':
                db.execute("UPDATE web_users SET role='admin' WHERE id=? AND status='active'", (row['user_id'],))
            return self.issue(db, row['user_id'], 'fastcas', link, identity)

    def finish(self, callback, binding, session):
        result = self.sdk.finish_login(callback, binding, local_account_ref=session['user_id'] if session else '', local_session_id=session['token'] if session else '')
        identity, tx = result['identity'], result['transaction']
        if tx['purpose'] in {'register', 'link'}:
            if tx['purpose'] == 'register':
                pending = self.pending(binding, prepared=True)
                user = json.loads(pending['prepared'])
                if pending['subject'] != identity['subject'] or tx['local_account_ref'] != user['id']:
                    raise AccountError(401, 'FastCAS 身份已改变，请重新登录')
            elif not session:
                raise AccountError(401, '请重新登录本地账号')
            link = self.sdk.prepare_link(result)
            if link['local_account_ref'] != tx['local_account_ref'] or link['subject'] != identity['subject']:
                raise AccountError(409, '认证关系不匹配')
            with self.write() as db:
                if tx['purpose'] == 'register':
                    self.insert(db, user)
                    db.execute('DELETE FROM web_cas_signups WHERE binding=?', (digest(binding),))
                elif not db.execute("SELECT s.token FROM web_sessions s JOIN web_users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.status='active'", (session['token'], time.time())).fetchone():
                    raise AccountError(401, '本地会话已失效')
                self.save_link(db, link)
            active = self.sdk.activate_link(link['id'])
            with self.write() as db:
                self.save_link(db, active)
                if identity.get('role') == 'admin':
                    db.execute("UPDATE web_users SET role='admin' WHERE id=? AND status='active'", (link['local_account_ref'],))
            return self.cas_login(active, identity) if tx['purpose'] == 'register' else None
        local = self.one('SELECT * FROM web_cas_links WHERE issuer=? AND subject=? ORDER BY version DESC LIMIT 1', (self.issuer, identity['subject']))
        if local and local['state'] == 'prepared':
            remote = self.sdk.get_link(local['id'])
            if remote['state'] == 'prepared':
                remote = self.sdk.activate_link(remote['id'])
            with self.write() as db:
                self.save_link(db, remote)
        from fastcas import FastCASError
        try:
            link = self.sdk.resolve_link(identity['subject'])
        except FastCASError as error:
            if local or not (error.status == 404 or error.status == 401 and error.code == 'authentication_required'):
                raise
            with self.write() as db:
                db.execute('DELETE FROM web_cas_signups WHERE expires<=?', (time.time(),))
                db.execute('INSERT OR REPLACE INTO web_cas_signups VALUES(?,?,?,?,?,NULL)', (digest(binding), identity['subject'], (identity.get('email') or '') if identity.get('email_verified') else '', identity.get('name') or '', time.time()+300))
            return 'register'
        return self.cas_login(link, identity)

    def task_allowed(self, session, task_id):
        return session['role'] == 'admin' or bool(self.one('SELECT task_id FROM web_task_owners WHERE task_id=? AND user_id=?', (task_id, session['user_id'])))

    def visible_tasks(self, session, tasks):
        if session['role'] == 'admin':
            return tasks
        ids = {row['task_id'] for row in self.all('SELECT task_id FROM web_task_owners WHERE user_id=?', (session['user_id'],))}
        return [task for task in tasks if task['id'] in ids]

    def own_task(self, session, payload):
        if isinstance(payload, dict) and payload.get('id') and self.one('SELECT id FROM tasks WHERE id=?', (payload['id'],)):
            with self.write() as db:
                db.execute('INSERT OR IGNORE INTO web_task_owners VALUES(?,?)', (payload['id'], session['user_id']))

    def cookie(self, name, value, age=300):
        return f'{name}={value}; HttpOnly; SameSite=Lax; Path=/; Max-Age={age}' + ('; Secure' if self.origin.startswith('https://') else '')

    def redirect(self, handler, target, cookies=()):
        handler.send_response(302)
        handler.send_header('Location', target)
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Referrer-Policy', 'no-referrer')
        for cookie in cookies:
            handler.send_header('Set-Cookie', cookie)
        handler.send_header('Content-Length', '0')
        handler.end_headers()

    def guard(self, handler, mutation):
        try:
            return self.handle(handler, mutation)
        except AccountError as error:
            handler.json_response(error.status, {'error': str(error)})
        except (ValueError, sqlite3.IntegrityError):
            handler.json_response(400, {'error': '账号资料或请求格式无效'})
        except Exception:
            handler.json_response(502, {'error': '账号认证暂时不可用，请重试'})
        return False

    def handle(self, handler, mutation):
        path = urlparse(handler.path).path
        host = handler.headers.get('Host', '')
        if host not in {urlparse(self.origin).netloc, f'127.0.0.1:{handler.server.server_port}', f'localhost:{handler.server.server_port}'}:
            raise AccountError(403, '请求地址无效')
        cookies = SimpleCookie(handler.headers.get('Cookie', ''))
        cookie = lambda key: cookies[key].value if key in cookies else ''
        if path in {'/api/auth/fastcas/events', '/api/auth/fastcas/backchannel-logout'}:
            if not self.sdk or handler.command != 'POST':
                raise AccountError(404, '接口不存在')
            return self.notification(handler, path)
        if mutation and handler.headers.get('Origin') != self.origin:
            raise AccountError(403, '不允许跨站请求')
        if path == '/api/auth/info' and handler.command == 'GET':
            handler.json_response(200, {'enabled': True, 'registration': False, 'fastcas': bool(self.sdk)})
            return False
        if path == '/api/auth/session-init' and handler.command == 'GET':
            csrf = secrets.token_urlsafe(32)
            handler.json_response(200, {'csrf': csrf}, cookie=self.cookie('fastlab_preauth', csrf, 1800))
            return False
        if path == '/api/auth/fastcas/login' and handler.command == 'GET':
            if not self.sdk:
                raise AccountError(404, '未启用 FastCAS')
            binding = secrets.token_urlsafe(32)
            self.redirect(handler, self.sdk.begin_login(binding), [self.cookie('fastlab_cas_binding', binding)])
            return False
        preauth = path in {'/api/auth/login', '/api/auth/register', '/api/auth/fastcas/registration'}
        session = None if preauth else self.session(cookie('fastlab_session'))
        handler.account = session
        if path == '/api/health' and handler.command == 'GET' and not session:
            handler.json_response(200, {'ok': True, 'service': 'fastlabs'})
            return False
        if path == '/api/auth/fastcas/callback' and handler.command == 'GET':
            if not self.sdk:
                raise AccountError(404, '未启用 FastCAS')
            try:
                result = self.finish(self.redirect_uri+'?'+urlparse(handler.path).query, cookie('fastlab_cas_binding'), session)
            except Exception:
                self.redirect(handler, '/?fastcas=failed', [self.cookie('fastlab_cas_binding', '', 0)])
                return False
            if result == 'register':
                self.redirect(handler, '/?fastcas=register')
            else:
                values = [self.cookie('fastlab_cas_binding', '', 0)]
                if result:
                    values.append(self.cookie('fastlab_session', result[0], 7*86400))
                self.redirect(handler, '/?fastcas=complete', values)
            return False
        if mutation:
            expected = cookie('fastlab_preauth') if preauth else session['csrf'] if session else ''
            if len(expected) < 32 or not hmac.compare_digest(expected, handler.headers.get('X-CSRF-Token', '')):
                raise AccountError(403 if preauth or session else 401, '请刷新页面后登录')
        if path == '/api/auth/register':
            raise AccountError(403, '自主注册已关闭，请使用 FastCAS 或联系管理员')
        if path == '/api/auth/login' and handler.command == 'POST':
            raw, csrf = self.login(handler.read_json(), handler.client_address[0])
            handler.json_response(200, {'csrf': csrf}, cookie=self.cookie('fastlab_session', raw, 7*86400))
            return False
        if path == '/api/auth/fastcas/registration' and self.sdk:
            if handler.command == 'GET':
                pending = self.pending(cookie('fastlab_cas_binding'))
                handler.json_response(200, {key: pending[key] for key in ('email', 'name')})
            elif handler.command == 'POST':
                binding, target = self.begin_registration(cookie('fastlab_cas_binding'), handler.read_json())
                handler.json_response(200, {'url': target}, cookie=self.cookie('fastlab_cas_binding', binding))
            else:
                raise AccountError(405, 'Method not allowed')
            return False
        if not path.startswith('/api/'):
            return True
        if not session:
            raise AccountError(401, '请先登录')
        if path == '/api/auth/me' and handler.command == 'GET':
            user = self.public({**session, 'id': session['user_id']})
            handler.json_response(200, {**user, 'csrf': session['csrf']})
            return False
        if path == '/api/auth/logout' and handler.command == 'POST':
            with self.write() as db:
                db.execute('DELETE FROM web_sessions WHERE token=?', (session['token'],))
            handler.json_response(200, {'ok': True}, cookie=self.cookie('fastlab_session', '', 0))
            return False
        if path.startswith('/api/auth/fastcas/'):
            return self.account_link(handler, path, session)
        if path == '/api/admin/users' or path.startswith('/api/admin/users/'):
            if session['role'] != 'admin':
                raise AccountError(403, '需要管理员权限')
            user_id = path.removeprefix('/api/admin/users').strip('/')
            if handler.command == 'GET' and not user_id:
                handler.json_response(200, [self.public(u) for u in self.all("SELECT * FROM web_users WHERE status!='deleted' ORDER BY email")])
            elif handler.command == 'POST' and not user_id or handler.command in {'PUT', 'DELETE'} and user_id:
                handler.json_response(201 if not user_id else 200, self.manage(session, user_id, handler.command, handler.read_json()))
            else:
                raise AccountError(405, 'Method not allowed')
            return False
        if session['role'] != 'admin' and mutation and path.startswith(('/api/repositories', '/api/settings', '/api/fastcas/')):
            raise AccountError(403, '需要管理员权限')
        if session['role'] != 'admin' and path == '/api/fastcas/status':
            handler.json_response(200, {'available': False, 'csrf': session['csrf']})
            return False
        task = re.match(r'/api/tasks/([a-f0-9]+)(?:/|$)', path)
        subtask = re.match(r'/api/subtasks/([^/]+)', path)
        task_id = task[1] if task else None
        if subtask:
            row = self.one('SELECT task_id FROM subtasks WHERE id=?', (subtask[1],))
            task_id = row['task_id'] if row else None
            if not row:
                raise AccountError(404, '子任务不存在')
        if task_id and not self.task_allowed(session, task_id):
            raise AccountError(403, '无权访问此任务')
        return True

    def account_link(self, handler, path, session):
        if not self.sdk:
            raise AccountError(404, '未启用 FastCAS')
        row = self.one("SELECT * FROM web_cas_links WHERE issuer=? AND user_id=? AND state!='revoked'", (self.issuer, session['user_id']))
        if path.endswith('/status') and handler.command == 'GET':
            handler.json_response(200, {'enabled': True, 'link': {k: row[k] for k in ('id', 'state', 'subject')} if row else None})
            return False
        if handler.command != 'POST':
            raise AccountError(405, 'Method not allowed')
        data = handler.read_json()
        if not path.endswith('/reconcile'):
            user = self.one('SELECT password FROM web_users WHERE id=?', (session['user_id'],))
            if not password_matches(data.get('password', ''), user['password']):
                raise AccountError(403, '请验证当前账号的本地密码')
        if path.endswith('/link'):
            if row:
                raise AccountError(409, '已有认证关系，请先完成或解除认证')
            binding = secrets.token_urlsafe(32)
            url = self.sdk.begin_link(binding, local_account_ref=session['user_id'], local_session_id=session['token'])
            handler.json_response(200, {'url': url}, cookie=self.cookie('fastlab_cas_binding', binding))
            return False
        if not row:
            raise AccountError(404, '没有认证关系')
        remote = self.sdk.get_link(row['id'])
        if any(remote[k] != json.loads(row['payload'])[k] for k in ('id', 'subject', 'local_account_ref', 'client_id')):
            raise AccountError(403, '认证关系不匹配')
        if path.endswith('/revoke'):
            if remote['state'] != 'revoked':
                remote = self.sdk.revoke_link(remote)
        elif path.endswith('/reconcile'):
            if remote['state'] == 'prepared':
                remote = self.sdk.activate_link(remote['id'])
        else:
            raise AccountError(404, '接口不存在')
        with self.write() as db:
            self.save_link(db, remote)
        handler.json_response(200, {'ok': True})
        return False

    def notification(self, handler, path):
        length = int(handler.headers.get('Content-Length', '0'))
        if not 0 < length <= 65536:
            raise AccountError(413, '通知大小无效')
        kind = handler.headers.get('Content-Type', '').split(';')[0]
        raw = handler.rfile.read(length).decode()
        def apply(event):
            with self.write() as db:
                if db.execute('SELECT id FROM web_cas_events WHERE id=?', (event['id'],)).fetchone():
                    return
                if event.get('type') == 'account_link.revoked':
                    local = db.execute('SELECT state,version FROM web_cas_links WHERE id=?', (event['link']['id'],)).fetchone()
                    if local and local['state'] != 'revoked' and event['link']['version'] >= local['version']:
                        self.save_link(db, event['link'])
                elif event.get('status') == 'disabled' or path.endswith('backchannel-logout'):
                    sql = "DELETE FROM web_sessions WHERE source='fastcas' AND link_id IN (SELECT id FROM web_cas_links WHERE issuer=? AND subject=?)"
                    values = [self.issuer, event['subject']]
                    if event.get('sid'):
                        sql += ' AND sid=?'
                        values.append(event['sid'])
                    db.execute(sql, values)
                db.execute('INSERT INTO web_cas_events VALUES(?,?)', (event['id'], time.time()))
        if path.endswith('/events') and kind == 'application/jwt':
            self.sdk.handle_notification(raw, apply)
        elif path.endswith('/backchannel-logout') and kind == 'application/x-www-form-urlencoded':
            values = parse_qs(raw)
            if len(values.get('logout_token', [])) != 1:
                raise AccountError(400, '退出通知无效')
            apply(self.sdk.verify_logout(values['logout_token'][0]))
        else:
            raise AccountError(415, '需要签名通知')
        handler.json_response(200, {'ok': True})
        return False
