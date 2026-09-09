"use client";

import React from "react";
import { ExternalLink, FileText } from "lucide-react";
import { isPortableEvidence, isPortableEvidenceQuote, type PortableEvidence } from "./portableEvidence";

export { isPortableEvidence };
export { isPortableEvidenceQuote };
export type { PortableEvidence };

export default function PortableEvidenceCard({
  evidence,
  title,
  citationIndex,
}: {
  evidence: PortableEvidence;
  title: string;
  citationIndex?: number;
}) {
  const displayTitle = isPortableEvidenceQuote(title) ? title : "未命名来源";
  return (
    <div className="rounded-xl border border-black/[0.06] bg-white p-3">
      <div className="flex items-start gap-2.5">
        <div className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-black/[0.055]">
          <FileText className="h-3 w-3 text-slate-600" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            {citationIndex !== undefined && (
              <span className="inline-flex h-4 min-w-[16px] items-center justify-center rounded bg-[#002fa7]/10 px-1 text-[10px] font-bold text-[#002fa7]">
                {citationIndex}
              </span>
            )}
            <p className="truncate text-[13px] font-medium text-slate-800" title={displayTitle}>
              {displayTitle}
            </p>
          </div>
          {evidence.quote ? (
            <p className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-slate-500">
              {evidence.quote}
            </p>
          ) : null}
          <code className="mt-2 block break-all text-[11px] leading-relaxed text-[#334f96]">
            {evidence.resource_uri}
          </code>
          {evidence.locator && Object.keys(evidence.locator).length > 0 ? (
            <p className="mt-1 text-[10px] text-slate-400">
              定位：{Object.entries(evidence.locator).map(([key, value]) => `${key}=${String(value)}`).join(" · ")}
            </p>
          ) : null}
          {evidence.revision ? (
            <p className="mt-1 break-all text-[10px] text-slate-400">Revision：{evidence.revision}</p>
          ) : null}
          <span className="mt-2 inline-flex items-center gap-1 text-[10px] text-slate-400">
            Platform Evidence
            <ExternalLink className="h-3 w-3" />
          </span>
        </div>
      </div>
    </div>
  );
}
