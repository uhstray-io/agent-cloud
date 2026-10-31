"""The template-write rule every test fake applies to a stored template, in one place.

A fake that stores a template PUT must bind the same environment the pinned Semaphore binds,
and two fakes that each carry their own copy can disagree. The rule is Semaphore v2.19.11's
ApplyLegacyEnvironmentField in db/Template.go: a body's environment_ids wins when it is a
non-null list; otherwise environment_id is copied into the list.
"""


def template_write(body, *, legacy=False):
    """Return the stored copy of a template PUT body.

    legacy=True models a pre-v2.19 API, which has no environment_ids field and ignores it.
    Otherwise an absent or null environment_ids is filled from environment_id, a non-empty
    list wins over environment_id, and the stored environment_id is the list's first element.
    An empty list is refused: its behaviour is not modelled, so a test that sends one fails
    loudly instead of getting a guessed binding.
    """
    stored = dict(body)
    if legacy:
        stored.pop("environment_ids", None)
        return stored
    ids = stored.get("environment_ids")
    if ids is None:
        ids = [stored["environment_id"]]
    if not ids:
        raise AssertionError("environment_ids [] is not modelled")
    stored["environment_ids"] = list(ids)
    stored["environment_id"] = ids[0]
    return stored
