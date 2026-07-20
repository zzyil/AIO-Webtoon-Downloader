import React from "react";
import { Button, Card, Badge } from "@/components/ui/primitives";
import { X, Trash2, Clock, Loader2, RotateCw, Zap } from "lucide-react";
import { cn, formatDuration } from "@/lib/utils";

function ProgressBar({ value, max, indeterminate, className }) {
  const percent = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className={cn("h-2 rounded-full bg-secondary overflow-hidden", className)}>
      {indeterminate ? (
        // Animated bar that slides back and forth when we don't know the total
        <div className="h-full w-1/3 rounded-full bg-primary animate-pulse" />
      ) : (
        <div
          className="h-full rounded-full bg-primary transition-all duration-500 ease-out"
          style={{ width: `${percent}%` }}
        />
      )}
    </div>
  );
}

function phaseLabel(phase) {
  const labels = {
    starting: "Starting…",
    downloading: "Downloading chapters…",
    resuming: "Resuming download…",
    retrying: "Retrying missed chapters…",
    building: "Building final file…",
    saving: "Saving output…",
    finishing: "Finishing up…",
    done: "Complete",
  };
  return labels[phase] || phase || "Preparing…";
}

export default function QueueTab({
  activeDownloads,
  queue,
  currentDownloadId,
  onCancel,
  onRemoveFromQueue,
  onStartQueuedNow,
  onClearCompleted,
}) {
  const running = [];
  const completed = [];

  for (const [id, dl] of Object.entries(activeDownloads || {})) {
    if (dl.status === "running") {
      running.push({ id, ...dl });
    } else {
      completed.push({ id, ...dl });
    }
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 min-h-0 overflow-y-auto px-5 py-4 space-y-4">
      {/* Active Downloads */}
      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-3">
          Active Downloads
        </h3>

        {running.length === 0 && (queue || []).length === 0 && (
          <Card className="p-6 text-center text-sm text-muted-foreground">
            <p>No active downloads</p>
            <p className="text-xs mt-1">Start a new download from the New tab</p>
          </Card>
        )}

        {running.map((dl) => (
          <Card key={dl.id} className="p-4 mb-2 animate-slide-up">
            <div className="flex items-start justify-between mb-2">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <Loader2 className="w-4 h-4 animate-spin text-primary shrink-0" />
                  <span className="font-semibold text-sm truncate">
                    {dl.progress?.title || dl.displayUrl || dl.url || "Downloading…"}
                  </span>
                  {/* Show "Batch ×3" badge for multi-URL downloads */}
                  {Array.isArray(dl.url) && dl.url.length > 1 && (
                    <Badge variant="secondary" className="text-[10px] shrink-0">
                      Batch ×{dl.url.length}
                    </Badge>
                  )}
                </div>
                <p className="text-xs text-muted-foreground mt-1">
                  {phaseLabel(dl.progress?.phase)}
                  {/* Batch progress: "2/5 completed" from the Python coordinator */}
                  {dl.progress?.batchCurrent > 0 && dl.progress?.batchTotal > 0 && (
                    <span className="ml-1">
                      — {dl.progress.batchCurrent}/{dl.progress.batchTotal} titles done
                    </span>
                  )}
                  {/* Chapter progress for non-batch or within batch */}
                  {dl.progress?.processedChapters > 0 && !dl.progress?.batchCurrent && (
                    <span className="ml-1">
                      {dl.progress.totalChapters > 0
                        ? `— Ch. ${dl.progress.processedChapters}/${dl.progress.totalChapters}`
                        : `— ${dl.progress.processedChapters} ch. processed`}
                    </span>
                  )}
                  {/* Show current chapter number */}
                  {dl.progress?.currentChapter > 0 && !dl.progress?.processedChapters && (
                    <span className="ml-1">— Ch. {dl.progress.currentChapter}</span>
                  )}
                </p>
              </div>
              <div className="flex gap-1 ml-2">
                <Button variant="ghost" size="icon" onClick={() => onCancel(dl.id)} title="Stop download">
                  <X className="w-4 h-4" />
                </Button>
              </div>
            </div>

            {/* Progress bar — determinate if we know total, indeterminate otherwise */}
            {dl.progress?.processedChapters > 0 ? (
              <ProgressBar
                value={dl.progress.processedChapters}
                max={dl.progress.totalChapters || 0}
                indeterminate={!dl.progress.totalChapters}
              />
            ) : dl.progress?.phase && dl.progress.phase !== "starting" ? (
              <ProgressBar indeterminate />
            ) : null}

            <div className="flex gap-1.5 mt-2 flex-wrap">
              {dl.args?.format && <Badge variant="secondary">{dl.args.format.toUpperCase()}</Badge>}
              {dl.args?.quality && <Badge variant="secondary">Q{dl.args.quality}</Badge>}
              {dl.args?.keepChapters && <Badge variant="secondary">Chapters</Badge>}
            </div>
          </Card>
        ))}
      </div>

      {/* Queued Downloads */}
      {(queue || []).length > 0 && (
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-3">
            Queued ({(queue || []).length})
          </h3>
          {(queue || []).map((item) => {
            // Resume jobs (type:"resume") and fresh downloads share the queue;
            // distinguish them at a glance with icon + a "Resume" badge.
            const isResume = item.type === "resume";
            const TypeIcon = isResume ? RotateCw : Clock;
            return (
              <Card key={item.id} className="p-3 mb-2 border-dashed animate-slide-up">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <TypeIcon className="w-4 h-4 text-muted-foreground shrink-0" />
                    <span className="text-sm truncate text-muted-foreground">
                      {item.displayUrl || item.url}
                    </span>
                  </div>
                  {/* Right cluster: promote-to-parallel ("start alongside") + remove */}
                  <div className="flex items-center gap-1 shrink-0">
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-7 text-xs gap-1 transition-all active:scale-[0.97]"
                      onClick={() => onStartQueuedNow?.(item.id)}
                      title="Start now, in parallel with the current download"
                    >
                      <Zap className="w-3 h-3" />
                      Start alongside
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7"
                      onClick={() => onRemoveFromQueue(item.id)}
                      title="Remove from queue"
                    >
                      <X className="w-4 h-4" />
                    </Button>
                  </div>
                </div>
                <div className="flex gap-1.5 mt-1.5 ml-6 flex-wrap">
                  {isResume && <Badge variant="default">Resume</Badge>}
                  {isResume && item.cachedChapters > 0 && (
                    <Badge variant="secondary">{item.cachedChapters} ch cached</Badge>
                  )}
                  {isResume
                    ? item.format && <Badge variant="secondary">{item.format.toUpperCase()}</Badge>
                    : item.args?.format && <Badge variant="secondary">{item.args.format.toUpperCase()}</Badge>}
                  {item.args?.quality && <Badge variant="secondary">Q{item.args.quality}</Badge>}
                </div>
              </Card>
            );
          })}
        </div>
      )}

      {/* Completed / Failed */}
      {completed.length > 0 && (
        <div>
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Recent
            </h3>
            <Button variant="ghost" size="sm" onClick={onClearCompleted}>
              <Trash2 className="w-3 h-3 mr-1" />
              Clear
            </Button>
          </div>
          {completed.map((dl) => (
            <Card
              key={dl.id}
              className={cn(
                "p-3 mb-2",
                dl.status === "completed" && "border-green-500/20",
                dl.status === "failed" && "border-red-500/20",
                dl.status === "cancelled" && "border-yellow-500/20"
              )}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 min-w-0">
                  <span className={cn("text-sm", dl.status === "completed" && "text-green-600 dark:text-green-400")}>
                    {dl.status === "completed" ? "✓" : dl.status === "failed" ? "✗" : "⏸"}
                  </span>
                  <span className="text-sm font-medium truncate">
                    {dl.progress?.title || dl.displayUrl || dl.url || "Download"}
                  </span>
                  <Badge
                    variant={dl.status === "completed" ? "success" : dl.status === "failed" ? "destructive" : "warning"}
                  >
                    {dl.status}
                  </Badge>
                </div>
                {dl.result?.duration && (
                  <span className="text-xs text-muted-foreground">{formatDuration(dl.result.duration)}</span>
                )}
              </div>
            </Card>
          ))}
        </div>
      )}
      </div>
    </div>
  );
}
