from pathlib import Path
import sys
import tempfile
import unittest

import openpyxl


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lqe_io
from lqe_corrections import CheckFormatError, validate_reference_target
from lqe_engine import build_review_policy
from lqe_suggestions import (
    DRAFT_SCHEMA,
    DRAFT_VERSION,
    _normalize_selection,
    build_candidate_artifact,
    build_suggestion_packet as _build_suggestion_packet,
    cmd_publish,
    validate_generation_draft,
)


WORKER_CONTEXT_MANIFEST_DIGEST = "a" * 64
GENERATION_RECEIPT = {"worker_id": "generation-worker", "run_id": "generation-run"}


def source_semantics():
    return {
        "subjects": ["source subject"],
        "actions": ["source action"],
        "objects": [],
        "negation": {"present": False, "scope": None},
        "polarity": "affirmative",
        "modality": [],
        "speech_act": "statement",
        "text_function": "inform",
        "intensity": "neutral",
        "omitted_source_elements": [],
        "unsupported_additions": [],
    }


def tone_decision():
    return {
        "register": "neutral",
        "politeness": "neutral",
        "depends_on_dialogue_context": False,
        "evidence": [{"type": "source_form", "value": "neutral source"}],
        "uncertainties": [],
    }


def suggestion_entry(segment_id, reference_target):
    return {
        "id": segment_id,
        "reference_target": reference_target,
        "source_semantics": source_semantics(),
        "tone_decision": tone_decision(),
    }


def build_suggestion_packet(*args, **kwargs):
    kwargs.setdefault(
        "worker_context_manifest_digest",
        WORKER_CONTEXT_MANIFEST_DIGEST,
    )
    return _build_suggestion_packet(*args, **kwargs)


def issue(
    comment,
    *,
    needs_confirmation=False,
    edit=None,
    category="Unidiomatic",
    severity="Major",
):
    return {
        "category": category,
        "severity": severity,
        "comment": comment,
        "needs_confirmation": needs_confirmation,
        "edit": edit,
    }


class ReferenceSuggestionContractTests(unittest.TestCase):
    def test_packet_uses_major_critical_candidates_and_agent_judgment(self):
        segments = [
            {
                "id": 0,
                "source": "Source 0",
                "target": "Target 0",
                "content_type": "UI/界面文本",
            },
            {"id": 1, "source": "Source 1", "target": "Target 1"},
            {
                "id": 2,
                "source": "Source 2",
                "target": "Target 2",
                "text_type_context": "主线剧情对话",
            },
            {
                "id": 3,
                "source": "Source 3",
                "target": "Target 3",
                "protected": True,
            },
        ]
        results = [
            {
                "id": 0,
                "errors": [
                    issue("Major problem."),
                    issue(
                        "Minor problem.",
                        severity="Minor",
                        needs_confirmation=True,
                    ),
                ],
                "corrected": None,
            },
            {
                "id": 1,
                "errors": [
                    issue(
                        "Minor only.",
                        severity="Minor",
                        needs_confirmation=True,
                    )
                ],
                "corrected": None,
            },
            {
                "id": 2,
                "errors": [issue("Critical problem.", severity="Critical")],
                "corrected": None,
            },
            {
                "id": 3,
                "errors": [issue("Protected problem.")],
                "corrected": None,
            },
        ]

        packet = build_suggestion_packet(segments, None, results)

        self.assertEqual(
            packet["selection"],
            {
                "categories": [],
                "severities": ["Critical", "Major"],
                "only_missing": False,
            },
        )
        self.assertEqual(packet["reviewed_ids"], [0, 2, 3])
        self.assertEqual(
            [error["severity"] for error in packet["segments"][0]["known_issues"]],
            ["Major", "Minor"],
        )
        self.assertEqual(len(packet["segments"][0]["trigger_issue_ids"]), 1)
        self.assertEqual(
            packet["segments"][0]["content_type"],
            "UI/界面文本",
        )
        self.assertEqual(
            packet["segments"][1]["text_type_context"],
            "主线剧情对话",
        )
        self.assertTrue(
            packet["instructions"]["sparse_suggestions_allowed"]
        )
        self.assertTrue(packet["instructions"]["agent_decides_reliability"])
        self.assertEqual(
            packet["excluded_segments"],
            [{
                "id": 3,
                "risk_route": "hard_reject",
                "reason_codes": ["SEGMENT_PROTECTED"],
            }],
        )

        with self.assertRaisesRegex(ValueError, "only support Major/Critical"):
            _normalize_selection(
                {
                    "categories": [],
                    "severities": ["Minor"],
                    "only_missing": False,
                }
            )

    def test_packet_excludes_findings_that_terminology_review_left_unresolved(self):
        segments = [
            {"id": 0, "source": "蓝果", "target": "blue"},
            {"id": 1, "source": "红果", "target": "red"},
            {"id": 2, "source": "Source", "target": "Target"},
            {"id": 3, "source": "花店", "target": ""},
        ]
        terminology_provenance = {
            "finding_origin": "ai_module",
            "ai_reviewed": True,
            "ai_edited": False,
            "review_module": "terminology",
            "reviewed_segment_id": 0,
            "edit_origin": None,
        }
        results = [
            {
                "id": 0,
                "errors": [{
                    **issue(
                        "No exact glossary entry; human confirmation is required.",
                        category="Inconsistency",
                        severity="Minor",
                        needs_confirmation=True,
                    ),
                    "review_provenance": terminology_provenance,
                }],
                "corrected": None,
            },
            {
                "id": 1,
                "errors": [{
                    **issue(
                        "The terminology decision is unresolved.",
                        category="Terminology",
                        needs_confirmation=True,
                    ),
                    "term_source": "红果",
                    "expected_targets": ["red fruit"],
                    "term_spans": {
                        "source": [{"start": 0, "end": 2, "text": "红果"}],
                        "target": [{"start": 0, "end": 3, "text": "red"}],
                    },
                }],
                "corrected": None,
            },
            {
                "id": 2,
                "errors": [issue(
                    "A full accuracy rewrite is still useful.",
                    category="Mistranslation",
                    needs_confirmation=True,
                )],
                "corrected": None,
            },
            {
                "id": 3,
                "errors": [{
                    **issue(
                        "The confirmed term is omitted and needs confirmation.",
                        category="Terminology",
                        needs_confirmation=True,
                    ),
                    "term_source": "花店",
                    "expected_targets": ["꽃집"],
                    "term_spans": {
                        "source": [{"start": 0, "end": 2, "text": "花店"}],
                        "target": [],
                    },
                }],
                "corrected": None,
            },
        ]

        packet = build_suggestion_packet(
            segments,
            None,
            results,
            review_policy=build_review_policy("full"),
        )

        self.assertEqual(packet["reviewed_ids"], [0, 1, 2, 3])
        self.assertEqual(
            packet["excluded_segments"],
            [
                {"id": 0, "risk_route": "hard_reject", "reason_codes": ["UNRESOLVED_TERMINOLOGY_REVIEW"]},
                {"id": 1, "risk_route": "hard_reject", "reason_codes": ["UNRESOLVED_TERMINOLOGY_REVIEW"]},
                {"id": 3, "risk_route": "hard_reject", "reason_codes": ["UNRESOLVED_TERMINOLOGY_REVIEW"]},
            ],
        )
        self.assertTrue(
            packet["instructions"][
                "unresolved_terminology_segments_hard_rejected"
            ]
        )

    def test_packet_keeps_structured_evidence_for_resolved_terminology(self):
        segments = [{"id": 0, "source": "红果", "target": "red"}]
        term_fields = {
            "term_source": "红果",
            "expected_targets": ["red fruit"],
            "term_spans": {
                "source": [{"start": 0, "end": 2, "text": "红果"}],
                "target": [{"start": 0, "end": 3, "text": "red"}],
            },
        }
        results = [{
            "id": 0,
            "errors": [{
                **issue("Use the confirmed project term.", category="Terminology"),
                **term_fields,
            }],
            "corrected": None,
        }]

        packet = build_suggestion_packet(segments, None, results)

        self.assertEqual(packet["reviewed_ids"], [0])
        projected = packet["segments"][0]["known_issues"][0]
        for field, value in term_fields.items():
            self.assertEqual(projected[field], value)

    def test_near_term_is_explicitly_non_authorizing_and_generation_is_source_first(self):
        segments = [{
            "id": 0,
            "source": "蓝果",
            "target": "blue",
            "term_near": [{
                "seg": "蓝果",
                "tb_src": "红果",
                "tb_tgt": "red fruit",
                "sim": 0.75,
            }],
        }]
        results = [{
            "id": 0,
            "errors": [issue(
                "The sentence meaning needs a complete rewrite.",
                category="Mistranslation",
            )],
            "corrected": None,
        }]

        packet = build_suggestion_packet(segments, None, results)
        constraints = packet["segments"][0]["generation_constraints"]
        self.assertEqual(constraints["term_actions"]["term_resolution"], "not_governed")
        self.assertEqual(
            constraints["term_actions"]["non_authorizing_evidence"],
            [{
                "source_term": "红果",
                "cannot_authorize_source": "蓝果",
                "target": "red fruit",
                "reason": (
                    "near or different source term cannot grant glossary authority"
                ),
            }],
        )
        contract = packet["instructions"]["source_first_contract"]
        self.assertEqual(contract["derive_semantic_propositions_from"], "source")
        self.assertTrue(contract["do_not_use_current_target_as_semantic_skeleton"])

    def test_packet_hard_rejects_blocked_segment(self):
        segments = [{
            "id": 0,
            "source": "Source",
            "target": "Target",
            "input_status": "blocked",
        }]
        results = [{
            "id": 0,
            "errors": [issue("Input cannot be reviewed.")],
            "corrected": None,
        }]

        packet = build_suggestion_packet(segments, None, results)

        self.assertEqual(packet["segments"], [])
        self.assertEqual(
            packet["excluded_segments"],
            [{
                "id": 0,
                "risk_route": "hard_reject",
                "reason_codes": ["INPUT_BLOCKED"],
            }],
        )

    def test_generation_draft_cannot_reintroduce_excluded_terminology_segment(self):
        segments = [{"id": 0, "source": "蓝果", "target": "blue"}]
        results = [{
            "id": 0,
            "errors": [{
                **issue(
                    "No exact glossary entry.",
                    category="Inconsistency",
                    needs_confirmation=True,
                ),
                "review_provenance": {
                    "review_module": "terminology",
                },
            }],
            "corrected": None,
        }]
        packet = build_suggestion_packet(segments, None, results)
        draft = {
            "schema": DRAFT_SCHEMA,
            "version": DRAFT_VERSION,
            "packet_digest": packet["packet_digest"],
            "worker_context_manifest_digest": packet[
                "worker_context_manifest_digest"
            ],
            "worker_receipt": GENERATION_RECEIPT,
            "selection": packet["selection"],
            "reviewed_ids": packet["reviewed_ids"],
            "entries": [suggestion_entry(0, "red fruit")],
            "abstained_ids": [],
            "abstention_reasons": [],
        }
        with self.assertRaisesRegex(ValueError, "coverage"):
            validate_generation_draft(draft, packet)

    def test_legacy_publish_fails_with_new_cli_instructions(self):
        with self.assertRaisesRegex(ValueError, "publish-candidates"):
            cmd_publish(None)

    def test_full_reference_target_preserves_protected_signature(self):
        segment = {
            "id": 7,
            "target": "Use {name}<b>\nKeep RAW",
            "protected_texts": ["RAW"],
        }
        accepted = "请使用 {name}<b>\n并保留 RAW"
        self.assertEqual(
            validate_reference_target(segment, accepted),
            accepted,
        )
        for rejected in (
            "请使用名字<b>\n并保留 RAW",
            "请使用 {name}<b> 并保留 RAW",
            "请使用 {name}<b>\n并保留",
        ):
            with self.subTest(rejected=rejected):
                with self.assertRaises(CheckFormatError):
                    validate_reference_target(segment, rejected)

        protected = {**segment, "protected": True}
        with self.assertRaises(CheckFormatError):
            validate_reference_target(protected, accepted)

    def test_packet_binding_ignores_score_only_repeated_flag(self):
        segments = [{"id": 0, "source": "Source", "target": "Target"}]
        results = [{
            "id": 0,
            "errors": [issue("Awkward.")],
            "corrected": None,
        }]
        manifest = {
            "manifest_digest": "manifest",
            "state_fingerprint": "state",
        }
        first = build_suggestion_packet(segments, manifest, results)
        repeated = [{
            **results[0],
            "errors": [{**results[0]["errors"][0], "repeated": True}],
        }]
        second = build_suggestion_packet(segments, manifest, repeated)
        self.assertEqual(first["packet_digest"], second["packet_digest"])

    def test_candidate_publisher_hard_rejects_unsafe_suggestion(self):
        segments = [{
            "id": 0,
            "source": "Source",
            "target": "Target {0}",
        }]
        results = [{
            "id": 0,
            "errors": [issue("Awkward.")],
            "corrected": None,
        }]
        packet = build_suggestion_packet(segments, None, results)

        draft = {
            "schema": DRAFT_SCHEMA,
            "version": DRAFT_VERSION,
            "packet_digest": packet["packet_digest"],
            "worker_context_manifest_digest": packet[
                "worker_context_manifest_digest"
            ],
            "worker_receipt": GENERATION_RECEIPT,
            "selection": packet["selection"],
            "reviewed_ids": packet["reviewed_ids"],
            "entries": [suggestion_entry(0, "Better target")],
            "abstained_ids": [],
            "abstention_reasons": [],
        }
        artifact = build_candidate_artifact(packet, draft, segments)
        self.assertEqual(artifact["entries"], [])
        self.assertEqual(
            artifact["routes"],
            [{
                "id": 0,
                "risk_route": "hard_reject",
                "reason_codes": ["DETERMINISTIC_VALIDATION_FAILED"],
            }],
        )

    def test_dialogue_dependent_tone_fails_closed_when_context_is_incomplete(self):
        segments = [{
            "id": 0,
            "source": "Follow me home!",
            "target": "Come with me.",
            "context": {
                "context_contract_version": 1,
                "status": "context_incomplete",
                "core": {"content_type": "dialogue"},
                "extensions": {
                    "dialogue": {
                        "status": "incomplete",
                        "speaker_id": "speaker",
                    }
                },
                "provenance": {},
                "missing_required": [],
            },
        }]
        results = [{
            "id": 0,
            "errors": [issue("The character voice is wrong.")],
            "corrected": None,
        }]
        context_view_basis = {
            "source_modules": ["accuracy", "suggestions"],
            "merged_view": {
                "capabilities": ["context.core@1", "context.dialogue@1"]
            },
        }
        packet = build_suggestion_packet(
            segments,
            None,
            results,
            context_view_basis=context_view_basis,
        )
        dependent_tone = tone_decision()
        dependent_tone["depends_on_dialogue_context"] = True
        draft = {
            "schema": DRAFT_SCHEMA,
            "version": DRAFT_VERSION,
            "packet_digest": packet["packet_digest"],
            "worker_context_manifest_digest": packet[
                "worker_context_manifest_digest"
            ],
            "worker_receipt": GENERATION_RECEIPT,
            "selection": packet["selection"],
            "reviewed_ids": packet["reviewed_ids"],
            "entries": [{
                "id": 0,
                "reference_target": "Follow me home!",
                "source_semantics": source_semantics(),
                "tone_decision": dependent_tone,
            }],
            "abstained_ids": [],
            "abstention_reasons": [],
        }

        artifact = build_candidate_artifact(packet, draft, segments)

        self.assertEqual(
            artifact["routes"][0]["risk_route"],
            "hard_reject",
        )
        self.assertIn(
            "DIALOGUE_CONTEXT_INCOMPLETE",
            artifact["routes"][0]["reason_codes"],
        )

    def test_full_mode_includes_minor_candidates(self):
        segments = [{"id": 0, "source": "Source", "target": "Target"}]
        results = [{
            "id": 0,
            "errors": [
                issue(
                    "Minor problem.",
                    severity="Minor",
                    needs_confirmation=False,
                )
            ],
            "corrected": None,
        }]
        policy = build_review_policy("full")

        packet = build_suggestion_packet(
            segments,
            None,
            results,
            review_policy=policy,
        )

        self.assertEqual(packet["reviewed_ids"], [0])
        self.assertEqual(
            packet["selection"]["severities"],
            ["Neutral", "Minor", "Major", "Critical"],
        )
        self.assertFalse(
            packet["instructions"]["text_type_routing_enabled"]
        )


class ReviewerWorkbookTests(unittest.TestCase):
    def test_minor_only_segment_never_displays_suggested_translation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "minor-review.xlsx"
            state = {
                "input_path": str(Path(tempdir) / "source.xlsx"),
                "headers": ["原文", "译文"],
                "rows_raw": [["Source", "Target"]],
                "target_col": 1,
                "segments": [
                    {"id": 0, "source": "Source", "target": "Target"}
                ],
                "wordcount": 1,
            }
            results = [
                {
                    "id": 0,
                    "errors": [
                        issue(
                            "Minor problem.",
                            severity="Minor",
                            needs_confirmation=True,
                        )
                    ],
                    "corrected": None,
                }
            ]
            lqe_io._build_xlsx(
                state,
                [{"iteration": 0, "errors": results}],
                99,
                98,
                output,
                reference_suggestions={0: "Must not be shown"},
            )

            workbook = openpyxl.load_workbook(output, data_only=True)
            try:
                sheet = workbook["LQE Results"]
                self.assertIsNone(sheet.cell(2, 4).value)
                self.assertEqual(
                    sheet.cell(2, 5).value,
                    "未生成建议，需人工处理",
                )
            finally:
                workbook.close()

    def test_full_mode_displays_minor_suggested_translation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "minor-full-review.xlsx"
            state = {
                "input_path": str(Path(tempdir) / "source.xlsx"),
                "headers": ["原文", "译文"],
                "rows_raw": [["Source", "Target"]],
                "target_col": 1,
                "review_policy": build_review_policy("full"),
                "segments": [
                    {"id": 0, "source": "Source", "target": "Target"}
                ],
                "wordcount": 1,
            }
            results = [
                {
                    "id": 0,
                    "errors": [
                        issue(
                            "Minor problem.",
                            severity="Minor",
                            needs_confirmation=False,
                            edit={
                                "from": "Target",
                                "to": "Better target",
                                "evidence": None,
                            },
                        )
                    ],
                    "corrected": "Better target",
                }
            ]
            lqe_io._build_xlsx(
                state,
                [{"iteration": 0, "errors": results}],
                99,
                98,
                output,
            )

            workbook = openpyxl.load_workbook(output, data_only=True)
            try:
                sheet = workbook["LQE Results"]
                headers = [cell.value for cell in sheet[1]]
                self.assertEqual(
                    sheet.cell(2, headers.index("AI/建议译文") + 1).value,
                    "Better target",
                )
            finally:
                workbook.close()

    def test_reviewer_view_has_two_visible_sheets_and_exact_statuses(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "review.xlsx"
            segments = [
                {"id": index, "source": f"Source {index}", "target": f"Target {index}"}
                for index in range(6)
            ]
            segments[4]["protected"] = True
            state = {
                "input_path": str(Path(tempdir) / "source.xlsx"),
                "headers": ["原文", "译文"],
                "rows_raw": [
                    [segment["source"], segment["target"]]
                    for segment in segments
                ],
                "target_col": 1,
                "segments": segments,
                "wordcount": 6,
            }
            safe_edit = {
                "from": "Target",
                "to": "Revised",
                "evidence": None,
            }
            results = [
                {
                    "id": 0,
                    "errors": [issue("Safe.", edit=safe_edit)],
                    "corrected": "Revised 0",
                },
                {
                    "id": 1,
                    "errors": [issue("Needs judgment.", needs_confirmation=True)],
                    "corrected": None,
                },
                {
                    "id": 2,
                    "errors": [
                        issue("Safe part.", edit=safe_edit),
                        issue("Open question.", needs_confirmation=True),
                    ],
                    "corrected": "Revised 2",
                },
                {
                    "id": 3,
                    "errors": [issue("No reliable rewrite.", needs_confirmation=True)],
                    "corrected": None,
                },
                {"id": 4, "errors": [], "corrected": None},
                {"id": 5, "errors": [], "corrected": None},
            ]
            history = [{"iteration": 0, "errors": results}]
            lqe_io._build_xlsx(
                state,
                history,
                99,
                98,
                output,
                reference_suggestions={1: "Reference 1"},
            )

            workbook = openpyxl.load_workbook(output, data_only=True)
            try:
                visible = [
                    sheet.title
                    for sheet in workbook.worksheets
                    if sheet.sheet_state == "visible"
                ]
                self.assertEqual(
                    visible,
                    ["说明·导读", "LQA Scorecard", "LQE Results"],
                )
                self.assertEqual(workbook.active.title, "说明·导读")
                guide = workbook["说明·导读"]
                guide_values = {
                    cell.value
                    for row in guide.iter_rows()
                    for cell in row
                    if cell.value is not None
                }
                for section in (
                    "三步读报告",
                    "LQA Scorecard 怎么读",
                    "审校区 10 列说明",
                    "建议状态说明",
                    "审校结论说明",
                    "阅读与交付提示",
                ):
                    self.assertIn(section, guide_values)

                results_sheet = workbook["LQE Results"]
                headers = [cell.value for cell in results_sheet[1]]
                self.assertEqual(
                    headers[:10],
                    [
                        "Segment ID",
                        "原文",
                        "原译",
                        "AI/建议译文",
                        "建议状态",
                        "错误类别",
                        "严重度",
                        "问题说明",
                        "审校结论",
                        "审校终稿或备注",
                    ],
                )
                rows = {
                    results_sheet.cell(row, 1).value: row
                    for row in range(2, results_sheet.max_row + 1)
                    if results_sheet.cell(row, 1).value is not None
                }
                expected_statuses = {
                    0: "可直接采用",
                    1: "建议待确认",
                    2: "部分修正，仍需确认",
                    3: "未生成建议，需人工处理",
                    4: "已保护",
                    5: None,
                }
                for segment_id, status in expected_statuses.items():
                    self.assertEqual(
                        results_sheet.cell(rows[segment_id], 5).value,
                        status,
                    )
                self.assertTrue(results_sheet.row_dimensions[rows[5]].hidden)
                hidden_issue_rows = [
                    row
                    for row in range(rows[2] + 1, rows[3])
                    if results_sheet.row_dimensions[row].hidden
                ]
                self.assertTrue(hidden_issue_rows)
                self.assertTrue(
                    all(
                        results_sheet.column_dimensions[
                            openpyxl.utils.get_column_letter(column)
                        ].hidden
                        for column in range(11, results_sheet.max_column + 1)
                    )
                )
                self.assertEqual(len(results_sheet.data_validations.dataValidation), 1)

                scorecard = workbook["LQA Scorecard"]
                detail_header = next(
                    row
                    for row in range(1, scorecard.max_row + 1)
                    if scorecard.cell(row, 1).value == "Segment ID"
                )
                self.assertFalse(scorecard.row_dimensions[detail_header].hidden)
                self.assertEqual(
                    [cell.value for cell in scorecard[detail_header]],
                    headers[:10],
                )
                self.assertFalse(
                    any(
                        scorecard.row_dimensions[row].hidden
                        for row in range(1, scorecard.max_row + 1)
                    )
                )
            finally:
                workbook.close()


if __name__ == "__main__":
    unittest.main()
