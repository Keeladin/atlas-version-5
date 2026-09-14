const preferredFields: Record<string, string[]> = {
  'gmail.message.send': ['to', 'cc', 'bcc', 'subject', 'body'],
  'gmail.message.reply': ['message_id', 'reply_all', 'to', 'cc', 'bcc', 'body'],
  'gmail.message.forward': ['message_id', 'to', 'cc', 'bcc', 'body', 'include_original_attachments'],
  'gmail.message.trash': ['message_id'],
  'gmail.message.delete_permanently': ['message_id'],
  'calendar.event.create': ['calendar_id', 'summary', 'start', 'end', 'attendees', 'location', 'description', 'meet'],
  'calendar.event.update': ['calendar_id', 'event_id', 'changes'],
  'calendar.event.delete': ['calendar_id', 'event_id'],
  'storage.projects.delete': ['path', 'expected_sha256'],
}
const labels: Record<string, string> = { to: 'To', cc: 'CC', bcc: 'BCC', subject: 'Subject', body: 'Message', message_id: 'Message ID', reply_all: 'Reply all', include_original_attachments: 'Include original attachments',
  calendar_id: 'Calendar', event_id: 'Event', summary: 'Title', start: 'Start', end: 'End', attendees: 'Attendees',
  location: 'Location', description: 'Description', meet: 'Create meeting link', changes: 'Changes', path: 'File',
  expected_sha256: 'Expected file SHA-256' }

export function approvalFields(operation: string, args: Record<string, unknown>) {
  const preferred = preferredFields[operation] ?? []
  const keys = [...preferred.filter((key) => Object.hasOwn(args, key)),
    ...Object.keys(args).filter((key) => !preferred.includes(key)).sort()]
  return keys.map((key) => ({ key, label: labels[key] ?? key,
    value: typeof args[key] === 'string' ? args[key] as string : JSON.stringify(args[key], null, 2) }))
}
