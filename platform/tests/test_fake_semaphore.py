"""Unit cases for the shared template-write rule in fake_semaphore.py."""

from fake_semaphore import template_write


def test_a_carried_list_wins_over_the_scalar():
    stored = template_write({"environment_id": 1, "environment_ids": [2]})
    assert stored == {"environment_id": 2, "environment_ids": [2]}


def test_an_absent_list_is_filled_from_the_scalar():
    assert template_write({"environment_id": 7}) == {"environment_id": 7, "environment_ids": [7]}


def test_a_null_list_is_filled_from_the_scalar():
    assert template_write({"environment_id": 7, "environment_ids": None}) == {
        "environment_id": 7,
        "environment_ids": [7],
    }


def test_legacy_drops_the_list_and_keeps_the_scalar():
    assert template_write({"environment_id": 1, "environment_ids": [2]}, legacy=True) == {"environment_id": 1}


def test_a_zero_scalar_binds_nothing():
    # db/Template.go:315 copies the scalar only when it is a positive ID.
    assert template_write({"environment_id": 0}) == {"environment_id": 0, "environment_ids": []}
    assert template_write({"environment_id": 0, "environment_ids": None}) == {
        "environment_id": 0,
        "environment_ids": [],
    }


def test_an_explicit_empty_list_clears_the_bindings_and_keeps_the_scalar():
    assert template_write({"environment_id": 1, "environment_ids": []}) == {
        "environment_id": 1,
        "environment_ids": [],
    }


def test_the_body_is_not_mutated():
    body = {"environment_id": 1, "environment_ids": [2]}
    template_write(body)
    assert body == {"environment_id": 1, "environment_ids": [2]}
