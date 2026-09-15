import assert from 'node:assert/strict'
import test from 'node:test'
import { localStorageFileUrl, projectStorageFileUrl } from '../src/api.ts'

test('workspace file links preserve nested paths and explicit download intent', () => {
  assert.equal(
    localStorageFileUrl('Manuals/OEM guide.pdf'),
    '/api/storage/local/file?path=Manuals%2FOEM+guide.pdf',
  )
  assert.equal(
    localStorageFileUrl('Manuals/OEM guide.pdf', true),
    '/api/storage/local/file?path=Manuals%2FOEM+guide.pdf&download=true',
  )
})

test('project file links use the protected project delivery route', () => {
  assert.equal(
    projectStorageFileUrl('Atlas version 5/README.md', true),
    '/api/storage/projects/file?path=Atlas+version+5%2FREADME.md&download=true',
  )
})
