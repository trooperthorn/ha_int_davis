"""Guards against strings.json / translations/en.json drifting apart.

translations/en.json is what Home Assistant actually loads at runtime;
strings.json is a template that's easy to edit and forget to mirror. This
session found several real UX bugs caused by exactly that drift (a config
flow step missing its field labels, an entire options-flow translation
section missing entirely) - these tests catch the class of bug rather than
any one instance of it.
"""
import json
from pathlib import Path

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "davis_vantage"


def load_strings() -> dict:
    return json.loads((COMPONENT_DIR / "strings.json").read_text())


def load_translations() -> dict:
    return json.loads((COMPONENT_DIR / "translations" / "en.json").read_text())


class TestConfigFlowStepsHaveTranslations:
    def test_every_actual_step_id_has_a_translations_entry(self):
        # The step_ids DavisVantageConfigFlow actually shows a form for.
        actual_step_ids = {
            "user",
            "reconfigure",
            "interface",
            "verify",
            "options",
        }
        translated_steps = set(load_translations()["config"]["step"].keys())
        missing = actual_step_ids - translated_steps
        assert not missing, f"translations/en.json is missing config steps: {missing}"

    def test_options_step_labels_the_fields_it_actually_asks_for(self):
        data = load_translations()["config"]["step"]["options"]["data"]
        for field in ("interval", "use_loop2", "persistent_connection"):
            assert field in data, f"options form field '{field}' has no label"

    def test_options_flow_has_translations(self):
        # Regression: translations/en.json had no "options" key at all - the
        # options flow form rendered raw field keys with no labels or
        # descriptions.
        options = load_translations()["options"]
        init_data = options["step"]["init"]["data"]
        assert "use_loop2" in init_data
        assert "interval" in init_data
        assert "persistent_connection" in init_data


class TestStringsAndTranslationsStayInSync:
    def test_config_step_ids_match(self):
        strings_steps = set(load_strings()["config"]["step"].keys())
        translations_steps = set(load_translations()["config"]["step"].keys())
        assert strings_steps == translations_steps, (
            f"strings.json and translations/en.json config steps differ: "
            f"only in strings.json: {strings_steps - translations_steps}, "
            f"only in translations: {translations_steps - strings_steps}"
        )

    def test_config_error_keys_match(self):
        strings_errors = set(load_strings()["config"]["error"].keys())
        translations_errors = set(load_translations()["config"]["error"].keys())
        assert strings_errors == translations_errors

    def test_options_step_ids_match(self):
        strings_options = set(load_strings().get("options", {}).get("step", {}).keys())
        translations_options = set(
            load_translations().get("options", {}).get("step", {}).keys()
        )
        assert strings_options == translations_options

