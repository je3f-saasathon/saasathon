import * as React from "react"

import { cn } from "@/lib/utils"

// A native <select> styled like Input: no popover dependency needed for simple choices.
// Options get explicit theme colours: the browser draws the open list itself, and some
// (Firefox/GTK) ignore color-scheme, leaving inherited light text on a light list.
const Select = React.forwardRef<HTMLSelectElement, React.ComponentProps<"select">>(
  ({ className, ...props }, ref) => {
    return (
      <select
        className={cn(
          "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-base shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 md:text-sm dark:[color-scheme:dark] [&_option]:bg-popover [&_option]:text-popover-foreground",
          className
        )}
        ref={ref}
        {...props}
      />
    )
  }
)
Select.displayName = "Select"

export { Select }
