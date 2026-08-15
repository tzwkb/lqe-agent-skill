import copy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lqe_context import (
    descriptor_registry,
    extract_segment_context,
    module_review_equivalence_key,
    module_review_equivalence_payload,
    project_segment_for_module,
    resolve_context_columns,
)


CORE = "context.core@1"
DIALOGUE = "context.dialogue@1"
UI = "context.ui@1"
MARKETING = "context.marketing@1"


def generic_profile(*extensions):
    configurations = {
        CORE: {
            "columns": {
                "content_type": ["Content Kind"],
                "context_note": ["Context Note"],
            }
        },
        DIALOGUE: {
            "columns": {
                "speaker_id": ["Speaker"],
                "addressee_ids": ["Addressees"],
                "scene_id": ["Scene"],
                "relationship_stage": ["Relationship Stage"],
                "scene_tone": ["Scene Tone"],
            },
            "applies_when": {"content_type": ["dialogue"]},
        },
        UI: {
            "columns": {
                "screen_id": ["Screen"],
                "component_type": ["Component"],
                "platform": ["Platform"],
                "char_limit": ["Character Limit"],
                "interaction_state": ["Interaction State"],
            },
            "applies_when": {"content_type": ["ui"]},
        },
        MARKETING: {
            "columns": {
                "campaign_id": ["Campaign"],
                "channel": ["Channel"],
                "market": ["Market"],
                "audience": ["Audience"],
                "cta_type": ["CTA Type"],
                "brand_tone": ["Brand Tone"],
            },
            "applies_when": {"content_type": ["marketing"]},
        },
    }
    capabilities = {
        CORE: {"required": True, "config": configurations[CORE]},
    }
    for capability_id in extensions:
        capabilities[capability_id] = {
            "required": False,
            "config": configurations[capability_id],
        }
    return {"capabilities": capabilities}


def runtime_registry(profile):
    resolution = {
        "enabled": {
            capability_id: {"effect": "enforce"}
            for capability_id in profile["capabilities"]
        }
    }
    return descriptor_registry(profile, capability_resolution=resolution)


def extract(profile, values, *, row_number=2):
    registry = runtime_registry(profile)
    headers = list(values)
    row = [values[header] for header in headers]
    mappings = resolve_context_columns(
        headers,
        registry,
        profile=profile,
    )
    context = extract_segment_context(
        row,
        mappings,
        registry,
        profile=profile,
        source_provenance={"row": row_number},
    )
    return registry, context


def segment(context, *, key="business-key"):
    return {
        "id": 1,
        "segment_key": key,
        "source": "Source text",
        "target": "Target text",
        "input_status": "ready",
        "protected": False,
        "protected_texts": [],
        "resolved_constraints": [],
        "context": context,
    }


class GenericContentProfileTests(unittest.TestCase):
    def test_ui_only_extraction_projection_and_equivalence_digest(self):
        profile = generic_profile(UI)
        registry, context = extract(
            profile,
            {
                "Content Kind": "ui",
                "Screen": "settings",
                "Component": "button",
                "Platform": "handheld",
                "Character Limit": "24",
                "Interaction State": "disabled",
            },
        )

        self.assertEqual(set(registry), {CORE, UI})
        self.assertEqual(context["status"], "ready")
        self.assertEqual(
            context["extensions"]["ui"],
            {
                "status": "ready",
                "screen_id": "settings",
                "component_type": "button",
                "platform": "handheld",
                "char_limit": 24,
                "interaction_state": "disabled",
            },
        )

        base = segment(context)
        accuracy = project_segment_for_module(base, "accuracy", registry)
        grammar = project_segment_for_module(base, "grammar", registry)
        self.assertEqual(
            accuracy["extensions"]["ui"],
            {
                "status": "ready",
                "screen_id": "settings",
                "component_type": "button",
                "interaction_state": "disabled",
            },
        )
        self.assertEqual(
            grammar["extensions"]["ui"],
            {"status": "ready", "char_limit": 24},
        )

        metadata_changed = copy.deepcopy(base)
        metadata_changed["segment_key"] = "another-business-key"
        metadata_changed["context"]["provenance"][
            "context.extensions.ui.platform"
        ]["row"] = 999
        self.assertEqual(
            module_review_equivalence_key(base, "naturalness", registry),
            module_review_equivalence_key(
                metadata_changed, "naturalness", registry
            ),
        )

        platform_changed = copy.deepcopy(base)
        platform_changed["context"]["extensions"]["ui"]["platform"] = "desktop"
        self.assertEqual(
            module_review_equivalence_key(base, "accuracy", registry),
            module_review_equivalence_key(platform_changed, "accuracy", registry),
        )
        self.assertNotEqual(
            module_review_equivalence_key(base, "naturalness", registry),
            module_review_equivalence_key(
                platform_changed, "naturalness", registry
            ),
        )
        digest = module_review_equivalence_key(base, "grammar", registry)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        payload = module_review_equivalence_payload(base, "accuracy", registry)
        self.assertNotIn("segment_key", repr(payload))
        self.assertNotIn("provenance", repr(payload))

    def test_marketing_only_extraction_projection_and_equivalence_digest(self):
        profile = generic_profile(MARKETING)
        registry, context = extract(
            profile,
            {
                "Content Kind": "marketing",
                "Campaign": "spring-launch",
                "Channel": "storefront",
                "Market": "global",
                "Audience": "returning-users",
                "CTA Type": "download",
                "Brand Tone": "energetic",
            },
        )

        self.assertEqual(set(registry), {CORE, MARKETING})
        self.assertEqual(context["extensions"]["marketing"]["status"], "ready")
        base = segment(context)
        accuracy = project_segment_for_module(base, "accuracy", registry)
        naturalness = project_segment_for_module(base, "naturalness", registry)
        suggestions = project_segment_for_module(base, "suggestions", registry)
        self.assertEqual(
            accuracy["extensions"]["marketing"],
            {
                "status": "ready",
                "audience": "returning-users",
                "cta_type": "download",
            },
        )
        self.assertNotIn("campaign_id", naturalness["extensions"]["marketing"])
        self.assertEqual(
            suggestions["extensions"]["marketing"]["campaign_id"],
            "spring-launch",
        )

        campaign_changed = copy.deepcopy(base)
        campaign_changed["context"]["extensions"]["marketing"]["campaign_id"] = (
            "summer-launch"
        )
        self.assertEqual(
            module_review_equivalence_key(base, "naturalness", registry),
            module_review_equivalence_key(
                campaign_changed, "naturalness", registry
            ),
        )
        self.assertNotEqual(
            module_review_equivalence_key(base, "suggestions", registry),
            module_review_equivalence_key(campaign_changed, "suggestions", registry),
        )

        channel_changed = copy.deepcopy(base)
        channel_changed["context"]["extensions"]["marketing"]["channel"] = "email"
        self.assertEqual(
            module_review_equivalence_key(base, "accuracy", registry),
            module_review_equivalence_key(channel_changed, "accuracy", registry),
        )
        self.assertNotEqual(
            module_review_equivalence_key(base, "naturalness", registry),
            module_review_equivalence_key(channel_changed, "naturalness", registry),
        )

    def test_mixed_profile_keeps_content_extensions_row_scoped(self):
        profile = generic_profile(DIALOGUE, UI, MARKETING)
        rows = {
            "dialogue": {
                "Content Kind": "dialogue",
                "Speaker": "guide",
                "Addressees": "player, companion",
                "Scene": "arrival",
                "Relationship Stage": "new",
                "Scene Tone": "urgent",
            },
            "ui": {
                "Content Kind": "ui",
                "Screen": "inventory",
                "Component": "label",
                "Platform": "desktop",
                "Character Limit": 18,
                "Interaction State": "active",
            },
            "marketing": {
                "Content Kind": "marketing",
                "Campaign": "release",
                "Channel": "social",
                "Market": "global",
                "Audience": "new-users",
                "CTA Type": "learn-more",
                "Brand Tone": "informative",
            },
        }
        contexts = {}
        registry = None
        for index, (content_type, values) in enumerate(rows.items(), start=2):
            registry, contexts[content_type] = extract(
                profile, values, row_number=index
            )

        self.assertEqual(set(registry), {CORE, DIALOGUE, UI, MARKETING})
        for content_type, context in contexts.items():
            self.assertEqual(context["status"], "ready")
            for extension in ("dialogue", "ui", "marketing"):
                expected = "ready" if extension == content_type else "not_applicable"
                self.assertEqual(
                    context["extensions"][extension]["status"],
                    expected,
                )

        projected_extensions = {}
        keys = {}
        for content_type, context in contexts.items():
            item = segment(context, key=f"row-{content_type}")
            projection = project_segment_for_module(item, "naturalness", registry)
            projected_extensions[content_type] = projection["extensions"]
            keys[content_type] = module_review_equivalence_key(
                item, "naturalness", registry
            )
        for content_type, extensions in projected_extensions.items():
            for extension in ("dialogue", "ui", "marketing"):
                if extension == content_type:
                    self.assertGreater(len(extensions[extension]), 1)
                    self.assertEqual(extensions[extension]["status"], "ready")
                else:
                    self.assertEqual(
                        extensions[extension], {"status": "not_applicable"}
                    )
        self.assertEqual(len(set(keys.values())), 3)

        dialogue = segment(contexts["dialogue"])
        speaker_changed = copy.deepcopy(dialogue)
        speaker_changed["context"]["extensions"]["dialogue"]["speaker_id"] = (
            "companion"
        )
        self.assertNotEqual(
            module_review_equivalence_key(dialogue, "accuracy", registry),
            module_review_equivalence_key(
                speaker_changed, "accuracy", registry
            ),
        )


if __name__ == "__main__":
    unittest.main()
