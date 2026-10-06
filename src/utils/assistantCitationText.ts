/** Keep exact evidence identifiers traceable through business-label formatting. */
export function formatAssistantCitationText(value: string, format: (text: string) => string): string {
  const citations: string[] = [];
  const protectedText = value.replace(/\b(?:doc_id|evidence_id)\s*=\s*[^）)\]\n]+/g, (citation) => {
    const index = citations.push(citation.replace(/^(?:doc_id|evidence_id)\s*=\s*/, "证据：")) - 1;
    return `CITATIONPLACEHOLDER${index}END`;
  });
  return format(protectedText).replace(/CITATIONPLACEHOLDER(\d+)END/g, (token, index) => citations[Number(index)] ?? token);
}

export type CitationReference = { id?: string; title: string; url?: string };
export type CitationPart = { text: string } | { reference: CitationReference; number: number } | { unmatched: true };

/** Resolve only identifiers present in this answer's evidence; never invent a URL. */
export function splitAssistantCitations(value: string, references: CitationReference[]): CitationPart[] {
  const unique = [...new Map(references.filter(item => item.id).map(item => [item.id!, item])).values()];
  const lookup = new Map(unique.map((item, index) => [item.id!, { reference: item, number: index + 1 }]));
  const pattern = /\[(?:证据[:：]\s*)?([A-Za-z][\w-]*:[^\]\n]+)\]|(?:doc_id|evidence_id)\s*[:：=]\s*([^）)\]\n]+)/g;
  const parts: CitationPart[] = [];
  let start = 0;
  for (const match of value.matchAll(pattern)) {
    if (match.index! > start) parts.push({ text: value.slice(start, match.index) });
    for (const id of (match[1] ?? match[2]).split(/[,，;；\s]+/).filter(Boolean)) {
      parts.push(lookup.get(id) ?? { unmatched: true });
    }
    start = match.index! + match[0].length;
  }
  if (start < value.length) parts.push({ text: value.slice(start) });
  return parts;
}
