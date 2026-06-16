Generate the evening family briefing for $today_date.

Local timezone: $local_tz ($local_offset).

Compose a structured summary covering (in order):
- Any items needing urgent attention (i.e. today & tomorrow, or other reasons). Include bin collection here if $bin_collection is non-empty and bin collection is tomorrow.
- What's happening this week (i.e. until the end of week, or next Monday if today is Friday or later, included)
- What's happening next week (i.e. from next Monday to next Sunday, included, skip Monday if it was covered by the previous section.)
- New & updated events from emails received since the last digest — show sync status:
  ✅ = synced to Google Calendar, 📋 = local only (not published)
  Mark updated events with 🔄 and a brief note (e.g. "time updated from new email")
- An "Other Notable Events" section at the bottom for any events that seem unlikely to be relevant based on the family profile, and are happening today or tomorrow, so they remain visible but are not mixed with urgent items. Summary of new emails received since the last digest. Also summary of events that have been skipped because they already exist on a shared calendar (if any).

Be concise, structured, and action-oriented. Make the report look nice and polished. Don't repeat events.
Skip sections that don't have any data. Just mention there were no emails/events/whatever you have skipped at the end of the report.
Use family profile to determine if an event is relevant or not. 

Here is some raw data:

## Family Profile
$profile

## New Emails Since Last Digest
$new_emails

## New Events Created from Emails
$new_events_from_emails

## Skipped Events (already exist on shared calendars)
$skipped_events

## Upcoming Events (from calendar)
$events

$bin_collection
