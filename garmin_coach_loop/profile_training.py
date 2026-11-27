"""Release configuration for the independent Plugin coach."""

VERSION = "1.4.11"
HEALTH_DATA = False
OAUTH_SCOPES_ON = ("ACTIVITY:READ", "WELLNESS:READ", "CALENDAR:WRITE", "SETTINGS:WRITE")
OAUTH_SCOPES_OFF = ("ACTIVITY:READ", "CALENDAR:WRITE", "SETTINGS:WRITE")
# This file owns packet selection; the packet owns its reviewed listing text.
SUBMISSION_PACKET = "chatgpt-app-submission.training.json"
TOOL_COUNT = 22
TOOL_CATALOGUE_SHA256 = "fc589a23db868d5ac4560e8f7db0ebb990e448a604196831edbc1f45ec37acc6"
INSTRUCTIONS_SHA256 = "d083c59b82214ac0c97d9ac6431118b8151d29fdb6d0a61e9c0dff5dc5dd3275"
