import type { Column, ColumnDef } from "@tanstack/react-table";
import { ArrowUpDown } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { LogEntry, LogLevel, LogStatus } from "./mock-logs";

const levelRank: Record<LogLevel, number> = { error: 0, warn: 1, info: 2 };

const levelVariant: Record<LogLevel, React.ComponentProps<typeof Badge>["variant"]> = {
  error: "destructive",
  warn: "default",
  info: "secondary",
};

// shadcn's Badge has no "warning" variant; tint the default one amber.
const levelClassName: Partial<Record<LogLevel, string>> = {
  warn: "bg-amber-500 text-white hover:bg-amber-500/80",
};

const statusVariant: Record<LogStatus, React.ComponentProps<typeof Badge>["variant"]> = {
  open: "default",
  investigating: "secondary",
  resolved: "outline",
  ignored: "outline",
};

function SortableHeader({ column, title }: { column: Column<LogEntry>; title: string }) {
  return (
    <Button
      variant="ghost"
      className="-ml-3"
      onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}
    >
      {title}
      <ArrowUpDown />
    </Button>
  );
}

function formatTimestamp(iso: string) {
  return iso.replace("T", " ").slice(0, 19);
}

export const columns: ColumnDef<LogEntry>[] = [
  {
    accessorKey: "timestamp",
    header: ({ column }) => <SortableHeader column={column} title="Timestamp (UTC)" />,
    cell: ({ row }) => (
      <span className="whitespace-nowrap font-mono text-xs">
        {formatTimestamp(row.getValue("timestamp"))}
      </span>
    ),
  },
  {
    accessorKey: "level",
    header: ({ column }) => <SortableHeader column={column} title="Level" />,
    sortingFn: (a, b) =>
      levelRank[a.getValue<LogLevel>("level")] - levelRank[b.getValue<LogLevel>("level")],
    cell: ({ row }) => {
      const level = row.getValue<LogLevel>("level");
      return (
        <Badge variant={levelVariant[level]} className={levelClassName[level]}>
          {level}
        </Badge>
      );
    },
  },
  {
    accessorKey: "source",
    header: ({ column }) => <SortableHeader column={column} title="Source" />,
    cell: ({ row }) => (
      <span className="whitespace-nowrap font-mono text-xs">{row.getValue("source")}</span>
    ),
  },
  {
    accessorKey: "message",
    header: "Message",
    cell: ({ row }) => (
      <div className="min-w-[16rem] max-w-xl truncate" title={row.getValue("message")}>
        {row.getValue("message")}
      </div>
    ),
  },
  {
    accessorKey: "status",
    header: ({ column }) => <SortableHeader column={column} title="Status" />,
    cell: ({ row }) => {
      const status = row.getValue<LogStatus>("status");
      return (
        <Badge variant={statusVariant[status]} className="capitalize">
          {status}
        </Badge>
      );
    },
  },
];
