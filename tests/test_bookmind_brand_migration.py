"""Deployment compatibility must preserve user settings and explicit overrides."""
from bookmind.compat import migrate_environment


def test_legacy_runtime_home_is_adopted_without_removing_old_value():
    env = {'DEEPTUTOR_HOME': 'D:/existing-learning', 'OTHER': 'keep'}
    migrate_environment(env)
    assert env == {
        'DEEPTUTOR_HOME': 'D:/existing-learning',
        'BOOKMIND_HOME': 'D:/existing-learning', 'OTHER': 'keep',
    }


def test_explicit_new_value_wins_even_when_empty():
    env = {'DEEPTUTOR_HOME': 'old', 'BOOKMIND_HOME': ''}
    migrate_environment(env)
    assert env['BOOKMIND_HOME'] == ''


def test_migration_is_idempotent_and_leaves_unrelated_credentials_alone():
    env = {'DEEPTUTOR_BACKEND_WORKERS': '2', 'OPENAI_API_KEY': 'example-not-a-secret'}
    migrate_environment(env)
    first = dict(env)
    migrate_environment(env)
    assert env == first
    assert env['BOOKMIND_BACKEND_WORKERS'] == '2'
    assert env['OPENAI_API_KEY'] == 'example-not-a-secret'
