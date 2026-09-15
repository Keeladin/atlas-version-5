import type { LocalStorageEntry, Turn } from './api'

export type ComposerAttachment = LocalStorageEntry & {
  media_type?: string | null
  preview_url?: string | null
}

export type TurnAttachment = {
  key: string
  artifact_id?: string
  filename: string
  media_type?: string | null
  preview_url?: string | null
  pending?: boolean
}

const IMAGE_EXTENSIONS = /\.(avif|bmp|gif|heic|heif|jpe?g|png|svg|webp)$/i

export function isImageAttachment(mediaType?: string | null, filename?: string | null): boolean {
  return Boolean(mediaType?.startsWith('image/') || (filename && IMAGE_EXTENSIONS.test(filename)))
}

export function turnAttachments(turn: Turn): TurnAttachment[] {
  return turn.blocks.flatMap((block, index) => {
    if (block.type !== 'artifact_ref' && block.type !== 'local_attachment') return []
    const candidate = block as Record<string, unknown>
    const artifactId = typeof candidate.artifact_id === 'string' ? candidate.artifact_id : undefined
    const filename = typeof candidate.filename === 'string' && candidate.filename ? candidate.filename : 'attachment'
    const mediaType = typeof candidate.media_type === 'string' ? candidate.media_type : null
    const previewUrl = typeof candidate.preview_url === 'string' ? candidate.preview_url : null
    if (!artifactId && !previewUrl && block.type !== 'local_attachment') return []
    return [{
      key: artifactId ?? `${turn.id}-local-${index}`,
      artifact_id: artifactId,
      filename,
      media_type: mediaType,
      preview_url: previewUrl,
      pending: block.type === 'local_attachment',
    }]
  })
}

export function mergeRestoredAttachments(
  original: ComposerAttachment[],
  current: ComposerAttachment[],
): ComposerAttachment[] {
  const merged = new Map<string, ComposerAttachment>()
  for (const item of original) merged.set(item.path, item)
  for (const item of current) merged.set(item.path, item)
  return [...merged.values()]
}

export function releaseAttachmentPreviews(attachments: ComposerAttachment[]): void {
  for (const item of attachments) {
    if (item.preview_url?.startsWith('blob:')) URL.revokeObjectURL(item.preview_url)
  }
}
