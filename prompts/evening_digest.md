Generate the evening family briefing for $today_date.

Local timezone: $local_tz ($local_offset).

Compose a structured summary covering (in order):
- Any items needing urgent attention (i.e. in next 48 hours, or other reasons). Include bin collection here if $bin_collection is non-empty.
- What's happening this week
- What's happening next week
- New emails received since the last digest (subjects listed above)
- New & updated events from those emails — show sync status:
  ✅ = synced to Google Calendar, 📋 = local only (not published)
  Mark updated events with 🔄 and a brief note (e.g. "time updated from new email")
- An "Other Notable Events" section at the bottom for any events that seem unlikely to be relevant based on the family profile, so they remain visible but are not mixed with urgent items.

Be concise, structured, and action-oriented. Make the report look polished. Don't repeat events.
Skip sections that don't have any data. Just mention there were no emails/events/whatever you have skipped at the end of the report.
Use family profile to determine if an event is relevant or not. 

## Family Profile
$profile

## New Emails Since Last Digest
$new_emails

## New Events Created from Emails
$new_events_from_emails

## Upcoming Events (from calendar)
$events

$bin_collection
