export function composerInputDisabled(providerOk: boolean, activeChatId: string | null): boolean {
  return !providerOk || !activeChatId
}

export function composerAttachmentDisabled(providerOk: boolean, uploading: boolean): boolean {
  return !providerOk || uploading
}

export function composerSendDisabled(
  providerOk: boolean,
  activeChatId: string | null,
  sending: boolean,
  uploading: boolean,
  hasContent: boolean,
): boolean {
  return !providerOk || !activeChatId || sending || uploading || !hasContent
}
