from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_profile_ingest import (
    ProfileIngestError,
    apply_segment_context_overrides,
    canonical_digest,
    source_digest,
    validate_segment_context_overrides,
)


def segments() -> list[dict]:
    source = "Buy now"
    return [
        {
            "segment_key": "catalog-001",
            "source": source,
            "source_digest": source_digest(source),
            "context": {
                "core": {"content_type": "ui_button"},
                "extensions": {"ui": {"status": "incomplete"}},
            },
        }
    ]


def sidecar() -> dict:
    return {
        "schema": "lqe.segment-context-overrides",
        "version": 1,
        "entries": [
            {
                "segment_key": "catalog-001",
                "source_digest": source_digest("Buy now"),
                "verification_status": "verified",
                "authority": {"issuer": "pm"},
                "context_patch": {
                    "extensions": {
                        "ui": {"char_limit": 12, "platform": "mobile"}
                    }
                },
                "provenance": {
                    "record_id": "override-001",
                    "source_id": "source.context-confirmation.v1",
                },
            }
        ],
    }


DECLARED = {"ui": ["char_limit", "platform", "component_type"]}
SOURCE_IDS = ["source.context-confirmation.v1"]


class ContextOverrideContractTests(unittest.TestCase):
    def validate(self, value=None, current_segments=None):
        return validate_segment_context_overrides(
            sidecar() if value is None else value,
            segments=segments() if current_segments is None else current_segments,
            declared_extensions=DECLARED,
            source_ids=SOURCE_IDS,
        )

    def test_valid_sidecar_is_bound_and_applied_with_verified_provenance(self):
        value = sidecar()
        self.assertEqual(self.validate(value), value)
        applied = apply_segment_context_overrides(
            value,
            segments=segments(),
            declared_extensions=DECLARED,
            source_ids=SOURCE_IDS,
        )
        ui = applied[0]["context"]["extensions"]["ui"]
        self.assertEqual(ui["char_limit"], 12)
        self.assertEqual(ui["platform"], "mobile")
        evidence = applied[0]["provenance"]["context.extensions.ui.char_limit"]
        self.assertEqual(evidence["method"], "human_sidecar")
        self.assertEqual(evidence["status"], "verified")
        self.assertEqual(canonical_digest(value), canonical_digest(sidecar()))

    def test_unknown_and_duplicate_segment_keys_fail_closed(self):
        unknown = sidecar()
        unknown["entries"][0]["segment_key"] = "missing"
        with self.assertRaisesRegex(ProfileIngestError, "unknown segment_key"):
            self.validate(unknown)

        duplicate = sidecar()
        duplicate["entries"].append(deepcopy(duplicate["entries"][0]))
        with self.assertRaisesRegex(ProfileIngestError, "duplicate segment_key"):
            self.validate(duplicate)

    def test_stale_source_digest_and_unverified_entry_fail_closed(self):
        stale = sidecar()
        stale["entries"][0]["source_digest"] = "0" * 64
        with self.assertRaisesRegex(ProfileIngestError, "stale"):
            self.validate(stale)

        unverified = sidecar()
        unverified["entries"][0]["verification_status"] = "candidate"
        with self.assertRaisesRegex(ProfileIngestError, "constant 'verified'"):
            self.validate(unverified)

    def test_undeclared_extension_field_and_source_id_fail_closed(self):
        extension = sidecar()
        extension["entries"][0]["context_patch"]["extensions"] = {
            "dialogue": {"speaker_id": "speaker-a"}
        }
        with self.assertRaisesRegex(ProfileIngestError, "undeclared extension"):
            self.validate(extension)

        field = sidecar()
        field["entries"][0]["context_patch"]["extensions"]["ui"][
            "screen_name"
        ] = "shop"
        with self.assertRaisesRegex(ProfileIngestError, "undeclared fields"):
            self.validate(field)

        source = sidecar()
        source["entries"][0]["provenance"]["source_id"] = "source.unknown"
        with self.assertRaisesRegex(ProfileIngestError, "undeclared source_id"):
            self.validate(source)

    def test_input_conflict_and_incompatible_extension_status_fail_closed(self):
        current = segments()
        current[0]["context"]["extensions"]["ui"]["char_limit"] = 8
        with self.assertRaisesRegex(ProfileIngestError, "input/sidecar conflict"):
            self.validate(current_segments=current)

        current = segments()
        current[0]["context"]["extensions"]["ui"]["status"] = "not_applicable"
        with self.assertRaisesRegex(ProfileIngestError, "cannot patch extension"):
            self.validate(current_segments=current)

        current = segments()
        current[0]["context"]["extensions"] = {
            "context.ui": {"status": "incomplete", "char_limit": 8}
        }
        with self.assertRaisesRegex(ProfileIngestError, "input/sidecar conflict"):
            self.validate(current_segments=current)

    def test_sidecar_cannot_set_status_or_use_stale_input_digest(self):
        status = sidecar()
        status["entries"][0]["context_patch"]["extensions"]["ui"][
            "status"
        ] = "ready"
        with self.assertRaisesRegex(ProfileIngestError, "cannot set extension status"):
            self.validate(status)

        current = segments()
        current[0]["source_digest"] = "f" * 64
        with self.assertRaisesRegex(ProfileIngestError, "input segment.*stale"):
            self.validate(current_segments=current)

    def test_validate_overrides_cli_checks_live_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                "sidecar": root / "sidecar.json",
                "segments": root / "segments.json",
                "extensions": root / "extensions.json",
                "sources": root / "sources.json",
            }
            values = {
                "sidecar": sidecar(),
                "segments": segments(),
                "extensions": DECLARED,
                "sources": SOURCE_IDS,
            }
            for name, path in paths.items():
                path.write_text(json.dumps(values[name]), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "lqe_profile_ingest.py"),
                    "validate-overrides",
                    "--input",
                    str(paths["sidecar"]),
                    "--segments",
                    str(paths["segments"]),
                    "--declared-extensions",
                    str(paths["extensions"]),
                    "--source-ids",
                    str(paths["sources"]),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["entries"], 1)
        self.assertEqual(payload["digest"], canonical_digest(sidecar()))


if __name__ == "__main__":
    unittest.main()
