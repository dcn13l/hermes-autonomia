// Minimal TypeScript declarations for linkpeek-api

declare class LinkPeek {
  constructor(opts?: {
    baseURL?: string;
    apiKey?: string;
    timeout?: number;
  });

  preview(url: string): Promise<any>;
  extract(url: string): Promise<any>;
  metadataFull(url: string): Promise<any>;
  metaTags(url: string): Promise<any>;
  openGraph(url: string): Promise<any>;
  oembed(url: string): Promise<any>;
  links(url: string, limit?: number): Promise<any>;
  wordCount(url: string, opts?: { wpm?: number; top?: number }): Promise<any>;
  batch(urls: string[]): Promise<any>;
  diff(url1: string, url2: string): Promise<any>;
  qr(text: string, opts?: { ecc?: string; fg?: string; bg?: string }): Promise<Buffer>;
  favicon(url: string, size?: number): Promise<Buffer>;
  robots(url: string): Promise<any>;
  headers(url: string): Promise<any>;
  rss(url: string): Promise<any>;
  screenshotHint(url: string): Promise<any>;
  createShortlink(url: string): Promise<any>;
  resolveShortlink(code: string): Promise<any>;
  issueKey(email: string): Promise<any>;
  validateKey(key: string): Promise<any>;
  subscribe(email: string): Promise<any>;
  status(): Promise<any>;
  health(): Promise<any>;

  // Support both default and named import
  static default: typeof LinkPeek;
  static LinkPeek: typeof LinkPeek;
}

export = LinkPeek;
