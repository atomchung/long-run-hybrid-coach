"""Release configuration for the full, independently stored coach."""

VERSION = "1.4.11"
HEALTH_DATA = True
OAUTH_SCOPES_ON = ("ACTIVITY:READ", "WELLNESS:READ", "CALENDAR:WRITE", "SETTINGS:WRITE")
# Disabling acquisition does not narrow an existing full-product grant.
OAUTH_SCOPES_OFF = OAUTH_SCOPES_ON
SUBMISSION_PACKET = "chatgpt-app-submission.json"
TOOL_COUNT = 24
TOOL_CATALOGUE_SHA256 = "70247010eddc172308ee39dee1f32be95c0dba2ed48bccf364224a370fcef3f7"
INSTRUCTIONS_SHA256 = "897b240fc1aeca9d979fda2ed088cfc4ef59a6c36f43cc48757d796ad9ea0dc4"
