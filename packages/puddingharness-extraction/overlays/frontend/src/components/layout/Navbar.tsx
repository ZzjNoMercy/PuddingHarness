/* Target Harness Navbar overlay: panel controls, title, and trace affordances. */
"use client";

import { ChevronDown, Cpu, PanelLeft, PanelRight } from "lucide-react";


interface NavbarProps {
  sidebarOpen?: boolean;
  toggleSidebar?: () => void;
  inspectorOpen?: boolean;
  inspectorAvailable?: boolean;
  toggleInspector?: () => void;
  onToggleTrace?: () => void;
  traceSpanCount?: number;
  traceActive?: boolean;
  /** Hide sidebar/inspector toggles on non-chat pages */
  showPanelToggles?: boolean;
  /** Optional centered title (e.g. current session name) */
  title?: string;
  /** Compact mode for rendering only the sidebar toggle inside the drawer. */
  compact?: boolean;
}

export default function Navbar({
  sidebarOpen,
  toggleSidebar,
  inspectorOpen,
  inspectorAvailable = true,
  toggleInspector,
  onToggleTrace,
  traceSpanCount,
  traceActive,
  showPanelToggles = false,
  title,
  compact = false,
}: NavbarProps) {

  if (compact) {
    return (
      <div className="flex h-full items-center">
        {showPanelToggles && toggleSidebar ? (
          <button
            onClick={toggleSidebar}
            className={`w-8 h-8 flex items-center justify-center rounded-lg transition-all ${
              sidebarOpen
                ? "bg-[#002fa7]/[0.08] text-[#002fa7] shadow-sm"
                : "text-gray-400 hover:text-gray-700 hover:bg-black/[0.04]"
            }`}
            title="切换侧栏"
            aria-label="切换侧栏"
          >
            <PanelLeft className="w-[16px] h-[16px]" />
          </button>
        ) : null}
      </div>
    );
  }

  return (
    <nav className="glass-nav sticky top-0 z-50 h-11 flex items-center justify-between px-3">
      {/* Left — Sidebar toggle (chat page only) or spacer */}
      <div className="w-[120px] flex items-center">
        {showPanelToggles && toggleSidebar ? (
          <button
            onClick={toggleSidebar}
            className={`w-8 h-8 flex items-center justify-center rounded-lg transition-all ${
              sidebarOpen
                ? "bg-[#002fa7]/[0.08] text-[#002fa7] shadow-sm"
                : "text-gray-400 hover:text-gray-700 hover:bg-black/[0.04]"
            }`}
            title="切换侧栏"
            aria-label="切换侧栏"
          >
            <PanelLeft className="w-[16px] h-[16px]" />
          </button>
        ) : null}
      </div>

      {/* Center — Title */}
      <div className="flex min-w-0 flex-1 items-center justify-center gap-2 px-4">
        {title ? (
          <button className="flex items-center gap-1.5 text-[14px] font-medium text-gray-800 hover:bg-black/[0.04] px-3 py-1.5 rounded-lg transition-colors">
            <span className="truncate">{title}</span>
            <ChevronDown className="w-[14px] h-[14px] text-gray-400" />
          </button>
        ) : null}
      </div>

      {/* Right — Inspector toggle (chat page only) or spacer */}
      <div className="w-[160px] flex justify-end gap-1.5 relative">
        {showPanelToggles && onToggleTrace ? (
          <button
            onClick={onToggleTrace}
            className={`h-8 flex items-center gap-1.5 rounded-lg px-2.5 text-[12px] font-semibold transition-all ${
              traceActive
                ? "bg-[#002fa7]/[0.08] text-[#002fa7] shadow-sm"
                : "text-gray-500 hover:text-gray-800 hover:bg-black/[0.04]"
            }`}
            title={traceActive ? "关闭 Trace 看板" : "打开 Trace 看板"}
            aria-label={traceActive ? "关闭 Trace 看板" : "打开 Trace 看板"}
          >
            <Cpu className="w-[15px] h-[15px]" />
            <span className="hidden lg:inline">Trace</span>
            {typeof traceSpanCount === "number" && traceSpanCount > 0 ? (
              <span className="rounded-full bg-black/[0.06] px-1.5 py-0.5 text-[10px] font-medium">
                {traceSpanCount}
              </span>
            ) : null}
          </button>
        ) : null}
        {showPanelToggles && toggleInspector ? (
          <button
            onClick={toggleInspector}
            disabled={!inspectorAvailable}
            className={`h-8 w-8 shrink-0 flex items-center justify-center rounded-lg transition-all ${
              !inspectorAvailable
                ? "cursor-not-allowed text-gray-200"
                : inspectorOpen
                ? "bg-[#002fa7]/[0.08] text-[#002fa7] shadow-sm"
                : "text-gray-400 hover:text-gray-700 hover:bg-black/[0.04]"
            }`}
            title={inspectorAvailable ? "切换右侧面板" : "暂无可展示内容"}
            aria-label={inspectorAvailable ? "切换右侧面板" : "右侧面板暂无内容"}
          >
            <PanelRight className="w-[16px] h-[16px]" />
          </button>
        ) : null}
      </div>
    </nav>
  );
}
