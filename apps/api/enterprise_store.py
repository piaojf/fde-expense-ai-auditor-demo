"""Durable principals, revocable sessions, ownership and append-only submission history."""

import hashlib
import hmac
import json
import secrets
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import uuid4

from apps.api.errors import APIError
from apps.api.login_captcha import CAPTCHA_TTL, generate_code, render_captcha
from domain.enterprise import CreateUser, FinanceAction

# Request-local execution capability, never reconstructed from an actor dictionary.
finance_execution = ContextVar("finance_execution", default=None)


def timestamp():
    return datetime.now(UTC).isoformat()


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(
        password.encode("utf-8"), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32
    ).hex()
    return salt + ":" + value


class EnterpriseStore:
    def __init__(self, repo):
        self.repo, self.db, self.lock = repo, repo._db, repo._lock
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS enterprise_users (
                    id TEXT PRIMARY KEY, company_id TEXT NOT NULL,
                    username TEXT NOT NULL, password_hash TEXT NOT NULL,
                    name TEXT NOT NULL, department TEXT NOT NULL, position TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('EMPLOYEE','FINANCE','ADMIN')),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(company_id,username)
                );
                CREATE TABLE IF NOT EXISTS enterprise_sessions (
                    hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS enterprise_login_attempts (
                    scope TEXT NOT NULL, at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS enterprise_attempt_scope
                    ON enterprise_login_attempts(scope,at);
                CREATE TABLE IF NOT EXISTS enterprise_login_captchas (
                    id_hash TEXT PRIMARY KEY, binding_hash TEXT NOT NULL UNIQUE,
                    answer_hash TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS enterprise_claims (
                    claim_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    stage TEXT NOT NULL, version INTEGER NOT NULL, revision INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS enterprise_versions (
                    claim_id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(claim_id,version)
                );
                CREATE TABLE IF NOT EXISTS enterprise_requests (
                    scope TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
                    claim_id TEXT NOT NULL, result TEXT,
                    PRIMARY KEY(scope,key)
                );
                CREATE TABLE IF NOT EXISTS enterprise_resources (
                    kind TEXT NOT NULL, id TEXT NOT NULL, company_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL, secret TEXT,
                    PRIMARY KEY(kind,id)
                );
                CREATE TABLE IF NOT EXISTS enterprise_events (
                    seq INTEGER PRIMARY KEY, company_id TEXT NOT NULL, claim_id TEXT,
                    body TEXT NOT NULL
                );
            """)
        self.dummy_password = password_hash("dummy-password-" + secrets.token_hex(16))
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS enterprise_authorities (
                    user_id TEXT PRIMARY KEY, generation TEXT NOT NULL
                );
                INSERT OR IGNORE INTO enterprise_authorities
                    SELECT id,lower(hex(randomblob(16))) FROM enterprise_users;
                CREATE TRIGGER IF NOT EXISTS enterprise_authority_created
                AFTER INSERT ON enterprise_users BEGIN
                    INSERT OR REPLACE INTO enterprise_authorities
                        VALUES(NEW.id,lower(hex(randomblob(16))));
                END;
                CREATE TRIGGER IF NOT EXISTS enterprise_authority_changed
                AFTER UPDATE OF enabled,role,company_id ON enterprise_users
                WHEN OLD.enabled != NEW.enabled OR OLD.role != NEW.role
                    OR OLD.company_id != NEW.company_id BEGIN
                    UPDATE enterprise_authorities SET generation=lower(hex(randomblob(16)))
                        WHERE user_id=NEW.id;
                END;
                CREATE TRIGGER IF NOT EXISTS enterprise_authority_deleted
                AFTER DELETE ON enterprise_users BEGIN
                    DELETE FROM enterprise_authorities WHERE user_id=OLD.id;
                END;
            """)
        repo.finance_guard = self.finance_guard

    def authority(self, user_id):
        row = self.db.execute(
            "SELECT generation FROM enterprise_authorities WHERE user_id=?", (user_id,)
        ).fetchone()
        return row[0] if row else None

    def live_actor(self, token, *roles, expected_generation=None, company_id=None):
        actor = self.principal(token)
        if roles and actor["role"] not in roles:
            raise APIError(403, "ROLE_FORBIDDEN", "当前账号没有此操作权限")
        if ((company_id is not None and actor["company_id"] != company_id)
                or (expected_generation is not None
                    and self.authority(actor["id"]) != expected_generation)):
            raise APIError(403, "FINANCE_AUTHORITY_REVOKED", "请求期间权限已变更，请重新操作")
        return actor

    def finance_authorization(self, actor, row):
        return dict(actor_id=actor["id"], company_id=row["company_id"],
                    claim_id=row["claim_id"], version=row["version"],
                    revision=row["revision"], generation=self.authority(actor["id"]))

    def authorize_finance(self, scope, key, token, *, effect_key=None, review=None,
                          effect_status=None, effect_operation=None):
        """Caller owns the SQLite writer transaction at every mutation boundary."""
        caller = self.live_actor(token, "FINANCE")
        record = self.db.execute(
            "SELECT payload,claim_id,result FROM enterprise_requests WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if not record or not record[2]:
            raise APIError(409, "FINANCE_INTENT_INVALID", "缺少有效财务意图")
        body = FinanceAction.model_validate_json(record[0])
        operation = json.loads(record[2])
        row = self.claim_row(record[1], caller)
        auth = operation.get("authorization") or {}
        expected_scope = str(auth.get("actor_id")) + ":finance:" + row["claim_id"]
        claim = self.repo.get(row["claim_id"])
        context = self.repo.run_context(row["claim_id"], operation.get("run_id")) or {}
        expected_review = dict(decision=body.action, reviewer_id=auth.get("actor_id"),
                               comment=body.reason)
        persisted_review = context.get("human_review")
        supplied_review = review if review is not None else persisted_review
        review_matches = all((supplied_review or {}).get(k) == v
                             for k, v in expected_review.items())
        if (
            scope != expected_scope or key != body.idempotency_key
            or auth.get("company_id") != row["company_id"]
            or auth.get("claim_id") != row["claim_id"]
            or auth.get("version") != row["version"]
            or operation.get("version") != row["version"]
            or auth.get("revision") != body.expected_revision
            or (row["stage"] == "FINANCE_APPLYING"
                and row["revision"] != body.expected_revision)
            or (row["stage"] == "ERP_SYNC_PENDING"
                and row["revision"] <= body.expected_revision)
            or row["stage"] not in ("FINANCE_APPLYING", "ERP_SYNC_PENDING")
            or str(claim.active_run_id) != operation.get("run_id")
            or not context or (context.get("superseded") and body.action != "RETURN")
            or operation.get("invalidated_reason")
            or (supplied_review is not None and not review_matches)
        ):
            raise APIError(409, "FINANCE_INTENT_INVALID", "财务意图与当前申请不一致")
        erp_key = f"{row['claim_id']}:{operation['run_id']}:write_decision"
        if effect_key is not None and (
            body.action != "APPROVE" or effect_status != "APPROVED"
            or effect_operation != "write_decision"
        ):
            raise APIError(409, "FINANCE_INTENT_INVALID", "入账操作与审批决定不一致")
        effects = self.repo.erp_effects(row["claim_id"])
        if effects:
            # Only reconcile an already committed matching effect. This grants no new approval.
            if (len(effects) != 1 or effects[0]["idempotency_key"] != erp_key
                    or effects[0]["status"] != "APPROVED" or body.action != "APPROVE"
                    or not persisted_review or not review_matches
                    or (effect_key is not None and effect_key != erp_key)):
                raise APIError(409, "FINANCE_INTENT_INVALID", "已有冲突的入账副作用")
            return self.user(auth["actor_id"]) or dict(
                id=auth["actor_id"], name="历史审核人", role="FINANCE",
                company_id=row["company_id"])
        actor = self.user(auth.get("actor_id"))
        if (not actor or not actor["enabled"] or actor["role"] != "FINANCE"
                or actor["company_id"] != row["company_id"] or not auth.get("generation")
                or self.authority(actor["id"]) != auth["generation"]):
            raise APIError(403, "FINANCE_AUTHORITY_REVOKED", "原财务授权已失效，需要人工恢复")
        if effect_key is not None and (effect_key != erp_key or not persisted_review):
            raise APIError(409, "FINANCE_INTENT_INVALID", "入账与审批意图不一致")
        return actor

    def finance_guard(self, claim_id, kind, *, run_id=None, review=None, effect_key=None,
                      effect_status=None, effect_operation=None):
        if not self.db.execute(
            "SELECT 1 FROM enterprise_claims WHERE claim_id=?", (claim_id,)
        ).fetchone():
            return  # Legacy/non-enterprise workflows retain their contract.
        execution = finance_execution.get()
        if not execution or execution["claim_id"] != claim_id:
            raise APIError(403, "FINANCE_EXECUTION_REQUIRED", "需要有效财务操作")
        if execution.get("denial"):
            raise execution["denial"]
        try:
            actor = self.authorize_finance(execution["scope"], execution["key"], execution["token"],
                                           effect_key=effect_key, review=review,
                                           effect_status=effect_status,
                                           effect_operation=effect_operation)
            if kind == "erp" and not self.repo.erp_effects(claim_id):
                # The caller owns the same writer transaction as the first effect.
                # A matching committed effect only reconciles; it grants no new approval.
                validator = execution.get("validate_evidence")
                if not callable(validator):
                    raise APIError(409, "FINANCE_EVIDENCE_INVALID", "缺少当前执行证据核验")
                validator()
        except APIError as exc:
            # Graph dependency handling may catch exceptions. A denied execution must
            # still fail at its final commit rather than become a successful HTTP result.
            execution["denial"] = exc
            raise
        record = self.db.execute(
            "SELECT result FROM enterprise_requests WHERE scope=? AND key=?",
            (execution["scope"], execution["key"]),
        ).fetchone()
        if run_id is not None and str(run_id) != json.loads(record[0])["run_id"]:
            raise APIError(409, "FINANCE_INTENT_INVALID", "审批运行不一致")
        return actor

    def user(self, user_id):
        with self.lock:
            row = self.db.execute(
                "SELECT id,company_id,username,name,department,position,role,enabled "
                "FROM enterprise_users WHERE id=?",
                (user_id,),
            ).fetchone()
        return (
            dict(
                zip(
                    (
                        "id",
                        "company_id",
                        "username",
                        "name",
                        "department",
                        "position",
                        "role",
                        "enabled",
                    ),
                    row,
                    strict=True,
                )
            )
            if row
            else None
        )

    def users(self, company_id):
        with self.lock:
            ids = self.db.execute(
                "SELECT id FROM enterprise_users WHERE company_id=? ORDER BY username",
                (company_id,),
            ).fetchall()
        return [self.user(row[0]) for row in ids]

    def event(self, principal, kind, claim_id=None, **details):
        body = dict(
            kind=kind,
            actor_id=principal["id"],
            actor_name=principal["name"],
            role=principal["role"],
            claim_id=claim_id,
            at=timestamp(),
            **details,
        )
        # Caller owns the transaction, so state and its audit event commit together.
        self.db.execute(
            "INSERT INTO enterprise_events(company_id,claim_id,body) VALUES(?,?,?)",
            (principal["company_id"], claim_id, json.dumps(body, ensure_ascii=False)),
        )

    def create_user(self, company_id, body: CreateUser, actor=None):
        value = password_hash(body.password.get_secret_value())
        with self.lock, self.db:
            if self.db.execute(
                "SELECT 1 FROM enterprise_users WHERE company_id=? AND username=?",
                (company_id, body.username.lower()),
            ).fetchone():
                raise APIError(409, "USER_EXISTS", "此企业已有该账号")
            uid = "USR-" + uuid4().hex
            self.db.execute(
                "INSERT INTO enterprise_users VALUES(?,?,?,?,?,?,?,?,1)",
                (
                    uid,
                    company_id,
                    body.username.lower(),
                    value,
                    body.name,
                    body.department,
                    body.position,
                    body.role,
                ),
            )
            self.event(
                actor or self.user(uid), "USER_CREATED", user_id=uid, assigned_role=body.role
            )
        return self.user(uid)

    def set_enabled(self, actor, user_id, enabled):
        with self.lock, self.db:
            user = self.user(user_id)
            if not user or user["company_id"] != actor["company_id"]:
                raise APIError(404, "USER_NOT_FOUND", "账号不存在")
            if user_id == actor["id"] and not enabled:
                raise APIError(409, "SELF_DISABLE", "不能停用当前登录的管理员账号")
            self.db.execute("UPDATE enterprise_users SET enabled=? WHERE id=?", (enabled, user_id))
            if not enabled:
                self.db.execute("DELETE FROM enterprise_sessions WHERE user_id=?", (user_id,))
            self.event(actor, "USER_ENABLED" if enabled else "USER_DISABLED", user_id=user_id)
        return self.user(user_id)

    def issue_captcha(self, peer, binding):
        now = time.time()
        scope = "captcha:" + digest(peer)
        challenge_id, code = secrets.token_urlsafe(32), generate_code()
        with self.lock, self.db:
            self.db.execute("DELETE FROM enterprise_login_attempts WHERE at<?", (now - 300,))
            count = self.db.execute(
                "SELECT COUNT(*) FROM enterprise_login_attempts WHERE scope=? AND at>?",
                (scope, now - 60),
            ).fetchone()[0]
            if count >= 30:
                raise APIError(429, "CAPTCHA_THROTTLED", "验证码刷新过于频繁，请稍后再试")
            image = render_captcha(code)
            self.db.execute("INSERT INTO enterprise_login_attempts VALUES(?,?)", (scope, now))
            self.db.execute(
                "DELETE FROM enterprise_login_captchas WHERE expires<=? OR binding_hash=?",
                (now, digest(binding)),
            )
            self.db.execute(
                "INSERT INTO enterprise_login_captchas VALUES(?,?,?,?)",
                (digest(challenge_id), digest(binding), digest(challenge_id + ":" + code),
                 now + CAPTCHA_TTL),
            )
        return {"captcha_id": challenge_id, "image": image, "expires_in": CAPTCHA_TTL}

    def login(self, body, peer, ttl, binding=""):
        now = time.time()
        scopes = (
            "ip:" + digest(peer),
            "user:" + digest(body.company_id + ":" + body.username.lower()),
        )
        with self.lock, self.db:
            self.db.execute("DELETE FROM enterprise_login_attempts WHERE at<?", (now - 300,))
            for scope, maximum in zip(scopes, (30, 8), strict=True):
                count = self.db.execute(
                    "SELECT COUNT(*) FROM enterprise_login_attempts WHERE scope=? AND at>?",
                    (scope, now - 60),
                ).fetchone()[0]
                if count >= maximum:
                    raise APIError(429, "LOGIN_THROTTLED", "尝试过于频繁，请稍后再登录")
            for scope in scopes:
                self.db.execute("INSERT INTO enterprise_login_attempts VALUES(?,?)", (scope, now))
            challenge = self.db.execute(
                "SELECT answer_hash,expires FROM enterprise_login_captchas "
                "WHERE id_hash=? AND binding_hash=?",
                (digest(body.captcha_id), digest(binding)),
            ).fetchone()
            self.db.execute(
                "DELETE FROM enterprise_login_captchas WHERE id_hash=? AND binding_hash=?",
                (digest(body.captcha_id), digest(binding)),
            )
        answer = body.captcha_code.get_secret_value().strip().lower()
        if not challenge or challenge[1] <= now or not hmac.compare_digest(
            digest(body.captcha_id + ":" + answer), challenge[0]
        ):
            raise APIError(400, "CAPTCHA_INVALID", "验证码不正确或已过期，请输入新验证码")
        with self.lock:
            row = self.db.execute(
                "SELECT id,password_hash,enabled FROM enterprise_users "
                "WHERE company_id=? AND username=?",
                (body.company_id, body.username.lower()),
            ).fetchone()
        encoded = row[1] if row else self.dummy_password
        salt, _ = encoded.split(":", 1)
        actual = password_hash(body.password.get_secret_value(), salt)
        if not hmac.compare_digest(actual, encoded) or not row or not row[2]:
            raise APIError(401, "LOGIN_FAILED", "企业、账号或密码不正确，或账号已停用")
        token = secrets.token_urlsafe(32)
        with self.lock, self.db:
            self.db.execute("DELETE FROM enterprise_sessions WHERE expires<?", (now,))
            self.db.execute(
                "INSERT INTO enterprise_sessions VALUES(?,?,?)", (digest(token), row[0], now + ttl)
            )
            self.event(self.user(row[0]), "LOGIN")
        return token, self.user(row[0])

    def principal(self, token):
        if not token or len(token) > 200:
            raise APIError(401, "LOGIN_REQUIRED", "请先登录")
        with self.lock:
            row = self.db.execute(
                "SELECT user_id FROM enterprise_sessions WHERE hash=? AND expires>?",
                (digest(token), time.time()),
            ).fetchone()
            user = self.user(row[0]) if row else None
        if not user or not user["enabled"]:
            raise APIError(401, "LOGIN_REQUIRED", "登录已失效，请重新登录")
        return user

    def logout(self, token):
        with self.lock, self.db:
            self.db.execute("DELETE FROM enterprise_sessions WHERE hash=?", (digest(token or ""),))

    def claim_row(self, claim_id, principal):
        with self.lock:
            row = self.db.execute(
                "SELECT claim_id,company_id,owner_id,stage,version,revision,"
                "reason,created_at FROM enterprise_claims WHERE claim_id=?",
                (claim_id,),
            ).fetchone()
        if (
            not row
            or row[1] != principal["company_id"]
            or (principal["role"] == "EMPLOYEE" and row[2] != principal["id"])
        ):
            raise APIError(404, "CLAIM_NOT_FOUND", "报销单不存在")
        return dict(
            zip(
                (
                    "claim_id",
                    "company_id",
                    "owner_id",
                    "stage",
                    "version",
                    "revision",
                    "reason",
                    "created_at",
                ),
                row,
                strict=True,
            )
        )

    def claims(self, principal):
        with self.lock:
            query, args = (
                "SELECT claim_id FROM enterprise_claims WHERE company_id=?",
                [principal["company_id"]],
            )
            if principal["role"] == "EMPLOYEE":
                query += " AND owner_id=?"
                args.append(principal["id"])
            return [row[0] for row in self.db.execute(query + " ORDER BY created_at DESC", args)]

    def own_resource(self, kind, resource_id, principal, secret=None):
        self.db.execute(
            "INSERT INTO enterprise_resources VALUES(?,?,?,?,?)",
            (kind, str(resource_id), principal["company_id"], principal["id"], secret),
        )

    def resource(self, kind, resource_id, principal, *, personal=False):
        with self.lock:
            row = self.db.execute(
                "SELECT company_id,owner_id,secret FROM enterprise_resources WHERE kind=? AND id=?",
                (kind, str(resource_id)),
            ).fetchone()
        if not row or row[0] != principal["company_id"] or (personal and row[1] != principal["id"]):
            raise APIError(404, "RESOURCE_NOT_FOUND", "资料不存在")
        return row[2]

    def replay(self, scope, key, body):
        row = self.db.execute(
            "SELECT payload,claim_id,result FROM enterprise_requests WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row and row[0] != body:
            raise APIError(409, "IDEMPOTENCY_CONFLICT", "相同请求编号不能用于不同操作")
        return row

    def remember(self, scope, key, body, claim_id, result=None):
        self.db.execute(
            "INSERT INTO enterprise_requests VALUES(?,?,?,?,?)",
            (scope, key, body, claim_id, json.dumps(result) if result else None),
        )

    def history(self, claim_id):
        with self.lock:
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT body FROM enterprise_versions WHERE claim_id=? ORDER BY version",
                    (claim_id,),
                )
            ]

    def update_version(self, claim_id, version, **updates):
        row = self.db.execute(
            "SELECT body FROM enterprise_versions WHERE claim_id=? AND version=?",
            (claim_id, version),
        ).fetchone()
        body = {**json.loads(row[0]), **updates}
        self.db.execute(
            "UPDATE enterprise_versions SET body=? WHERE claim_id=? AND version=?",
            (json.dumps(body, ensure_ascii=False), claim_id, version),
        )

    def events(self, principal, claim_id=None):
        with self.lock:
            query = "SELECT body FROM enterprise_events WHERE company_id=?"
            args = [principal["company_id"]]
            if claim_id:
                query += " AND claim_id=?"
                args.append(claim_id)
            return [json.loads(row[0]) for row in self.db.execute(query + " ORDER BY seq", args)]
