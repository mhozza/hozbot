New emails found in the shared inbox with extracted attachment content:

$email_summary

Analyze these emails and the extracted content from their attachments. Follow these rules:
1. Use get_profile to check our family profile for context on what's relevant.
2. Extract any important dates, events, or deadlines relevant to our family.
3. Use add_calendar_event to add relevant events to the calendar.
4. Ignore dates that are not relevant based on the family profile.
5. If you are unsure about something, note it.
6. If anything urgent is found (events within 48 hours), prefix your response with [URGENT].
7. If there is an error or problem, prefix with [ERROR].
8. Otherwise, prefix with [SILENT].
