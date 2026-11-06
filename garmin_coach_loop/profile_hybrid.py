"""Release configuration for the full, independently stored coach."""

VERSION = "1.4.11"
HEALTH_DATA = True
OAUTH_SCOPES_ON = ("ACTIVITY:READ", "WELLNESS:READ", "CALENDAR:WRITE", "SETTINGS:WRITE")
# Disabling acquisition does not narrow an existing full-product grant.
OAUTH_SCOPES_OFF = OAUTH_SCOPES_ON
SUBMISSION_PACKET = "chatgpt-app-submission.json"
TOOL_COUNT = 23
TOOL_CATALOGUE_SHA256 = "a92d1f4da5461a6ea47038d26614c7d2de7b11a6188b4744126bc6985bf99d2b"
INSTRUCTIONS_SHA256 = "897b240fc1aeca9d979fda2ed088cfc4ef59a6c36f43cc48757d796ad9ea0dc4"
