"""The template-write rule every test fake applies to a stored template, in one place.

A fake that stores a template PUT must bind the same environment the pinned Semaphore binds,
and two fakes that each carry their own copy can disagree. The rule is Semaphore v2.19.11's
db/Template.go: ApplyLegacyEnvironmentField (lines 311-318) copies environment_id into
environment_ids only when the list is nil and the scalar is a positive ID, and leaves an
explicit [] alone, which clears every binding; FillTemplate (lines 355-357) reads the scalar
back as the list's first element when the list is not empty.
"""


def template_write(body, *, legacy=False):
    """Return the stored copy of a template PUT body.

    legacy=True models a pre-v2.19 API, which has no environment_ids field and ignores it.
    Otherwise a non-null list wins and the stored environment_id is its first element; an
    absent or null list is filled from a positive environment_id and stays empty for 0; an
    explicit [] is kept, and the scalar is then left as sent.
    """
    stored = dict(body)
    if legacy:
        stored.pop("environment_ids", None)
        return stored
    ids = stored.get("environment_ids")
    if ids is None:
        scalar = stored.get("environment_id")
        ids = [scalar] if isinstance(scalar, int) and scalar > 0 else []
    stored["environment_ids"] = list(ids)
    if ids:
        stored["environment_id"] = ids[0]
    return stored
