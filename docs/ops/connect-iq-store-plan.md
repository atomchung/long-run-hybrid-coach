# Publishing the watch companion on the Connect IQ Store — plan

Status as of 2026-11-08: approved by the owner as recommended below. Nothing has been
submitted to Garmin or accepted there.

Order of work:

1. Device pairing storage in the identity registry (`identity.py`): PR #534, merged.
2. Pairing routes, the `connectWatch` tool, per-owner device credentials in place of the
   environment variables, and the watch field's pairing screen: built after PR #533
   merged; not yet deployed.
3. Store build, listing and privacy page; submission from the owner's developer account.
4. Next milestone: own account (Intervals.icu plus Google), the Coach without Intervals.icu,
   and the device app.

What ships: a Connect IQ **data field** for the full (hybrid) product. During a run it sends
samples about every 6 s through Garmin Connect on the phone; after the run the Coach recaps it
without waiting for Garmin → Intervals.icu. PR #533 already holds the run in gateway memory for
24 h and returns it as `watch_run` from `startCoachSession` / `getCoachState`, for one owner
configured by environment variables. The Store version adds what many athletes need: no secret
in the build, pairing to the athlete's own Coach account, and the listing.

## Is this allowed

**The restriction that hits Intervals.icu is in a different agreement.** The Garmin Connect
Developer Program (cloud API) forbids passing API data to unapproved third parties and has AI
terms. Watch apps fall under the Connect IQ Developer Agreement (2024-08-06), which has no AI
clause. Its privacy terms (Exhibit A) ask for a privacy policy, the athlete's consent, no
retention beyond need, and no selling, renting or transferring of user data received from Garmin.

**The Store already carries the same pattern.** Approved Connect IQ apps, read on
apps.garmin.com on 2026-11-08:

| App | Type | What it does | Released / updated |
| --- | --- | --- | --- |
| Pulse AI Coach (GPT, Claude, Gemini) | Data field, $4.99, 100+ downloads | Sends run data to the athlete's chosen OpenAI, Anthropic or Google model | 2026-05-21 / 2026-08-25 |
| AI Coach (TheBigGoal) | Device app, paid, 100+ | Reads Body Battery, stress, recovery, resting HR and workouts for AI advice | 2026-05-21 / 2026-11-05 |
| Ai Run Coach | Device app, free, 100+ | Real-time AI coaching during the run, with a phone app | 2026-05-04 / 2026-11-05 |
| Kinetiq IA - Coach AI | Device app, free, 100+ | AI training advice from the athlete's sports data | 2026-09-15 / 2026-10-22 |
| coachd | Device app, free | Self-hosted gateway between an AI coach and the watch for workouts | 2026-10-26 / 2026-10-27 |
| Athlete Space: AI Coach | Device app, free | Daily session from an AI coach | 2026-11-04 |

A search for "AI coach" returned 977 results. Approval is not a legal opinion, but it shows the
review passes apps that send watch data to AI services. No prior question to Garmin is planned.
If Garmin objects, the cost is a rejected or removed watch field; the hosted Coach is untouched.

## Pairing without Intervals.icu

The athlete is already signed in to the Coach in their AI chat, so the chat is where the watch
gets bound. Intervals.icu plays no part in pairing.

1. Unpaired, the field asks the gateway for a pairing code and shows it, e.g. `Code K7P4QX`.
   The watch keeps its own secret for that request.
2. The athlete tells the Coach in chat: "connect my watch K7P4QX". A new hybrid-only tool,
   `connectWatch(code)`, binds the code to the owner behind that MCP connection. Its result says
   what the field sends, that runs are kept 24 h, and that removing the field stops it.
3. The watch, polling with its secret, receives a write-only device credential, stores it in
   `Application.Storage` (encrypted for Store-signed apps) and shows `Connected` with the
   account's masked email (`t***@gmail.com`), so the athlete sees whose account it joined.

The same steps work for an athlete who signed in with Google and has no Intervals.icu: the code
binds to whichever account the chat is signed in to.

The code is single-use and expires in 10 minutes; the credential can only upload samples for
its owner. Connecting another watch, or the same watch again, replaces the old credential.

Garmin has no linking step of its own at install; the install shows permissions and, on API
5.2+, offers to add the field to activities. What Garmin documents are two pieces an app
triggers itself: `Authentication.makeOAuthRequest` (open to data fields in the foreground since
System 5), which opens our sign-in page in a web view inside the Connect IQ Store app, and
settings fields the athlete fills in that app.

Alternatives considered: a code typed into the field's settings (the same new tool plus about
five more taps), and Garmin's OAuth page. That page needs the Coach's sign-in inside Garmin's
web view: Intervals.icu today, Google once accounts exist. Google refuses sign-in from embedded
web views, so whether "Continue with Google" works there needs a device test.

### Before → after for another athlete

- **Before:** nothing to set up. A recap waits for Garmin → Intervals.icu: on the owner's
  account, 23 runs from 2026-09-01 to 11-07, median 0.8 min, P90 34 min, worst 14.5 h.
- **After, once:**
  1. Install the field from the Connect IQ Store app and accept Garmin's permission prompt.
     Since 2025-11-20 that app is the only place to install Connect IQ apps.
  2. Add the field to Run. Watches on API 5.2+ offer this right after install; others need it
     done in Run's data screens.
  3. Open Run, read the code, send one chat message. Some clients ask to approve the tool once.
- **After, each run:** carry the phone with Garmin Connect. Stop the watch, ask for the recap.
  Without the phone the run arrives the old way, through Intervals.icu.

Step counts are not measured with a second athlete.

## Watches

Data fields can send from the foreground from API 5.0.0 on. Of 148 watch profiles in SDK 9.3.0,
76 qualify, including Forerunner 165, 255, 265, 570, 955, 965, 970, 70 and 170; fēnix 7, 8, 9
and E; epix Gen 2; Enduro 2, 3 and 4; Venu 2, 3, 4 and X1; vívoactive 5 and 6; Instinct 3 and
Instinct E; MARQ Gen 2 and 3. Instinct 3 Solar and Instinct E give data fields 32 KB, so they
need a memory check. Older watches (Forerunner 55, 245, 745, 945, fēnix 6, Instinct 2, Venu,
vívoactive 4) can only send every 5 minutes from a background service, so v1 leaves them out.
Only the Forerunner 570 has run the field on a real device.

## Store requirements

- Developer account (18+), the Connect IQ Developer Agreement, free listing. Identity
  verification applies only to apps that take payment.
- Review: officially within 72 hours; October 2026 reports describe week-long queues.
  Uploads also pass an automated security scan.
- Manifest: `type="datafield"`, `minApiLevel="5.0.0"`, permission `Communications`, a running
  `activityFilter`, a new app id. No secret or owner id in the build.
- Signing key: every update needs the same key. Losing it means no further updates.
- Listing: no Garmin name or logo in the name or icon. State the requirements: a Coach account
  in an AI chat, and Garmin Connect on the phone during the run. List devices accurately, make no
  medical claims, and give a support email (shown publicly). The listing has a privacy policy
  link.
- Privacy page: add the watch field — what it sends, the 24 h hold, how to stop. Add the sentence
  Garmin requires: data from the field goes to this service, not Garmin, and Garmin is not
  responsible for it.

## Gateway changes on top of PR #533

1. Replace the single-owner environment credential with per-owner device credentials, stored as
   keyed fingerprints in the hybrid identity registry.
2. Add pairing routes for the watch: request a code, then poll. Add the `connectWatch` tool to
   the hybrid catalogue only. The tool is a model-facing change, so the change gates apply:
   client acceptance, and a new catalogue digest.
3. Show `watch_run` to every connection of its owner. PR #533 shows it only to connections whose
   recorded Intervals.icu scopes include `ACTIVITY:READ`; that ties a watch recap to an
   Intervals.icu permission, so drop the check.
4. Keep PR #533's 24 h memory hold. A redeploy still clears held runs, which now affects every
   paired athlete.
5. Keep the training (Plugin) product closed: no route, no tool, no `watch_run`. PR #533's
   boundary tests extend to the pairing routes.
6. Rate-limit each device, and log pairing and failed device authentication as security events.

## Own account

Today the Intervals.icu athlete *is* the Coach account (`provider_identities` holds
`('intervals', athlete id)`), and every MCP request resolves its owner from the Intervals.icu
token sealed inside the client's access token. So an athlete without Intervals.icu cannot use
the Coach at all, and losing Intervals.icu (issue #522) would also lose the sign-in.

Proposal: one Coach account holding one email address, with more than one way to sign in to
it. Athletes who use Intervals.icu keep signing in with Intervals.icu, and their email comes
from there. Athletes who do not sign in with Google, or later with an emailed code. The
registry already allows several identities per owner, so each method is one row:
`('intervals', athlete id)`, `('google', sub)`, `('email', address)`. The account is keyed by
its owner id; the email can change without moving the account.

The Intervals.icu email is already readable: the full product holds `SETTINGS:WRITE`, and the
gateway reads `GET /api/v1/athlete/0` (id, name, email) to label the connected account. Today
it drops the address after the request.

Storing the address in plain text is ordinary account data. It needs a line in the privacy page
(what it is for), a place in the owner export and in account deletion, and no appearance in
logs, usage counters or security events. It also gives emailed deletion requests a real check:
a request from the stored address comes from the account holder.

What has to change, roughly in this order:

1. The authorize page offers "Continue with Intervals.icu" (unchanged) and "Continue with
   Google" (OpenID Connect, `openid email`). A Google OAuth client must be created in the
   owner's Google Cloud project; only the owner can do that. An emailed code needs a mail
   sender. The site domain's DNS is on Cloudflare and its MX is Cloudflare Email Routing,
   which forwards mail in but does not send. Sending takes a transactional mail service, its
   DKIM and SPF records added in Cloudflare, a DMARC record, and its API key as a gateway
   secret; the gateway calls it over HTTPS with the standard library, so no Python dependency
   is added. The service becomes a data processor named in the privacy page.
2. The access token carries the owner, and an Intervals.icu token only when the athlete signed
   in with Intervals.icu. Owner resolution and the revocation epoch stop depending on the
   Intervals.icu token.
3. An email never opens an account. Nobody types an email to sign in: a sign-in is a login at
   Intervals.icu or Google, and it opens only the account of that Intervals.icu athlete or that
   Google user, as Intervals.icu sign-in does today. Two methods join one account only when the
   athlete signs in with both in the same flow. A matching email (letter case ignored) only
   prompts the offer: "this email already has a Coach account; sign in with Google to combine
   them". Automatic joining on email is ruled out: Intervals.icu confirms the address at
   registration (its forum describes the confirmation mail), but no source confirmed that a
   later change of address is confirmed, and its API does not say. Joining on email would let
   anyone who sets another person's address on their own Intervals.icu account open that
   person's training data.
4. Evidence without Intervals.icu: provider reads are skipped and reported as unknown, never as
   zero. The watch run becomes the record of what was trained. PR #533 left watch runs out of
   reconciliation; that choice changes for owners with no Intervals.icu link.
5. The privacy page and `docs/account-lifecycle.md` change: a Google identity and the email are
   stored, and for a Google account, revoking Intervals.icu no longer disconnects it.
6. The training (Plugin) product stays on Intervals.icu sign-in. It has its own registry.

Before → after when an athlete connects an AI client to the full product:

- **With Intervals.icu:** unchanged. Connect → Intervals.icu sign-in → Authorize. The account
  is the one they already have.
- **Without Intervals.icu, before:** cannot use the Coach.
- **Without Intervals.icu, after:** Connect → Continue with Google. Then install the watch field
  and send its code. The Coach knows runs from the watch onwards, what the athlete says, and
  imported history.
- **Uses both methods:** the first sign-in with the second method sees the matching email and
  offers to combine; the athlete signs in with the first method once more, and the two join.
  Declining keeps two separate accounts. From then on either method opens the same account,
  and signing in with Intervals.icu brings its data.
- **Someone types another person's address into their own Intervals.icu settings:** their
  sign-in opens their own account. The combine offer asks them to sign in as the other
  person, which they cannot.

The account pays off only together with step 4; the Google sign-in alone serves nobody until
the Coach can work without Intervals.icu.

## What else the watch can do with the Coach

One Connect IQ app has one type, and only a data field sees a native run live. Saving workouts
and drawing a glance need a device app (`PersistedContent` is not open to data fields). So the
watch side is at most two apps, each paired once with a chat code.

| Coach job | Today | With Connect IQ | App | State |
| --- | --- | --- | --- | --- |
| Recap the run just done | Waits for Intervals.icu sync | Live samples → `watch_run` | Data field | PR #533; a 58-minute pilot run delivered every sample |
| Record what was trained | Intervals.icu activities | Watch runs as the record for owners without Intervals.icu; the field can also go on other activity profiles for heart rate and time | Data field | Not built |
| Put workouts on the watch | Intervals.icu calendar → Garmin | Download the workout FIT into the watch's workout list; the app reports when it is saved, so delivery becomes observable | Device app | FR570: foreground download works with Chinese names and HR targets; a background download saved the workout but its callback failed |
| Show today's session | Garmin calendar | A glance with today's session and why; open it to start the workout | Device app | Not built. Writing the watch calendar directly was tried and dropped: it replaced the whole calendar |
| Strength execution | Athlete states sets in chat | Show the prescribed session; tap done or changed per exercise → `recordStrengthExecution` | Device app | Not built |
| Morning recovery | Intervals.icu wellness, or the athlete's own numbers | Resting heart rate (`UserProfile`), sleep score, Body Battery and stress (complications), once each morning | Device app background | Not verified that a background service can read complications; HRV status is not available to Connect IQ |
| Older watches (API < 5.0) | — | Summary upload when an activity completes | Background service | Not verified |

In-run alerts and coaching stay out, as decided after the pilot run.

## Owner decisions (approved as recommended)

| Decision | Options | Cost if wrong | Recommendation |
| --- | --- | --- | --- |
| Order | Store data field now, account later / account first | Account first delays the watch field for current athletes and adds a sign-in with no gain yet | Data field now; account, no-Intervals evidence and the device app as the next milestone |
| Sign-in methods | Intervals.icu plus Google, emailed code later / Google for everyone | Google for everyone adds a step for every current athlete | Intervals.icu plus Google |
| Email | One plain address per account, from Intervals.icu or Google / none | None: no way to link methods or to check who sends an emailed request | One plain address |
| Joining two sign-in methods | Only by signing in with both / automatic on matching email | Automatic: an address typed into someone's own Intervals.icu settings opens another person's data — ruled out by the owner | Only by signing in with both |
| Mail sending | Transactional service with Cloudflare DNS records, when emailed codes are added / now | Now: setup and a new processor before anything needs it | When emailed codes are added; the owner creates the service account |
| Intervals.icu token | Per connection, as today / stored on the server | Stored: the gateway holds every athlete's long-lived token and the privacy page changes | Per connection; Intervals.icu users keep signing in with it |
| Watch runs as training record | For owners without Intervals.icu / never | Never: those athletes have no actuals at all | For owners without Intervals.icu |
| Pairing | Code on watch → chat / code in settings / browser sign-in | Settings: about 5 more taps. Browser: depends on Intervals.icu until the account exists | Code on watch → chat |
| Watches in v1 | All API 5.0+ that fit in memory / Forerunner 570 only | Claiming untested models risks bad reviews; FR570 only reaches few athletes | All API 5.0+, listed as "tested on Forerunner 570" |
| Name | Long Run Hybrid Coach / Live Coach | "Live" suggests in-run coaching, which is dropped | Long Run Hybrid Coach, field label "Coach" |
| Public support email | Site-domain forwarding address / private Gmail | The listing publishes it | Site-domain address |

## Sources

- Connect IQ Developer Agreement (updated 2024-08-06) and App Review Guidelines (2021-10-13),
  read 2026-11-07.
- SDK 9.3.0 docs and device profiles (2026-10-20). Garmin announcements: System 5 SDK
  (2022-03-22), Trader Verification (2024-12-23), Changes to the Connect IQ Store (2025-11-20).
- Store listings above, read 2026-11-08; Notebookcheck (2026-10-19) on review queues.
- PR #533.
