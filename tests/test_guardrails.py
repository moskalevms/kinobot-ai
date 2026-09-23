from guardrails import (
    MESSAGE_MAX_LENGTH,
    precheck_message,
    validate_classifier_output,
)


def test_precheck_blocks_too_long_message():
    assert precheck_message('а' * (MESSAGE_MAX_LENGTH + 1)) == 'length'


def test_precheck_allows_message_at_limit():
    assert precheck_message('а' * MESSAGE_MAX_LENGTH) is None


# --- A3: нормализация critics_approved в ответе классификатора ---

def test_validate_normalizes_critics_approved_bool():
    params = validate_classifier_output({'intent': 'initial', 'critics_approved': True})
    assert params['critics_approved'] is True


def test_validate_critics_approved_missing_defaults_false():
    params = validate_classifier_output({'intent': 'initial'})
    assert params['critics_approved'] is False


def test_validate_critics_approved_strings():
    """Строковые «false»/«нет» не должны становиться True."""
    for value in ('false', 'нет', '0', ''):
        params = validate_classifier_output({'intent': 'initial', 'critics_approved': value})
        assert params['critics_approved'] is False, value
    for value in ('true', 'да', '1'):
        params = validate_classifier_output({'intent': 'initial', 'critics_approved': value})
        assert params['critics_approved'] is True, value
