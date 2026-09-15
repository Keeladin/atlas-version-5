import { isImageAttachment, type TurnAttachment } from './chatAttachments'

function attachmentKindLabel(attachment: TurnAttachment): string {
  if (attachment.media_type && attachment.media_type !== 'application/octet-stream') {
    return attachment.media_type.split('/').at(-1)?.toUpperCase() ?? 'FILE'
  }
  const suffix = attachment.filename.includes('.') ? attachment.filename.split('.').at(-1) : null
  return suffix ? suffix.slice(0, 5).toUpperCase() : 'FILE'
}

export function ChatAttachmentView({ attachment, owner }: { attachment: TurnAttachment; owner: boolean }) {
  const artifactUrl = attachment.artifact_id ? `/api/artifacts/${encodeURIComponent(attachment.artifact_id)}` : null
  const displayUrl = attachment.preview_url ?? artifactUrl
  if (isImageAttachment(attachment.media_type, attachment.filename) && displayUrl) {
    const className = owner ? 'turn-owner-image-attachment' : 'turn-image-artifact'
    return (
      <figure className={className}>
        <a className="turn-image-link" href={displayUrl} target="_blank" rel="noreferrer">
          <img src={displayUrl} alt={attachment.filename} loading="lazy" />
        </a>
        <figcaption>
          <span>{attachment.filename}</span>
          {!owner && artifactUrl ? <a href={artifactUrl} download={attachment.filename}>Save image</a> : null}
          {owner && artifactUrl ? <a href={artifactUrl} target="_blank" rel="noreferrer">Open</a> : null}
          {owner && !artifactUrl ? <span>Attached</span> : null}
        </figcaption>
      </figure>
    )
  }
  const content = (
    <>
      <span className="turn-file-badge" aria-hidden="true">{attachmentKindLabel(attachment)}</span>
      <span className="turn-file-copy">
        <strong>{attachment.filename}</strong>
        <small>{attachment.pending ? 'Attached' : 'File attachment'}</small>
      </span>
      {artifactUrl ? <span className="turn-file-open">Open</span> : null}
    </>
  )
  return artifactUrl ? (
    <a className="turn-file-artifact" href={artifactUrl} target="_blank" rel="noreferrer">{content}</a>
  ) : (
    <div className="turn-file-artifact pending">{content}</div>
  )
}
