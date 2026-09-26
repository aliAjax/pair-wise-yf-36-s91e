from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import WITHDRAWAL_DISPOSAL, RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "withdrawal" and action == "execute":
            return self._execute_withdrawal(actor, entity, dict(data or {}), expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _execute_withdrawal(self, actor, entity, data, expected_version):
        # 重复执行同一申请：直接返回已有结果，不重复记时、不反复处置样本。
        if entity["status"] == "executed":
            return entity
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, "execute", data, self._lookup
        )
        executed_at = patch["executed_at"]
        merged = dict(entity["data"])
        merged.update(patch)
        # 按批准的样本清单逐份制定处置计划；核对已在规则层完成，这里再读一次
        # 当前状态，若与核对结果不一致（并发变更）则整体停止。
        disposals = []
        results = {}
        for sample_id in merged.get("sample_ids") or []:
            sample = self.repository.get_entity(sample_id)
            if not sample or sample["status"] not in WITHDRAWAL_DISPOSAL:
                raise ValidationError(
                    "sample %s cannot be disposed in its current state" % sample_id
                )
            target = WITHDRAWAL_DISPOSAL[sample["status"]]
            sample_data = dict(sample["data"])
            sample_data["disposal"] = {
                "withdrawal_id": entity["id"],
                "result": target,
                "disposed_at": executed_at,
            }
            disposals.append(
                (sample["id"], sample["version"], target, sample_data, sample["status"])
            )
            results[sample["id"]] = {"result": target, "disposed_at": executed_at}
        merged["disposal_results"] = results
        updates = [(entity["id"], expected, next_status, merged)]
        updates.extend((sid, ver, status, sdata) for sid, ver, status, sdata, _ in disposals)
        # 撤回申请与全部样本在同一事务内更新，任何冲突都整体回滚。
        self.repository.update_entities_atomic(updates)
        self.audit.record(
            entity["id"],
            actor,
            "execute",
            entity["status"],
            next_status,
            {"patch": patch, "disposal_results": results},
        )
        for sample_id, _, target, _, from_status in disposals:
            self.audit.record(
                sample_id,
                actor,
                "dispose",
                from_status,
                target,
                {"withdrawal_id": entity["id"], "disposed_at": executed_at},
            )
        return self.repository.get_entity(entity["id"])

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
