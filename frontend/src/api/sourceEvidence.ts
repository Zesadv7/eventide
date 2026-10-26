export interface FileEvidence {
  kind: "file";
  path: string;
  status: "read" | "error";
  sha256?: string;
  total_lines?: number;
  start_line?: number;
  end_line?: number;
  start_character?: number;
  end_character?: number;
  next_token?: string | null;
  complete?: boolean;
}
export interface SearchEvidence {
  kind: "search";
  mode: "literal" | "regex";
  case_sensitive: boolean;
  context_lines: number;
  scanned: number;
  skipped: number;
  unverified: number;
  timed_out: boolean;
  output_capped: boolean;
  partial: boolean;
  next_offset: number | null;
  sources: {
    path: string;
    sha256: string;
    lines: { line: number; match: boolean; text: string }[];
  }[];
}
export type SourceEvidence = FileEvidence | SearchEvidence;
