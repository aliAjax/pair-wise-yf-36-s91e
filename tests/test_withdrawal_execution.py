import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class WithdrawalExecutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")
        self.other = Actor("other", "biobank")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_participant(self, name="Participant One"):
        return self.service.create(
            self.actor, "participant", {"name": name}
        )["id"]

    def _make_stored_sample(self, participant, code):
        consent = self.service.create(
            self.actor,
            "consent",
            {"participant_id": participant, "scope": ["research"]},
        )["id"]
        self.service.transition(
            self.actor,
            consent,
            "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"},
        )
        sample = self.service.create(
            self.actor,
            "sample",
            {
                "participant_id": participant,
                "sample_code": code,
                "collected_at": "2026-01-01",
            },
        )["id"]
        self.service.transition(
            self.actor,
            sample,
            "store",
            {"freezer": "F1", "position": "A1", "consent_id": consent},
        )
        return sample

    def _loan_sample(self, sample):
        self.service.transition(
            self.actor,
            sample,
            "loan",
            {"recipient": "external-lab", "purpose": "study", "due_at": "2026-12-01"},
        )

    def _make_approved_withdrawal(self, participant, sample_ids, requested_at="2026-03-01"):
        withdrawal = self.service.create(
            self.actor,
            "withdrawal",
            {"participant_id": participant, "requested_at": requested_at},
        )["id"]
        self.service.transition(
            self.actor,
            withdrawal,
            "approve",
            {"reason": "participant request", "sample_ids": list(sample_ids)},
        )
        return withdrawal

    def test_execute_destroys_stored_and_recalls_loaned_samples(self):
        participant = self._make_participant()
        stored = self._make_stored_sample(participant, "B-001")
        loaned = self._make_stored_sample(participant, "B-002")
        self._loan_sample(loaned)
        withdrawal = self._make_approved_withdrawal(participant, [stored, loaned])

        result = self.service.transition(
            self.actor,
            withdrawal,
            "execute",
            {"executed_at": "2026-03-02T10:00:00Z"},
        )

        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["data"]["executed_at"], "2026-03-02T10:00:00Z")
        self.assertEqual(result["data"]["executed_by"], "admin")
        results = result["data"]["sample_results"]
        self.assertEqual(len(results), 2)
        by_sample = {item["sample_id"]: item for item in results}
        self.assertEqual(
            by_sample[stored],
            {
                "sample_id": stored,
                "from_status": "stored",
                "to_status": "destroyed",
                "disposition": "destroyed",
            },
        )
        self.assertEqual(
            by_sample[loaned],
            {
                "sample_id": loaned,
                "from_status": "on_loan",
                "to_status": "pending_recall",
                "disposition": "pending_recall",
            },
        )

        stored_entity = self.service.get(stored)
        self.assertEqual(stored_entity["status"], "destroyed")
        self.assertEqual(stored_entity["data"]["withdrawal_disposition"], "destroyed")
        self.assertEqual(stored_entity["data"]["withdrawn_at"], "2026-03-02T10:00:00Z")
        self.assertEqual(stored_entity["data"]["withdrawal_id"], withdrawal)

        loaned_entity = self.service.get(loaned)
        self.assertEqual(loaned_entity["status"], "pending_recall")
        self.assertEqual(
            loaned_entity["data"]["withdrawal_disposition"], "pending_recall"
        )
        self.assertEqual(loaned_entity["data"]["withdrawn_at"], "2026-03-02T10:00:00Z")
        self.assertEqual(loaned_entity["data"]["withdrawal_id"], withdrawal)

        audit = self.service.audit_log()
        sample_actions = {(row["entity_id"], row["action"]) for row in audit}
        self.assertIn((stored, "destroy"), sample_actions)
        self.assertIn((loaned, "recall"), sample_actions)
        execute_rows = [
            row
            for row in audit
            if row["entity_id"] == withdrawal and row["action"] == "execute"
        ]
        self.assertEqual(len(execute_rows), 1)
        self.assertEqual(execute_rows[0]["from_status"], "approved")
        self.assertEqual(execute_rows[0]["to_status"], "executed")
        self.assertEqual(
            execute_rows[0]["detail"]["executed_at"], "2026-03-02T10:00:00Z"
        )
        recall_rows = [
            row
            for row in audit
            if row["entity_id"] == loaned and row["action"] == "recall"
        ]
        self.assertEqual(
            recall_rows[0]["detail"]["executed_at"], "2026-03-02T10:00:00Z"
        )
        self.assertEqual(recall_rows[0]["detail"]["withdrawal_id"], withdrawal)

    def test_execute_aborts_when_foreign_sample_mixed_in(self):
        participant = self._make_participant()
        own_sample = self._make_stored_sample(participant, "B-001")
        other_participant = self._make_participant("Participant Two")
        foreign_sample = self._make_stored_sample(other_participant, "B-002")
        withdrawal = self._make_approved_withdrawal(
            participant, [own_sample, foreign_sample]
        )

        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor,
                withdrawal,
                "execute",
                {"executed_at": "2026-03-02T10:00:00Z"},
            )

        # Whole batch stopped: withdrawal still approved, no sample touched.
        self.assertEqual(self.service.get(withdrawal)["status"], "approved")
        self.assertEqual(self.service.get(own_sample)["status"], "stored")
        self.assertEqual(self.service.get(foreign_sample)["status"], "stored")
        self.assertNotIn(
            "sample_results", self.service.get(withdrawal)["data"]
        )
        execute_audit = [
            row
            for row in self.service.audit_log()
            if row["action"] in ("execute", "destroy", "recall")
        ]
        self.assertEqual(execute_audit, [])

    def test_execute_aborts_when_sample_already_destroyed(self):
        participant = self._make_participant()
        intact = self._make_stored_sample(participant, "B-001")
        disposed = self._make_stored_sample(participant, "B-002")
        self.service.transition(
            self.actor, disposed, "destroy", {"reason": "manual cleanup"}
        )
        withdrawal = self._make_approved_withdrawal(
            participant, [intact, disposed]
        )

        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor,
                withdrawal,
                "execute",
                {"executed_at": "2026-03-02T10:00:00Z"},
            )

        self.assertEqual(self.service.get(withdrawal)["status"], "approved")
        self.assertEqual(self.service.get(intact)["status"], "stored")
        self.assertEqual(self.service.get(disposed)["status"], "destroyed")

    def test_execute_aborts_when_sample_status_unknown(self):
        # A freshly collected (never stored) sample is not eligible.
        participant = self._make_participant()
        consent = self.service.create(
            self.actor,
            "consent",
            {"participant_id": participant, "scope": ["research"]},
        )["id"]
        self.service.transition(
            self.actor,
            consent,
            "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"},
        )
        collected = self.service.create(
            self.actor,
            "sample",
            {
                "participant_id": participant,
                "sample_code": "B-009",
                "collected_at": "2026-01-01",
            },
        )["id"]
        withdrawal = self._make_approved_withdrawal(participant, [collected])

        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor,
                withdrawal,
                "execute",
                {"executed_at": "2026-03-02T10:00:00Z"},
            )
        self.assertEqual(self.service.get(withdrawal)["status"], "approved")
        self.assertEqual(self.service.get(collected)["status"], "collected")

    def test_execute_aborts_when_sample_vanishes_after_approval(self):
        participant = self._make_participant()
        sample = self._make_stored_sample(participant, "B-001")
        withdrawal = self._make_approved_withdrawal(participant, [sample])
        # Simulate the approved sample disappearing outside the normal flow.
        with self.repo._connect() as connection:
            connection.execute("DELETE FROM entities WHERE id = ?", (sample,))
            connection.commit()

        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor,
                withdrawal,
                "execute",
                {"executed_at": "2026-03-02T10:00:00Z"},
            )
        self.assertEqual(self.service.get(withdrawal)["status"], "approved")

    def test_repeated_execute_returns_existing_result_without_changes(self):
        participant = self._make_participant()
        stored = self._make_stored_sample(participant, "B-001")
        loaned = self._make_stored_sample(participant, "B-002")
        self._loan_sample(loaned)
        withdrawal = self._make_approved_withdrawal(participant, [stored, loaned])

        first = self.service.transition(
            self.actor,
            withdrawal,
            "execute",
            {"executed_at": "2026-03-02T10:00:00Z"},
        )
        stored_after_first = self.service.get(stored)
        loaned_after_first = self.service.get(loaned)
        audit_count_after_first = len(self.service.audit_log())

        # Re-running with a different time must not re-record anything.
        second = self.service.transition(
            self.other,
            withdrawal,
            "execute",
            {"executed_at": "2027-01-01T00:00:00Z"},
        )

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["status"], "executed")
        self.assertEqual(second["version"], first["version"])
        self.assertEqual(
            second["data"]["sample_results"], first["data"]["sample_results"]
        )
        self.assertEqual(
            second["data"]["executed_at"], "2026-03-02T10:00:00Z"
        )
        # Samples and audit trail are untouched by the repeat call.
        self.assertEqual(self.service.get(stored), stored_after_first)
        self.assertEqual(self.service.get(loaned), loaned_after_first)
        self.assertEqual(
            len(self.service.audit_log()), audit_count_after_first
        )
        execute_rows = [
            row
            for row in self.service.audit_log()
            if row["entity_id"] == withdrawal and row["action"] == "execute"
        ]
        self.assertEqual(len(execute_rows), 1)

    def test_execute_requires_permitted_role(self):
        participant = self._make_participant()
        sample = self._make_stored_sample(participant, "B-001")
        withdrawal = self._make_approved_withdrawal(participant, [sample])
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                withdrawal,
                "execute",
                {"executed_at": "2026-03-02T10:00:00Z"},
            )
        self.assertEqual(self.service.get(withdrawal)["status"], "approved")

    def test_execute_respects_optimistic_version(self):
        participant = self._make_participant()
        sample = self._make_stored_sample(participant, "B-001")
        withdrawal = self._make_approved_withdrawal(participant, [sample])
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.actor,
                withdrawal,
                "execute",
                {"executed_at": "2026-03-02T10:00:00Z"},
                expected_version=999,
            )
        self.assertEqual(self.service.get(withdrawal)["status"], "approved")
        self.assertEqual(self.service.get(sample)["status"], "stored")


if __name__ == "__main__":
    unittest.main()
