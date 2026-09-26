import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class WithdrawalExecuteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _participant_with_sample(self, name, code):
        participant = self.service.create(self.actor, "participant", {"name": name})
        consent = self.service.create(
            self.actor,
            "consent",
            {"participant_id": participant["id"], "scope": ["research"]},
        )
        self.service.transition(
            self.actor,
            consent["id"],
            "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"},
        )
        sample = self.service.create(
            self.actor,
            "sample",
            {
                "participant_id": participant["id"],
                "sample_code": code,
                "collected_at": "2026-01-01",
            },
        )
        return participant, consent, sample

    def _store(self, consent, sample, position="A1"):
        return self.service.transition(
            self.actor,
            sample["id"],
            "store",
            {"freezer": "F1", "position": position, "consent_id": consent["id"]},
        )

    def _approved_withdrawal(self, participant, sample_ids):
        withdrawal = self.service.create(
            self.actor,
            "withdrawal",
            {"participant_id": participant["id"], "requested_at": "2026-03-01"},
        )
        return self.service.transition(
            self.actor,
            withdrawal["id"],
            "approve",
            {"reason": "participant request", "sample_ids": sample_ids},
        )

    def test_execute_disposes_mixed_status_samples(self):
        participant = self.service.create(self.actor, "participant", {"name": "Participant One"})
        consent = self.service.create(
            self.actor,
            "consent",
            {"participant_id": participant["id"], "scope": ["research"]},
        )
        self.service.transition(
            self.actor,
            consent["id"],
            "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"},
        )
        samples = []
        for index, code in enumerate(("B-001", "B-002")):
            sample = self.service.create(
                self.actor,
                "sample",
                {
                    "participant_id": participant["id"],
                    "sample_code": code,
                    "collected_at": "2026-01-01",
                },
            )
            self._store(consent, sample, "A%d" % index)
            samples.append(sample)
        # 第二份样本借出。
        self.service.transition(
            self.actor,
            samples[1]["id"],
            "loan",
            {"recipient": "Lab X", "purpose": "analysis", "due_at": "2026-12-01"},
        )
        withdrawal = self._approved_withdrawal(
            participant, [samples[0]["id"], samples[1]["id"]]
        )
        executed = self.service.transition(
            self.actor, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )
        self.assertEqual(executed["status"], "executed")
        results = executed["data"]["disposal_results"]
        self.assertEqual(
            results,
            {
                samples[0]["id"]: {"result": "destroyed", "disposed_at": "2026-03-02"},
                samples[1]["id"]: {"result": "pending_recall", "disposed_at": "2026-03-02"},
            },
        )
        first = self.service.get(samples[0]["id"])
        second = self.service.get(samples[1]["id"])
        self.assertEqual(first["status"], "destroyed")
        self.assertEqual(second["status"], "pending_recall")
        for sample, result in ((first, "destroyed"), (second, "pending_recall")):
            disposal = sample["data"]["disposal"]
            self.assertEqual(disposal["withdrawal_id"], withdrawal["id"])
            self.assertEqual(disposal["result"], result)
            self.assertEqual(disposal["disposed_at"], "2026-03-02")
        # 每份样本的处置都有审计记录。
        disposed = [
            entry
            for entry in self.service.audit_log()
            if entry["action"] == "dispose"
        ]
        self.assertEqual(len(disposed), 2)
        self.assertEqual(
            {entry["to_status"] for entry in disposed}, {"destroyed", "pending_recall"}
        )

    def test_execute_rejects_foreign_sample_and_stops_all(self):
        participant, consent, own = self._participant_with_sample("Participant One", "B-001")
        other, _, foreign = self._participant_with_sample("Participant Two", "B-101")
        self._store(consent, own)
        withdrawal = self._approved_withdrawal(
            participant, [own["id"], foreign["id"]]
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
            )
        # 全部停止：申请仍在 approved，自己的样本也未被处置。
        self.assertEqual(self.service.get(withdrawal["id"])["status"], "approved")
        self.assertEqual(self.service.get(own["id"])["status"], "stored")

    def test_execute_rejects_already_disposed_sample(self):
        participant, consent, sample = self._participant_with_sample("Participant One", "B-001")
        self._store(consent, sample)
        withdrawal = self._approved_withdrawal(participant, [sample["id"]])
        # 批准后样本被其他流程销毁。
        self.service.transition(
            self.actor, sample["id"], "destroy", {"reason": "contaminated"}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
            )
        self.assertEqual(self.service.get(withdrawal["id"])["status"], "approved")

    def test_execute_rejects_unknown_status_sample(self):
        participant, consent, sample = self._participant_with_sample("Participant One", "B-001")
        # 样本仍是 collected，未入库。
        withdrawal = self._approved_withdrawal(participant, [sample["id"]])
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
            )
        self.assertEqual(self.service.get(withdrawal["id"])["status"], "approved")
        self.assertEqual(self.service.get(sample["id"])["status"], "collected")

    def test_execute_is_idempotent(self):
        participant, consent, sample = self._participant_with_sample("Participant One", "B-001")
        self._store(consent, sample)
        withdrawal = self._approved_withdrawal(participant, [sample["id"]])
        first = self.service.transition(
            self.actor, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )
        # 重复执行：即使带了不同的时间，也返回已有结果。
        second = self.service.transition(
            self.actor, withdrawal["id"], "execute", {"executed_at": "2026-04-01"}
        )
        self.assertEqual(second["data"]["executed_at"], "2026-03-02")
        self.assertEqual(second["version"], first["version"])
        self.assertEqual(
            second["data"]["disposal_results"], first["data"]["disposal_results"]
        )
        # 样本没有被反复处置。
        destroyed = self.service.get(sample["id"])
        self.assertEqual(destroyed["status"], "destroyed")
        self.assertEqual(destroyed["data"]["disposal"]["disposed_at"], "2026-03-02")
        dispose_entries = [
            entry
            for entry in self.service.audit_log(sample["id"])
            if entry["action"] == "dispose"
        ]
        self.assertEqual(len(dispose_entries), 1)


if __name__ == "__main__":
    unittest.main()
