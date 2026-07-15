from backup_audit.config import DEFAULT_TARGETS, load_config
from backup_audit.models import TargetKind


def test_defaults_when_env_empty():
    config = load_config(env={})
    assert config.targets == DEFAULT_TARGETS
    assert len(config.targets) == 4
    assert all(t.kind == TargetKind.GITHUB_RELEASE for t in config.targets)
    locations = {t.location for t in config.targets}
    assert locations == {
        "cyntra360hub/cert-sentinel",
        "cyntra360hub/status-watch",
        "cyntra360hub/ci-triage",
        "cyntra360hub/alert-dedupe",
    }


def test_custom_targets_from_json_env():
    import json

    targets_json = json.dumps(
        [{"name": "custom", "kind": "url", "location": "https://example.test/x", "freshness_hours": 12}]
    )
    config = load_config(env={"BACKUP_AUDIT_TARGETS": targets_json})
    assert len(config.targets) == 1
    assert config.targets[0].name == "custom"
    assert config.targets[0].kind == TargetKind.URL
    assert config.targets[0].freshness_hours == 12


def test_custom_targets_freshness_defaults_when_omitted():
    import json

    targets_json = json.dumps([{"name": "custom", "kind": "file", "location": "/tmp/x"}])
    config = load_config(env={"BACKUP_AUDIT_TARGETS": targets_json})
    assert config.targets[0].freshness_hours == 24 * 30
