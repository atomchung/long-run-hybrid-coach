"""Release configuration for the full, independently stored coach."""

VERSION = "1.4.11"
HEALTH_DATA = True
OAUTH_SCOPES_ON = ("ACTIVITY:READ", "WELLNESS:READ", "CALENDAR:WRITE", "SETTINGS:WRITE")
# Disabling acquisition does not narrow an existing full-product grant.
OAUTH_SCOPES_OFF = OAUTH_SCOPES_ON
SUBMISSION_PACKET = "chatgpt-app-submission.json"
TOOL_COUNT = 24
TOOL_CATALOGUE_SHA256 = "f6c6e155f4f43c8ee58294ddb3c5123441602ebeca8b1459561cb7e64966238f"
INSTRUCTIONS_SHA256 = "897b240fc1aeca9d979fda2ed088cfc4ef59a6c36f43cc48757d796ad9ea0dc4"
