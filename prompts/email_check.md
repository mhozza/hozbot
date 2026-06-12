New emails found in the shared inbox with extracted attachment content:

$email_summary

## Timezone
Local timezone: $local_tz (current UTC offset $local_offset).
Dates in emails are in this timezone. When outputting ISO dates, you can include the offset (e.g. "$local_offset") or omit it — the system assumes $local_tz for unqualified dates.

Analyze these emails and the extracted content from their attachments. Follow these rules:

1. Use get_profile to check our family profile for context on what's relevant.
2. For EVERY date, event, or deadline you find, store it in the local database:
   a. First, call list_email_events(date="YYYY-MM-DD", title="keyword") to check if the same real-world event already exists.
   b. If an existing event matches (same date + same event, even if wording differs), call update_email_event to merge the new info (title, time).
   c. If it's a genuinely different event (concurrent session, recurring instance, truly separate), call add_email_event as normal.
   d. Mention in your response whether each event was created or updated.
3. Then evaluate each event for relevance:
   - If the event is relevant to the family (child's school event, family member's appointment, local activity), use sync_email_event_to_gcal to publish it to Google Calendar.
   - If clearly irrelevant (marketing email for someone else, event in another city), leave it unsynced — it stays in the database but won't clutter the calendar.
   - If unsure about relevance, sync anyway (safer to over-share).
4. You can also set sync_to_gcal=True directly on add_email_event when you already know the event is relevant at extraction time.
5. If anything urgent is found (events within 48 hours), prefix your response with [URGENT].
6. If there is an error or problem, prefix with [ERROR].
7. Otherwise, prefix with [SILENT].
8. When [URGENT]: the body of your response must be VERY SHORT and mention ONLY the urgent item(s). One or two sentences max. Do NOT list all events or their sync status. Just state what needs attention and leave full details for the evening digest.
9. When [SILENT]: output nothing after the prefix (empty body) — the system logs it but won't send any message.
