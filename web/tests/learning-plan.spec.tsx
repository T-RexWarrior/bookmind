import { describe, expect, it } from 'vitest'
import { knowledgeTypeLabel } from '@/components/space/learning/format'

describe('textbook learning goal labels', () => {
  it('labels outline placeholders as awaiting planning instead of concept requirements', () => {
    expect(knowledgeTypeLabel('concept', key => key, 'outline')).toBe('待规划')
    expect(knowledgeTypeLabel('concept', key => key, 'planned')).toBe('Concept')
  })
})
