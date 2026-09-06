"""Compare declared fields with Semaphore's omitempty JSON representations.

SurveyVar's optional zero values are omitted by the official v2.18.12 model:
https://github.com/semaphoreui/semaphore/blob/v2.18.12/db/Template.go
"""


def publication_matches(expected, actual):
    """Ignore server-added keys and omitted JSON zero values, but retain order."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            publication_matches(value, actual[key]) if key in actual
            else value is None or value is False or value == "" or value == []
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(expected) == len(actual)
                and all(publication_matches(a, b) for a, b in zip(expected, actual, strict=True)))
    if isinstance(expected, bool):
        return isinstance(actual, bool) and expected == actual
    return expected == actual


def publication_settings(record):
    """Exclude survey payload and computed runtime metadata from preservation."""
    return {key: value for key, value in record.items()
            if key not in {"survey_vars", "last_task", "tasks", "permissions"}}


class FilterModule:
    def filters(self):
        return {"publication_matches": publication_matches, "publication_settings": publication_settings}
