// The Coach server this field pairs with and sends to. A public address, not a secret,
// and compiled in rather than kept as an app property: a property's stored value
// outlives a new build on the watch, so a changed address would never arrive.
// build.py writes a test deployment's address here when given one.
const COACH_SERVER = "https://mcp.paceandstaystrong.com";
